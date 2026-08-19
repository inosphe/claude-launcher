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

from . import config, credentials, lineage, providers
from .profile import Profile

#: Environment variable Claude Code reads for a setup-token login.
OAUTH_TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"

#: Bearer token Claude Code sends to a custom (provider-overridden) backend.
AUTH_TOKEN_ENV = "ANTHROPIC_AUTH_TOKEN"


class RunnerError(Exception):
    """Raised when the ``claude`` executable cannot be launched."""


@dataclass(frozen=True)
class Heartbeat:
    """Result of a non-interactive ``claude -p`` health check."""

    ok: bool
    code: Optional[int]
    reason: str
    output: str


def child_env(
    profile: Profile,
    *,
    with_token: bool,
    borrow: Optional[Profile] = None,
    base_env: Optional[dict] = None,
    provider_override: Optional[str] = None,
    null_token: bool = False,
) -> dict:
    """The full environment for a ``claude`` child of ``profile``.

    Public so the session daemon can assemble an identical environment when it
    spawns claude under a managed PTY. ``base_env`` defaults to this process's
    environment (for the daemon that means sessions inherit the *daemon's* env,
    like tmux server semantics). ``provider_override`` (``run --provider``)
    replaces the config-file provider resolution for this call only.
    ``null_token`` (``run --null``) launches with no OAuth token at all: the
    profile's stored token is not injected, and any value inherited from the
    shell or pinned by profile/provider env is dropped, so claude starts
    unauthenticated (log in with /login).
    """
    env = dict(os.environ if base_env is None else base_env)
    env[config.CLAUDE_CONFIG_DIR_ENV] = str(profile.config_dir)
    provider_env: dict = {}
    if with_token:
        # A `--borrow` swaps both the token *and* the provider: the running
        # profile's config dir, env and skills stay put, but auth (and the
        # backend it talks to) comes from the borrowed profile. An explicit
        # --provider beats both.
        auth_source = borrow if borrow is not None else profile
        provider = provider_override or providers.resolve_name(auth_source)
        # Provider env is a low-priority backend default: it sits above the
        # shell but *below* the profile's own env, applied next, which can
        # override any provider key (e.g. CLAUDE_CODE_AUTO_COMPACT_WINDOW).
        provider_env = providers.provider_env(provider)
        env.update(provider_env)
    # Per-profile env vars (inherited from any parent, then the profile's own)
    # take precedence over the shell and the provider — that is the point of an
    # isolated profile.
    profile_env = lineage.effective_env(profile)
    env.update(profile_env)
    if with_token:
        if provider != providers.DEFAULT_PROVIDER:
            # A provider is overriding the backend: auth comes from the
            # profile's stored `set-token` secret (own, inherited, or the
            # borrowed profile's), which OVERRIDES any plaintext
            # ANTHROPIC_AUTH_TOKEN in the config file — so backend keys can
            # live in the per-machine 0600 token file instead of the yaml.
            stored = lineage.stored_auth_token(auth_source)
            if stored:
                env[AUTH_TOKEN_ENV] = stored
            # A custom backend never uses the Anthropic OAuth var; drop any
            # shell leftover unless the config file set it explicitly (the
            # provider pattern pins it to "").
            if OAUTH_TOKEN_ENV not in {**provider_env, **profile_env}:
                env.pop(OAUTH_TOKEN_ENV, None)
        else:
            # Plain Anthropic: inject the (own/inherited/borrowed) OAuth token.
            # `--null` suppresses the lookup so the pop below clears the var.
            token = (
                None
                if null_token
                else lineage.lookup_token(borrow)
                if borrow is not None
                else lineage.injectable_token(profile)
            )
            if token:
                env[OAUTH_TOKEN_ENV] = token
            else:
                # No token to inject — the profile (or borrowed profile) logs in
                # interactively via /login. Don't set the var to None, and don't
                # let a stale shell/profile token shadow the fresh login flow.
                env.pop(OAUTH_TOKEN_ENV, None)
    else:
        # During login the profile may hold a stale token; don't let it shadow
        # the fresh setup-token flow. Login always targets Anthropic, so no
        # provider override is applied here.
        env.pop(OAUTH_TOKEN_ENV, None)
    if null_token:
        # `--null` means *no* OAuth token, full stop — even one pinned by the
        # profile's own env or a provider pattern loses to the explicit flag.
        env.pop(OAUTH_TOKEN_ENV, None)
    return env


def _spawn(
    profile: Profile,
    args: Sequence[str],
    *,
    with_token: bool,
    borrow: Optional[Profile] = None,
    provider_override: Optional[str] = None,
    null_token: bool = False,
    cwd: Optional[str] = None,
) -> int:
    cmd = [config.claude_bin(), *args]
    try:
        completed = subprocess.run(
            cmd,
            cwd=cwd,
            env=child_env(
                profile,
                with_token=with_token,
                borrow=borrow,
                provider_override=provider_override,
                null_token=null_token,
            ),
        )
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


def run(
    profile: Profile,
    args: Sequence[str] = (),
    *,
    borrow: Optional[Profile] = None,
    provider: Optional[str] = None,
    null_token: bool = False,
    cwd: Optional[str] = None,
) -> int:
    """Launch ``claude`` for the profile, optionally borrowing another's token.

    ``provider`` (from ``run --provider``) overrides the config-file provider
    resolution for this run only. ``null_token`` (from ``run --null``) launches
    with no OAuth token at all (see :func:`child_env`). ``cwd`` (from ``run
    --worktree``) starts claude in another directory; ``None`` inherits this
    process's, which is what every run that did not ask for a worktree wants.
    """
    auth_source = borrow if borrow is not None else profile
    if provider:
        name, source = provider, "--provider"
    else:
        name, source = providers.resolve_with_source(auth_source)
    if name != providers.DEFAULT_PROVIDER:
        # Tell the user why auth behaves differently on this run: with a
        # provider overriding the backend, the stored set-token (if any) is
        # exported as ANTHROPIC_AUTH_TOKEN instead of the OAuth injection.
        stored = lineage.stored_auth_token(auth_source)
        via = (
            "auth: stored set-token exported as ANTHROPIC_AUTH_TOKEN"
            if stored
            else "auth: no stored set-token; using the provider's env as configured"
        )
        print(
            f"provider {name!r} active ({source}); {via}",
            file=sys.stderr,
        )
    elif null_token:
        print(
            f"launching with no OAuth token ({OAUTH_TOKEN_ENV} cleared); "
            "log in with /login",
            file=sys.stderr,
        )
    elif borrow is not None:
        if lineage.lookup_token(borrow) is None:
            # An empty token is allowed: launch anyway so the user can /login
            # interactively inside Claude Code instead of hard-failing here.
            print(
                f"warning: profile {borrow.name!r} has no token to borrow; "
                f"log in with /login, or run 'claunch login {borrow.name}' first",
                file=sys.stderr,
            )
    return _spawn(
        profile,
        list(args),
        with_token=True,
        borrow=borrow,
        provider_override=provider,
        null_token=null_token,
        cwd=cwd,
    )


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
            env=child_env(profile, with_token=True),
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
