"""The gate that gives the worker's ``end-hold`` step a mechanical refusal.

``improv-worker``'s ending is now put to the session above it, and a refusal
routes to ``end-hold``. Refusing is not what keeps the session alive: the
daemon reaps a finished one-shot run's session on sight of ``done`` and the
only condition that stops it is ``session.sdef.keep_alive``. Until this tool
the step said "run ``claunch keep-alive``" in prose and nothing checked that
it happened -- a refused ending ended the session anyway.

What these pin is which answer comes back, because the three are acted on
differently:

* ``0`` the flag is set -- the refusal is in force
* ``1`` it is not -- the command was not run, and running it is the fix
* ``2`` the tool could not look -- a different fix entirely, and collapsing
  it into ``1`` would send the reader to run a command that was never the
  problem

The daemon is faked. What this tool does is read one field off one route, so
a real daemon would add a process and a port and pin nothing extra.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "tools" / "keepalive_check.py"


def _load():
    spec = importlib.util.spec_from_file_location("keepalive_check_under_test", GATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


keepalive_check = _load()


class _FakeClient:
    """One route, and a record of what was asked for."""

    def __init__(self, info, error=None):
        self.info = info
        self.error = error
        self.asked = []

    def get(self, path):
        self.asked.append(path)
        if self.error is not None:
            raise self.error
        return self.info


@pytest.fixture
def daemon(monkeypatch):
    """Install a fake daemon; the test hands back what the route returns."""

    box = {}

    def install(info, error=None, client=True):
        c = _FakeClient(info, error) if client else None
        box["client"] = c
        monkeypatch.setattr(keepalive_check.daemon_client, "connect", lambda: c)
        return c

    monkeypatch.setenv("CLAUNCH_SESSION", "w1")
    return install


def test_the_flag_being_set_is_the_only_pass(daemon, capsys):
    client = daemon({"name": "w1", "keep_alive": True})
    assert keepalive_check.main(["keepalive_check.py"]) == 0
    assert client.asked == ["/api/sessions/w1"]
    assert "SET" in capsys.readouterr().out


def test_an_unset_flag_fails_and_names_the_command_that_fixes_it(daemon, capsys):
    daemon({"name": "w1", "keep_alive": False})
    assert keepalive_check.main(["keepalive_check.py"]) == 1
    out = capsys.readouterr().out
    # The refusal is inert until this runs, so the message must carry it.
    assert "claunch keep-alive w1" in out


def test_a_session_named_on_the_command_line_wins_over_the_environment(daemon):
    client = daemon({"name": "other", "keep_alive": True})
    assert keepalive_check.main(["keepalive_check.py", "other"]) == 0
    assert client.asked == ["/api/sessions/other"]


def test_no_session_name_cannot_tell(daemon, monkeypatch, capsys):
    daemon({"keep_alive": True})
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    assert keepalive_check.main(["keepalive_check.py"]) == 2
    assert "cannot tell" in capsys.readouterr().out


def test_an_unreachable_daemon_cannot_tell(daemon, capsys):
    daemon(None, client=False)
    assert keepalive_check.main(["keepalive_check.py"]) == 2
    assert "cannot tell" in capsys.readouterr().out


def test_an_api_error_cannot_tell(daemon, capsys):
    daemon(
        None,
        error=keepalive_check.daemon_client.DaemonClientError("no session named 'w1'"),
    )
    assert keepalive_check.main(["keepalive_check.py"]) == 2
    out = capsys.readouterr().out
    assert "cannot tell" in out and "no session named" in out


def test_a_record_without_the_field_cannot_tell_rather_than_reading_false(daemon, capsys):
    """A missing key is not a false flag.

    ``keep_alive`` is part of the session record. If it stops being there the
    tool is looking at something else, and answering ``1`` would send a
    reader to run a command whose effect this tool can no longer see.
    """
    daemon({"name": "w1", "state": "running"})
    assert keepalive_check.main(["keepalive_check.py"]) == 2
    assert "cannot tell" in capsys.readouterr().out


def test_the_check_never_starts_a_daemon(monkeypatch):
    """``connect``, not ``ensure_running``.

    ``ensure_running`` spawns a daemon when none answers. A check that starts
    one would be answering a question nobody asked, and there is no session
    to keep alive in a daemon that was not running.
    """
    monkeypatch.setenv("CLAUNCH_SESSION", "w1")
    monkeypatch.setattr(keepalive_check.daemon_client, "connect", lambda: None)

    def boom(*a, **k):
        raise AssertionError("the check started a daemon")

    monkeypatch.setattr(keepalive_check.daemon_client, "ensure_running", boom)
    assert keepalive_check.main(["keepalive_check.py"]) == 2
