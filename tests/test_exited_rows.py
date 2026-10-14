"""An exited session's list row is read once and kept while its files hold.

``daemon/exited_rows.py`` (claunch-0gysk): the session list re-ran every
per-row read for exited sessions on each poll. The tests pin the three things
the user decided -- the row is read once after the exit, later polls answer
from the kept row without touching the readers, and a change to a source file
reads it again -- and that the kept answer equals the live one. The files
are looked at once every ``CHECK_TTL`` seconds, not on every poll
(claunch-lcwv3).
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import pytest

from claude_launcher import transcripts
from claude_launcher.daemon import (
    ctxsize, exited_rows, session_prs, tokenusage, work_checkout,
)
from claude_launcher.daemon.harness import SessionDef

CID = "cafe0000-0000-0000-0000-0000000000aa"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    # Most tests below pin what a look at the files sees, so they look at
    # every poll; the wait between looks (claunch-lcwv3) has its own tests.
    monkeypatch.setattr(exited_rows, "CHECK_TTL", 0.0)
    ctxsize.forget()
    exited_rows.forget()
    yield
    ctxsize.forget()
    exited_rows.forget()


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def _turn(mid: str, at: float, *, read: int, out: int, tools: int = 0) -> str:
    return json.dumps({
        "type": "assistant", "isSidechain": False, "timestamp": _iso(at),
        "message": {
            "id": mid, "role": "assistant", "model": "claude-opus-5",
            "content": [{"type": "text", "text": "x"}] + [
                {"type": "tool_use", "id": f"{mid}-{i}", "name": "Bash", "input": {}}
                for i in range(tools)
            ],
            "usage": {"input_tokens": 10, "cache_read_input_tokens": read,
                      "cache_creation_input_tokens": 0, "output_tokens": out},
        },
    })


class _Session:
    def __init__(self, sdef, exited_at="2026-10-14T06:00:00+00:00"):
        self.sdef = sdef
        self.exited = True
        self.exited_at = exited_at

    def info(self):
        return {"name": self.sdef.name, "status": "exited", "exited_at": self.exited_at}


def _transcript(tmp_path):
    cwd = tmp_path / "work"
    cwd.mkdir(exist_ok=True)
    pdir = transcripts.project_dir(tmp_path / ".claude-config", str(cwd))
    pdir.mkdir(parents=True, exist_ok=True)
    return cwd, pdir / f"{CID}.jsonl"


def _session(tmp_path, *lines):
    cwd, path = _transcript(tmp_path)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    sdef = SessionDef(name="old", harness="claude", cwd=str(cwd), conversation_id=CID)
    return _Session(sdef), path


def _live(session, cwd):
    """What the list's live path attaches, for comparison."""
    spend = tokenusage.budget()
    try:
        info = ctxsize.attach(session)
        tokenusage.attach(info, session.sdef, spend)
        work_checkout.attach(info, session.sdef)
        info["prs"] = session_prs.for_session(cwd, info["name"])
    finally:
        spend.release()
    return info


def _kept(session, cwd):
    spend = tokenusage.budget()
    try:
        return exited_rows.attach(session, spend, cwd)
    finally:
        spend.release()


def _count_reads(monkeypatch):
    calls = []
    for mod, fn in ((ctxsize, "attach"), (tokenusage, "attach"),
                    (work_checkout, "attach"), (session_prs, "for_session")):
        real = getattr(mod, fn)

        def spy(*a, _real=real, _name=f"{mod.__name__}.{fn}", **kw):
            calls.append(_name)
            return _real(*a, **kw)
        monkeypatch.setattr(mod, fn, spy)
    return calls


