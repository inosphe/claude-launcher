"""Invoke the ``claude`` CLI with a profile's ``CLAUDE_CONFIG_DIR``.

This is the only module that shells out to ``claude``. It builds the child
environment (injecting ``CLAUDE_CONFIG_DIR`` and, for ``run``, the stored
``CLAUDE_CODE_OAUTH_TOKEN``) and never decides *which* profile to use — callers
pass a resolved :class:`~claude_launcher.profile.Profile`.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Optional, Sequence

from . import config, credentials, settings
from .profile import Profile

#: Environment variable Claude Code reads for a setup-token login.
OAUTH_TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"


class RunnerError(Exception):
    """Raised when the ``claude`` executable cannot be launched."""


@dataclass(frozen=True)
class Heartbeat:
    """Result of a non-interactive ``claude -p`` health check."""

    ok: bool
    code: Optional[int]
    reason: str
    output: str


def _child_env(profile: Profile, *, with_token: bool) -> dict:
    env = os.environ.copy()
    env[config.CLAUDE_CONFIG_DIR_ENV] = str(profile.config_dir)
    # Per-profile env vars (from settings.json "env") take precedence over the
    # inherited shell environment — that is the point of an isolated profile.
    env.update(settings.get_env(profile))
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
    """Run ``claude setup-token`` interactively for the profile.

    ``setup-token`` renders a full-screen TUI and drives an interactive OAuth
    flow, so its stdio is left attached to the terminal (no piping/capture).
    Claude Code persists the resulting login inside the profile's
    ``CLAUDE_CONFIG_DIR``; if it instead only prints a token, the user can store
    it with ``claunch set-token``.
    """
    code = _spawn(profile, ["setup-token"], with_token=False)
    if code != 0:
        return code
    if credentials.has_token(profile):
        print(
            f"\nprofile {profile.name!r} is logged in.",
            file=sys.stderr,
        )
    else:
        print(
            f"\nlogin finished but no token was saved for profile {profile.name!r}. "
            f"If setup-token printed a token, store it with:\n"
            f"    claunch set-token {profile.name} <token>",
            file=sys.stderr,
        )
    return code


def run(profile: Profile, args: Sequence[str] = ()) -> int:
    """Launch ``claude`` (optionally with passthrough ``args``) for the profile."""
    return _spawn(profile, list(args), with_token=True)


def heartbeat(
    profile: Profile, prompt: str = "heartbeat", timeout: float = 120.0
) -> Heartbeat:
    """Run ``claude -p <prompt>`` non-interactively and report whether it worked.

    Captures output instead of attaching the terminal, so a broken/expired login
    fails fast rather than dropping into an interactive prompt.
    """
    cmd = [config.claude_bin(), "-p", prompt]
    try:
        completed = subprocess.run(
            cmd,
            env=_child_env(profile, with_token=True),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise RunnerError(
            f"could not find {config.claude_bin()!r} executable; "
            f"is Claude Code installed? (override with {config.LAUNCHER_BIN_ENV})"
        ) from exc
    except subprocess.TimeoutExpired:
        return Heartbeat(ok=False, code=None, reason=f"timed out after {int(timeout)}s", output="")

    output = (completed.stdout or "").strip()
    if completed.returncode == 0:
        return Heartbeat(ok=True, code=0, reason="ok", output=output)
    reason = (completed.stderr or "").strip() or output or f"exit {completed.returncode}"
    return Heartbeat(ok=False, code=completed.returncode, reason=reason, output=output)
