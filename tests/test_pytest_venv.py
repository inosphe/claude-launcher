"""``claude_launcher.pytest_venv``: a pytest on another tree's interpreter.

claunch-3gx: a worktree whose ``.venv`` is empty runs the main checkout's
pytest without a word. The gate refuses that (``foreign_pytest``,
claunch-7sj); these pin the warning every other entry point now prints.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from claude_launcher import pytest_venv, test_window


def _tree(tmp_path: Path, *, venv: bool) -> Path:
    root = tmp_path / "wt"
    (root / "src").mkdir(parents=True)
    if venv:
        (root / ".venv").mkdir()
    return root


def test_another_interpreter_beside_the_trees_venv_warns(tmp_path):
    root = _tree(tmp_path, venv=True)
    other = tmp_path / "main" / ".venv"
    exe = str(other / "bin" / "python")
    note = pytest_venv.foreign_interpreter(root, str(other), exe)
    assert note is not None
    assert exe in note  # the interpreter that ran
    assert str(root / ".venv") in note  # the one that should have
    assert "uv sync --extra test" in note


def test_no_venv_in_the_tree_is_not_warned(tmp_path):
    """CI installs into an environment outside the checkout and creates no
    ``.venv`` in it; that run is not the case this is about."""
    root = _tree(tmp_path, venv=False)
    other = tmp_path / "ci-env"
    assert pytest_venv.foreign_interpreter(root, str(other), "python") is None


def test_the_trees_own_venv_is_not_warned(tmp_path):
    root = _tree(tmp_path, venv=True)
    venv = root / ".venv"
    assert pytest_venv.foreign_interpreter(root, str(venv), "python") is None


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symlinks")
def test_the_trees_venv_through_a_symlink_is_not_warned(tmp_path):
    """``sys.prefix`` may name the venv by another path to the same place."""
    root = _tree(tmp_path, venv=True)
    link = tmp_path / "link"
    try:
        link.symlink_to(root / ".venv", target_is_directory=True)
    except OSError:
        pytest.skip("cannot create a symlink here")
    assert pytest_venv.foreign_interpreter(root, str(link), "python") is None


class _Stop(Exception):
    """Ends the hook right after the part under test."""


def _conftest(pytestconfig):
    want = Path(__file__).with_name("conftest.py").resolve()
    for plugin in pytestconfig.pluginmanager.get_plugins():
        path = getattr(plugin, "__file__", None)
        if path and Path(path).resolve() == want:
            return plugin
    raise AssertionError("tests/conftest.py is not a loaded plugin")


@pytest.mark.parametrize("venv,warned", [(True, True), (False, False)])
def test_session_start_prints_it(pytestconfig, tmp_path, monkeypatch, capsys,
                                 venv, warned):
    """The hook every pytest entry passes through asks before the window."""
    conftest = _conftest(pytestconfig)
    root = _tree(tmp_path, venv=venv)
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    monkeypatch.delenv(test_window.WINDOW_REPORT_ENV, raising=False)
    monkeypatch.setattr(conftest, "_session_started", conftest._session_started)

    def stop():
        raise _Stop

    monkeypatch.setattr(test_window, "inherited_grant", stop)
    session = SimpleNamespace(config=SimpleNamespace(rootpath=root, args=[]))
    with pytest.raises(_Stop):
        conftest.pytest_sessionstart(session)
    err = capsys.readouterr().err
    assert ("claunch-3gx" in err) is warned
    if warned:
        assert sys.executable in err
