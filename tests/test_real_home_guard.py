"""A test that loses its throwaway home fails instead of writing the real one.

claunch-gziru: the real ~/.claude-launcher held profiles (play, work) and
daemon instances (daemons/a, alpha, b, inst, other, x) that only the suite
creates. Each came from a test whose isolation was undone -- a runner variable
the conftest did not clear, a local fixture shadowing conftest's ``home`` --
and each of those tests passed. ``tests/conftest.py`` now exports the real
homes as ``config.TEST_REAL_HOMES_ENV``; while it is set, resolving one of them
raises.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from claude_launcher import config
from claude_launcher.daemon import paths


def test_the_fixture_marks_the_default_home_and_the_user_home_as_real():
    real = os.environ[config.TEST_REAL_HOMES_ENV].split(os.pathsep)
    assert str(config._DEFAULT_HOME) in real
    assert str(Path.home()) in real


def test_the_isolated_home_and_config_still_resolve(home):
    assert config.launcher_home() == home
    assert config.sync_file() == home / ".claunch.yaml"
    assert paths.base_home() == home


def test_the_default_launcher_home_is_refused(monkeypatch):
    monkeypatch.delenv(config.LAUNCHER_HOME_ENV)
    with pytest.raises(config.RealHomeInTest, match="real launcher home"):
        config.launcher_home()


def test_a_home_variable_pointing_at_the_real_home_is_refused(monkeypatch):
    monkeypatch.setenv(config.LAUNCHER_HOME_ENV, str(config._DEFAULT_HOME))
    with pytest.raises(config.RealHomeInTest):
        config.profiles_dir()


def test_an_instance_base_at_the_real_home_is_refused(monkeypatch):
    # The claunch-llmi1 shape: CLAUNCH_INSTANCE_BASE wins over the launcher
    # home, so daemons/<name> was created under the real one.
    monkeypatch.setenv(paths.INSTANCE_BASE_ENV, str(config._DEFAULT_HOME))
    with pytest.raises(config.RealHomeInTest):
        paths.base_home()


def test_the_default_config_file_is_refused(monkeypatch):
    monkeypatch.delenv(config.LAUNCHER_HOME_ENV)
    with pytest.raises(config.RealHomeInTest):
        config.sync_file()


def test_the_old_config_variable_does_not_move_the_config_file(home, monkeypatch):
    # CLAUDE_LAUNCHER_SYNC_FILE is gone: isolating the launcher home is all it
    # takes, and a leftover variable cannot point the store at the real file.
    monkeypatch.setenv("CLAUDE_LAUNCHER_SYNC_FILE", str(Path.home() / ".claunch.yaml"))
    assert config.sync_file() == home / ".claunch.yaml"


def test_the_config_file_follows_the_launcher_home(home, tmp_path, monkeypatch):
    other = tmp_path / "other-home"
    monkeypatch.setenv(config.LAUNCHER_HOME_ENV, str(other))
    assert config.sync_file() == other / ".claunch.yaml"


def test_a_path_under_the_real_home_is_not_the_home(tmp_path, monkeypatch):
    # The suite's basetemp may sit under the real launcher home; a test's own
    # directory there is not the real home.
    monkeypatch.setenv(config.TEST_REAL_HOMES_ENV, str(tmp_path))
    monkeypatch.setenv(config.LAUNCHER_HOME_ENV, str(tmp_path / "basetemp" / "t0"))
    assert config.launcher_home() == tmp_path / "basetemp" / "t0"


def test_a_process_the_test_starts_inherits_the_guard(home):
    env = dict(os.environ)
    env.pop(config.LAUNCHER_HOME_ENV)
    proc = subprocess.run(
        [sys.executable, "-c",
         "from claude_launcher import config; config.launcher_home()"],
        env=env, capture_output=True, text=True,
    )
    assert proc.returncode != 0
    assert "RealHomeInTest" in proc.stderr


def test_outside_the_suite_nothing_is_refused(monkeypatch):
    monkeypatch.delenv(config.TEST_REAL_HOMES_ENV)
    monkeypatch.delenv(config.LAUNCHER_HOME_ENV)
    assert config.launcher_home() == config._DEFAULT_HOME
