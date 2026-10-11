"""A session's declared kind and the progress it publishes (claunch-siz7t).

Two additions that let a session say what it is and how far its work has got
when neither is visible from the harness or the busy/idle state -- a pi
session whose extension drives a durable run is the case that asked for them.

``kind`` is a definition field: set at creation, durable across a restart,
absent from a record that never declared one, never inherited by a child.
Progress is a side store fed two ways -- the session's own terminal
(``777;claunch;progress;<JSON>``) and ``POST /api/sessions/{name}/progress``
-- and the terminal sequence must never reach a screen or a viewer.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher import cli_sessions
from claude_launcher.daemon import paths, progress
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.harness import SessionDef, check_kind
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.session import DeadSession, Session

BEARER = {"Authorization": "Bearer sekrit"}


def osc(body, term=b"\x07") -> bytes:
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return b"\x1b]" + progress.OSC_PREFIX + raw + term


# ------------------------------------------------------------------- kind

def test_kind_rides_the_session_record():
    sdef = SessionDef(name="run", kind="pi-durable")
    assert sdef.to_dict()["kind"] == "pi-durable"
    assert SessionDef.from_dict(sdef.to_dict()) == sdef


def test_a_session_without_a_kind_writes_no_key():
    """A plain session's record on disk is what it was before the field."""
    sdef = SessionDef(name="plain")
    assert "kind" not in sdef.to_dict()
    assert SessionDef.from_dict({"name": "old"}).kind is None


@pytest.mark.parametrize("raw,want", [
    ("pi-durable", "pi-durable"), ("  sim.v2_run ", "sim.v2_run"),
    ("", None), (None, None), ("   ", None),
])
def test_check_kind_accepts(raw, want):
    assert check_kind(raw) == want


@pytest.mark.parametrize("raw", ["Pi-Durable", "-x", "a b", "x" * 33, 7, "ö"])
def test_check_kind_refuses(raw):
    with pytest.raises(ValueError):
        check_kind(raw)


# --------------------------------------------------------- the record shape

def test_clean_keeps_the_known_fields_and_drops_empties():
    got = progress.clean({
        "stage": "  round   two ", "done": 3, "total": 10, "active": 0,
        "text": "", "file": " /runs/x/status.json ",
    })
    assert got == {
        "stage": "round two", "done": 3, "total": 10, "active": 0,
        "file": "/runs/x/status.json",
    }


@pytest.mark.parametrize("value", [None, {}, {"text": "  "}])
def test_clean_reads_empty_as_clear(value):
    assert progress.clean(value) is None


@pytest.mark.parametrize("value", [
    {"phase": "x"}, {"done": -1}, {"done": True}, {"done": 1.5},
    {"stage": 3}, {"stage": "x" * 65}, {"text": "x" * 201}, [1], "stage",
])
def test_clean_refuses(value):
    with pytest.raises(ValueError):
        progress.clean(value)


def test_parse_osc_refuses_what_is_not_json():
    with pytest.raises(ValueError):
        progress.parse_osc(b"{not json")


# ------------------------------------------------------------- the filter

def test_a_whole_sequence_is_taken_out_and_the_rest_kept():
    f = progress.OscFilter()
    out, bodies = f.feed(b"before" + osc({"stage": "a"}) + b"after")
    assert out == b"beforeafter"
    assert [json.loads(b) for b in bodies] == [{"stage": "a"}]


def test_the_string_terminator_ends_one_too():
    out, bodies = progress.OscFilter().feed(osc({"done": 1}, term=b"\x1b\\") + b"x")
    assert out == b"x" and json.loads(bodies[0]) == {"done": 1}


def test_a_sequence_cut_across_every_byte_is_still_found():
    """A PTY read can end anywhere; nothing of the sequence may leak out on
    the way, and the text around it must come through whole."""
    data = b"ab" + osc({"stage": "s", "total": 4}) + b"cd" + osc({}) + b"ef"
    f = progress.OscFilter()
    out, bodies = b"", []
    for i in range(len(data)):
        o, b = f.feed(data[i:i + 1])
        out += o
        bodies += b
    assert out == b"abcdef"
    assert [json.loads(b) for b in bodies] == [{"stage": "s", "total": 4}, {}]


def test_other_escapes_pass_straight_through():
    data = b"\x1b]0;title\x07\x1b[31mred\x1b]777;claunch;activity;busy\x07"
    assert progress.OscFilter().feed(data) == (data, [])


def test_a_tail_that_cannot_become_one_is_not_held():
    f = progress.OscFilter()
    assert f.feed(b"x\x1b[") == (b"x\x1b[", [])
    assert f.feed(b"\x1b]0;") == (b"\x1b]0;", [])


