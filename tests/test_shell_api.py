"""A session's own shell and its agent face (daemon/shell_api.py, shell_mcp.py).

Every session has one shell (``daemon/session_shells.py``), shown in that
session's ``cli`` split pane. The agent may read it and *propose* input; only
the user decides. These tests pin the boundary, not the terminal protocol
(tests/test_cli_pty.py does that):

  * a proposal never reaches the child, and the accept route is the user's
    alone;
  * a shell is addressed by its session and nothing else — two sessions'
    shells do not share proposals or cursors, and a proposal cannot be made
    for somebody else's shell;
  * the shell goes when its session ends;
  * the dashboard's CLI tab has no agent route at all (claunch-jg8pt rolled
    the sharing back to this shape).
"""

from __future__ import annotations

import asyncio
import sys
import time
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher import cli_shell, mcp_server, prompt_packs, shell_mcp
from claude_launcher.daemon import api as api_mod
from claude_launcher.daemon import clipty, session_shells, shell_api
from claude_launcher.daemon.manager import SessionManager

#: The same quiet REPL tests/test_cli_pty.py uses: -q drops the banner, -u
#: flushes eagerly, so ">>> " means the child is ready.
REPL = [sys.executable, "-i", "-u", "-q"]


class FakeManager:
    """What the registry and the routes read: names, and exit hooks."""

    def __init__(self, names):
        self.sessions = {
            name: SimpleNamespace(sdef=SimpleNamespace(name=name, cwd=None))
            for name in names
        }
        self.exit_hooks = []

    def get(self, name):
        if name not in self.sessions:
            raise KeyError(name)
        return self.sessions[name]