def test_the_kept_row_answers_what_the_live_path_answers(home, tmp_path):
    now = time.time()
    session, _ = _session(tmp_path, _turn("m1", now - 30, read=42_000, out=300, tools=2))
    cwd = session.sdef.cwd
    live = _live(session, cwd)
    first = _kept(session, cwd)
    again = _kept(session, cwd)
    assert first == live and again == live
    assert live["context"]["tokens"] > 0
    assert live["tool_calls"] == 2 and live[tokenusage.INFO_KEY]["output"] == 300


def test_later_polls_do_not_run_the_readers_until_a_file_changes(home, tmp_path, monkeypatch):
    now = time.time()
    session, path = _session(tmp_path, _turn("m1", now - 30, read=1_000, out=5))
    cwd = session.sdef.cwd
    calls = _count_reads(monkeypatch)
    _kept(session, cwd)
    assert len(calls) == 4  # read once after the exit
    for _ in range(3):
        row = _kept(session, cwd)
    assert len(calls) == 4  # kept: no reader ran
    assert row[tokenusage.INFO_KEY]["output"] == 5
    # The transcript changed: the row is read again and shows the change.
    with path.open("a", encoding="utf-8") as fh:
        fh.write(_turn("m2", now - 10, read=2_000, out=7) + "\n")
    row = _kept(session, cwd)
    assert len(calls) == 8
    assert row[tokenusage.INFO_KEY]["output"] == 12


def test_a_new_subagent_file_or_pr_record_reads_the_row_again(home, tmp_path, monkeypatch):
    now = time.time()
    session, path = _session(tmp_path, _turn("m1", now - 30, read=1_000, out=5))
    cwd = session.sdef.cwd
    _kept(session, cwd)
    calls = _count_reads(monkeypatch)
    sub = path.parent / path.stem / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-1.jsonl").write_text(_turn("a1", now - 20, read=1, out=40) + "\n",
                                       encoding="utf-8")
    row = _kept(session, cwd)
    assert len(calls) == 4
    assert row[tokenusage.INFO_KEY]["subagents"]["output"] == 40
    session_prs.record("old", {"ok": True, "branch": "b", "base": "master",
                               "pr": {"url": "https://h/o/r/pull/7"}}, {"title": "t"})
    row = _kept(session, cwd)
    assert len(calls) == 8 and [p["number"] for p in row["prs"]] == [7]
    _kept(session, cwd)
    assert len(calls) == 8


def test_a_new_exit_reads_the_row_again(home, tmp_path, monkeypatch):
    session, _ = _session(tmp_path, _turn("m1", time.time() - 30, read=1_000, out=5))
    cwd = session.sdef.cwd
    _kept(session, cwd)
    calls = _count_reads(monkeypatch)
    session.exited_at = "2026-10-14T07:00:00+00:00"  # resumed and exited again
    _kept(session, cwd)
    assert len(calls) == 4


def test_the_tool_count_still_moves_with_the_clock(home, tmp_path, monkeypatch):
    now = time.time()
    session, _ = _session(tmp_path, _turn("m1", now - 30, read=1_000, out=5, tools=3))
    cwd = session.sdef.cwd
    assert _kept(session, cwd)["tool_calls"] == 3
    later = now + 2 * ctxsize.TOOL_WINDOW
    monkeypatch.setattr(ctxsize.time, "time", lambda: later)
    assert _kept(session, cwd)["tool_calls"] == 0


def test_a_kept_row_is_not_shared_with_its_readers(home, tmp_path):
    session, _ = _session(tmp_path, _turn("m1", time.time() - 30, read=1_000, out=5))
    cwd = session.sdef.cwd
    _kept(session, cwd)["context"]["tokens"] = -1
    _kept(session, cwd)[tokenusage.INFO_KEY]["output"] = -1
    row = _kept(session, cwd)
    assert row["context"]["tokens"] > 0 and row[tokenusage.INFO_KEY]["output"] == 5


