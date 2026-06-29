"""Shared fixtures: isolate every test in its own launcher home + config file."""

from __future__ import annotations

import pytest


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Point the launcher at a throwaway home, config file and (empty) seed.

    Returns the launcher home directory. ``CLAUDE_LAUNCHER_SEED`` is an empty
    directory so seeding copies nothing (tests never touch the real ~/.claude).
    """
    h = tmp_path / "home"
    h.mkdir()
    seed = tmp_path / "seed"
    seed.mkdir()
    monkeypatch.setenv("CLAUDE_LAUNCHER_HOME", str(h))
    monkeypatch.setenv("CLAUDE_LAUNCHER_SYNC_FILE", str(h / ".claunch.yaml"))
    monkeypatch.setenv("CLAUDE_LAUNCHER_SEED", str(seed))
    return h


@pytest.fixture
def config_file(home):
    """Path to the live config file (``~/.claunch.yaml`` equivalent)."""
    return home / ".claunch.yaml"
