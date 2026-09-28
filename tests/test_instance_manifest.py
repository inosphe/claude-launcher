"""Per-instance manifests (daemon/instance_manifest.py): a named daemon
instance declares its own launcher home, config file and port once, and
``claunch -L NAME`` applies them."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from claude_launcher import cli, cli_sessions, config
from claude_launcher.daemon import instance_manifest as im
from claude_launcher.daemon import paths


@pytest.fixture(autouse=True)
def clean_env(home, monkeypatch):
    # apply() writes os.environ directly; registering every variable it may
    # touch makes monkeypatch put each one back after the test. The home and
    # config variables the `home` fixture points at a temp dir are re-set to
    # the same value, never deleted: deleting them sends the test to the
    # developer's real ~/.claude-launcher.
    for var in (paths.INSTANCE_ENV, *im.ENV_VARS):
        if var in (config.LAUNCHER_HOME_ENV, config.LAUNCHER_SYNC_ENV):
            monkeypatch.setenv(var, os.environ[var])
        else:
            monkeypatch.delenv(var, raising=False)
    assert config.launcher_home() == home


def _manifest(base: Path, name: str, doc: dict) -> Path:
    p = base / "daemons" / name / "instance.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return p


def test_no_manifest_changes_nothing(home, monkeypatch):
    monkeypatch.setenv(paths.INSTANCE_ENV, "team")
    assert im.apply() is None
    assert config.launcher_home() == home
    assert im.APPLIED_ENV not in os.environ


def test_manifest_moves_home_config_and_port(home, tmp_path, monkeypatch):
    team_home = tmp_path / "team-home"
    team_cfg = tmp_path / "team.yaml"
    _manifest(home, "team", {"home": str(team_home), "config": str(team_cfg), "port": 8390})
    monkeypatch.setenv(paths.INSTANCE_ENV, "team")

    m = im.apply()

    assert m is not None and m.name == "team"
    assert config.launcher_home() == team_home
    assert config.profiles_dir() == team_home / "profiles"
    assert config.sync_file() == team_cfg
    assert os.environ["CLAUNCH_DAEMON_PORT"] == "8390"
    # The instance's state stays next to its manifest, under the base home.
    assert paths.base_home() == home
    assert paths.daemon_dir() == home / "daemons" / "team"
    assert paths.known_instances() == ["team"]


def test_omitted_keys_stay_shared(home, monkeypatch):
    _manifest(home, "team", {"port": 8391})
    monkeypatch.setenv(paths.INSTANCE_ENV, "team")
    im.apply()
    assert config.launcher_home() == home
    assert config.sync_file() == home / ".claunch.yaml"
    assert os.environ["CLAUNCH_DAEMON_PORT"] == "8391"


def test_apply_is_idempotent_within_a_process_tree(home, tmp_path, monkeypatch):
    _manifest(home, "team", {"home": str(tmp_path / "t")})
    monkeypatch.setenv(paths.INSTANCE_ENV, "team")
    assert im.apply() is not None
    # A daemon or session the CLI started inherits the env and must not
    # re-resolve the manifest against the moved home.
    assert im.apply() is None
    assert config.launcher_home() == tmp_path / "t"


def test_switching_instance_undoes_the_previous_manifest(home, tmp_path, monkeypatch):
    _manifest(home, "team", {"home": str(tmp_path / "t"), "config": str(tmp_path / "t.yaml")})
    monkeypatch.setenv(paths.INSTANCE_ENV, "team")
    im.apply()

    monkeypatch.setenv(paths.INSTANCE_ENV, "other")  # no manifest
    assert im.apply() is None
    assert config.launcher_home() == home
    assert config.sync_file() == home / ".claunch.yaml"
    assert im.APPLIED_ENV not in os.environ

    monkeypatch.setenv(paths.INSTANCE_ENV, "team")
    im.apply()
    monkeypatch.delenv(paths.INSTANCE_ENV)  # back to the default instance
    im.apply()
    assert config.launcher_home() == home
    assert paths.daemon_dir() == home / "daemon"


def test_manifest_wins_over_a_preset_home(home, tmp_path, monkeypatch):
    # CLAUDE_LAUNCHER_HOME is set (the fixture does it, as a user with a
    # custom base home would); it is the base, and the manifest still applies.
    assert os.environ[config.LAUNCHER_HOME_ENV] == str(home)
    _manifest(home, "team", {"home": str(tmp_path / "t")})
    monkeypatch.setenv(paths.INSTANCE_ENV, "team")
    im.apply()
    assert config.launcher_home() == tmp_path / "t"


def test_two_instances_on_one_home_are_refused(home, tmp_path, monkeypatch):
    shared = tmp_path / "same"
    _manifest(home, "a", {"home": str(shared)})
    _manifest(home, "b", {"home": str(shared)})
    monkeypatch.setenv(paths.INSTANCE_ENV, "b")
    with pytest.raises(im.InstanceManifestError, match="already the home of instance 'a'"):
        im.apply()
    assert config.launcher_home() == home


@pytest.mark.parametrize("doc, match", [
    ({"hmoe": "/x"}, "unknown key"),
    ({"home": "relative/dir"}, "absolute path"),
    ({"port": 0}, "1-65535"),
    ({"port": "8390"}, "1-65535"),
    (["home"], "expected a mapping"),
])
def test_bad_manifest_is_refused(home, monkeypatch, doc, match):
    _manifest(home, "team", doc)
    monkeypatch.setenv(paths.INSTANCE_ENV, "team")
    with pytest.raises(im.InstanceManifestError, match=match):
        im.apply()


def test_cli_refuses_to_run_on_a_bad_manifest(home, capsys):
    _manifest(home, "team", {"home": "relative"})
    assert cli.main(["-L", "team", "daemon", "instance", "ls"]) == 2
    assert "absolute path" in capsys.readouterr().err


def test_cli_create_show_ls_and_apply(home, tmp_path, capsys):
    (home / ".claunch.yaml").write_text(yaml.safe_dump({
        "version": 1,
        "profiles": {"work": {"env": {"A": "1"}}},
        "sync": {"url": "https://sync.example", "namespace": "me"},
    }), encoding="utf-8")
    team_home = tmp_path / "team-home"
    team_cfg = tmp_path / "team.yaml"

    rc = cli.main(["daemon", "instance", "create", "team", "--home", str(team_home),
                   "--config", str(team_cfg), "--port", "8392", "--seed"])
    out = capsys.readouterr().out
    assert rc == 0, out
    manifest = yaml.safe_load((home / "daemons" / "team" / "instance.yaml").read_text())
    assert manifest == {"home": str(team_home), "config": str(team_cfg), "port": 8392}
    seeded = yaml.safe_load(team_cfg.read_text(encoding="utf-8"))
    assert seeded["profiles"] == {"work": {"env": {"A": "1"}}}
    assert "sync" not in seeded
    assert team_home.is_dir()

    # A second create does not overwrite the manifest by accident.
    assert cli.main(["daemon", "instance", "create", "team"]) == 1
    capsys.readouterr()

    assert cli.main(["daemon", "instance", "ls"]) == 0
    assert f"team: home={team_home}" in capsys.readouterr().out

    # With -L, the command itself runs under the instance's home and config.
    assert cli.main(["-L", "team", "daemon", "instance", "show"]) == 0
    out = capsys.readouterr().out
    assert "instance 'team'" in out and "8392" in out
    assert config.launcher_home() == team_home
    assert config.sync_file() == team_cfg


def test_seed_needs_config_and_never_overwrites(home, tmp_path, capsys):
    assert cli.main(["daemon", "instance", "create", "x", "--seed"]) == 2
    target = tmp_path / "exists.yaml"
    target.write_text("version: 1\n", encoding="utf-8")
    assert cli.main(["daemon", "instance", "create", "x", "--config", str(target), "--seed"]) == 0
    assert "not seeded" in capsys.readouterr().out
    assert target.read_text(encoding="utf-8") == "version: 1\n"


def test_create_refuses_a_home_another_instance_owns(home, tmp_path, capsys):
    shared = tmp_path / "same"
    assert cli.main(["daemon", "instance", "create", "a", "--home", str(shared)]) == 0
    assert cli.main(["daemon", "instance", "create", "b", "--home", str(shared)]) == 2
    assert "already the home of instance 'a'" in capsys.readouterr().err
    assert not (home / "daemons" / "b" / "instance.yaml").exists()


def test_show_without_manifest(home, capsys):
    assert cli.main(["daemon", "instance", "show", "solo"]) == 0
    assert "has no manifest" in capsys.readouterr().out


def test_restart_all_applies_each_instance_and_restores_env(home, tmp_path, monkeypatch):
    (home / "daemon").mkdir()
    _manifest(home, "team", {"home": str(tmp_path / "t"), "port": 8393})
    (home / "daemons" / "plain").mkdir(parents=True)
    seen = []

    def fake_connect():
        seen.append((paths.instance(), str(config.launcher_home()),
                     os.environ.get("CLAUNCH_DAEMON_PORT")))
        return None  # "not running": nothing is stopped or started

    monkeypatch.setattr(cli_sessions.daemon_client, "connect", fake_connect)
    before = {v: os.environ.get(v) for v in (paths.INSTANCE_ENV, *im.ENV_VARS)}

    assert cli_sessions._restart_all_instances() == 0

    assert seen == [
        ("", str(home), None),
        ("plain", str(home), None),
        ("team", str(tmp_path / "t"), "8393"),
    ]
    assert {v: os.environ.get(v) for v in before} == before


def test_real_daemon_process_runs_under_its_manifest(home, tmp_path):
    """A daemon started as ``--name team`` (no CLI in front of it) binds the
    manifest's port, keeps its state under the base home, and serves the
    profiles of the manifest's home -- not the base home's."""
    import json
    import socket
    import subprocess
    import sys
    import time

    import claude_launcher
    from claude_launcher.daemon_client import DaemonClient

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    team_home = tmp_path / "team-home"
    (team_home / "profiles" / "teamonly").mkdir(parents=True)
    (home / "profiles" / "baseonly").mkdir(parents=True)
    team_cfg = tmp_path / "team.yaml"
    team_cfg.write_text("version: 1\n", encoding="utf-8")
    _manifest(home, "team", {"home": str(team_home), "config": str(team_cfg), "port": port})

    env = os.environ.copy()
    for var in (paths.INSTANCE_ENV, *im.ENV_VARS):
        if var not in (config.LAUNCHER_HOME_ENV, config.LAUNCHER_SYNC_ENV):
            env.pop(var, None)
    src = str(Path(claude_launcher.__file__).resolve().parents[1])
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    log_path = tmp_path / "daemon.out"
    with open(log_path, "wb") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "claude_launcher.daemon", "--name", "team"],
            env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
        )
    inst_dir = home / "daemons" / "team"
    try:
        deadline = time.monotonic() + 30
        while not (inst_dir / "daemon.json").is_file():
            assert proc.poll() is None, log_path.read_text(errors="replace")
            assert time.monotonic() < deadline, "daemon did not come up"
            time.sleep(0.2)
        doc = json.loads((inst_dir / "daemon.json").read_text(encoding="utf-8"))
        assert doc["port"] == port
        assert not (team_home / "daemons").exists()
        token = (inst_dir / "token").read_text(encoding="utf-8").strip()
        client = DaemonClient(f"http://127.0.0.1:{port}", token)
        body = client.get("/api/profiles")
        names = set(body["profiles"])
        assert "teamonly" in names and "baseonly" not in names
        client.post("/api/daemon/shutdown", timeout=5.0)
        proc.wait(timeout=20)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
