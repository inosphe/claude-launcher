"""The relay binary lookup the relay test modules share (``_relay_exe``).

These run on every host, including the ones where the lookup finds nothing:
they are what says why a relay test skipped there.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from _relay_exe import ENV, EXE, find_relay_exe, relay_roots, skip_reason

#: Every module that starts a real relay, and the skip marker it carries.
RELAY_MODULES = {
    "test_federation_integration": "requires_relay",
    "test_relay_multi_integration": "pytestmark",
    "test_multi_daemon_mesh": "pytestmark",
    "test_relay_integration": "pytestmark",
}


def test_posix_looks_for_the_relay_under_the_home_directory():
    """claunch-wt12o.1.1: only ``F:/`` roots were searched, which on POSIX
    are cwd-relative paths, so every relay test skipped on Linux."""
    roots = relay_roots("posix", home=PurePosixPath("/home/u"))
    assert roots == [
        PurePosixPath("/home/u/works/mux-relay/target"),
        PurePosixPath("/home/u/works/mux-relay/.claude/worktrees/relay-tunnel/target"),
    ]
    assert all(root.is_absolute() for root in roots)


def test_windows_keeps_its_relay_roots():
    roots = relay_roots("nt", home=PurePosixPath("/home/u"))
    assert roots == [
        PureWindowsPath("F:/works/mux-relay/target"),
        PureWindowsPath("F:/works/mux-relay/.claude/worktrees/relay-tunnel/target"),
    ]
    assert all(root.is_absolute() for root in roots)


def _build(root: Path, prof: str) -> Path:
    exe = root / prof / EXE
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    return exe


def test_the_first_root_holding_a_build_wins(tmp_path, monkeypatch):
    """The order is the caller's: ``test_relay_integration`` asks for the
    relay-tunnel worktree first, the other modules for the main checkout."""
    monkeypatch.delenv(ENV, raising=False)
    main, tunnel = tmp_path / "main", tmp_path / "tunnel"
    main_exe = _build(main, "release")
    tunnel_exe = _build(tunnel, "debug")
    assert find_relay_exe([main, tunnel]) == main_exe
    assert find_relay_exe([tunnel, main]) == tunnel_exe
    assert find_relay_exe([tmp_path / "nothing"]) is None


def test_debug_is_taken_before_release(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    release = _build(tmp_path, "release")
    assert find_relay_exe([tmp_path]) == release
    debug = _build(tmp_path, "debug")
    assert find_relay_exe([tmp_path]) == debug


def test_the_environment_variable_wins_over_every_root(tmp_path, monkeypatch):
    built = _build(tmp_path / "root", "debug")
    named = tmp_path / "elsewhere" / EXE
    named.parent.mkdir()
    named.write_bytes(b"")
    monkeypatch.setenv(ENV, str(named))
    assert find_relay_exe([tmp_path / "root"]) == named
    # A name that is not a file falls through to the roots.
    monkeypatch.setenv(ENV, str(tmp_path / "missing"))
    assert find_relay_exe([tmp_path / "root"]) == built


def test_the_skip_reason_names_where_it_looked():
    roots = relay_roots("posix", home=PurePosixPath("/home/u"))
    reason = skip_reason(roots)
    for root in roots:
        assert str(root) in reason
    assert ENV in reason


@pytest.mark.parametrize("module, marker", sorted(RELAY_MODULES.items()))
def test_each_relay_module_skips_naming_where_it_looked(module, marker):
    """claunch-wt12o.1.1.1: three of these said only "binary not found", so a
    skip on a host where the build sat elsewhere did not say where to put it."""
    mark = getattr(importlib.import_module(module), marker)
    reason = mark.kwargs["reason"]
    for root in relay_roots():
        assert str(root) in reason
    assert ENV in reason


def test_no_test_module_keeps_its_own_copy_of_the_lookup():
    """Four copies drifted apart once (claunch-wt12o.1.1.1): one looked under
    the home directory on POSIX, three did not."""
    tests = Path(__file__).parent
    own_copy = re.compile(r"^def _(find_relay_exe|relay_roots)\b", re.M)
    offenders = [
        path.name
        for path in sorted(tests.glob("*.py"))
        if own_copy.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []
