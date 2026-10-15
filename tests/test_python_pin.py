"""The daemon's Python version is written down in two places that must agree.

``uv tool install`` does not read ``.python-version`` (measured, claunch-4ermn.1):
the interpreter the daemon runs on is the ``--python`` on the README's install
commands, and ``.python-version`` pins the same version for ``uv run`` /
``uv sync`` in a checkout. A command that drops the flag, or a version bumped in
one file only, lets a reinstall move the daemon to whatever interpreter uv finds
first.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _pinned() -> str:
    return (ROOT / ".python-version").read_text(encoding="utf-8").strip()


def _install_section() -> str:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    start = readme.index("## Install\n")
    end = readme.index("\n## ", start + 1)
    return readme[start:end]


def test_python_version_file_names_one_regular_build():
    pinned = _pinned()
    # A bare minor version: '3.14t' would select the free-threaded build,
    # which claunch-4ermn left undecided.
    assert re.fullmatch(r"3\.\d+", pinned), pinned


def test_every_readme_install_command_pins_that_version():
    pinned = _pinned()
    section = _install_section()
    # Command lines in code blocks, and inline commands that carry arguments;
    # prose naming the bare command (`uv tool install` does not read ...) is
    # not an install command.
    commands = re.findall(r"^uv tool install .*$", section, re.M)
    commands += re.findall(r"`(uv tool install [^`]+)`", section)
    assert len(commands) >= 3, commands
    for command in commands:
        assert f"--python {pinned} " in command + " ", command