def test_a_tail_that_might_become_one_waits_for_the_next_read():
    f = progress.OscFilter()
    assert f.feed(b"x\x1b]777;cl") == (b"x", [])
    out, bodies = f.feed(b"aunch;progress;{}\x07y")
    assert (out, bodies) == (b"y", [b"{}"])


def test_a_never_ending_sequence_is_let_through():
    """The screen must not wait forever on bytes that will never close."""
    f = progress.OscFilter()
    start = b"\x1b]" + progress.OSC_PREFIX
    assert f.feed(start + b"{") == (b"", [])
    out, bodies = f.feed(b"x" * (progress.MAX_BYTES + 64))
    assert bodies == [] and out.startswith(start) and len(out) > progress.MAX_BYTES


# ------------------------------------------------------------ the session

def test_the_session_keeps_the_sequence_off_every_screen(home, tmp_path, monkeypatch):
    """The renderer and the viewers get the output without the sequence, the
    hook gets its body, and pi's own busy signal still counts."""
    async def run():
        s = Session(SessionDef(name="p", harness="pi", cwd=str(tmp_path)),
                    idle_threshold=2, scrollback=20)
        rendered, sent, heard = [], [], []
        monkeypatch.setattr(s._feeder, "submit", rendered.append)
        monkeypatch.setattr(s, "_broadcast", sent.append)
        s.on_progress = lambda sess, body: heard.append(body)
        try:
            s._on_output(b"\x1b]777;claunch;activity;busy\x07hi" + osc({"stage": "x"}))
            s._on_output(osc({"done": 2}))  # nothing else in this read
            assert rendered == [b"\x1b]777;claunch;activity;busy\x07hi"]
            assert sent == [("data", b"\x1b]777;claunch;activity;busy\x07hi")]
            assert [json.loads(b) for b in heard] == [{"stage": "x"}, {"done": 2}]
            assert s._activity.busy(15)
        finally:
            s._feeder.close()
            s._log.close()
    asyncio.run(run())


# --------------------------------------------------------------- the store

def test_the_store_dedups_stamps_persists_and_forgets(home):
    path = paths.session_progress_json()
    store = progress.ProgressStore(path)
    first = store.set("a", {"stage": "one"})
    assert first["stage"] == "one" and first["at"]
    stamp = path.stat().st_mtime_ns
    assert store.set("a", {"stage": "one"}) == first  # same value: no write
    assert path.stat().st_mtime_ns == stamp
    store.set("b", {"done": 1, "total": 2})
    assert progress.ProgressStore(path).get("b")["total"] == 2  # survives a restart
    store.forget(["a"])
    assert store.get("a") is None
    assert store.set("b", {}) is None and store.get("b") is None


# ---------------------------------------------------------------- the API

@pytest.fixture
def mgr(home):
    return SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)


def _dead(mgr, name, cwd, **sdef_fields):
    mgr._sessions[name] = DeadSession(
        SessionDef(name=name, harness="pi", cwd=str(cwd), **sdef_fields), exit_code=0
    )


def _client(mgr, scenario):
    async def run():
        app = build_app(mgr, "sekrit", started_at=time.monotonic())
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            return await scenario(client)
        finally:
            await client.close()

    return asyncio.run(run())


def test_the_endpoint_publishes_reads_and_clears(mgr, tmp_path):
    _dead(mgr, "r1", tmp_path)

    async def scenario(client):
        url = "/api/sessions/r1/progress"
        put = await client.post(url, headers=BEARER, json={"stage": "ask", "done": 1, "total": 3})
        assert put.status == 200, await put.text()
        got = (await (await client.get(url, headers=BEARER)).json())["progress"]
        assert got["stage"] == "ask" and got["at"]
        assert (await client.delete(url, headers=BEARER)).status == 200
        after = (await (await client.get(url, headers=BEARER)).json())["progress"]
        return after

    assert _client(mgr, scenario) is None


def test_the_endpoint_refuses_bad_bodies_and_unknown_sessions(mgr, tmp_path):
    _dead(mgr, "r1", tmp_path)
    mgr.progress.set("r1", {"stage": "keep"})

    async def scenario(client):
        url = "/api/sessions/r1/progress"
        unknown = await client.post(url, headers=BEARER, json={"phase": "x"})
        malformed = await client.post(
            url, headers={**BEARER, "Content-Type": "application/json"}, data=b"{nope")
        missing = await client.get("/api/sessions/nobody/progress", headers=BEARER)
        return unknown.status, malformed.status, missing.status

    assert _client(mgr, scenario) == (400, 400, 404)
    # A refused body must not have cleared what was there.
    assert mgr.progress.get("r1")["stage"] == "keep"


