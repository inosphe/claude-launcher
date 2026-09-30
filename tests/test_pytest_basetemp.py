"""``claude_launcher.pytest_basetemp``: the root every gate tool hands pytest.

The tools' own use of it is pinned next to them (tests/test_changed_tests.py,
tests/test_sweep.py). These pin the two branches and the one side effect.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from claude_launcher import config, pytest_basetemp


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
