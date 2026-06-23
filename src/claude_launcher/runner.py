"""Invoke the ``claude`` CLI with a profile's ``CLAUDE_CONFIG_DIR``.

This is the only module that shells out to ``claude``. It builds the child
environment (injecting ``CLAUDE_CONFIG_DIR``) and never decides *which* profile
to use — callers pass a resolved :class:`~claude_launcher.profile.Profile`.
"""

from __future__ import annotations

import os
import subprocess
from typing import Sequence

from . import config
from .profile import Profile


class RunnerError(Exception):
    """Raised when the ``claude`` executable cannot be launched."""


def _child_env(profile: Profile) -> dict:
    env = os.environ.copy()
    env[config.CLAUDE_CONFIG_DIR_ENV] = str(profile.config_dir)
    return env


def _spawn(profile: Profile, args: Sequence[str]) -> int:
    cmd = [config.claude_bin(), *args]
    try:
        completed = subprocess.run(cmd, env=_child_env(profile))
    except FileNotFoundError as exc:
        raise RunnerError(
            f"could not find {config.claude_bin()!r} executable; "
            f"is Claude Code installed? (override with {config.LAUNCHER_BIN_ENV})"
        ) from exc
    except OSError as exc:
        raise RunnerError(
            f"could not launch {config.claude_bin()!r}: {exc} "
            f"(override the executable with {config.LAUNCHER_BIN_ENV})"
        ) from exc
    return completed.returncode


def login(profile: Profile) -> int:
    """Run ``claude setup-token`` so the token lands in the profile's dir."""
    return _spawn(profile, ["setup-token"])


def run(profile: Profile, args: Sequence[str] = ()) -> int:
    """Launch ``claude`` (optionally with passthrough ``args``) for the profile."""
    return _spawn(profile, list(args))
