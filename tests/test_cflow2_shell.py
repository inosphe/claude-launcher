"""Step commands run in a POSIX shell (claunch-2kz0r).

A step's ``verify`` and its ``awaits`` probe are written for a POSIX shell --
``"$CFLOW_IN_KEY"``, ``a || b``, ``true``. ``shell=True`` gives that shell on
POSIX and cmd.exe on Windows, where none of those work: the string reaches the
program with its quotes still in it. Measured on a coordinator verify, which
answered ``passed`` under bash and ``key must be <canonical board
directory>#<issue id>`` under cmd.exe for the same environment.

These tests hold the choice in place at the two places that make it
(:func:`.cflow2.runtime.shell_command`, used by the runtime's verify runner and
by the daemon clock's probe runner), including the machine that has no bash at
all, where the old behaviour is what remains.
"""

from __future__ import annotations

import os
import shutil
from types import SimpleNamespace

import pytest

from claude_launcher.cflow2 import runtime
from claude_launcher.daemon import cflow2_clock

#: A command only a POSIX shell answers correctly: cmd.exe prints the dollars.
POSIX_ONLY = 'echo "value=$CLAUNCH_SHELL_MARK"'
MARK = "s1217-mark"

BASH = shutil.which("bash")

needs_bash = pytest.mark.skipif(
    BASH is None, reason="no POSIX shell on this machine to run the command with")


@pytest.fixture
def on_windows(monkeypatch):
    """Make ``runtime.os.name`` read ``nt`` without touching the real os
    module: ``shell_command`` reads nothing else from it but ``environ``."""
    monkeypatch.setattr(runtime, "os", SimpleNamespace(name="nt", environ=os.environ))
    monkeypatch.delenv(runtime.SHELL_ENV, raising=False)
    return monkeypatch


@pytest.fixture
def on_posix(monkeypatch):
    monkeypatch.setattr(runtime, "os", SimpleNamespace(name="posix", environ=os.environ))
    monkeypatch.delenv(runtime.SHELL_ENV, raising=False)
    return monkeypatch


def test_posix_runs_the_platform_shell(on_posix):
    command, shell = runtime.shell_command(POSIX_ONLY)
    assert command == POSIX_ONLY
    assert shell is True


def test_windows_runs_a_bash_when_one_is_on_path(on_windows):
    on_windows.setattr(runtime.shutil, "which", lambda name: "/usr/bin/bash")
    command, shell = runtime.shell_command(POSIX_ONLY)
    assert command == ["/usr/bin/bash", "-c", POSIX_ONLY]
    assert shell is False


def test_windows_without_bash_keeps_the_platform_shell(on_windows):
    on_windows.setattr(runtime.shutil, "which", lambda name: None)
    command, shell = runtime.shell_command(POSIX_ONLY)
    assert command == POSIX_ONLY
    assert shell is True


def test_an_argv_command_runs_without_a_shell(on_windows):
    on_windows.setattr(runtime.shutil, "which", lambda name: "/usr/bin/bash")
    argv = ["claunch", "reposync", "verify", "allow-local"]
    command, shell = runtime.shell_command(argv)
    assert command == argv
    assert shell is False


def test_the_shell_variable_forces_the_platform_shell(on_windows):
    on_windows.setattr(runtime.shutil, "which", lambda name: "/usr/bin/bash")
    on_windows.setenv(runtime.SHELL_ENV, "cmd")
    command, shell = runtime.shell_command(POSIX_ONLY)
    assert command == POSIX_ONLY
    assert shell is True


def test_the_shell_variable_names_the_shell(on_posix):
    on_posix.setenv(runtime.SHELL_ENV, "/bin/dash")
    command, shell = runtime.shell_command(POSIX_ONLY)
    assert command == ["/bin/dash", "-c", POSIX_ONLY]
    assert shell is False


@needs_bash
def test_the_verify_runner_expands_a_posix_variable(tmp_path):
    """The end-to-end fact the fix is for: the command sees its variable."""
    code, output, timed_out = runtime.default_verify_runner(
        POSIX_ONLY, str(tmp_path), {"CLAUNCH_SHELL_MARK": MARK, "PATH": os.environ["PATH"]}, 60)
    assert timed_out is False
    assert code == 0
    assert f"value={MARK}" in output


@needs_bash
def test_the_probe_runner_expands_a_posix_variable(tmp_path):
    """The daemon clock makes the same choice, from the same function."""
    code, output = cflow2_clock.run_probe(
        POSIX_ONLY, str(tmp_path), timeout=60, env={"CLAUNCH_SHELL_MARK": MARK})
    assert code == 0
    assert f"value={MARK}" in output


def test_a_failed_string_command_still_reports_its_exit_code(tmp_path):
    """The shell choice does not swallow the code: a run blocks on it."""
    code, output, timed_out = runtime.default_verify_runner(
        "exit 3", str(tmp_path), {"PATH": os.environ["PATH"]}, 60)
    assert timed_out is False
    assert code == 3