def test_the_configured_windows_follow_the_configuration(home, tmp_path, monkeypatch):
    """``compact_window`` and ``model_context_window`` come from the config
    store, not the transcript: a kept row shows a changed value as the live
    path does (s463-rev, CHANGES REQUESTED @ 5553fb13)."""
    session, _ = _session(tmp_path, _turn("m1", time.time() - 30, read=1_000, out=5))
    cwd = session.sdef.cwd
    monkeypatch.setattr(ctxsize, "compact_window_of", lambda sdef: 100_000)
    monkeypatch.setattr(ctxsize, "declared_context_window", lambda sdef: 400_000)
    first = _kept(session, cwd)
    assert first["context"]["compact_window"] == 100_000
    assert first["context"]["model_context_window"] == 400_000
    monkeypatch.setattr(ctxsize, "compact_window_of", lambda sdef: 200_000)
    monkeypatch.setattr(ctxsize, "declared_context_window", lambda sdef: 1_000_000)
    kept = _kept(session, cwd)
    live = _live(session, cwd)
    assert kept["context"] == live["context"]
    assert kept["context"]["compact_window"] == 200_000
    assert kept["context"]["model_context_window"] == 1_000_000


def test_a_partial_spend_reading_is_not_kept(home, tmp_path, monkeypatch):
    session, _ = _session(tmp_path, _turn("m1", time.time() - 30, read=1_000, out=5))
    cwd = session.sdef.cwd
    monkeypatch.setattr(tokenusage, "INLINE_BUDGET", 0)
    first = _kept(session, cwd)
    assert first[tokenusage.INFO_KEY].get("partial")
    tokenusage.drain(timeout=10)
    monkeypatch.setattr(tokenusage, "INLINE_BUDGET", 4 * 1024 * 1024)
    row = _kept(session, cwd)
    assert not row[tokenusage.INFO_KEY].get("partial")
    assert row[tokenusage.INFO_KEY]["output"] == 5


def test_a_transcript_appearing_after_a_miss_reads_the_row_again(home, tmp_path, monkeypatch):
    cwd, path = _transcript(tmp_path)
    sdef = SessionDef(name="old", harness="claude", cwd=str(cwd), conversation_id=CID)
    session = _Session(sdef)
    row = _kept(session, str(cwd))
    assert "context" not in row and tokenusage.INFO_KEY not in row
    path.write_text(_turn("m1", time.time() - 30, read=1_000, out=5) + "\n", encoding="utf-8")
    monkeypatch.setattr(ctxsize, "MISS_TTL", 0.0)
    row = _kept(session, str(cwd))
    assert row[tokenusage.INFO_KEY]["output"] == 5


def test_the_session_list_reads_an_exited_row_once(home, tmp_path, monkeypatch):
    """Through ``GET /api/sessions``: the running row is read on every poll,
    the exited row once after its exit, and both polls answer the same."""
    import asyncio
    import sys

    from claude_launcher import store
    from claude_launcher.daemon.api import build_app
    from claude_launcher.daemon.manager import SessionManager
    from claude_launcher.daemon.mesh import MeshManager

    store.update(lambda doc: doc.update({"harnesses": {
        "py": {"command": [sys.executable, "-u", "-c", "import time; time.sleep(60)"]},
        "quick": {"command": [sys.executable, "-u", "-c", "pass"]},
    }}))
    bearer = {"Authorization": "Bearer sekrit"}
    reads = []
    real = ctxsize.attach
    monkeypatch.setattr(ctxsize, "attach",
                        lambda s: reads.append(s.sdef.name) or real(s))

    async def run():
        from aiohttp.test_utils import TestClient, TestServer

        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        client = TestClient(TestServer(build_app(
            mgr, "sekrit", started_at=time.monotonic(), mesh=MeshManager(mgr)
        )))
        await client.start_server()
        try:
            mgr.create(SessionDef(name="live", harness="py", cwd=str(tmp_path)))
            mgr.create(SessionDef(name="done", harness="quick", cwd=str(tmp_path)))
            for _ in range(200):
                if mgr.get("done").exited:
                    break
                await asyncio.sleep(0.05)
            assert mgr.get("done").exited
            session_prs.record("done", {"ok": True, "branch": "b", "base": "master",
                                        "pr": {"url": "https://h/o/r/pull/3"}}, {"title": "t"})
            bodies = []
            for _ in range(3):
                resp = await client.get("/api/sessions?view=rail", headers=bearer)
                assert resp.status == 200
                bodies.append({s["name"]: s for s in (await resp.json())["sessions"]})
            assert reads.count("live") == 3
            assert reads.count("done") == 1
            assert bodies[0]["done"]["prs"] == bodies[2]["done"]["prs"]
            assert [p["number"] for p in bodies[2]["done"]["prs"]] == [3]
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


