"""``claude_launcher.pytest_basetemp``: the root every gate tool hands pytest.

The tools' own use of it is pinned next to them (tests/test_changed_tests.py,
tests/test_sweep.py). These pin the two branches, the configured override,
and the one side effect.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from claude_launcher import config, pytest_basetemp, store

#: The real implementation, captured before conftest's autouse ``home``
#: fixture redirects this function away from the machine-wide root: these
#: two tests pin the naming itself, on a configured tmp root.
_REAL_SESSION_TEST_DIR = pytest_basetemp.session_test_dir


def test_windows_keeps_its_short_root(monkeypatch):
    """The string Windows tools have always used; its length is budgeted
    against MAX_PATH in the tools' tests."""
    monkeypatch.setattr(pytest_basetemp, "_windows", lambda: True)
    assert pytest_basetemp.root() == Path("C:/t")


def test_posix_roots_under_the_launcher_home(monkeypatch):
    """claunch-wt12o.5: the Windows string is relative on POSIX, which is how
    pytest came to make ``C:/t/...`` inside the checkout under test."""
    assert not PurePosixPath(pytest_basetemp.WINDOWS_ROOT).is_absolute()
    monkeypatch.setattr(pytest_basetemp, "_windows", lambda: False)
    got = pytest_basetemp.root()
    assert got == config.launcher_home() / "basetemp"
    assert got.is_absolute()


def test_root_honors_the_test_output_dir_setting(monkeypatch, tmp_path):
    """claunch-pyv3d: the root is declared in settings, not only hardcoded."""
    configured = tmp_path / "declared-output"
    monkeypatch.setattr(store, "daemon_config",
                        lambda: {"test_output_dir": str(configured)})
    assert pytest_basetemp.root() == configured


def test_root_anchors_a_relative_setting_at_the_launcher_home(monkeypatch, home):
    """A relative setting must not resolve against a session's cwd -- that is
    exactly the accident (a test tree inside a checkout) this root exists
    to prevent (claunch-wt12o.5)."""
    monkeypatch.setattr(store, "daemon_config",
                        lambda: {"test_output_dir": "declared-output"})
    assert pytest_basetemp.root() == home / "declared-output"


def test_root_falls_back_to_the_default_for_empty_or_bad_values(monkeypatch):
    for value in ("", "   ", None, 42):
        monkeypatch.setattr(store, "daemon_config",
                            lambda: {"test_output_dir": value})
        assert pytest_basetemp.root() == pytest_basetemp._default_root()


def test_root_falls_back_when_the_config_cannot_be_read(monkeypatch):
    def boom():
        raise store.StoreError("unparseable")
    monkeypatch.setattr(store, "daemon_config", boom)
    assert pytest_basetemp.root() == pytest_basetemp._default_root()


def test_session_test_dir_is_the_bare_session_name_under_the_root(
        monkeypatch, tmp_path):
    configured = tmp_path / "declared-output"
    monkeypatch.setattr(store, "daemon_config",
                        lambda: {"test_output_dir": str(configured)})
    monkeypatch.setattr(pytest_basetemp, "session_test_dir", _REAL_SESSION_TEST_DIR)
    assert pytest_basetemp.session_test_dir("s7") == configured / "s7"


def test_two_sessions_never_share_a_test_dir(monkeypatch, tmp_path):
    configured = tmp_path / "declared-output"
    monkeypatch.setattr(store, "daemon_config",
                        lambda: {"test_output_dir": str(configured)})
    monkeypatch.setattr(pytest_basetemp, "session_test_dir", _REAL_SESSION_TEST_DIR)
    seen = {pytest_basetemp.session_test_dir(n) for n in ("s1", "s2", "s10")}
    assert len(seen) == 3


def test_ensure_makes_the_parents_pytest_does_not(tmp_path):
    target = tmp_path / "a" / "b"
    assert pytest_basetemp.ensure(target) == target
    assert target.is_dir()
    assert pytest_basetemp.ensure(target) == target  # a second call is a no-op


def test_ensure_leaves_a_root_it_cannot_make_to_pytest(tmp_path):
    """Housekeeping never decides a gate: pytest reports the same missing
    directory by name, and a raise here would only hide which path it was."""
    blocker = tmp_path / "file"
    blocker.write_text("", encoding="utf-8")
    target = blocker / "root"
    assert pytest_basetemp.ensure(target) == target
    assert not target.exists()
