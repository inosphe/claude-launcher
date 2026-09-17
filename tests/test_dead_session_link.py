"""Attaching to a session that had ALREADY finished.

Reported from the dashboard: ``#/s/s137`` on a killed session sat there
saying ``disconnected``, reconnecting, and redrawing itself every few
seconds. Nothing was wrong with the network and nothing was wrong with the
daemon — the loop was built out of three facts that are each individually
reasonable:

* the ``exit`` frame is published by a child *ending*, so a viewer that
  arrives after the child has already ended never receives one, and a client
  that learns "the program is over" only from that frame never learns it;
* the repaint a fresh socket is seeded with is the program's own last screen,
  mouse modes included — a claude session leaves ``?1003h`` (any-event
  tracking) on, so under it a mouse *movement* over the terminal is a report,
  and a report is a write;
* a write that finds no child ended the socket with a bare close, which is
  exactly what a dropped network looks like from the browser.

So: attach, move the mouse, socket closes, link calls it an outage,
reconnect, repaint, move the mouse. This file pins the daemon's half of the
cure — ``init`` says whether there is a program there at all, and a write
that finds none says why before the socket ends. The client's half (reading
that flag rather than waiting for a frame that cannot come) is pinned by
``tests/web/reconnect_check.js``.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

from claude_launcher import store
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.session import DeadSession

BEARER = {"Authorization": "Bearer sekrit"}

CHILD = (
    "import sys\n"
    "print('READY')\n"
    "for line in sys.stdin:\n"
    "    print('echo:' + line.strip())\n"
)


def _retired(mgr, name, cwd, *, exit_code=2, pid=32868):
    """Put an exited record in the registry, the way a restart does.

    ``SessionManager._retire`` builds exactly this for every definition the
    daemon did not relaunch, which is the state the reported session was in:
    the record is still there (it is still respawnable), the child is not.
    """
    dead = DeadSession(
        SessionDef(name=name, harness="py", cwd=str(cwd)),
        exit_code=exit_code,
        pid=pid,
    )
    mgr._sessions[name] = dead
    return dead


def _client(mgr):
    from aiohttp.test_utils import TestClient, TestServer

    app = build_app(mgr, "sekrit", started_at=time.monotonic())
    return TestClient(TestServer(app))


def test_init_says_the_program_is_already_over(home, tmp_path):
    """The only telling a late viewer can get.

    ``status`` has always said ``exited`` here, but a status is what the
    session *is*, not what this socket is: it changes while a viewer watches
    and the client mirrors it onto a badge. ``exited`` is the flag the link
    machine can act on, and ``exit_code`` is what the client would otherwise
    have to go and fetch to say the same sentence the ``exit`` frame says.
    """

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200,
                             restore_default=True)
        _retired(mgr, "s137", tmp_path)
        client = _client(mgr)
        await client.start_server()
        try:
            ws = await client.ws_connect("/api/sessions/s137/ws", headers=BEARER)
            init = json.loads((await ws.receive(timeout=10)).data)
            assert init["type"] == "init"
            assert init["status"] == "exited"
            assert init["exited"] is True
            assert init["exit_code"] == 2
            await ws.close()
        finally:
            await client.close()

    asyncio.run(run())


def test_a_live_session_is_not_flagged_as_over(home, tmp_path):
    """The other half of the same field: a running child must not trip it.

    A client that reads ``exited`` closes its send-keys strip, drops held
    keystrokes and stops answering the socket, so a false positive here is a
    terminal that silently refuses to type.
    """
    store.update(
        lambda doc: doc.update(
            {"harnesses": {"py": {"command": [sys.executable, "-u", "-c", CHILD]}}}
        )
    )

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200,
                             restore_default=True)
        client = _client(mgr)
        await client.start_server()
        try:
            mgr.create(SessionDef(name="s1", harness="py", cwd=str(tmp_path)))
            session = mgr.get("s1")
            await session.wait_for("idle", timeout=10.0, threshold=0.5)
            ws = await client.ws_connect("/api/sessions/s1/ws", headers=BEARER)
            init = json.loads((await ws.receive(timeout=10)).data)
            assert init["exited"] is False
            assert init["exit_code"] is None
            await ws.close()
            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_a_write_with_no_child_says_why_before_the_socket_ends(home, tmp_path):
    """The bare close was the second half of the loop.

    The binary frame below is not a person typing: it is the SGR mouse report
    xterm.js emits on its own account once the repaint has re-asserted
    ``?1003h``. The daemon cannot write it anywhere, and ending the socket on
    it is right — but ending it *silently* is what a browser reads as a
    dropped network, and a link machine answers a dropped network by
    reconnecting into the very same repaint. An ``exit`` frame first turns an
    outage the client would have retried into a fact it can act on.
    """

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200,
                             restore_default=True)
        _retired(mgr, "s137", tmp_path)
        client = _client(mgr)
        await client.start_server()
        try:
            ws = await client.ws_connect("/api/sessions/s137/ws", headers=BEARER)
            await ws.receive(timeout=10)   # init
            await ws.receive(timeout=10)   # repaint

            # ESC [ < 35 ; 10 ; 5 M — a motion report under SGR encoding.
            await ws.send_bytes(b"\x1b[<35;10;5M")

            frames = []
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                msg = await ws.receive(timeout=10)
                if msg.type.name == "TEXT":
                    frames.append(json.loads(msg.data))
                    if frames[-1].get("type") == "exit":
                        break
                elif msg.type.name in ("CLOSE", "CLOSED", "CLOSING", "ERROR"):
                    break
            assert any(f.get("type") == "exit" for f in frames), frames
            exit_frame = next(f for f in frames if f.get("type") == "exit")
            assert exit_frame["code"] == 2
            await ws.close()
        finally:
            await client.close()

    asyncio.run(run())


def test_the_final_screen_is_still_served_and_the_socket_is_not_dropped(
    home, tmp_path, caplog
):
    """Reading a dead session's last screen is a thing this route supports.

    ``DeadSession`` exists so a record can still be listed, captured and
    attached to; the repaint and the scroll controls answer for it exactly as
    they do for a live one. Pinned because the cure for the loop must not
    become "refuse the socket": the reader who opened ``#/s/s137`` wanted to
    see what it last said.
    """

    caplog.set_level("INFO", logger="claunch.daemon.ws")

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200,
                             restore_default=True)
        _retired(mgr, "s137", tmp_path)
        client = _client(mgr)
        await client.start_server()
        try:
            ws = await client.ws_connect(
                "/api/sessions/s137/ws?scrollback=1", headers=BEARER
            )
            assert json.loads((await ws.receive(timeout=10)).data)["type"] == "init"
            repaint = await ws.receive(timeout=10)
            assert repaint.type.name == "BINARY"

            # Text control frames are read-only questions and stay answerable.
            await ws.send_str(json.dumps({"type": "ping"}))
            assert json.loads((await ws.receive(timeout=10)).data)["type"] == "pong"
            await ws.send_str(json.dumps({"type": "repaint"}))
            assert json.loads((await ws.receive(timeout=10)).data)["type"] == "scrolled"
            assert (await ws.receive(timeout=10)).type.name == "BINARY"

            # And a resize it cannot honour is still not a reason to hang up.
            await ws.send_str(json.dumps({"type": "resize", "cols": 100, "rows": 30}))
            await ws.send_str(json.dumps({"type": "ping"}))
            assert json.loads((await ws.receive(timeout=10)).data)["type"] == "pong"
            assert not ws.closed
            await ws.close(code=4001, message=b"diagnostic close")
        finally:
            await client.close()

    asyncio.run(run())
    assert any(
        "terminal websocket closed session=s137 code=4001 error=None" in record.message
        for record in caplog.records
    )
