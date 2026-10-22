"""The shared user shell's agent face (daemon/shell_api.py, shell_mcp.py).

The shell is the CLI tab's persistent PTY: the user types into it in a
browser, and agent sessions may read its output and *propose* input. These
tests pin the boundary, not the terminal protocol (tests/test_cli_pty.py
does that): a proposal never reaches the child, the accept route is the
user's alone, and a reader gets a cursor rather than a copy.
"""

from __future__ import annotations

import asyncio
import sys
import time
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher import mcp_server, prompt_packs, shell_mcp
from claude_launcher.daemon import shell_api
from claude_launcher.daemon.clipty import ShellPty

#: The same quiet REPL tests/test_cli_pty.py uses: -q drops the banner, -u
#: flushes eagerly, so ">>> " means the child is ready.
REPL = [sys.executable, "-i", "-u", "-q"]


class FakeManager:
    def __init__(self, names):
        self.sessions = {
            name: SimpleNamespace(sdef=SimpleNamespace(name=name)) for name in names
        }

    def get(self, name):
        if name not in self.sessions:
            raise KeyError(name)
        return self.sessions[name]


async def _wait_for(shell, needle: str, timeout: float = 20.0) -> dict:
    """Poll the read route's engine until ``needle`` is in the output."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = shell.read_output(None)
        if needle in last["text"]:
            return last
        await asyncio.sleep(0.05)
    raise AssertionError(f"{needle!r} never appeared; tail={last and last['text'][-500:]!r}")


def run(tmp_path, scenario):
    async def go():
        shell = ShellPty(argv=REPL, cwd=str(tmp_path))
        app = web.Application()
        app["shell"] = shell
        app["manager"] = FakeManager(["s1", "s2"])
        shell_api.install(app)
        async with TestClient(TestServer(app)) as client:
            shell.start_once()

            async def req(method, path, body=None, status=200):
                resp = await client.request(method, path, json=body)
                data = await resp.json()
                assert resp.status == status, data
                return data

            try:
                await scenario(req, shell, client)
            finally:
                await shell.shutdown()

    asyncio.run(go())


def test_read_is_a_cursor_and_status_matches_it(tmp_path):
    async def scenario(req, shell, _client):
        await _wait_for(shell, ">>>")
        status = await req("GET", "/api/shell")
        assert status["id"] == "cli"
        assert status["exited"] is False
        assert status["offset"] == shell.total > 0
        assert status["pending"] == 0

        first = await req("GET", "/api/shell/output")
        assert ">>>" in first["text"]
        assert first["offset"] == status["offset"]
        assert first["dropped"] == 0

        # A user's keystroke in the terminal, then the agent's next read
        # starts exactly where the first one ended.
        await shell.write_bytes(b"print(6 * 7)\r")
        second = await req("GET", f"/api/shell/output?offset={first['offset']}")
        deadline = time.monotonic() + 20
        while "42" not in second["text"] and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
            second = await req("GET", f"/api/shell/output?offset={first['offset']}")
        assert "42" in second["text"]
        assert second["start"] >= first["offset"]
        assert second["offset"] > first["offset"]

    run(tmp_path, scenario)


def test_a_proposal_waits_for_the_user_and_the_agent_cannot_accept_it(tmp_path):
    async def scenario(req, shell, _client):
        await _wait_for(shell, ">>>")
        made = await req("POST", "/api/shell/suggestions", {
            "session": "s1", "text": "print(40 + 2)", "why": "need the value",
        }, status=201)
        sid = made["suggestion"]["id"]
        assert made["suggestion"]["state"] == "pending"
        assert made["suggestion"]["session"] == "s1"

        # Nothing was typed: the REPL has not seen the line.
        assert "42" not in shell.read_output(None)["text"]

        # A session-shaped accept is refused even though the session exists.
        await req("POST", f"/api/shell/suggestions/{sid}/accept",
                  {"session": "s1"}, status=403)
        assert "42" not in shell.read_output(None)["text"]

        # The user's accept (no session) types it and runs it.
        accepted = await req("POST", f"/api/shell/suggestions/{sid}/accept", {})
        assert accepted["suggestion"]["state"] == "accepted"
        assert accepted["suggestion"]["decided_by"] == "user"
        await _wait_for(shell, "42")

        decided = await req("GET", "/api/shell/suggestions?state=accepted")
        assert [r["id"] for r in decided["suggestions"]] == [sid]

    run(tmp_path, scenario)


def test_withdraw_and_reject(tmp_path):
    async def scenario(req, shell, _client):
        await _wait_for(shell, ">>>")
        one = (await req("POST", "/api/shell/suggestions",
                         {"session": "s1", "text": "echo one"}, status=201))["suggestion"]
        # Another session may not take it back...
        await req("POST", f"/api/shell/suggestions/{one['id']}/withdraw",
                  {"session": "s2"}, status=403)
        # ...the proposer may.
        gone = await req("POST", f"/api/shell/suggestions/{one['id']}/withdraw",
                         {"session": "s1"})
        assert gone["suggestion"]["state"] == "withdrawn"

        two = (await req("POST", "/api/shell/suggestions",
                         {"session": "s1", "text": "echo two"}, status=201))["suggestion"]
        rejected = await req("POST", f"/api/shell/suggestions/{two['id']}/reject", {})
        assert rejected["suggestion"]["state"] == "rejected"
        # A decided proposal is not decided twice.
        await req("POST", f"/api/shell/suggestions/{two['id']}/reject", {}, status=409)
        await req("POST", "/api/shell/suggestions/sh-nope/reject", {}, status=404)

    run(tmp_path, scenario)


def test_proposals_are_bounded_and_validated(tmp_path):
    async def scenario(req, shell, _client):
        await _wait_for(shell, ">>>")
        await req("POST", "/api/shell/suggestions", {"text": "echo x"}, status=403)
        await req("POST", "/api/shell/suggestions",
                  {"session": "nobody", "text": "echo x"}, status=404)
        await req("POST", "/api/shell/suggestions",
                  {"session": "s1", "text": "   "}, status=400)
        await req("POST", "/api/shell/suggestions",
                  {"session": "s1", "text": "x" * 5000}, status=400)
        for i in range(10):
            await req("POST", "/api/shell/suggestions",
                      {"session": "s1", "text": f"echo {i}"}, status=201)
        await req("POST", "/api/shell/suggestions",
                  {"session": "s1", "text": "echo over"}, status=400)
        # The queue is per session; another session still has its own room.
        await req("POST", "/api/shell/suggestions",
                  {"session": "s2", "text": "echo fine"}, status=201)

    run(tmp_path, scenario)


def test_accept_refused_once_the_shell_exited(tmp_path):
    async def scenario(req, shell, _client):
        await _wait_for(shell, ">>>")
        made = (await req("POST", "/api/shell/suggestions",
                          {"session": "s1", "text": "echo later"}, status=201))["suggestion"]
        await shell.shutdown()
        await req("POST", f"/api/shell/suggestions/{made['id']}/accept", {}, status=409)
        assert shell.suggestions("pending")[0]["id"] == made["id"]

    run(tmp_path, scenario)


def test_a_cursor_from_an_earlier_incarnation_is_flagged(tmp_path):
    """A reader that kept an offset across a shell restart is told the
    cursor began again instead of being handed a silent empty read."""

    async def go():
        shell = ShellPty(argv=REPL, cwd=str(tmp_path))
        shell.start_once()
        await _wait_for(shell, ">>>")
        got = shell.read_output(after=10 ** 9)
        assert got["reset"] is True
        assert got["offset"] == shell.total
        await shell.shutdown()

    asyncio.run(go())


def test_the_agent_surface_has_no_input_tool():
    names = {tool["name"] for tool in shell_mcp.SERVER.tools}
    assert names == {
        "shell_status", "shell_read", "shell_suggest", "shell_suggestions",
        "shell_withdraw",
    }
    assert not {"shell_accept", "shell_input", "shell_type", "shell_run"} & names
    merged = {tool["name"] for tool in mcp_server.TOOLS}
    assert names <= merged


def test_the_shell_pack_ships_on_demand():
    """The family's guidance is a loadable mcp- pack, not an opening pack:
    the merged instructions have a hard client budget and this family's
    pointer is one line in it."""
    pack = prompt_packs.shipped("mcp-shell")
    assert pack.title.startswith("MCP")
    assert "'mcp-shell'" in mcp_server.SERVER.instructions
    assert "mcp-shell" not in prompt_packs.DEFAULT_PACKS
    assert "mcp-shell" in prompt_packs.mcp_ids()
    assert prompt_packs.loadable_copy(None, "mcp-shell").body == pack.body
    assert "shell_suggest" in pack.body and "only when the user accepts" in pack.body


def test_suggestions_broadcast_to_attached_viewers(tmp_path):
    """A proposal is pushed to the CLI tab without a poll, and only pending
    rows are sent."""

    async def run_attached():
        shell = ShellPty(argv=REPL, cwd=str(tmp_path))
        shell.start_once()
        queue, _replay = shell.attach()
        row = shell.suggest("s1", "echo hi", "check")
        kind, payload = await asyncio.wait_for(queue.get(), 5)
        assert kind == "suggestions"
        assert [r["id"] for r in payload] == [row["id"]]
        shell.decide(row["id"], "rejected", "user")
        kind, payload = await asyncio.wait_for(queue.get(), 5)
        assert kind == "suggestions" and payload == []
        await shell.shutdown()

    asyncio.run(run_attached())
