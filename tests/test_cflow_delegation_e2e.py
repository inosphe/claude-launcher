"""End-to-end: one session's workflow decided by another, over a real daemon.

Everything the unit tests stub is real here — an HTTP daemon on a socket, a
mesh with two PTY-backed members, the roster read that turns `{role: leader}`
into a session name, and the message that lands in the responder's terminal.
The point is the seam: `cflow/responders.py` talks to the daemon the same way
an agent's MCP server does, so a change on either side that stops agreeing
shows up here rather than in production.

The engine calls run on worker threads because they issue *synchronous* HTTP
requests to the daemon serving them — which is exactly the production shape
(the daemon's own clock does this, see `daemon/cflow_clock.py`), and why a
call from inside the loop's own thread would deadlock.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

from claude_launcher import store
from claude_launcher.daemon import runtime_state
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager

TOKEN = "delegation-e2e"

CHILD = (
    "import sys\n"
    "print('READY')\n"
    "for line in sys.stdin:\n"
    "    print('echo:' + line.strip())\n"
)

WORKFLOW = """
name: shipit
steps:
  impl:
    instructions: implement the thing
    next: ship
  ship:
    ask:
      prompt: the diff is green — approve the push?
      from: [{role: leader}]
      on_decline: impl
    instructions: push it
