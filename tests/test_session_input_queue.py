"""A line typed into the web session line while the session has exited is
queued, not refused, and is typed when the session is launched again.

The input journal (``daemon/session_input.py``) is the queue: the send writes
``queued`` and nothing else, a respawn's launch types every line still
``queued`` oldest first and records ``accepted`` then ``sent``, and a line
can be withdrawn (``cancelled``) until then. The panel reads the folded
``requests`` view to show which lines reached the terminal and which did not.
"""

from __future__ import annotations

import asyncio
import sys
import time

from claude_launcher import store
from claude_launcher.daemon import session as session_mod
from claude_launcher.daemon import session_input
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager

BEARER = {"Authorization": "Bearer sekrit"}

CHILD = (
    "import sys\n"
    "print('READY')\n"
    "for line in sys.stdin:\n"
    "    if line.strip() == 'quit':\n"
    "        break\n"
    "    print('echo:' + line.strip())\n"
)


def _statuses(name: str, request_id: str) -> list:
    return [e["status"] for e in session_input.read(name, limit=200)
            if e.get("request_id") == request_id]


# ---------------------------------------------------------------- journal


def test_the_journal_folds_events_into_one_row_per_line(home):
    session_input.write("s1", "input_accepted", request_id="a", text="one",
                        status="accepted")
    session_input.write("s1", "input_sent", request_id="a", text="one",
                        status="sent")
    session_input.queue("s1", request_id="b", text="two")
    session_input.queue("s1", request_id="c", text="three")

    rows = session_input.requests("s1")
    assert [(r["request_id"], r["status"]) for r in rows] == [
        ("a", "sent"), ("b", "queued"), ("c", "queued")]
    assert [r["request_id"] for r in session_input.pending("s1")] == ["b", "c"]


def test_only_a_queued_line_can_be_withdrawn(home):
    session_input.queue("s1", request_id="b", text="two")
    session_input.write("s1", "input_sent", request_id="a", text="one",
                        status="sent")

    assert session_input.cancel("s1", "b")["status"] == "cancelled"
    assert session_input.pending("s1") == []
    # twice, a sent line, and an unknown id all refuse without writing
    assert session_input.cancel("s1", "b") is None
    assert session_input.cancel("s1", "a") is None
    assert session_input.cancel("s1", "nope") is None
    assert _statuses("s1", "b") == ["queued", "cancelled"]


# ------------------------------------------------------------ end to end


def _setup(monkeypatch):
    store.update(
        lambda doc: doc.update(
            {"harnesses": {"py": {"command": [sys.executable, "-u", "-c", CHILD]}}}
        )
    )
    monkeypatch.setattr(session_mod, "FORCE_TYPING_GRACE", 0.1)
    monkeypatch.setattr(session_mod, "PASTE_ENTER_DELAY", 0.0)


async def _exit(mgr, name: str) -> None:
    session = mgr.get(name)
    await session.wait_for("idle", timeout=10.0, threshold=0.3)
    await session.send_keys(["quit", "Enter"])
    deadline = time.monotonic() + 10
    while not mgr.get(name).exited and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert mgr.get(name).exited


def test_an_exited_session_queues_the_line_and_a_respawn_types_it(
        home, tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    _setup(monkeypatch)

    async def run():
        mgr = SessionManager(idle_threshold=0.3, scrollback=200,
                             restore_default=True)
        app = build_app(mgr, "sekrit", started_at=time.monotonic())
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            mgr.create(SessionDef(name="s1", harness="py", cwd=str(tmp_path)))
            await _exit(mgr, "s1")

            first = {"keys": ["first line", "Enter"], "force": True,
                     "input_id": "req-1"}
            resp = await client.post("/api/sessions/s1/keys", json=first,
                                     headers=BEARER)
            assert resp.status == 202, await resp.text()
            doc = await resp.json()
            assert doc["queued"] is True and doc["position"] == 1

            second = {"paste": "second\nline", "enter": True, "force": True,
                      "input_id": "req-2"}
            resp = await client.post("/api/sessions/s1/keys", json=second,
                                     headers=BEARER)
            assert resp.status == 202
            assert (await resp.json())["position"] == 2

            third = {"keys": ["withdrawn", "Enter"], "force": True,
                     "input_id": "req-3"}
            resp = await client.post("/api/sessions/s1/keys", json=third,
                                     headers=BEARER)
            assert resp.status == 202

            # the same id again is the same line, not a second queued copy
            resp = await client.post("/api/sessions/s1/keys", json=first,
                                     headers=BEARER)
            assert resp.status == 200
            assert (await resp.json())["duplicate"] is True

            resp = await client.delete(
                "/api/sessions/s1/input-journal/req-3", headers=BEARER)
            assert resp.status == 200, await resp.text()

            resp = await client.get("/api/sessions/s1/input-journal",
                                    headers=BEARER)
            doc = await resp.json()
            assert doc["queued"] == 2
            assert [(r["request_id"], r["status"]) for r in doc["requests"]] == [
                ("req-1", "queued"), ("req-2", "queued"), ("req-3", "cancelled")]

            relaunched = mgr.respawn("s1")
            assert mgr._input_flush_tasks, "respawn did not schedule a flush"
            await asyncio.wait_for(
                asyncio.gather(*list(mgr._input_flush_tasks)), timeout=15)

            assert _statuses("s1", "req-1") == ["queued", "accepted", "sent"]
            assert _statuses("s1", "req-2") == ["queued", "accepted", "sent"]
            assert _statuses("s1", "req-3") == ["queued", "cancelled"]
            assert session_input.pending("s1") == []

            deadline = time.monotonic() + 10
            screen = ""
            while time.monotonic() < deadline:
                screen = "\n".join(relaunched.capture(history=True)
                                   + relaunched.capture())
                if "echo:first line" in screen and "echo:line" in screen:
                    break
                await asyncio.sleep(0.05)
            assert "echo:first line" in screen, screen
            assert "echo:withdrawn" not in screen, screen

            # a withdrawn or sent line cannot be withdrawn again
            resp = await client.delete(
                "/api/sessions/s1/input-journal/req-1", headers=BEARER)
            assert resp.status == 409
            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_an_exited_session_refuses_a_feedback_point_and_a_bare_keys_call(
        home, tmp_path, monkeypatch):
    """A queued line never carries a reward/penalty (it applies to input
    that landed), and a keys call with no ``input_id`` still fails: without
    a durable id nobody could see or withdraw the queued line."""
    from aiohttp.test_utils import TestClient, TestServer

    _setup(monkeypatch)

    async def run():
        mgr = SessionManager(idle_threshold=0.3, scrollback=200,
                             restore_default=True)
        app = build_app(mgr, "sekrit", started_at=time.monotonic())
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            mgr.create(SessionDef(name="s1", harness="py", cwd=str(tmp_path),
                                  score_goal=True))
            await _exit(mgr, "s1")

            resp = await client.post(
                "/api/sessions/s1/keys", headers=BEARER,
                json={"keys": ["x", "Enter"], "force": True,
                      "input_id": "r", "feedback": "reward"})
            assert resp.status == 409, await resp.text()

            resp = await client.post(
                "/api/sessions/s1/keys", headers=BEARER,
                json={"keys": ["x", "Enter"], "force": True})
            assert resp.status == 409, await resp.text()

            assert session_input.pending("s1") == []
            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())