class _Clock:
    def __init__(self, at=1000.0):
        self.at = at

    def __call__(self):
        return self.at


def _waits(monkeypatch, *, ttl=30.0, spread=0.5, draw=1.0):
    """A ``CHECK_TTL`` of ``ttl`` on a clock the test moves; every drawn
    spread is ``draw``. Returns the clock and the list of looks at a file."""
    clock = _Clock()
    monkeypatch.setattr(exited_rows, "CHECK_TTL", ttl)
    monkeypatch.setattr(exited_rows, "CHECK_SPREAD", spread)
    monkeypatch.setattr(exited_rows, "_now", clock)
    monkeypatch.setattr(exited_rows.random, "random", lambda: draw)
    looks = []
    real = exited_rows._stat
    monkeypatch.setattr(exited_rows, "_stat",
                        lambda p: looks.append(p) or real(p))
    return clock, looks


def test_a_kept_row_is_not_looked_at_until_its_wait_is_over(home, tmp_path, monkeypatch):
    now = time.time()
    session, path = _session(tmp_path, _turn("m1", now - 30, read=1_000, out=5))
    cwd = session.sdef.cwd
    clock, looks = _waits(monkeypatch)
    _kept(session, cwd)
    looks.clear()
    with path.open("a", encoding="utf-8") as fh:
        fh.write(_turn("m2", now - 10, read=2_000, out=7) + "\n")
    clock.at += 44.0  # inside 30s stretched by the drawn 50%
    for _ in range(5):
        row = _kept(session, cwd)
    assert looks == []
    assert row[tokenusage.INFO_KEY]["output"] == 5
    clock.at += 1.0  # the wait is over: the files are looked at
    row = _kept(session, cwd)
    assert looks
    assert row[tokenusage.INFO_KEY]["output"] == 12


def test_an_unchanged_row_waits_again_after_a_look(home, tmp_path, monkeypatch):
    session, _ = _session(tmp_path, _turn("m1", time.time() - 30, read=1_000, out=5))
    cwd = session.sdef.cwd
    clock, looks = _waits(monkeypatch, draw=0.0)
    calls = _count_reads(monkeypatch)
    _kept(session, cwd)
    clock.at += 30.0
    looks.clear()
    _kept(session, cwd)
    assert looks and len(calls) == 4  # looked, unchanged, not read again
    looks.clear()
    clock.at += 29.0
    _kept(session, cwd)
    assert looks == []


def test_the_spread_keeps_each_wait_between_the_ttl_and_its_stretch(home, tmp_path, monkeypatch):
    session, _ = _session(tmp_path, _turn("m1", time.time() - 30, read=1_000, out=5))
    clock, _ = _waits(monkeypatch, draw=0.25)
    _kept(session, session.sdef.cwd)
    assert exited_rows._rows["old"].due == clock.at + 30.0 * 1.125