"""


def _register_py_harness():
    store.update(
        lambda doc: doc.update(
            {"harnesses": {"py": {"command": [sys.executable, "-u", "-c", CHILD]}}}
        )
    )


async def _wait_for(check, what: str, timeout: float = 15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        got = check()
        if got:
            return got
        await asyncio.sleep(0.1)
    raise AssertionError(f"{what} never happened")


def test_a_leader_approves_a_workers_run(home, tmp_path, monkeypatch):
    _register_py_harness()
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    from aiohttp.test_utils import TestServer

    from claude_launcher.cflow import engine as cflow_engine, mcp as cflow_mcp
    from claude_launcher.cflow import state as cflow_state

    proj = tmp_path / "proj"
    (proj / ".claunch" / "workflows").mkdir(parents=True)
    (proj / ".claunch" / "workflows" / "shipit.yaml").write_text(
        WORKFLOW, encoding="utf-8"
    )
    cwd = str(Path(proj).resolve())

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        app = build_app(mgr, TOKEN, started_at=time.monotonic())
        server = TestServer(app)
        await server.start_server()
        # Publish the daemon the way the real one does, so daemon_client's
        # own discovery finds it. Nothing in cflow is told where to look.
        runtime_state.write_daemon_json("127.0.0.1", server.port)
        runtime_state.paths.token_file().parent.mkdir(parents=True, exist_ok=True)
        runtime_state.paths.token_file().write_text(TOKEN, encoding="utf-8")
        try:
            mesh_mgr = app["mesh"]
            # `build_app` builds the mesh manager; the daemon's main() starts
            # its delivery workers. Do both, or nothing is ever typed.
            mesh_mgr.settle = 0.05
            mesh_mgr.start()
            leader = mgr.create(SessionDef(name="lead1", harness="py", cwd=cwd))
            # `parent` is what makes this a spawn edge, and what the join
            # wires: dev1 starts connected to lead1 and to nobody else.
            mgr.create(
                SessionDef(name="dev1", harness="py", cwd=cwd, parent="lead1")
            )
            mesh_mgr.create("team")
            await mesh_mgr.join("team", "lead1", handle="lead1", role="leader")
            await mesh_mgr.join("team", "dev1", handle="dev1", role="worker")

            # --- the run, driven as dev1 -------------------------------- #
            payload = await asyncio.to_thread(
                cflow_engine.start, "shipit", cwd=cwd, scope="dev1"
            )
            check = payload["delegation_check"]
            assert check["steps"][0]["resolves"] == ["lead1"]

            await asyncio.to_thread(
                cflow_engine.report, "implemented", cwd=cwd, scope="dev1"
            )
            payload = await asyncio.to_thread(
                cflow_engine.next_step, cwd=cwd, scope="dev1"
            )
            assert payload["status"] == "waiting_answer"
            assert "instructions" not in payload  # the step is withheld
            asked = payload["ask"]["asked"]
            assert [e["session"] for e in asked] == ["lead1"]
            ask_id = payload["ask"]["id"]

            # --- the doorbell ------------------------------------------- #
            history = mesh_mgr.get("team").messages
            msg = await _wait_for(
                lambda: next((m for m in history if m["type"] == "decide"), None),
                "a decide message",
            )
            assert msg["from"] == "dev1" and msg["to"] == ["lead1"]
            assert msg["ref"] == {"kind": "cflow.ask", "id": ask_id, "step": "ship"}
            assert "approve the push?" in msg["body"]

            # it reaches the leader's terminal...
            await _wait_for(
                lambda: "approve the push?" in "\n".join(leader.capture()),
                "the question on lead1's screen",
            )
            # ...and having been delivered, it still does not sit in the owed
            # ledger, where nothing could ever close it: the answer is not a
            # reply to this thread, so a debt here would only ever be closed
            # by an unrelated message and reported as handled.
            assert mesh_mgr.get("team").owed("lead1") == []

            # --- who may answer ----------------------------------------- #
            def answer_as(session, decision):
                monkeypatch.setenv(cflow_state.SESSION_ENV, session)
                cflow_mcp._seen_run = None
                return cflow_mcp.call_tool(
                    "answer", {"ask": ask_id, "decision": decision}
                )

            # Through the tool, neither the asking run nor a bystander can
            # even find it: `answer` locates the run by scanning for asks put
            # to the calling session, and no ask was put to either of them.
            for impostor in ("dev1", "nobody"):
                with pytest.raises(
                    cflow_engine.CflowError, match="no open request"
                ):
                    await asyncio.to_thread(answer_as, impostor, "approve")

            # And naming the run outright — which the tool gives no way to do
            # — still fails, on the check the whole feature rests on.
            with pytest.raises(cflow_engine.CflowError, match="cannot approve itself"):
                await asyncio.to_thread(
                    lambda: cflow_engine.answer(
                        ask_id, "approve", by_session="dev1", cwd=cwd, scope="dev1"
                    )
                )
            with pytest.raises(cflow_engine.CflowError, match="was not asked this"):
                await asyncio.to_thread(
                    lambda: cflow_engine.answer(
                        ask_id, "approve", by_session="nobody", cwd=cwd, scope="dev1"
                    )
                )

            # the leader sees it without being told where the run lives
            monkeypatch.setenv(cflow_state.SESSION_ENV, "lead1")
            cflow_mcp._seen_run = None
            waiting = await asyncio.to_thread(cflow_mcp.call_tool, "asks", {})
            assert [e["ask"] for e in waiting["waiting_on_you"]] == [ask_id]
            assert waiting["waiting_on_you"][0]["from_session"] == "dev1"

            receipt = await asyncio.to_thread(answer_as, "lead1", "approve")
            assert receipt["status"] == "answered"
            assert "instructions" not in receipt  # never the asking step's work

            # --- and the run moves -------------------------------------- #
            payload = await asyncio.to_thread(
                cflow_engine.next_step, cwd=cwd, scope="dev1"
            )
            assert payload["status"] == "step"
            assert payload["instructions"].strip() == "push it"
        finally:
            monkeypatch.delenv(cflow_state.SESSION_ENV, raising=False)
            await mesh_mgr.shutdown()
            await mgr.shutdown_all()
            await server.close()

    asyncio.run(run())


def test_a_child_cannot_be_conjured_into_approving_its_parent(
    home, tmp_path, monkeypatch
):
    """The invariant over the real graph: dev1 spawns a 'leader' and gains
    nothing. It is wired to it, and it still is not a candidate."""
    _register_py_harness()
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    from aiohttp.test_utils import TestServer

    from claude_launcher.cflow import engine as cflow_engine, state as cflow_state

    proj = tmp_path / "proj"
    (proj / ".claunch" / "workflows").mkdir(parents=True)
    (proj / ".claunch" / "workflows" / "shipit.yaml").write_text(
        WORKFLOW, encoding="utf-8"
    )
    cwd = str(Path(proj).resolve())

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        app = build_app(mgr, TOKEN, started_at=time.monotonic())
        server = TestServer(app)
        await server.start_server()
        runtime_state.write_daemon_json("127.0.0.1", server.port)
        runtime_state.paths.token_file().parent.mkdir(parents=True, exist_ok=True)
        runtime_state.paths.token_file().write_text(TOKEN, encoding="utf-8")
        try:
            mesh_mgr = app["mesh"]
            mgr.create(SessionDef(name="dev1", harness="py", cwd=cwd))
            mgr.create(
                SessionDef(name="tame1", harness="py", cwd=cwd, parent="dev1")
            )
            mesh_mgr.create("team")
            await mesh_mgr.join("team", "dev1", handle="dev1", role="worker")
            # dev1's own child, admitted under the role the workflow asks for
            await mesh_mgr.join("team", "tame1", handle="tame1", role="leader")
            # the join wired them to each other — reach is not the boundary
            assert mesh_mgr.get("team").connected("dev1", "tame1")

            payload = await asyncio.to_thread(
                cflow_engine.start, "shipit", cwd=cwd, scope="dev1"
            )
            assert payload["delegation_check"]["steps"][0]["resolves"] == []

            await asyncio.to_thread(
                cflow_engine.report, "implemented", cwd=cwd, scope="dev1"
            )
            payload = await asyncio.to_thread(
                cflow_engine.next_step, cwd=cwd, scope="dev1"
            )
            # it fell to a human, not to the session dev1 made for the purpose
            assert payload["status"] == "waiting_approval"
            assert payload["ask"]["asked"] == []
            assert "spawned itself are never candidates" in (
                payload["ask"]["skipped"][0]["reason"]
            )
            with pytest.raises(cflow_engine.CflowError, match="was not asked this"):
                await asyncio.to_thread(
                    cflow_engine.answer,
                    payload["ask"]["id"], "approve",
                    by_session="tame1", cwd=cwd, scope="dev1",
                )
            # nothing was delivered either: there was nobody to deliver to
            assert not [
                m for m in mesh_mgr.get("team").messages if m["type"] == "decide"
            ]
        finally:
            monkeypatch.delenv(cflow_state.SESSION_ENV, raising=False)
            await mgr.shutdown_all()
            await server.close()

    asyncio.run(run())
