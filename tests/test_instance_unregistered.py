"""``-L NAME`` does not create an instance; a relay's name is not an
instance name (claunch-19liw.2.2).

On 10-21 'claunch -L shared-daemon-main mesh members' started a new
instance under a relay's directory label, and that instance registered with
both relays under the main daemon's name.
"""

from __future__ import annotations

import pytest

from claude_launcher import cli, daemon_client, store
from claude_launcher.daemon import paths


@pytest.fixture
def spawns(monkeypatch):
    calls = []
    monkeypatch.setattr(daemon_client, "spawn_daemon",
                        lambda env=None, **kw: calls.append(kw))
    monkeypatch.setattr(daemon_client, "START_TIMEOUT", 0.2)
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    return calls


def _relays(monkeypatch, *names):
    monkeypatch.setattr(store, "relays_config",
                        lambda doc=None: [{"id": f"r{i}", "name": n}
                                          for i, n in enumerate(names)])


def test_an_unregistered_name_is_refused_without_a_spawn(spawns, monkeypatch, capsys):
    monkeypatch.setenv(paths.INSTANCE_ENV, "typo")
    with pytest.raises(daemon_client.DaemonClientError) as exc:
        daemon_client.ensure_running()
    msg = str(exc.value)
    assert "no daemon instance named 'typo'" in msg
    assert "claunch -L typo daemon start" in msg
    assert spawns == []
    assert not (paths.base_home() / "daemons" / "typo").exists()


def test_the_cli_reports_it_and_leaves_no_state(spawns, monkeypatch, capsys):
    monkeypatch.delenv(paths.INSTANCE_ENV, raising=False)
    assert cli.main(["-L", "typo", "mesh", "history", "home"]) == 1
    assert "no daemon instance named 'typo'" in capsys.readouterr().err
    assert spawns == []
    assert not (paths.base_home() / "daemons" / "typo").exists()


def test_a_session_shell_is_refused_too_and_queues_nothing(spawns, monkeypatch):
    monkeypatch.setenv("CLAUNCH_SESSION", "s-test")
    monkeypatch.setenv(paths.INSTANCE_ENV, "typo")
    with pytest.raises(daemon_client.DaemonClientError, match="no daemon instance"):
        daemon_client.ensure_running()
    assert not (paths.base_home() / "daemons" / "typo").exists()


def test_a_registered_instance_passes_the_check(spawns, monkeypatch):
    monkeypatch.setenv(paths.INSTANCE_ENV, "alt")
    (paths.base_home() / "daemons" / "alt").mkdir(parents=True)
    _relays(monkeypatch)
    with pytest.raises(daemon_client.DaemonClientError, match="did not come up"):
        daemon_client.ensure_running()
    assert len(spawns) == 1


def test_daemon_start_creates_an_unregistered_instance(spawns, monkeypatch):
    monkeypatch.setenv(paths.INSTANCE_ENV, "fresh")
    _relays(monkeypatch, "shared-daemon-main")
    with pytest.raises(daemon_client.DaemonClientError, match="did not come up"):
        daemon_client.connect_or_start()
    assert len(spawns) == 1


def test_daemon_start_refuses_a_relay_name(spawns, monkeypatch, capsys):
    monkeypatch.delenv(paths.INSTANCE_ENV, raising=False)
    _relays(monkeypatch, "shared-daemon-main", "")
    assert cli.main(["-L", "shared-daemon-main", "daemon", "start"]) == 1
    err = capsys.readouterr().err
    assert "is a relay name" in err and "shared-daemon-main" in err
    assert spawns == []
