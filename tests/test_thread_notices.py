"""Thread notices for a session that has exited (claunch-sgxfh).

A user message in a thread is typed into the owning session as a
``[claunch thread <id>]`` block. A held session is woken for it and a live
one is queued into its terminal; an exited one has neither, so the block is
recorded in a per-session journal (``daemon/thread_notices.py``) and typed
in when the session is launched again — a person's respawn or the daemon's
restore. The journal is the queue, as for the operator input journal, so the
notices survive a daemon restart with nothing else to persist.
"""

from __future__ import annotations

import asyncio
import sys
import time
from types import SimpleNamespace

from claude_launcher import store
from claude_launcher.daemon import session as session_mod
from claude_launcher.daemon import thread_notices, threads_api
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


# ---------------------------------------------------------------- journal


def test_the_journal_folds_into_one_row_per_notice(home):
    thread_notices.queue("s1", "th-1:m1", "first")
    thread_notices.queue("s1", "th-1:m2", "second")
    assert [r["notice"] for r in thread_notices.pending("s1")] == ["th-1:m1", "th-1:m2"]

    # the same thread message again is the same notice, not a second copy
    assert thread_notices.queue("s1", "th-1:m1", "first") is True
    assert [r["notice"] for r in thread_notices.pending("s1")] == ["th-1:m1", "th-1:m2"]

    # a notice without an id or text is nothing anyone could deliver
    assert thread_notices.queue("s1", "", "no id") is False
    assert thread_notices.queue("s1", "th-1:m3", "") is False


class FakeSession:
    """The half of a Session :func:`thread_notices.flush` uses."""

    def __init__(self, name):
        self.sdef = SimpleNamespace(name=name)
        self.exited = False
        self.typed = []
        self.dying_after = None    # exit once this many lines are typed

    async def deliver(self, text, *, wait_for_draft=False):
        self.typed.append(text)
        if self.dying_after is not None and len(self.typed) >= self.dying_after:
            self.exited = True
        return True


def test_flush_types_queued_notices_oldest_first_and_settles_them(home):
    thread_notices.queue("s1", "th-1:m1", "first")
    thread_notices.queue("s1", "th-1:m2", "second")

    session = FakeSession("s1")
    assert asyncio.run(thread_notices.flush(session)) == 2
    assert session.typed == ["first", "second"]
    assert [r["status"] for r in thread_notices.requests("s1")] == ["sent", "sent"]
    assert thread_notices.pending("s1") == []

    # a second launch types nothing a second time
    assert asyncio.run(thread_notices.flush(session)) == 0
    assert session.typed == ["first", "second"]


def test_an_exit_mid_flush_leaves_the_rest_queued(home):
    thread_notices.queue("s1", "th-1:m1", "first")
    thread_notices.queue("s1", "th-1:m2", "second")

    session = FakeSession("s1")
    session.dying_after = 1
    assert asyncio.run(thread_notices.flush(session)) == 1
    assert session.typed == ["first"]
    assert [(r["notice"], r["status"]) for r in thread_notices.requests("s1")] == [
        ("th-1:m1", "sent"), ("th-1:m2", "queued")]


# ------------------------------------------------------------- with a daemon


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


def test_a_post_to_an_exited_session_is_typed_in_after_a_respawn(
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

            resp = await client.post(
                "/api/threads",
                json={"to": "s1", "title": "Log format", "text": "why json?"},
                headers=BEARER)
            assert resp.status == 200, await resp.text()
            doc = await resp.json()
            tid = doc["thread"]["id"]
            assert doc["delivered"] is True and doc["hold"] == "exited"
            assert [r["text"] for r in thread_notices.pending("s1")]
            assert "why json?" in thread_notices.pending("s1")[0]["text"]

            relaunched = mgr.respawn("s1")
            assert threads_api._flush_tasks, "respawn did not schedule a flush"
            await asyncio.wait_for(
                asyncio.gather(*list(threads_api._flush_tasks)), timeout=15)
            assert [r["status"] for r in thread_notices.requests("s1")] == ["sent"]

            deadline = time.monotonic() + 10
            screen = ""
            while time.monotonic() < deadline:
                screen = "\n".join(relaunched.capture(history=True)
                                   + relaunched.capture())
                if "why json?" in screen:
                    break
                await asyncio.sleep(0.05)
            assert f"[claunch thread {tid}" in screen, screen
            assert "why json?" in screen, screen
            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())