def test_kind_and_progress_reach_the_rail_and_the_kind_filter(mgr, tmp_path):
    """``view=rail`` answers from an allow-list; a field left out of it never
    reaches the dashboard."""
    _dead(mgr, "d1", tmp_path, kind="pi-durable")
    _dead(mgr, "p1", tmp_path)
    mgr.progress.set("d1", {"stage": "round", "done": 2, "total": 5, "active": 3})

    async def scenario(client):
        rail = await (await client.get("/api/sessions?view=rail", headers=BEARER)).json()
        only = await (await client.get("/api/sessions?kind=pi-durable", headers=BEARER)).json()
        return {r["name"]: r for r in rail["sessions"]}, [r["name"] for r in only["sessions"]]

    rows, filtered = _client(mgr, scenario)
    assert rows["d1"]["kind"] == "pi-durable"
    assert rows["d1"]["progress"]["active"] == 3
    assert "kind" not in rows["p1"] and "progress" not in rows["p1"]
    assert filtered == ["d1"]


def test_removing_a_session_drops_its_progress(mgr, tmp_path):
    _dead(mgr, "gone", tmp_path)
    mgr.progress.set("gone", {"stage": "x"})
    mgr.remove("gone")
    assert mgr.progress.get("gone") is None


def test_the_manager_hook_stores_valid_and_drops_invalid(mgr, tmp_path):
    _dead(mgr, "h", tmp_path)
    s = mgr.get("h")
    mgr._session_progress(s, b'{"stage": "ok"}')
    mgr._session_progress(s, b'{"bogus": 1}')
    mgr._session_progress(s, b"not json")
    assert mgr.progress.get("h")["stage"] == "ok"


# --------------------------------------------------------- create with kind

_CHILD = "import sys\nprint('READY')\nfor line in sys.stdin:\n    pass\n"


def _launching(scenario):
    import sys

    from claude_launcher import lineage, profile, store

    store.update(lambda doc: doc.update(
        {"harnesses": {"py": {"command": [sys.executable, "-u", "-c", _CHILD]}}}))
    if not profile.resolve("py").exists():
        lineage.set_harness(profile.create("py"), "py")

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        app = build_app(mgr, "sekrit", started_at=time.monotonic())
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            await scenario(client, mgr)
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_create_takes_a_kind_and_a_child_does_not_inherit_it(home, tmp_path):
    async def scenario(client, mgr):
        resp = await client.post("/api/sessions", headers=BEARER, json={
            "name": "k1", "profile": "py", "cwd": str(tmp_path),
            "beads": False, "kind": "pi-durable",
        })
        assert resp.status == 201, await resp.text()
        assert (await resp.json())["kind"] == "pi-durable"
        assert mgr.get("k1").sdef.kind == "pi-durable"

        resp = await client.post("/api/sessions/k1/children", headers=BEARER, json={
            "name": "k2", "beads": False,
        })
        assert resp.status == 201, await resp.text()
        assert mgr.get("k2").sdef.kind is None

    _launching(scenario)


def test_create_refuses_a_bad_kind_before_anything_starts(home, tmp_path):
    async def scenario(client, mgr):
        resp = await client.post("/api/sessions", headers=BEARER, json={
            "name": "k3", "profile": "py", "cwd": str(tmp_path),
            "beads": False, "kind": "Not A Word",
        })
        assert resp.status == 400, await resp.text()
        assert "kind" in (await resp.json())["error"]
        assert "k3" not in mgr._sessions

    _launching(scenario)


# -------------------------------------------------------------------- CLI

@pytest.mark.parametrize("prog,want", [
    (None, ""), ({}, ""),
    ({"stage": "ask", "done": 3, "total": 10, "active": 2, "text": "B"},
     "ask 3/10 (2 active) B"),
    ({"total": 4}, "0/4"), ({"done": 7}, "7"), ({"file": "/x"}, ""),
])
def test_progress_brief(prog, want):
    assert cli_sessions.progress_brief(prog) == want


def test_new_session_and_progress_parse_their_flags():
    from claude_launcher import cli

    parser = cli.build_parser()
    args = parser.parse_args(["new-session", "--profile", "ds4:pi", "--kind", "pi-durable"])
    assert args.kind == "pi-durable"
    args = parser.parse_args([
        "progress", "-t", "sim-1", "--stage", "ask", "--done", "2", "--total", "9",
        "--active", "1", "--file", "/r/status.json",
    ])
    assert (args.target, args.stage, args.done, args.total, args.active, args.file) == (
        "sim-1", "ask", 2, 9, 1, "/r/status.json")
    assert args.func is cli_sessions._cmd_progress
