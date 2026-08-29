"""Shared fixtures: isolate every test in its own launcher home + config file."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from claude_launcher import test_window


_test_window_grant = None


def _pytest_window_class(args) -> str:
    """Classify pytest's collected scope before collection starts."""
    selected = [str(arg).replace("\\", "/").rstrip("/") for arg in args]
    if not selected or all(arg in (".", "tests") for arg in selected):
        return test_window.SWEEP
    return test_window.TARGETED


def pytest_sessionstart(session):
    """Guard every direct pytest entry point, including unwrapped commands."""
    global _test_window_grant
    if os.environ.get("PYTEST_XDIST_WORKER") or test_window.inherited_grant():
        return
    cls = _pytest_window_class(session.config.args)
    label = "pytest " + " ".join(str(arg) for arg in session.config.args)
    try:
        _test_window_grant = test_window.acquire(cls, label=label)
    except test_window.WindowUnavailable as exc:
        raise pytest.UsageError(str(exc)) from exc
    _test_window_grant.install_environment()


def _release_test_window() -> None:
    global _test_window_grant
    grant = _test_window_grant
    if grant is None:
        return
    _test_window_grant = None
    grant.release()
    if os.environ.get(test_window.WINDOW_GRANT_ENV) == grant.grant_id:
        os.environ.pop(test_window.WINDOW_GRANT_ENV, None)
        os.environ.pop(test_window.WINDOW_CLASS_ENV, None)
        os.environ.pop(test_window.WINDOW_WORKERS_ENV, None)


def pytest_sessionfinish(session, exitstatus):
    _release_test_window()


def pytest_unconfigure(config):
    # Sessionfinish is skipped by some early pytest failures. Release is
    # idempotent, so this is the process-exit backstop.
    _release_test_window()


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
