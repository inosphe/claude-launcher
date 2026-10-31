"""Re-verification 3 findings (vm-setup-8s6): who may turn the network isolation
off, what the guest reaper matches, what is taken for a tart error, bounded
output."""
from __future__ import annotations

import argparse
import asyncio
import sys
import types

import pytest

from claude_launcher import cli_sessions, store
from claude_launcher.daemon import vm_channel, vm_frames, vm_tart
from claude_launcher.daemon.vmhub import Vm


# 1 ------------------------------------------------------------------------- #
def _config(key, value):
    return cli_sessions._cmd_daemon_config(argparse.Namespace(key=key, value=value))


@pytest.mark.parametrize("key,value", [("vm_softnet", "false"), ("vm_softnet_block", "1.2.3.4/32")])
def test_an_agent_session_cannot_turn_the_isolation_off(home, monkeypatch, capsys, key, value):
    monkeypatch.setenv("CLAUNCH_SESSION", "s9")
    before = store.daemon_config()[key]
    assert _config(key, value) == 1
    assert store.daemon_config()[key] == before
    assert "person's to set" in capsys.readouterr().err


def test_the_person_can_set_them_and_a_bad_block_is_refused(home, monkeypatch, capsys):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    assert _config("vm_softnet_block", "10.0.0.0/8,192.168.0.0/16") == 0
    assert store.daemon_config()["vm_softnet_block"] == "10.0.0.0/8,192.168.0.0/16"
    for bad in ("not-a-cidr", "10.0.0.0/8,oops", "", " "):
        assert _config("vm_softnet_block", bad) == 1, bad
    assert store.daemon_config()["vm_softnet_block"] == "10.0.0.0/8,192.168.0.0/16"


@pytest.mark.parametrize("soft,block,want", [
    (True, "10.0.0.0/8,172.16.0.0/12", (True, "10.0.0.0/8,172.16.0.0/12")),
    ("false", vm_tart.SOFTNET_BLOCK, (False, vm_tart.SOFTNET_BLOCK)),
    ("no", "", (False, vm_tart.SOFTNET_BLOCK)),
    ("true", "", (True, vm_tart.SOFTNET_BLOCK)),                    # empty is not "no block"
    (True, None, (True, vm_tart.SOFTNET_BLOCK)),
    (True, "oops", (True, vm_tart.SOFTNET_BLOCK)),
    (True, "10.0.0.0/8, 192.168.1.5", (True, "10.0.0.0/8,192.168.1.5/32")),
    (True, ["10.0.0.0/8", "172.16.0.0/12"], (True, "10.0.0.0/8,172.16.0.0/12")),
    (True, "['10.0.0.0/8']", (True, vm_tart.SOFTNET_BLOCK)),        # a list repr is not a list
    (None, "10.0.0.0/8", (True, "10.0.0.0/8")),                      # unclear means on
    ("banana", "10.0.0.0/8", (True, "10.0.0.0/8")),
])
def test_the_softnet_settings_fall_back_to_isolation(soft, block, want):
    assert vm_tart.parse_softnet(soft, block) == want


# 2, 3 ---------------------------------------------------------------------- #
def _vm(cua="/Users/admin/.local/bin/cua-driver"):
    return Vm(id="a", tart_name="agent-a", cua_path=cua)


def test_the_reaper_matches_the_exact_command_line_of_this_vms_driver(monkeypatch):
    seen = {}

    class Proc:
        returncode = 1

        async def wait(self):
            return 1

    async def fake_exec(*argv, **kw):
        seen["argv"] = list(argv)
        return Proc()
    monkeypatch.setattr(vm_channel.asyncio, "create_subprocess_exec", fake_exec)
    asyncio.run(vm_channel.reap_stale(_vm("/opt/cua/driver")))
    assert seen["argv"] == ["tart", "exec", "--", "agent-a", "/usr/bin/pkill", "-x", "-f",
                            "/opt/cua/driver mcp"]


def test_a_reaper_that_times_out_kills_its_tart_exec(monkeypatch):
    state = {"killed": False}

    class Proc:
        returncode = None

        async def wait(self):
            if state["killed"]:
                return -9
            await asyncio.sleep(60)

        def kill(self):
            state["killed"] = True
            self.returncode = -9

    async def fake_exec(*a, **k):
        return Proc()
    monkeypatch.setattr(vm_channel.asyncio, "create_subprocess_exec", fake_exec)
    asyncio.run(vm_channel.reap_stale(_vm(), timeout=0.05))
    assert state["killed"]


# 4 ------------------------------------------------------------------------- #
@pytest.mark.parametrize("rc,err,agent", [
    (2, 'VM "agent-a" is not running\n', True),
    (1, "Error: unavailable (14): Transport became inactive\n", True),
    (1, 'the specified VM "x" does not exist', True),
    (1, "Error: guest agent is not running", True),
    (1, "make: Error: transport layer failed\nmore output", False),   # the command's own words
    (1, "ls: x: No such file or directory", False),
    (1, "Error: unavailable (14): and then the command went on\nmore", False),
    (0, 'VM "a" is not running', False),                               # rc 0 is a success
    (1, "", False),
])
def test_only_tarts_own_line_is_taken_for_a_missing_agent(rc, err, agent):
    assert vm_tart.is_agent_error(rc, "", err) is agent


# 5 ------------------------------------------------------------------------- #
def test_command_output_is_bounded_while_it_is_read():
    code = "import sys; sys.stdout.write('x' * 3_000_000); sys.stderr.write('e' * 3_000_000)"
    rc, out, err = vm_tart.run_command([sys.executable, "-c", code], 30)
    assert rc == 0 and len(out) == vm_tart.READ_CAP == len(err)


def test_a_command_that_runs_past_its_time_is_killed():
    rc, out, err = vm_tart.run_command([sys.executable, "-c", "import time; time.sleep(30)"], 0.3)
    assert rc == 124 and "timed out" in err


# 6 ------------------------------------------------------------------------- #
def test_a_capture_removes_what_an_interrupted_one_left():
    assert vm_frames.CAPTURE.startswith("/bin/rm -f /tmp/claunch-frame-*.jpg;")
