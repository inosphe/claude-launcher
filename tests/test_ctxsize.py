"""The context reading the dashboard shows for each session.

``daemon/ctxsize.py`` answers one question — how full is this session's
conversation — from the transcript claude itself writes. The tests here pin
the four things that can go wrong with that: reading the *wrong* turn (an
older one, or a subagent's), reading a turn that says nothing, paying for the
read twice, and reporting a number where there is none.

The last is the one worth being strict about. There is no denominator
anywhere — nothing records the context limit and it differs by model — so the
only defence against a plausible-looking lie is that "not known" must never
arrive as a number, not even as zero.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

import pytest

from claude_launcher import transcripts
from claude_launcher.daemon import ctxsize
from claude_launcher.daemon.harness import SessionDef

CID = "cafe0000-0000-0000-0000-0000000000ff"


@pytest.fixture(autouse=True)
def _fresh_cache():
    ctxsize.forget()
    yield
    ctxsize.forget()


def turn(*, read=0, write=0, fresh=0, out=0, model="claude-opus-5",
         at="2026-08-20T12:00:00.000Z", side=False) -> str:
    """One assistant entry, shaped the way claude writes them."""
    return json.dumps({
        "type": "assistant",
        "isSidechain": side,
        "timestamp": at,
        "message": {
            "role": "assistant",
            "model": model,
            "usage": {
                "input_tokens": fresh,
                "cache_read_input_tokens": read,
                "cache_creation_input_tokens": write,
                "output_tokens": out,
            },
        },
    })


def write_jsonl(tmp_path, *lines):
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# one entry: what counts as a reading
# --------------------------------------------------------------------------- #
def test_context_is_the_whole_input_side_however_it_was_billed():
    """fresh + replayed + written are one number, not three."""
    got = ctxsize.usage_of(json.loads(turn(fresh=2, read=154_073, write=631, out=210)))
    assert got["tokens"] == 154_706
    assert (got["input"], got["cache_read"], got["cache_write"]) == (2, 154_073, 631)
    assert got["output"] == 210
    assert got["model"] == "claude-opus-5"
    assert got["at"] == "2026-08-20T12:00:00.000Z"


def test_a_subagents_turn_is_not_this_conversations_size():
    """A sidechain runs on its own context; counting it mislabels the row."""
    assert ctxsize.usage_of(json.loads(turn(read=9_000, side=True))) is None


@pytest.mark.parametrize("entry", [
    {"type": "user", "message": {"role": "user", "content": "hi"}},
    {"type": "assistant", "message": {"role": "assistant"}},          # no usage
    {"type": "assistant", "message": "not a dict"},
    {"type": "system", "subtype": "compact_boundary"},
])
def test_entries_that_hold_no_reading(entry):
    assert ctxsize.usage_of(entry) is None


def test_a_turn_reporting_no_input_at_all_is_not_a_conversation_of_size_zero():
    """An interrupted record must read as "no reading", never as empty."""
    assert ctxsize.usage_of(json.loads(turn())) is None


def test_unparseable_counts_do_not_become_numbers():
    entry = json.loads(turn(read=10))
    entry["message"]["usage"]["cache_read_input_tokens"] = "lots"
    entry["message"]["usage"]["input_tokens"] = 5
    got = ctxsize.usage_of(entry)
    assert got["tokens"] == 5 and got["cache_read"] == 0


# --------------------------------------------------------------------------- #
# the file: which turn is read
# --------------------------------------------------------------------------- #
def test_the_newest_turn_wins(tmp_path):
    path = write_jsonl(
        tmp_path,
        turn(read=10_000, at="2026-08-20T10:00:00.000Z"),
        turn(read=90_000, at="2026-08-20T11:00:00.000Z"),
    )
    got = ctxsize.read_tail(path)
    assert got["tokens"] == 90_000 and got["at"] == "2026-08-20T11:00:00.000Z"


def test_a_compaction_needs_no_special_handling(tmp_path):
    """The turn after a compact reports the small context, so reading the
    newest turn already tells the truth about it."""
    path = write_jsonl(
        tmp_path,
        turn(read=168_000),
        json.dumps({"type": "system", "subtype": "compact_boundary",
                    "compactMetadata": {"trigger": "auto", "preTokens": 168_023}}),
        turn(read=9_962),
    )
    assert ctxsize.read_tail(path)["tokens"] == 9_962


def test_a_subagent_answering_last_does_not_hide_the_session(tmp_path):
    path = write_jsonl(
        tmp_path,
        turn(read=120_000),
        turn(read=4_000, side=True),
        turn(read=1_500, side=True),
    )
    assert ctxsize.read_tail(path)["tokens"] == 120_000


def test_broken_lines_are_skipped_not_fatal(tmp_path):
    path = write_jsonl(tmp_path, turn(read=7_000), "{not json", "")
    assert ctxsize.read_tail(path)["tokens"] == 7_000


def test_the_read_widens_past_a_wall_of_tool_output(tmp_path):
    """The turn is small; the tool results before it are not. The first
    window must not be mistaken for the whole file."""
    filler = json.dumps({"type": "user", "message": {"role": "user", "content": "x" * 20_000}})
    path = write_jsonl(
        tmp_path, turn(read=55_555), *[filler] * 8
    )
    assert path.stat().st_size > ctxsize.FIRST_CHUNK
    assert ctxsize.read_tail(path)["tokens"] == 55_555


def test_a_file_with_no_assistant_turn_reads_as_not_known(tmp_path):
    path = write_jsonl(tmp_path, json.dumps({"type": "user", "message": {"content": "hi"}}))
    assert ctxsize.read_tail(path) is None


def test_a_transcript_that_is_not_there_reads_as_not_known(tmp_path):
    assert ctxsize.read_tail(tmp_path / "nope.jsonl") is None


# --------------------------------------------------------------------------- #
# a session: locating, caching, and the absence of a number
# --------------------------------------------------------------------------- #
def _claude_session(tmp_path, *lines):
    """A claude session whose transcript sits where claude would keep it."""
    cwd = tmp_path / "work"
    cwd.mkdir(exist_ok=True)
    pdir = transcripts.project_dir(tmp_path / ".claude-config", str(cwd))
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / f"{CID}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return SessionDef(name="s1", harness="claude", cwd=str(cwd), conversation_id=CID)


def test_a_session_is_read_through_the_path_claude_files_it_under(home, tmp_path):
    sdef = _claude_session(tmp_path, turn(read=42_000, write=1_000))
    assert ctxsize.for_session(sdef)["tokens"] == 43_000


def test_another_harness_keeps_no_such_file(home, tmp_path):
    sdef = _claude_session(tmp_path, turn(read=42_000))
    assert ctxsize.for_session(SessionDef(
        name="s1", harness="codex", cwd=sdef.cwd, conversation_id=CID
    )) is None


def test_a_session_with_no_conversation_pinned_has_nothing_to_read(home, tmp_path):
    sdef = _claude_session(tmp_path, turn(read=42_000))
    assert ctxsize.for_session(SessionDef(
        name="s1", harness="claude", cwd=sdef.cwd
    )) is None


def test_an_unchanged_file_is_not_read_twice(home, tmp_path, monkeypatch):
    sdef = _claude_session(tmp_path, turn(read=42_000))
    assert ctxsize.for_session(sdef)["tokens"] == 42_000
    monkeypatch.setattr(
        ctxsize, "read_tail",
        lambda path: pytest.fail("the poll re-read a file that had not changed")
    )
    assert ctxsize.for_session(sdef)["tokens"] == 42_000


def test_a_new_turn_is_picked_up(home, tmp_path):
    sdef = _claude_session(tmp_path, turn(read=42_000))
    assert ctxsize.for_session(sdef)["tokens"] == 42_000
    pdir = transcripts.project_dir(tmp_path / ".claude-config", sdef.cwd)
    with (pdir / f"{CID}.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(turn(read=61_000) + "\n")
    assert ctxsize.for_session(sdef)["tokens"] == 61_000


class _Stub:
    """The shape ``attach`` needs from a session: its record and its facts."""

    def __init__(self, sdef, **extra):
        self.sdef = sdef
        self._extra = extra

    def info(self):
        return {"name": self.sdef.name, "status": "busy", **self._extra}


def test_attach_hangs_the_reading_on_the_session(home, tmp_path):
    sdef = _claude_session(tmp_path, turn(read=42_000, out=300))
    info = ctxsize.attach(_Stub(sdef))
    assert info["name"] == "s1" and info["status"] == "busy"
    assert info["context"]["tokens"] == 42_000
    assert info["context"]["output"] == 300


def test_attach_omits_the_key_rather_than_reporting_zero(home, tmp_path):
    """No denominator exists, so a fabricated numerator is the whole risk:
    a reader must not be able to draw "not known" as a measurement."""
    cwd = tmp_path / "work"
    cwd.mkdir(exist_ok=True)
    info = ctxsize.attach(_Stub(SessionDef(name="s2", harness="claude", cwd=str(cwd))))
    assert "context" not in info


# --------------------------------------------------------------------------- #
# the compact window: the one threshold worth drawing on the gauge
# --------------------------------------------------------------------------- #
def test_window_comes_from_the_sessions_own_env_first(home, monkeypatch):
    """Spawn precedence, mirrored: sdef.env is applied last by build_command,
    so it must win here too."""
    monkeypatch.setenv(ctxsize.COMPACT_WINDOW_ENV, "111000")
    sdef = SessionDef(name="s", harness="claude",
                      env={ctxsize.COMPACT_WINDOW_ENV: "250000"})
    assert ctxsize.compact_window_of(sdef) == 250_000


def test_window_falls_back_to_the_daemons_environment(home, monkeypatch):
    """A session whose profile cannot be resolved still inherited the daemon's
    env at spawn, so that value is the honest floor — not None."""
    monkeypatch.setenv(ctxsize.COMPACT_WINDOW_ENV, "111000")
    assert ctxsize.compact_window_of(
        SessionDef(name="s", harness="claude")) == 111_000


def test_the_profile_chain_overrides_the_daemons_environment(home, monkeypatch):
    from claude_launcher import profile as profile_mod, settings
    prof = profile_mod.create("nc")
    settings.set_env(prof, {ctxsize.COMPACT_WINDOW_ENV: "250000"})
    monkeypatch.setenv(ctxsize.COMPACT_WINDOW_ENV, "111000")
    sdef = SessionDef(name="s", harness="claude", profile="nc")
    assert ctxsize.compact_window_of(sdef) == 250_000


def test_garbage_and_absence_read_as_no_window(home, monkeypatch):
    """An unset, garbled or zero value is "no window configured" — the gauge
    must not grow a tick at zero, or at whatever int('lots') is not."""
    monkeypatch.delenv(ctxsize.COMPACT_WINDOW_ENV, raising=False)
    assert ctxsize.compact_window_of(SessionDef(name="s", harness="claude")) is None
    assert ctxsize.compact_window_of(SessionDef(
        name="s", harness="claude",
        env={ctxsize.COMPACT_WINDOW_ENV: "lots"})) is None
    assert ctxsize.compact_window_of(SessionDef(
        name="s2", harness="claude",
        env={ctxsize.COMPACT_WINDOW_ENV: "0"})) is None


def test_another_harness_has_no_window(home, monkeypatch):
    """The window is claude's knob; hanging it on a codex row would claim a
    compaction that harness will never perform."""
    monkeypatch.setenv(ctxsize.COMPACT_WINDOW_ENV, "111000")
    assert ctxsize.compact_window_of(SessionDef(name="s", harness="codex")) is None


def test_attach_hangs_the_window_beside_the_reading(home, tmp_path, monkeypatch):
    monkeypatch.setenv(ctxsize.COMPACT_WINDOW_ENV, "200000")
    sdef = _claude_session(tmp_path, turn(read=42_000))
    info = ctxsize.attach(_Stub(sdef))
    assert info["context"]["compact_window"] == 200_000
    # attach copies the cached reading before annotating it — the shared cache
    # entry must not accumulate a key the next caller did not resolve
    assert "compact_window" not in ctxsize.for_session(sdef)


def test_attach_does_not_fabricate_a_window(home, tmp_path, monkeypatch):
    monkeypatch.delenv(ctxsize.COMPACT_WINDOW_ENV, raising=False)
    sdef = _claude_session(tmp_path, turn(read=42_000))
    assert "compact_window" not in ctxsize.attach(_Stub(sdef))["context"]


# --------------------------------------------------------------------------- #
# the endpoints: what the dashboard actually receives
# --------------------------------------------------------------------------- #
CHILD = "import time\nprint('READY')\ntime.sleep(60)\n"
BEARER = {"Authorization": "Bearer sekrit"}


async def _serve(mgr):
    from aiohttp.test_utils import TestClient, TestServer

    from claude_launcher.daemon.api import build_app
    from claude_launcher.daemon.mesh import MeshManager

    app = build_app(mgr, "sekrit", started_at=time.monotonic(), mesh=MeshManager(mgr))
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


def test_both_endpoints_carry_the_reading(home, tmp_path, monkeypatch):
    """The rail reads the list, the details panel reads the meta; both must
    hold the same number, since either could be the one someone believes.

    The session here runs python rather than claude — nothing in a test can
    start the real harness — so the *name* that counts as claude is the one
    thing swapped out. The locating, reading, caching and serving are the
    shipped code, over real HTTP.
    """
    from claude_launcher import store
    from claude_launcher.daemon.manager import SessionManager

    store.update(
        lambda doc: doc.update(
            {"harnesses": {"py": {"command": [sys.executable, "-u", "-c", CHILD]}}}
        )
    )
    monkeypatch.setattr(ctxsize, "CLAUDE_HARNESS", "py")

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        client = await _serve(mgr)
        try:
            cwd = tmp_path / "work"
            cwd.mkdir()
            mgr.create(SessionDef(name="s1", harness="py", cwd=str(cwd),
                                  conversation_id=CID))
            pdir = transcripts.project_dir(tmp_path / ".claude-config", str(cwd))
            pdir.mkdir(parents=True)
            (pdir / f"{CID}.jsonl").write_text(
                turn(fresh=2, read=154_073, write=631, out=210) + "\n",
                encoding="utf-8",
            )

            resp = await client.get("/api/sessions", headers=BEARER)
            assert resp.status == 200
            listed = (await resp.json())["sessions"][0]
            assert listed["context"]["tokens"] == 154_706
            assert listed["context"]["model"] == "claude-opus-5"

            resp = await client.get("/api/sessions/s1/meta", headers=BEARER)
            assert resp.status == 200
            meta = (await resp.json())["session"]
            assert meta["context"] == listed["context"]
        finally:
            await client.close()
            await mgr.shutdown_all()

    asyncio.run(run())


def test_a_session_with_no_transcript_carries_no_key(home, tmp_path, monkeypatch):
    """Absence must arrive as absence — a dashboard cannot render a missing
    key as a number, but it can render a zero as one."""
    from claude_launcher import store
    from claude_launcher.daemon.manager import SessionManager

    store.update(
        lambda doc: doc.update(
            {"harnesses": {"py": {"command": [sys.executable, "-u", "-c", CHILD]}}}
        )
    )
    monkeypatch.setattr(ctxsize, "CLAUDE_HARNESS", "py")

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        client = await _serve(mgr)
        try:
            cwd = tmp_path / "work"
            cwd.mkdir()
            mgr.create(SessionDef(name="s1", harness="py", cwd=str(cwd),
                                  conversation_id=CID))
            resp = await client.get("/api/sessions", headers=BEARER)
            assert "context" not in (await resp.json())["sessions"][0]
        finally:
            await client.close()
            await mgr.shutdown_all()

    asyncio.run(run())


def test_both_endpoints_carry_the_git_branch(home, tmp_path, monkeypatch):
    """The rail row and the detail panel read the branch from the session's
    own checkout — the same reading, so whichever one someone believes holds.
    A directory with no git carries '' rather than a guess.

    The reading itself happens off the event loop, so it is the *second* poll
    that carries a branch for a directory nobody has read yet. That is the
    daemon's side of the bargain — a git that hangs costs a branch, never the
    loop — and every screen that shows a branch is on a 2s poll, so the cost
    is one tick rather than a blank that never fills."""
    import subprocess

    from claude_launcher import store
    from claude_launcher.daemon import api as api_mod
    from claude_launcher.daemon.manager import SessionManager

    def git(*args, cwd):
        return subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
        )

    store.update(
        lambda doc: doc.update(
            {"harnesses": {"py": {"command": [sys.executable, "-u", "-c", CHILD]}}}
        )
    )
    # A directory outside any repository is arranged, not assumed: pytest's
    # basetemp can sit inside a checkout, and git's discovery walks upward.
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))

    # A real repository on a branch that already differs from master, and a
    # plain directory that is no checkout at all.
    repo = tmp_path / "repo"
    repo.mkdir()
    git("init", "-q", cwd=repo)
    git("config", "user.email", "t@example.com", cwd=repo)
    git("config", "user.name", "t", cwd=repo)
    (repo / "a.txt").write_text("hi\n", encoding="utf-8")
    git("add", "-A", cwd=repo)
    git("commit", "-qm", "init", cwd=repo)
    git("checkout", "-qb", "s149-review", cwd=repo)
    plain = tmp_path / "plain"
    plain.mkdir()

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        client = await _serve(mgr)
        try:
            mgr.create(SessionDef(name="on-branch", harness="py", cwd=str(repo),
                                  conversation_id=CID))
            mgr.create(SessionDef(name="no-repo", harness="py", cwd=str(plain),
                                  conversation_id="cafe0000-0000-0000-0000-0000000000fe"))

            # A directory nobody has read yet: the poll starts the reading
            # and is answered from the empty cache, immediately. Deterministic
            # rather than racy — the value is captured while the response is
            # built, before any worker thread can land.
            resp = await client.get("/api/sessions", headers=BEARER)
            assert resp.status == 200
            first = {s["name"]: s for s in (await resp.json())["sessions"]}
            assert first["on-branch"]["branch"] == ""

            # Wait for both readings to land — both, not merely the one with a
            # branch to show. Off the loop, an empty branch is two different
            # answers ("looked, no git there" and "not looked at yet"), and
            # only the first is worth asserting; waiting until the repository
            # answers would let the plain directory pass without ever having
            # been read. The keys come from the API's own canonicaliser so the
            # wait cannot drift from what the cache is actually keyed by.
            keys = [api_mod._session_cwd(s) for s in mgr.list()]
            assert len(keys) == 2
            deadline = time.monotonic() + 10.0
            while not all(k in api_mod._branch_cache for k in keys):
                assert time.monotonic() < deadline, (
                    f"branch readings never landed: {keys} / {api_mod._branch_cache}"
                )
                await asyncio.sleep(0.02)

            # Both endpoints now carry it, and carry the same thing — one
            # cache behind both. The plain directory's '' is a reading that
            # happened and found no git, not one still in flight.
            resp = await client.get("/api/sessions", headers=BEARER)
            assert resp.status == 200
            rows = {s["name"]: s for s in (await resp.json())["sessions"]}
            assert rows["on-branch"]["branch"] == "s149-review"
            assert rows["no-repo"]["branch"] == ""

            resp = await client.get("/api/sessions/on-branch/meta", headers=BEARER)
            assert resp.status == 200
            meta = (await resp.json())["session"]
            assert meta["branch"] == "s149-review"
        finally:
            await client.close()
            await mgr.shutdown_all()

    asyncio.run(run())
