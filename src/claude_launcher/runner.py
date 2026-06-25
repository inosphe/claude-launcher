"""Invoke the ``claude`` CLI with a profile's ``CLAUDE_CONFIG_DIR``.

This is the only module that shells out to ``claude``. It builds the child
environment (injecting ``CLAUDE_CONFIG_DIR`` and, for ``run``, the stored
``CLAUDE_CODE_OAUTH_TOKEN``) and never decides *which* profile to use — callers
pass a resolved :class:`~claude_launcher.profile.Profile`.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from typing import Optional, Sequence

from . import config, credentials
from .profile import Profile

#: Environment variable Claude Code reads for a setup-token login.
OAUTH_TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"

#: setup-token output looks like ``sk-ant-oat01-...``; capture it from the stream.
_TOKEN_RE = re.compile(r"sk-ant-oat[\w-]+")


class RunnerError(Exception):
    """Raised when the ``claude`` executable cannot be launched."""


def _child_env(profile: Profile, *, with_token: bool) -> dict:
    env = os.environ.copy()
    env[config.CLAUDE_CONFIG_DIR_ENV] = str(profile.config_dir)
    if with_token:
        token = credentials.stored_token(profile)
        if token:
            env[OAUTH_TOKEN_ENV] = token
    else:
        # During login the profile may hold a stale token; don't let it shadow
        # the fresh setup-token flow.
        env.pop(OAUTH_TOKEN_ENV, None)
    return env


def _spawn(profile: Profile, args: Sequence[str], *, with_token: bool) -> int:
    cmd = [config.claude_bin(), *args]
    try:
        completed = subprocess.run(cmd, env=_child_env(profile, with_token=with_token))
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
    """Run ``claude setup-token`` and store the printed token in the profile.

    The child's combined output is streamed to the terminal in real time (so the
    interactive OAuth prompts stay visible) while we scan it for the token.
    """
    cmd = [config.claude_bin(), "setup-token"]
    try:
        proc = subprocess.Popen(
            cmd,
            env=_child_env(profile, with_token=False),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError as exc:
        raise RunnerError(
            f"could not find {config.claude_bin()!r} executable; "
            f"is Claude Code installed? (override with {config.LAUNCHER_BIN_ENV})"
        ) from exc
    except OSError as exc:
        raise RunnerError(f"could not launch {config.claude_bin()!r}: {exc}") from exc

    token: Optional[str] = None
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        match = _TOKEN_RE.search(line)
        if match:
            token = match.group(0)
    code = proc.wait()

    if token:
        credentials.save_token(profile, token)
        print(
            f"\nstored token for profile {profile.name!r} "
            f"(used as {OAUTH_TOKEN_ENV} on 'claunch run')",
            file=sys.stderr,
        )
    elif code == 0:
        print(
            f"\nwarning: no token detected in output for profile {profile.name!r}; "
            f"nothing stored. Capture it manually with 'claunch set-token {profile.name}'.",
            file=sys.stderr,
        )
    return code


def run(profile: Profile, args: Sequence[str] = ()) -> int:
    """Launch ``claude`` (optionally with passthrough ``args``) for the profile."""
    return _spawn(profile, list(args), with_token=True)
