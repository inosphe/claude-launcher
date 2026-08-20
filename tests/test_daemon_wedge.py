"""Telling a wedged daemon apart from an absent one, and getting past it.

A daemon whose event loop stops turning keeps everything that makes it look
alive -- its pid, its port, its ``daemon.json`` -- and, decisively, its
singleton lock. The client used to report that as "not running", which sends
the operator to start a replacement that then stands down against a lock the
"absent" daemon is still holding; the way out was for a human to find the pid
themselves. These tests pin the diagnosis that ends that, and the one
recovery it enables.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from claude_launcher import cli, daemon_client
from claude_launcher.daemon import runtime_state


# --------------------------------------------------------------------------- #
# liveness, without the probe that kills what it asks about
# --------------------------------------------------------------------------- #
def test_this_process_is_alive():
    assert daemon_client.process_alive(os.getpid()) is True


def test_a_finished_process_reads_as_gone():
    assert daemon_client.process_alive(_dead_pid()) is False


def test_a_liveness_probe_does_not_kill_its_subject():
    """On Windows ``os.kill(pid, 0)`` is TerminateProcess -- the POSIX habit
    would end the daemon it was merely asking about."""
    child = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()"],
        stdin=subprocess.PIPE,
    )
    try:
        for _ in range(3):
            assert daemon_client.process_alive(child.pid) is True
        assert child.poll() is None  # still there after being asked about
    finally:
        child.kill()
        child.wait(timeout=10)


def test_a_nonsense_pid_is_unknown_rather_than_alive():
    assert daemon_client.process_alive(0) is None
    assert daemon_client.process_alive(-1) is None


# --------------------------------------------------------------------------- #
# diagnosis
# --------------------------------------------------------------------------- #
def test_nothing_announced_is_not_running(home):
    report = daemon_client.diagnose()
    assert report["state"] == daemon_client.NOT_RUNNING
    assert report["pid"] is None


def test_an_announcement_whose_process_is_gone_is_a_stale_record(home, monkeypatch):
    gone = _dead_pid()
    _announce(monkeypatch, pid=gone)
    report = daemon_client.diagnose()
    assert report["state"] == daemon_client.STALE_RECORD
    assert str(gone) in report["why"]


def test_a_live_pid_that_answers_nothing_is_wedged(home, monkeypatch):
    """The whole point: alive + announced + silent is its own state, and the
    report says which pid to act on."""
    _announce(monkeypatch, pid=os.getpid())
    report = daemon_client.diagnose()
    assert report["state"] == daemon_client.WEDGED
    assert report["pid"] == os.getpid()
    assert "health check" in report["why"]


def test_a_daemon_that_answers_is_serving(home, monkeypatch):
    _announce(monkeypatch, pid=os.getpid())
    monkeypatch.setattr(daemon_client, "_health_ok", lambda url: True)
    assert daemon_client.diagnose()["state"] == daemon_client.SERVING


def test_wedged_needs_every_probe_to_fail(home, monkeypatch):
    """One missed check on a loaded machine means busy, not wedged -- and the
    difference decides whether something gets killed."""
    _announce(monkeypatch, pid=os.getpid())
    answers = iter([False, True])
    monkeypatch.setattr(daemon_client, "_health_ok", lambda url: next(answers))
    report = daemon_client.diagnose(probes=2, gap=0.0)
    assert report["state"] == daemon_client.SERVING


# --------------------------------------------------------------------------- #
# the CLI: an honest status, and a force that is not automatic
# --------------------------------------------------------------------------- #
def test_status_reports_wedged_instead_of_not_running(home, monkeypatch, capsys):
    _announce(monkeypatch, pid=os.getpid())
    assert cli.main(["daemon", "status"]) == 1
    err = capsys.readouterr().err
    assert "WEDGED" in err
    assert "--force" in err          # and what to do about it
    assert "not running" not in err  # the lie this replaces


def test_restart_refuses_to_flail_against_a_wedged_daemon(home, monkeypatch, capsys):
    """Without this the operator gets 'did not come up within 15s', which
    reads as a broken install rather than a daemon to replace."""
    _announce(monkeypatch, pid=os.getpid())
    monkeypatch.setattr(
        daemon_client, "spawn_daemon",
        lambda *a, **k: pytest.fail("a replacement cannot start against the lock"),
    )
    assert cli.main(["daemon", "restart"]) == 1
    assert "WEDGED" in capsys.readouterr().err


def test_force_ends_the_wedged_process_then_starts_a_successor(
    home, monkeypatch, capsys
):
    ended, started = [], []
    _announce(monkeypatch, pid=4242)
    monkeypatch.setattr(daemon_client, "process_alive", lambda pid: True)
    monkeypatch.setattr(
        daemon_client, "terminate_process",
        lambda pid, **kw: ended.append(pid) or True,
    )
    monkeypatch.setattr(
        daemon_client, "ensure_running",
        lambda **kw: started.append(True) or daemon_client.DaemonClient("http://x", "t"),
    )
    assert cli.main(["daemon", "restart", "--force"]) == 0
    assert ended == [4242] and started == [True]
    out = capsys.readouterr().out
    assert "wedged" in out and "sessions went with it" in out


def test_force_on_a_healthy_daemon_restarts_it_gracefully_instead(
    home, monkeypatch, capsys
):
    """--force names a situation, not a bigger hammer: a daemon that answers
    is stopped the polite way even when force was asked for."""
    _announce(monkeypatch, pid=os.getpid())
    monkeypatch.setattr(daemon_client, "_health_ok", lambda url: True)
    monkeypatch.setattr(
        daemon_client, "terminate_process",
        lambda pid, **kw: pytest.fail("a serving daemon must not be killed"),
    )
    monkeypatch.setattr(daemon_client, "stop", lambda **kw: True)
    monkeypatch.setattr(
        daemon_client, "ensure_running",
        lambda **kw: daemon_client.DaemonClient("http://x", "t"),
    )
    assert cli.main(["daemon", "restart", "--force"]) == 0
    assert "no force needed" in capsys.readouterr().err


def test_force_with_nothing_running_just_starts_one(home, monkeypatch, capsys):
    monkeypatch.setattr(
        daemon_client, "terminate_process",
        lambda pid, **kw: pytest.fail("there is nothing to end"),
    )
    monkeypatch.setattr(
        daemon_client, "ensure_running",
        lambda **kw: daemon_client.DaemonClient("http://x", "t"),
    )
    assert cli.main(["daemon", "restart", "--force"]) == 0
    assert "daemon started" in capsys.readouterr().out


def _dead_pid() -> int:
    """A pid that certainly belonged to a process and certainly does not now."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=30)
    return proc.pid


def _announce(monkeypatch, *, pid: int) -> None:
    """Write a daemon.json naming ``pid`` at a port nothing listens on."""
    monkeypatch.setattr(runtime_state, "read_daemon_json", lambda: {
        "pid": pid, "host": "127.0.0.1", "port": 59999, "version": "test",
    })
    monkeypatch.setattr(
        daemon_client.runtime_state, "read_daemon_json",
        lambda: {"pid": pid, "host": "127.0.0.1", "port": 59999, "version": "test"},
    )