async def _wait_for(shell, needle: str, timeout: float = 20.0) -> dict:
    """Poll the read engine until ``needle`` is in the output."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = shell.read_output(None)
        if needle in last["text"]:
            return last
        await asyncio.sleep(0.05)
    raise AssertionError(f"{needle!r} never appeared; tail={last and last['text'][-500:]!r}")


def run(tmp_path, scenario, names=("s1", "s2")):
    """Serve the session shell routes over a REPL in place of the config's
    shell, and hand the scenario the request helper, the registry and the
    fast-spawned shells."""

    async def go():
        manager = FakeManager(list(names))
        app = web.Application()
        app["manager"] = manager
        registry = session_shells.install(app)
        # The config's shell would be the user's real one; a test wants the
        # quiet REPL, and the registry is the one seam that chooses.
        registry._factory = lambda name: clipty.SessionShell(  # noqa: SLF001
            argv=REPL, cwd=str(tmp_path)
        )
        shell_api.install(app)
        async with TestClient(TestServer(app)) as client:

            async def req(method, path, body=None, status=200):
                resp = await client.request(method, path, json=body)
                data = await resp.json()
                assert resp.status == status, data
                return data

            async def live(name):
                shell = registry.get_or_create(name)
                shell.start_once()
                await _wait_for(shell, ">>>")
                return shell

            try:
                await scenario(req, registry, live)
            finally:
                await registry.shutdown()

    asyncio.run(go())


def test_each_session_reads_its_own_shell(tmp_path):
    async def scenario(req, registry, live):
        one = await live("s1")
        two = await live("s2")

        status = await req("GET", "/api/sessions/s1/shell")
        assert status["id"] == "s1"
        assert status["exited"] is False
        assert status["offset"] == one.total > 0
        assert status["pending"] == 0

        await one.write_bytes(b"print(6 * 7)\r")
        await _wait_for(one, "42")

        # The same keystrokes straight into s2, a different line: neither
        # shell's reader is handed the other's output.
        await two.write_bytes(b"print('two')\r")
        await _wait_for(two, "two")

        mine = await req("GET", "/api/sessions/s1/shell/output?offset=0")
        theirs = await req("GET", "/api/sessions/s2/shell/output?offset=0")
        assert "42" in mine["text"] and "two" not in mine["text"]
        assert "two" in theirs["text"] and "42" not in theirs["text"]
        # ...and the cursors are per shell, not one daemon-wide counter.
        assert mine["total"] == one.total
        assert theirs["total"] == two.total

    run(tmp_path, scenario)


def test_a_proposal_waits_for_the_user_and_the_agent_cannot_accept_it(tmp_path):
    async def scenario(req, registry, live):
        shell = await live("s1")
        await live("s2")

        made = await req("POST", "/api/sessions/s1/shell/suggestions", {
            "session": "s1", "text": "print(40 + 2)", "why": "need the value",
        }, status=201)
        sid = made["suggestion"]["id"]
        assert made["suggestion"]["state"] == "pending"
        assert made["suggestion"]["session"] == "s1"

        # Nothing was typed: the REPL has not seen the line.
        assert "42" not in shell.read_output(None)["text"]

        # It is this session's queue only.
        other = await req("GET", "/api/sessions/s2/shell/suggestions?state=pending")
        assert other["suggestions"] == []

        # A session-shaped accept is refused even though the session exists.
        await req("POST", f"/api/sessions/s1/shell/suggestions/{sid}/accept",
                  {"session": "s1"}, status=403)
        assert "42" not in shell.read_output(None)["text"]

        # The user's accept (no session) types it and runs it.
        accepted = await req("POST", f"/api/sessions/s1/shell/suggestions/{sid}/accept", {})
        assert accepted["suggestion"]["state"] == "accepted"
        assert accepted["suggestion"]["decided_by"] == "user"
        await _wait_for(shell, "42")

        decided = await req("GET", "/api/sessions/s1/shell/suggestions?state=accepted")
        assert [r["id"] for r in decided["suggestions"]] == [sid]

    run(tmp_path, scenario)


def test_a_proposal_cannot_name_another_sessions_shell(tmp_path):
    """The path is the session; a body that disagrees with it is refused, so
    "propose into somebody else's shell" is not an operation that exists."""

    async def scenario(req, registry, live):
        await live("s1")
        shell2 = await live("s2")

        await req("POST", "/api/sessions/s1/shell/suggestions",
                  {"session": "s2", "text": "echo hijacked"}, status=403)
        await req("POST", "/api/sessions/s2/shell/suggestions",
                  {"text": "echo unnamed"}, status=403)

        # Neither shell got a row, and nothing was typed into s2's.
        assert (await req("GET", "/api/sessions/s1/shell/suggestions"))["suggestions"] == []
        assert (await req("GET", "/api/sessions/s2/shell/suggestions"))["suggestions"] == []
        assert "hijacked" not in shell2.read_output(None)["text"]

    run(tmp_path, scenario)


def test_a_suggestion_is_accepted_only_where_it_was_made(tmp_path):
    async def scenario(req, registry, live):
        shell1 = await live("s1")
        shell2 = await live("s2")
        made = (await req("POST", "/api/sessions/s1/shell/suggestions",
                          {"session": "s1", "text": "print('mine')",
                           "why": "s1"}, status=201))["suggestion"]

        # s2's shell has never heard of it: the id is not an address.
        await req("POST", f"/api/sessions/s2/shell/suggestions/{made['id']}/accept",
                  {}, status=404)
        await req("POST", f"/api/sessions/s2/shell/suggestions/{made['id']}/withdraw",
                  {"session": "s2"}, status=404)
        assert shell1.suggestions("pending")[0]["id"] == made["id"]
        assert "mine" not in shell1.read_output(None)["text"]
        assert "mine" not in shell2.read_output(None)["text"]

    run(tmp_path, scenario)


def test_withdraw_and_reject(tmp_path):
    async def scenario(req, registry, live):
        await live("s1")
        await live("s2")
        base = "/api/sessions/s1/shell/suggestions"
        one = (await req("POST", base, {"session": "s1", "text": "echo one"},
                         status=201))["suggestion"]
        # Another session may not take it back...
        await req("POST", f"{base}/{one['id']}/withdraw", {"session": "s2"}, status=403)
        # ...the proposer may.
        gone = await req("POST", f"{base}/{one['id']}/withdraw", {"session": "s1"})
        assert gone["suggestion"]["state"] == "withdrawn"

        two = (await req("POST", base, {"session": "s1", "text": "echo two"},
                         status=201))["suggestion"]
        rejected = await req("POST", f"{base}/{two['id']}/reject", {})
        assert rejected["suggestion"]["state"] == "rejected"
        # A decided proposal is not decided twice.
        await req("POST", f"{base}/{two['id']}/reject", {}, status=409)
        await req("POST", f"{base}/sh-nope/reject", {}, status=404)

    run(tmp_path, scenario)


def test_proposals_are_bounded_and_validated(tmp_path):
    async def scenario(req, registry, live):
        await live("s1")
        base = "/api/sessions/s1/shell/suggestions"
        await req("POST", base, {"text": "echo x"}, status=403)
        await req("POST", base, {"session": "s1", "text": "   "}, status=400)
        await req("POST", base, {"session": "s1", "text": "x" * 5000}, status=400)
        for i in range(10):
            await req("POST", base, {"session": "s1", "text": f"echo {i}"}, status=201)
        await req("POST", base, {"session": "s1", "text": "echo over"}, status=400)
        # The cap is per shell: another session's shell has its own room.
        two = "/api/sessions/s2/shell/suggestions"
        await live("s2")
        await req("POST", two, {"session": "s2", "text": "echo fine"}, status=201)

    run(tmp_path, scenario)


def test_accept_refused_once_the_shell_exited(tmp_path):
    async def scenario(req, registry, live):
        shell = await live("s1")
        made = (await req("POST", "/api/sessions/s1/shell/suggestions",
                          {"session": "s1", "text": "echo later"},
                          status=201))["suggestion"]
        await shell.shutdown()
        await req("POST", f"/api/sessions/s1/shell/suggestions/{made['id']}/accept",
                  {}, status=409)
        assert shell.suggestions("pending")[0]["id"] == made["id"]

    run(tmp_path, scenario)


def test_unknown_session_is_404_and_reading_does_not_start_a_shell(tmp_path):
    async def scenario(req, registry, live):
        await live("s1")
        await req("GET", "/api/sessions/nope/shell", status=404)
        await req("GET", "/api/sessions/nope/shell/output", status=404)
        await req("POST", "/api/sessions/nope/shell/suggestions",
                  {"session": "nope", "text": "echo x"}, status=404)
        # s2 exists but nothing has opened its pane: reading is a 404 rather
        # than a PTY spawned by a reader...
        await req("GET", "/api/sessions/s2/shell", status=404)
        await req("GET", "/api/sessions/s2/shell/output", status=404)
        # ...while a proposal creates the record (no child) so the agent can
        # queue one before anyone looks.
        made = await req("POST", "/api/sessions/s2/shell/suggestions",
                         {"session": "s2", "text": "echo x"}, status=201)
        assert made["suggestion"]["state"] == "pending"
        assert registry.get("s2").exited is True   # the record, not a child
        # The pane's viewer is what starts the child; the row is waiting.
        shell = await live("s2")
        assert shell.suggestions("pending")[0]["id"] == made["suggestion"]["id"]

    run(tmp_path, scenario)


def test_the_shell_goes_when_its_session_ends(tmp_path):
    async def scenario(req, registry, live):
        manager_shell = await live("s1")
        kept = await live("s2")

        # The manager's exit hook is what build_app appends; fire the one the
        # registry installed, the way the manager fires it on a real ending.
        hooks = registry._manager.exit_hooks  # noqa: SLF001
        assert registry.session_exited in hooks
        for hook in hooks:
            hook(SimpleNamespace(sdef=SimpleNamespace(name="s1")))

        assert registry.get("s1") is None
        assert registry.get("s2") is kept
        deadline = time.monotonic() + 10
        while not manager_shell.exited and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert manager_shell.exited is True
        assert kept.exited is False

    run(tmp_path, scenario)


def test_a_cursor_from_an_earlier_incarnation_is_flagged(tmp_path):
    """A reader that kept an offset across a shell restart is told the
    cursor began again instead of being handed a silent empty read."""

    async def go():
        shell = clipty.SessionShell(argv=REPL, cwd=str(tmp_path))
        shell.start_once()
        await _wait_for(shell, ">>>")
        got = shell.read_output(after=10 ** 9)
        assert got["reset"] is True
        assert got["offset"] == shell.total
        await shell.shutdown()

    asyncio.run(go())


def test_the_agent_surface_has_no_input_tool_and_names_no_session():
    names = {tool["name"] for tool in shell_mcp.SERVER.tools}
    assert names == {
        "shell_status", "shell_read", "shell_suggest", "shell_suggestions",
        "shell_withdraw",
    }
    assert not {"shell_accept", "shell_input", "shell_type", "shell_run"} & names
    merged = {tool["name"] for tool in mcp_server.TOOLS}
    assert names <= merged
    # The session is the caller's, never an argument: a tool that could name
    # another session could propose into its shell.
    for tool in shell_mcp.SERVER.tools:
        props = (tool.get("inputSchema") or {}).get("properties") or {}
        assert "session" not in props, tool["name"]


def test_the_tools_address_the_callers_own_session(monkeypatch):
    calls = []

    class FakeClient:
        def get(self, path):
            calls.append(("GET", path))
            return {}

        def post(self, path, body=None):
            calls.append(("POST", path, body))
            return {}

    monkeypatch.setattr(shell_mcp, "_client", lambda: FakeClient())
    monkeypatch.setenv("CLAUNCH_SESSION", "s1")
    shell_mcp.call_tool("shell_status", {})
    shell_mcp.call_tool("shell_read", {"offset": 10})
    shell_mcp.call_tool("shell_suggest", {"text": "ls", "why": "look"})
    shell_mcp.call_tool("shell_withdraw", {"suggestion": "sh-abc123"})

    assert calls[0] == ("GET", "/api/sessions/s1/shell")
    assert calls[1] == ("GET", "/api/sessions/s1/shell/output?offset=10")
    assert calls[2] == ("POST", "/api/sessions/s1/shell/suggestions",
                        {"session": "s1", "text": "ls", "why": "look"})
    assert calls[3] == ("POST", "/api/sessions/s1/shell/suggestions/sh-abc123/withdraw",
                        {"session": "s1"})


def test_the_cli_takes_a_session_but_a_proposal_acts_as_its_own(monkeypatch):
    """``--session`` is how a person looks at (and decides) another session's
    shell; the verbs that create a proposal stay with the caller's session."""
    monkeypatch.setenv("CLAUNCH_SESSION", "s1")
    ns = SimpleNamespace
    assert cli_shell._target(ns(session=None)) == "s1"
    assert cli_shell._target(ns(session="s2")) == "s2"
    # ...inspecting and deciding another session's shell is allowed...
    assert cli_shell._owner(ns(session="s1")) == "s1"
    # ...proposing into it is not.
    assert cli_shell._owner(ns(session="s2")) is None

    monkeypatch.delenv("CLAUNCH_SESSION")
    assert cli_shell._target(ns(session="s9")) == "s9"
    assert cli_shell._target(ns(session=None)) is None


