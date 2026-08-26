"""Shared fixtures: isolate every test in its own launcher home + config file."""

from __future__ import annotations

from pathlib import Path

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


@pytest.fixture(scope="session", autouse=True)
def repo_history_guard(pytestconfig):
    """Refuse, for the whole session, any read of THIS repository's history.

    Autouse and session-scoped for the same reason ``home`` is autouse: the
    thing it prevents is not a test failure, it is a *silent* one. Both
    ``tools/sweep.py`` and ``tools/changed_tests.py`` hand a green receipt
    recorded for one commit to another commit with the same tree, and that is
    only correct while nothing in this suite can tell the two apart. See
    ``tests/_repo_history_guard.py`` for what counts as telling them apart --
    naming an object by its hash does not, and one test does that on purpose.

    Yielded so a test can prove the patch is really installed, and so a test
    that genuinely needs the real ``Popen`` can call ``uninstall()``.
    """
    from _repo_history_guard import Guard

    guard = Guard(Path(pytestconfig.rootpath)).install()
    try:
        yield guard
    finally:
        guard.uninstall()