def test_expire_looks_at_the_files_on_the_next_poll(home, tmp_path, monkeypatch):
    session, _ = _session(tmp_path, _turn("m1", time.time() - 30, read=1_000, out=5))
    cwd = session.sdef.cwd
    _waits(monkeypatch)
    assert _kept(session, cwd)["prs"] == []
    session_prs.record("old", {"ok": True, "branch": "b", "base": "master",
                               "pr": {"url": "https://h/o/r/pull/9"}}, {"title": "t"})
    assert _kept(session, cwd)["prs"] == []  # inside the wait
    exited_rows.expire("old")
    assert [p["number"] for p in _kept(session, cwd)["prs"]] == [9]
    exited_rows.expire("nobody")  # no kept row: nothing to do


def test_expire_during_a_look_is_not_lost(home, tmp_path, monkeypatch):
    """A write and its :func:`expire` landing while another poll looks at the
    files (and finds them unchanged) still reach the next poll."""
    session, _ = _session(tmp_path, _turn("m1", time.time() - 30, read=1_000, out=5))
    cwd = session.sdef.cwd
    clock, _ = _waits(monkeypatch)
    _kept(session, cwd)
    clock.at += 60.0
    real = exited_rows._fresh

    def racing(row, sdef):
        fresh = real(row, sdef)
        exited_rows.expire("old")
        return fresh
    monkeypatch.setattr(exited_rows, "_fresh", racing)
    _kept(session, cwd)
    assert exited_rows._rows["old"].due == 0.0


def test_a_new_exit_inside_the_wait_reads_the_row_again(home, tmp_path, monkeypatch):
    session, _ = _session(tmp_path, _turn("m1", time.time() - 30, read=1_000, out=5))
    cwd = session.sdef.cwd
    _waits(monkeypatch)
    _kept(session, cwd)
    calls = _count_reads(monkeypatch)
    session.exited_at = "2026-10-14T07:00:00+00:00"
    _kept(session, cwd)
    assert len(calls) == 4


def test_a_pr_the_wizard_records_shows_on_the_next_list_poll(home, tmp_path, monkeypatch):
    """Through ``POST /api/sessions/<name>/pr``: the wizard's record expires
    the kept row (:func:`exited_rows.expire`), so the list shows the PR on
    its next poll instead of after ``CHECK_TTL`` (claunch-lcwv3)."""
    import asyncio
    import sys

    from claude_launcher import prflow, store
    from claude_launcher.daemon.api import build_app
    from claude_launcher.daemon.manager import SessionManager
    from claude_launcher.daemon.mesh import MeshManager

    monkeypatch.setattr(exited_rows, "CHECK_TTL", 3600.0)
    store.update(lambda doc: doc.update({"harnesses": {
        "quick": {"command": [sys.executable, "-u", "-c", "pass"]},
    }}))
    monkeypatch.setattr(prflow, "run", lambda cwd, request, *, session="": {
        "ok": True, "cwd": cwd, "remote": "gh", "base": "master",
        "branch": "done-pr-1", "pr": {"url": "https://h/o/r/pull/5", "number": 5},
        "steps": []})
    bearer = {"Authorization": "Bearer sekrit"}

    async def run():
        from aiohttp.test_utils import TestClient, TestServer

        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        client = TestClient(TestServer(build_app(
            mgr, "sekrit", started_at=time.monotonic(), mesh=MeshManager(mgr)
        )))
        await client.start_server()
        try:
            mgr.create(SessionDef(name="done", harness="quick", cwd=str(tmp_path)))
            for _ in range(200):
                if mgr.get("done").exited:
                    break
                await asyncio.sleep(0.05)
            assert mgr.get("done").exited

            async def prs():
                resp = await client.get("/api/sessions?view=rail", headers=bearer)
                assert resp.status == 200
                rows = (await resp.json())["sessions"]
                return next(s for s in rows if s["name"] == "done")["prs"]

            assert await prs() == []  # kept, and not looked at for an hour
            resp = await client.post("/api/sessions/done/pr",
                                     json={"remote": "gh", "branch": "done-pr-1"},
                                     headers=bearer)
            assert resp.status == 200
            assert [p["number"] for p in await prs()] == [5]
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())
