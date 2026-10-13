"""claunch-xixag: a claude session with MCP servers gets its opening after
they are connected, not as an argv prompt that goes out first with no tools."""

from __future__ import annotations

import asyncio
import json

import pytest

from claude_launcher import config, profile
from claude_launcher.daemon import harness, mcp_ready, onboard
from claude_launcher.daemon.harness import SessionDef

PLANE = {"gds6-plane": {"type": "stdio", "command": "plane", "args": []}}


def _sdef(tmp_path, *args):
    if not (config.profiles_dir() / "work").exists():
        profile.create("work")
    return harness.normalize(
        SessionDef(name="x", profile="work", cwd=str(tmp_path), args=tuple(args))
    )


def _config_file(tmp_path, servers):
    path = tmp_path / "job-mcp.json"
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    return str(path)


def test_declared_servers_read_the_flag_file_inline_json_and_project_file(tmp_path):
    cfg = _config_file(tmp_path, PLANE)
    inline = json.dumps({"mcpServers": {"inline": {"command": "i"}}})
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"claunch": {"command": "claunch"}}}), encoding="utf-8"
    )
    found = mcp_ready.declared_servers(
        ["--mcp-config", cfg, inline, "--model=opus"], str(tmp_path), None
    )
    assert set(found) == {"gds6-plane", "inline", "claunch"}
    strict = mcp_ready.declared_servers(
        ["--mcp-config", cfg, "--strict-mcp-config"], str(tmp_path), None
    )
    assert set(strict) == {"gds6-plane"}


def test_user_scope_servers_count(tmp_path):
    user = tmp_path / "u.json"
    user.write_text(json.dumps({"mcpServers": {"claunch": {"command": "c"}}}), encoding="utf-8")
    assert set(mcp_ready.declared_servers([], str(tmp_path), user)) == {"claunch"}


def test_plan_waits_for_the_signal_and_for_servers_without_one():
    assert not mcp_ready.plan_for_servers({}).waits
    only = mcp_ready.plan_for_servers({"claunch": {}})
    assert only.signal and only.settle == 0
    plane = mcp_ready.plan_for_servers(PLANE)
    assert plane.waits and not plane.signal and plane.settle == mcp_ready.SETTLE
    both = mcp_ready.plan_for_servers({**PLANE, "claunch": {}})
    assert both.signal and both.settle == mcp_ready.SETTLE


def test_a_job_mcp_config_keeps_the_opening_off_argv(tmp_path):
    """The gds6 shape: --mcp-config on the session's args."""
    sdef = _sdef(tmp_path, "--mcp-config", _config_file(tmp_path, PLANE))
    argv, _, _ = harness.build_command(sdef, opening="take the API")
    assert not any("take the API" in a for a in argv)
    assert not harness.carries_opening(argv, "take the API")


def test_without_mcp_servers_the_opening_stays_on_argv(tmp_path):
    argv, _, _ = harness.build_command(_sdef(tmp_path), opening="take the API")
    assert harness.carries_opening(argv, "take the API")


class FakeSession:
    def __init__(self, sdef, started):
        self.sdef = sdef
        self.argv = ["claude"]
        self._started_mono = started
        self.exited = False
        self.events = []
        self._ev = asyncio.Event()

    def mcp_ready_event(self):
        return self._ev

    async def deliver(self, block, **kw):
        self.events.append(("deliver", block))
        return True


def test_open_with_delivers_only_after_the_claunch_announcement(tmp_path, monkeypatch):
    import time

    sdef = _sdef(tmp_path)
    user = profile.require("work").config_dir / ".claude.json"
    user.write_text(json.dumps({"mcpServers": {"claunch": {"command": "c"}}}), encoding="utf-8")

    async def scenario():
        s = FakeSession(sdef, time.monotonic())
        onboard.open_with(s, "hello")
        await asyncio.sleep(0.2)
        assert s.events == []  # claude is up, claunch mcp has not announced
        s.mcp_ready_event().set()
        await asyncio.sleep(0.2)
        assert s.events == [("deliver", "hello")]

    asyncio.run(scenario())


def test_open_with_gives_a_job_server_its_settle_time(tmp_path, monkeypatch):
    import time

    monkeypatch.setattr(mcp_ready, "SETTLE", 0.4)
    sdef = _sdef(tmp_path, "--mcp-config", _config_file(tmp_path, PLANE))

    async def scenario():
        s = FakeSession(sdef, time.monotonic())
        onboard.open_with(s, "hello")
        await asyncio.sleep(0.15)
        assert s.events == []
        await asyncio.sleep(0.5)
        assert s.events == [("deliver", "hello")]

    asyncio.run(scenario())


def test_the_wait_is_bounded(tmp_path):
    import time

    plan = mcp_ready.plan_for_servers({"claunch": {}})

    async def scenario():
        s = FakeSession(_sdef(tmp_path), time.monotonic())
        assert await mcp_ready.wait(s, plan, cap=0.1) is False

    asyncio.run(scenario())


def test_no_servers_means_no_wait(tmp_path):
    import time

    async def scenario():
        s = FakeSession(_sdef(tmp_path), time.monotonic())
        onboard.open_with(s, "hello")
        await asyncio.sleep(0.05)
        assert s.events == [("deliver", "hello")]

    asyncio.run(scenario())


def test_initialized_notification_announces_the_session(monkeypatch):
    from claude_launcher import mcp_rpc

    called = []
    monkeypatch.setattr(mcp_ready, "announce", lambda: called.append(1))
    server = mcp_rpc.Server(name="t", tools=(), dispatch=lambda n, a: {}, errors=())
    assert server.handle({"method": "notifications/initialized"}) is None
    assert called == [1]
