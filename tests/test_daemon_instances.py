"""Named daemon instances (tmux ``-L`` style): path isolation and selection."""

from __future__ import annotations

import pytest

from claude_launcher import cli
from claude_launcher.daemon import paths, runtime_state


def test_default_instance_paths(home, monkeypatch):
    monkeypatch.delenv(paths.INSTANCE_ENV, raising=False)
    assert paths.instance() == ""
    assert paths.daemon_dir() == home / "daemon"
    assert paths.mesh_root() == home / "daemon" / "mesh"


def test_named_instance_paths(home, monkeypatch):
    monkeypatch.setenv(paths.INSTANCE_ENV, "alpha")
    assert paths.instance() == "alpha"
    assert paths.daemon_dir() == home / "daemons" / "alpha"
    assert paths.daemon_json() == home / "daemons" / "alpha" / "daemon.json"
    assert paths.mesh_root() == home / "daemons" / "alpha" / "mesh"
    # sibling instances never share a directory
    monkeypatch.setenv(paths.INSTANCE_ENV, "beta")
    assert paths.daemon_dir() == home / "daemons" / "beta"


@pytest.mark.parametrize(
    "bad",
    ["../evil", "a/b", "a\\b", ".hidden", "-lead", "spa ce", "x" * 65],
)
def test_invalid_instance_names_rejected(home, monkeypatch, bad):
    with pytest.raises(ValueError):
        paths.validate_instance(bad)
    monkeypatch.setenv(paths.INSTANCE_ENV, bad)
    with pytest.raises(ValueError):
        paths.instance()


def test_daemon_json_records_instance(home, monkeypatch):
    monkeypatch.setenv(paths.INSTANCE_ENV, "alpha")
    runtime_state.write_daemon_json("127.0.0.1", 12345)
    doc = runtime_state.read_daemon_json()
    assert doc["instance"] == "alpha"
    assert doc["port"] == 12345
    # the default instance sees nothing — separate discovery files
    monkeypatch.delenv(paths.INSTANCE_ENV)
    assert runtime_state.read_daemon_json() is None
    runtime_state.write_daemon_json("127.0.0.1", 1)
    assert runtime_state.read_daemon_json()["instance"] is None


def test_instance_locks_are_independent(home, monkeypatch):
    monkeypatch.delenv(paths.INSTANCE_ENV, raising=False)
    default_lock = runtime_state.SingletonLock()
    monkeypatch.setenv(paths.INSTANCE_ENV, "alpha")
    alpha_lock = runtime_state.SingletonLock()
    alpha_lock2 = runtime_state.SingletonLock()
    try:
        assert default_lock.acquire()
        assert alpha_lock.acquire()  # different instance -> different lock file
        assert not alpha_lock2.acquire()  # same instance -> excluded
    finally:
        alpha_lock.release()
        default_lock.release()


def test_known_instances_enumeration(home):
    assert paths.known_instances() == []
    (home / "daemon").mkdir()
    (home / "daemons" / "beta").mkdir(parents=True)
    (home / "daemons" / "alpha").mkdir()
    (home / "daemons" / ".not-an-instance").mkdir()  # invalid name: ignored
    assert paths.known_instances() == ["", "alpha", "beta"]


def test_restart_all_skips_stopped_instances(home, capsys):
    # state dirs exist but no server answers -> everything skipped, exit 0
    (home / "daemon").mkdir()
    (home / "daemons" / "alpha").mkdir(parents=True)
    from claude_launcher import cli

    assert cli.main(["daemon", "restart", "--all"]) == 0
    out = capsys.readouterr().out
    assert "default: not running -- skipped" in out
    assert "alpha: not running -- skipped" in out
    assert "0 daemon(s) restarted" in out


def test_cli_rejects_bad_instance_flag(home, monkeypatch, capsys):
    monkeypatch.setenv(paths.INSTANCE_ENV, "placeholder")  # so teardown restores
    assert cli.main(["-L", "bad/name", "daemon", "status"]) == 2
    assert "bad daemon instance name" in capsys.readouterr().err


def test_cli_instance_flag_sets_env(home, monkeypatch, capsys):
    import os

    monkeypatch.setenv(paths.INSTANCE_ENV, "placeholder")  # so teardown restores
    # daemon isn't running: status exits 1, but the env must be switched first
    assert cli.main(["-L", "alpha", "daemon", "status"]) == 1
    assert os.environ[paths.INSTANCE_ENV] == "alpha"
    assert "not running" in capsys.readouterr().out
