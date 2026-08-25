"""Invoke the CLI harness selected by a profile.

Claude keeps its established provider/token environment. Other harnesses get
their namespaced storage and authentication boundary here. Callers always pass
a resolved :class:`~claude_launcher.profile.Profile`.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Optional, Sequence

from . import config, credentials, harnesses, lineage, providers
from .profile import Profile

#: Environment variable Claude Code reads for a setup-token login.
OAUTH_TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"

#: Bearer token Claude Code sends to a custom (provider-overridden) backend.
AUTH_TOKEN_ENV = "ANTHROPIC_AUTH_TOKEN"

# OAuth-backed harnesses must not silently switch to an API-key login merely
# because either the shell or a legacy profile environment exported one.
# Their own OAuth credential store is the only supported auth source here.
_OAUTH_SHELL_KEYS = {
    "codex": ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN"),
    "kimi": ("KIMI_API_KEY",),
    "agent": ("CURSOR_API_KEY",),
}


class RunnerError(Exception):
    """Raised when the selected harness cannot be launched."""


@dataclass(frozen=True)
class Heartbeat:
    """Result of a non-interactive harness health check."""

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
    unauthenticated (log in with /login). ``borrow`` (``run --borrow``)
    swaps auth — and the backend it talks to — to another profile: the
    lender's provider token *and* its env come along, its env as a fill layer
    below the runner's own env, which keeps final responsibility for every key.
    """
    env = dict(os.environ if base_env is None else base_env)
    provider_env: dict = {}
    lender_env: dict = {}
    if with_token:
        # A `--borrow` swaps auth, and the backend it talks to, to the lender:
        # the running profile's config dir and skills stay put, but the lender's
        # provider *and* its own env arrive as backend defaults — the lender's
        # env fills keys the runner never set, while the runner's env below
        # keeps final say over every key. An explicit --provider beats both.
        auth_source = borrow if borrow is not None else profile
        provider = provider_override or providers.resolve_name(auth_source)
        # Provider env is a low-priority backend default: it sits above the
        # shell but *below* the profile's own env, applied next, which can
        # override any provider key (e.g. CLAUDE_CODE_AUTO_COMPACT_WINDOW).
        provider_env = providers.provider_env(provider)
        env.update(provider_env)
        if borrow is not None:
            # The lender's profile env is one more fill layer: above the
            # provider's defaults (borrowing a profile should behave like that
            # profile's backend natively) but below the runner's own env
            # (borrowing never surrenders a key the runner sets itself).
            lender_env = lineage.effective_env(borrow)
            env.update(lender_env)
    # Per-profile env vars (inherited from any parent, then the profile's own)
    # take precedence over the shell, the provider and a borrow's env — that is
    # the point of an isolated profile.
    profile_env = lineage.effective_env(profile)
    env.update(profile_env)
    if with_token:
        if provider != providers.DEFAULT_PROVIDER:
            # A provider is overriding the backend: auth comes from the
            # profile's separate provider key (or legacy set-token; own,
            # inherited, or borrowed), which OVERRIDES any plaintext
            # ANTHROPIC_AUTH_TOKEN in the config file — so backend keys can
            # live in the per-machine 0600 token file instead of the yaml.
            stored = lineage.stored_auth_token(auth_source)
            if stored:
                env[AUTH_TOKEN_ENV] = stored
            # A custom backend never uses the Anthropic OAuth var; drop any
            # shell leftover unless the config file set it explicitly (the
            # provider pattern pins it to "").
            if OAUTH_TOKEN_ENV not in {**provider_env, **lender_env, **profile_env}:
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
    # Profile identity is not a user override. Set it last so neither a stale
    # shell value nor a synced ``env`` entry can escape this profile.
    env[config.CLAUDE_CONFIG_DIR_ENV] = str(profile.config_dir)
    return env


def harness_child_env(
    profile: Profile,
    harness: harnesses.Harness,
    *,
    base_env: Optional[dict] = None,
) -> dict:
    """Environment for a non-Claude profile harness.

    Non-Claude-safe profile env remains available, while Claude Code's
    namespace is filtered below. Authentication storage is separate: OAuth
    CLIs read their own namespaced home, while a launcher-managed API key is
    injected only into the explicitly configured ``api_key_env``.
    """
    if harness.builtin:
        return child_env(profile, with_token=True, base_env=base_env)
    env = dict(os.environ if base_env is None else base_env)
    env.update(harness.env)
    env.update(lineage.effective_env(profile))

    finalize_harness_env(profile, harness, env)
    return env


def finalize_harness_env(
    profile: Profile, harness: harnesses.Harness, env: dict
) -> None:
    """Enforce profile auth/storage boundaries after all environment layers.

    Existing profiles often carry many ``ANTHROPIC_*`` and
    ``CLAUDE_CODE_*`` values. They remain untouched for Claude Code (including
    Kimi-compatible Claude backends), but are not sprayed into unrelated CLI
    agents. Pi receives its one launcher-managed provider key again below.

    The daemon calls this a second time after applying per-session ``--env``;
    otherwise that dead customization path could escape the same boundary.
    """
    if harness.builtin:
        return
    key_env = lineage.effective_api_key_env(profile)
    managed_key = (
        lineage.stored_api_key(profile) if harness.auth == "api-key" else None
    )
    for key in list(env):
        if key.startswith(("CLAUDE_CODE_", "ANTHROPIC_")) and not (
            harness.auth == "api-key" and managed_key and key == key_env
        ):
            env.pop(key, None)
        elif (
            harness.auth == "api-key"
            and key.upper().endswith("API_KEY")
            and (not managed_key or key != key_env)
        ):
            # A Pi profile has one explicit key route. Do not let whatever
            # provider keys happened to start claunch choose Pi's backend.
            env.pop(key, None)
    for key in _OAUTH_SHELL_KEYS.get(harness.name, ()):
        env.pop(key, None)
    if managed_key:
        if not key_env:
            raise RunnerError(
                f"profile {profile.name!r} has a stored API key but no "
                "api_key_env; store it again with 'claunch set-key "
                f"{profile.name} ENV_VAR'"
            )
        env[key_env] = managed_key
    if harness.home_env:
        home = harness.profile_home(profile.config_dir)
        home.mkdir(parents=True, exist_ok=True)
        # Like CLAUDE_CONFIG_DIR, the storage boundary is launcher-owned.
        env[harness.home_env] = str(home)


