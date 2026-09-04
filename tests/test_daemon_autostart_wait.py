"""Auto-start waits for the daemon it spawned, not for a fixed number.

Regression for the 2026-09-04 false failure: eleven restorable sessions put
the daemon's "listening" 15.8s after its process start, and a 15s wait told
the operator it "did not come up" 0.8s before it did.
"""

from __future__ import annotations

import time

import pytest

from claude_launcher import daemon_client


class _FakeProc:
    def __init__(self, pid: int = 4242, exit_after: float | None = None, code: int = 0):
        self.pid = pid
        self.returncode = None
        self._born = time.monotonic()
        self._exit_after = exit_after
        self._code = code

    def poll(self):
        if self._exit_after is not None and time.monotonic() - self._born >= self._exit_after:
            self.returncode = self._code
        return self.returncode


def _serving_after(monkeypatch, seconds: float):
    """connect() answers None until ``seconds`` have passed, then a client."""
    born = time.monotonic()
    client = object()
    monkeypatch.setattr(
        daemon_client,
        "connect",
        lambda: client if time.monotonic() - born >= seconds else None,
    )
    return client


@pytest.fixture
def short_clocks(monkeypatch):
    monkeypatch.setattr(daemon_client, "START_TIMEOUT", 0.3)
    monkeypatch.setattr(daemon_client, "START_ALIVE_TIMEOUT", 3.0)


def test_a_live_daemon_that_needs_longer_than_the_fixed_wait_is_waited_for(
    short_clocks, monkeypatch, capsys
):
    client = _serving_after(monkeypatch, 0.8)  # past START_TIMEOUT, inside ALIVE
    monkeypatch.setattr(daemon_client, "spawn_daemon", lambda env=None: _FakeProc())

    assert daemon_client.ensure_running() is client
    err = capsys.readouterr().err
    assert "still starting" in err and "pid 4242" in err


def test_a_daemon_that_exited_with_an_error_is_reported_as_such_not_as_slow(
    short_clocks, monkeypatch
):
    monkeypatch.setattr(daemon_client, "connect", lambda: None)
    monkeypatch.setattr(
        daemon_client, "spawn_daemon", lambda env=None: _FakeProc(exit_after=0.05, code=1)
    )
    t0 = time.monotonic()
    with pytest.raises(daemon_client.DaemonClientError) as exc:
        daemon_client.ensure_running()
    assert "exited with code 1" in str(exc.value)
    # the fixed clock, not the alive one: nothing is alive to wait for
    assert time.monotonic() - t0 < 2.0


def test_a_lock_loser_exiting_zero_still_gives_the_winner_the_fixed_wait(
    short_clocks, monkeypatch
):
    """A racing double start: our spawn loses the lock and exits 0 while the
    winner is coming up. Its exit is not a failure and the wait goes on --
    for START_TIMEOUT, since there is no live process of ours to follow."""
    client = _serving_after(monkeypatch, 0.2)
    monkeypatch.setattr(
        daemon_client, "spawn_daemon", lambda env=None: _FakeProc(exit_after=0.0, code=0)
    )
    assert daemon_client.ensure_running() is client


def test_an_unwatchable_spawn_keeps_the_fixed_wait(short_clocks, monkeypatch):
    """Spawns the tests monkeypatch return None; that path is the old one."""
    monkeypatch.setattr(daemon_client, "connect", lambda: None)
    monkeypatch.setattr(daemon_client, "spawn_daemon", lambda env=None: None)
    t0 = time.monotonic()
    with pytest.raises(daemon_client.DaemonClientError) as exc:
        daemon_client.ensure_running()
    assert "did not come up" in str(exc.value)
    assert time.monotonic() - t0 < 2.0


def test_a_live_daemon_that_never_listens_fails_at_the_alive_cap(
    short_clocks, monkeypatch
):
    monkeypatch.setattr(daemon_client, "START_ALIVE_TIMEOUT", 0.7)
    monkeypatch.setattr(daemon_client, "connect", lambda: None)
    monkeypatch.setattr(daemon_client, "spawn_daemon", lambda env=None: _FakeProc())
    with pytest.raises(daemon_client.DaemonClientError) as exc:
        daemon_client.ensure_running()
    assert "did not come up" in str(exc.value)