def test_the_cli_tab_keeps_no_agent_route():
    """The rollback half of claunch-jg8pt: the dashboard's CLI tab is the
    user's own terminal, so the global /api/shell* surface is gone, and the
    session routes exist only under the session."""
    async def go():
        # The app builds its shells on a running loop (ShellPty binds one).
        app = api_mod.build_app(
            SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True),
            "sekrit", started_at=time.monotonic(),
        )
        # A resource route's address is on its resource, not on the route.
        paths = {route.resource.canonical for route in app.router.routes()
                 if getattr(route, "resource", None) is not None}
        assert not [p for p in paths if p.startswith("/api/shell")], paths
        assert "/api/sessions/{name}/shell/ws" in paths
        assert "/api/sessions/{name}/shell" in paths
        assert "/api/sessions/{name}/shell/output" in paths
        assert "/api/sessions/{name}/shell/suggestions" in paths

    asyncio.run(go())


def test_the_shell_pack_ships_on_demand():
    """The family's guidance is a loadable mcp- pack, not an opening pack:
    the merged instructions have a hard client budget and this family's
    pointer is one line in it."""
    pack = prompt_packs.shipped("mcp-shell")
    assert pack.title.startswith("MCP")
    assert "'mcp-shell'" in mcp_server.SERVER.instructions
    assert "mcp-shell" not in prompt_packs.shipped_default_packs()
    assert "mcp-shell" in prompt_packs.mcp_ids()
    assert prompt_packs.loadable_copy(None, "mcp-shell").body == pack.body
    assert "shell_suggest" in pack.body and "only when the user accepts" in pack.body
    assert "session" in pack.body


def test_suggestions_broadcast_to_attached_viewers(tmp_path):
    """A proposal is pushed to the session's pane without a poll, and only
    pending rows are sent."""

    async def run_attached():
        shell = clipty.SessionShell(argv=REPL, cwd=str(tmp_path))
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
