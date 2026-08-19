"""Shared fixtures: isolate every test in its own launcher home + config file."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """Point the launcher at a throwaway home, config file and (empty) seed.

    Returns the launcher home directory. ``CLAUDE_LAUNCHER_SEED`` is an empty
    directory so seeding copies nothing (tests never touch the real ~/.claude).

    Autouse, because forgetting it is not a test failure — it is a write into
    the developer's real ``~/.claude-launcher``. ``claunch install --global``
    seeds the global workflow layer there, so a test that installs without
    this fixture quietly edits the machine it is running on, and then passes.
    ``CLAUDE_CONFIG_DIR`` is pointed away for the same reason: the global
    install writes skills and the user-scope ``.claude.json`` under it.
    """
    h = tmp_path / ".home"
    h.mkdir()
    # Dotted names, because this now runs for every test: a fixture that
    # claims `tmp_path/home` or `tmp_path/seed` collides with the tests that
    # build directories of those names themselves.
    seed = tmp_path / ".seed"
    seed.mkdir()
    monkeypatch.setenv("CLAUDE_LAUNCHER_HOME", str(h))
    monkeypatch.setenv("CLAUDE_LAUNCHER_SYNC_FILE", str(h / ".claunch.yaml"))
    monkeypatch.setenv("CLAUDE_LAUNCHER_SEED", str(seed))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude-config"))
    return h


@pytest.fixture
def config_file(home):
    """Path to the live config file (``~/.claunch.yaml`` equivalent)."""
    return home / ".claunch.yaml"
