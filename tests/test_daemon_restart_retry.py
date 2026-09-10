"""``daemon_client.restart()``: retrying the successor spawn across a
predecessor's slow shutdown, and failing clearly when it never lets go.

claunch-a5l9: a restart's external stop+start flow (``cli_sessions.
_cmd_daemon``'s ordinary and ``--force`` branches) called ``stop()`` (which
gives up waiting for the predecessor's lock after 10s regardless of whether
it actually let go — see its own docstring) and then ``ensure_running()``
exactly once. The spawned successor's own grace window
(``daemon/__main__.py``'s ``_acquire_with_grace``, 15s) can lose the same
race for the same reason, and by design exits quietly (code 0) — correct for
a genuine double-start against a daemon that is actually serving, wrong here.
Measured: shutdown requested 16:10:05, successor gave up 16:10:31 — 26s,
almost exactly ``stop()``'s 10s plus one 15s grace window — while the
predecessor was still draining and the daemon stayed down for 13 minutes
until a person restarted it by hand.

These tests pin ``restart()``'s replacement for that one-shot pair: retry
the spawn, with backoff, for as long as something other than an answering
daemon still holds the lock, and raise a clear, attempt-counted error once
exhausted.
"""

from __future__ import annotations

import pytest

from claude_launcher import daemon_client
from claude_launcher.daemon import runtime_state


class _FakeTime:
    """Stands in for the ``time`` module inside ``daemon_client`` so the
    backoff sleeps are recorded instead of actually waited out."""

    def __init__(self) -> None:
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


def _still_draining(monkeypatch) -> _FakeTime:
    """Common setup: a stop that reports accepted, no real waiting, and a
    lock that stays held (the predecessor has not exited yet)."""
    monkeypatch.setattr(daemon_client, "stop", lambda timeout=10.0: True)
    monkeypatch.setattr(runtime_state, "lock_is_free", lambda: False)
    fake_time = _FakeTime()
    monkeypatch.setattr(daemon_client, "time", fake_time)
    return fake_time


def test_restart_retries_until_the_predecessor_finally_lets_go(home, monkeypatch):
    """Two lost lock races, then the predecessor is gone: the third spawn
    attempt succeeds and ``restart()`` returns its client."""
    fake_time = _still_draining(monkeypatch)
    sentinel = object()
    attempts = {"n": 0}

    def fake_ensure_running():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise daemon_client.DaemonClientError(
                "daemon did not come up within 15s (see daemon.log)"
            )
        return sentinel

    monkeypatch.setattr(daemon_client, "ensure_running", fake_ensure_running)

    assert daemon_client.restart() is sentinel
    assert attempts["n"] == 3
    # backoff grew between the two failed attempts, never hammering the lock
    assert fake_time.sleeps == [
        daemon_client.RESTART_BACKOFF_START,
        daemon_client.RESTART_BACKOFF_START * 2,
    ]


def test_restart_fails_clearly_once_every_attempt_is_exhausted(home, monkeypatch):
    """A predecessor that never releases the lock exhausts every retry, and
    the failure names the attempt count and the last thing that went wrong
    — the only place left to say so, since no daemon survives to carry a
    restart notice."""
    _still_draining(monkeypatch)

    def always_fails():
        raise daemon_client.DaemonClientError(
            "daemon did not come up within 15s (see daemon.log)"
        )

    monkeypatch.setattr(daemon_client, "ensure_running", always_fails)

    with pytest.raises(daemon_client.DaemonClientError) as excinfo:
        daemon_client.restart()
    message = str(excinfo.value)
    assert f"{daemon_client.RESTART_SPAWN_ATTEMPTS} attempt" in message
    assert "did not come up" in message  # the underlying failure is carried through


def test_restart_stops_early_once_the_lock_frees_but_spawning_still_fails(
    home, monkeypatch
):
    """Once the lock is free, a further failure is not the predecessor's
    fault (a crash, a missing interpreter, ...) — retrying blindly would not
    fix it, so ``restart()`` gives up after the first attempt instead of
    spending the whole backoff budget on a problem it cannot solve."""
    monkeypatch.setattr(daemon_client, "stop", lambda timeout=10.0: True)
    monkeypatch.setattr(runtime_state, "lock_is_free", lambda: True)
    monkeypatch.setattr(daemon_client, "time", _FakeTime())

    calls = {"n": 0}

    def always_fails():
        calls["n"] += 1
        raise daemon_client.DaemonClientError(
            "daemon exited with code 1 before coming up (see daemon.log)"
        )

    monkeypatch.setattr(daemon_client, "ensure_running", always_fails)

    with pytest.raises(daemon_client.DaemonClientError):
        daemon_client.restart()
    assert calls["n"] == 1


def test_restart_succeeds_on_the_first_try_when_nothing_is_racing(home, monkeypatch):
    """The common case: the predecessor is already gone, so the first spawn
    just works and nothing backs off or retries."""
    fake_time = _still_draining(monkeypatch)
    sentinel = object()
    monkeypatch.setattr(daemon_client, "ensure_running", lambda: sentinel)

    assert daemon_client.restart() is sentinel
    assert fake_time.sleeps == []