def profile_harness(profile: Profile) -> harnesses.Harness:
    name = lineage.effective_harness(profile)
    entry = harnesses.get(name)
    if entry is None:  # effective_harness already validates; belt and braces
        raise RunnerError(f"unknown harness {name!r}")
    return entry


def _plain_spawn(
    profile: Profile,
    harness: harnesses.Harness,
    args: Sequence[str],
    *,
    cwd: Optional[str] = None,
) -> int:
    cmd = [*harness.launch_command(), *harness.args, *args]
    try:
        return subprocess.run(
            cmd, cwd=cwd, env=harness_child_env(profile, harness)
        ).returncode
    except FileNotFoundError as exc:
        raise RunnerError(
            f"could not find harness {harness.name!r} command "
            f"{harness.program()!r}; install it or update the harness declaration"
        ) from exc
    except OSError as exc:
        raise RunnerError(f"could not launch harness {harness.name!r}: {exc}") from exc


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
    """Run the selected harness's interactive login for ``profile``.

    ``setup-token`` renders a full-screen TUI and drives an interactive OAuth
    flow, so its stdio is left attached to the terminal (no piping/capture).
    Claude Code persists the resulting login inside the profile's
    ``CLAUDE_CONFIG_DIR``; if it instead only prints a token, the user can store
    it with ``claunch set-token``.
    """
    harness = profile_harness(profile)
    if not harness.builtin:
        if not harness.login_args:
            if harness.auth == "api-key":
                raise RunnerError(
                    f"harness {harness.name!r} uses an API key; store one with "
                    f"'claunch set-key {profile.name} ENV_VAR'"
                )
            raise RunnerError(
                f"harness {harness.name!r} has no login command declared"
            )
        return _plain_spawn(profile, harness, harness.login_args)

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
    """Launch the harness selected by the profile.

    ``provider`` (from ``run --provider``) overrides the config-file provider
    resolution for this run only. ``null_token`` (from ``run --null``) launches
    with no OAuth token at all (see :func:`child_env`). ``cwd`` (from ``run
    --worktree``) starts claude in another directory; ``None`` inherits this
    process's, which is what every run that did not ask for a worktree wants.
    """
    harness = profile_harness(profile)
    if not harness.builtin:
        incompatible = []
        if borrow is not None:
            incompatible.append("--borrow")
        if provider:
            incompatible.append("--provider")
        if null_token:
            incompatible.append("--null")
        if incompatible:
            raise RunnerError(
                f"{', '.join(incompatible)} only applies to the claude harness; "
                f"profile {profile.name!r} selects {harness.name!r}"
            )
        return _plain_spawn(profile, harness, list(args), cwd=cwd)

    auth_source = borrow if borrow is not None else profile
    if provider:
        name, source = provider, "--provider"
    else:
        name, source = providers.resolve_with_source(auth_source)
    if name != providers.DEFAULT_PROVIDER:
        # Tell the user why auth behaves differently on this run: with a
        # provider overriding the backend, the stored provider key (if any) is
        # exported as ANTHROPIC_AUTH_TOKEN instead of the OAuth injection.
        stored = lineage.stored_auth_token(auth_source)
        via = (
            "auth: stored provider key exported as ANTHROPIC_AUTH_TOKEN"
            if stored
            else "auth: no stored provider key; using the provider's env as configured"
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
    """Run the profile harness non-interactively and report whether it worked.

    Captures output instead of attaching the terminal, so a broken/expired login
    fails fast rather than dropping into an interactive prompt.
    """
    harness = profile_harness(profile)
    if harness.builtin:
        cmd = [config.claude_bin(), "-p", prompt]
        env = child_env(profile, with_token=True)
    else:
        if not harness.heartbeat_args:
            raise RunnerError(
                f"harness {harness.name!r} has no non-interactive health-check "
                "command; declare harnesses.<name>.heartbeat_args to enable validate"
            )
        cmd = [
            *harness.launch_command(),
            *harness.args,
            *harness.heartbeat_args,
            prompt,
        ]
        env = harness_child_env(profile, harness)
    try:
        completed = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise RunnerError(
            f"could not find harness {harness.name!r} command "
            f"{harness.program()!r}"
        ) from exc
    except subprocess.TimeoutExpired:
        return Heartbeat(ok=False, code=None, reason=f"timed out after {int(timeout)}s", output="")

    output = (completed.stdout or "").strip()
    if completed.returncode == 0:
        return Heartbeat(ok=True, code=0, reason="ok", output=output)
    reason = (completed.stderr or "").strip() or output or f"exit {completed.returncode}"
    return Heartbeat(ok=False, code=completed.returncode, reason=reason, output=output)
