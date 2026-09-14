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

import json

from . import (
    borrowing,
    config,
    credentials,
    harness_policy,
    harnesses,
    lineage,
    pi_provider,
    providers,
    routing,
    translators,
)
from .profile import Profile

#: Environment variable Claude Code reads for a setup-token login.
OAUTH_TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"

#: Bearer token Claude Code sends to a custom (provider-overridden) backend.
AUTH_TOKEN_ENV = "ANTHROPIC_AUTH_TOKEN"

#: Backend keys a provider or profile owns outright — endpoint, auth, and the
#: model pins. A stale value for any of these in the ambient environment must
#: never reach the child: the daemon is usually started from inside a claude
#: session (which itself may run on a borrowed backend), so its inherited env
#: carries that backend's keys, and every later spawn would keep talking to it
#: whenever the new profile/provider leaves the key unset. ``child_env`` drops
#: these from the base env, together with every key any provider in the
#: registry defines; the config file is the only legitimate source for them.
BACKEND_ENV_KEYS = frozenset(
    {
        "ANTHROPIC_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_FABLE_MODEL",
        "ANTHROPIC_SMALL_FAST_MODEL",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_API_KEY",
        AUTH_TOKEN_ENV,
        "CLAUDE_CODE_SUBAGENT_MODEL",
    }
)


def _managed_env_keys() -> frozenset:
    """Keys stripped from the base env: backend keys plus any provider's."""
    keys = set(BACKEND_ENV_KEYS) | set(pi_provider.PROJECTION_ENV)
    for env in providers.registry().values():
        keys.update(env)
    return frozenset(keys)


class RunnerError(Exception):
    """Raised when the selected harness cannot be launched."""


@dataclass(frozen=True)
class Heartbeat:
    """Result of a non-interactive harness health check."""

    ok: bool
    code: Optional[int]
    reason: str
    output: str


def _finalize_declared_auth(harness: harnesses.Harness, env: dict) -> None:
    """Apply the selected harness's declared authentication boundary."""
    for name in harness.clear_env:
        env.pop(name, None)
    for name in harness.empty_env:
        env[name] = ""


def _profile_backend_pins_apply(profile: Profile, provider: str) -> bool:
    """Do ``profile``'s own backend pins still describe the backend in play?

    ``provider`` is the backend this run actually talks to, after a borrow and
    any ``--provider`` override. A profile's ``ANTHROPIC_MODEL`` and friends
    are written against the provider the profile itself resolves to; when the
    run is on that same provider they are the user's deliberate refinement and
    keep their final say. When it is on another one they are leftovers naming
    models and endpoints of a backend nobody is calling, and letting them win
    is how a session ends up asking Anthropic for a Fireworks model id.
    """
    try:
        return providers.resolve_name(profile) == provider
    except Exception:  # pragma: no cover - a broken config file
        # Unresolvable provider selection: leave the historical layering alone
        # rather than dropping keys on a guess.
        return True


def _claude_profile_env(profile: Profile) -> dict:
    """The profile chain's Claude-vocabulary overrides.

    The legacy ``env`` (still the Claude harness's raw override channel) plus
    the schema's ``harness_options.claude.env``, root ancestor first, the
    profile itself last.
    """
    env = dict(lineage.effective_env(profile))
    try:
        env.update(providers.profile_overlay(profile).option_env("claude"))
    except providers.ProviderError as exc:
        raise RunnerError(str(exc)) from exc
    return env


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
    Backend keys (see :data:`BACKEND_ENV_KEYS`, plus anything any provider
    defines) are dropped from the base env first, so a value leaked into the
    daemon's or shell's environment by a *previous* backend cannot shadow a
    profile that simply leaves the key unset.

    The same reasoning bounds the profile's own final say. A profile that pins
    model ids or a base URL is describing *its* backend, so those pins stop
    being true the moment this run talks to another one -- a borrow of a
    lender on a different provider, or an explicit ``--provider``. Keeping
    them then produces the one combination that cannot work: the lender's
    endpoint asked for the borrower's model ids. So the profile layer keeps
    final say over every key, except that :data:`BACKEND_ENV_KEYS` are dropped
    from it when the backend in play is not the profile's own (see
    :func:`_profile_backend_pins_apply`). Borrowing inside one provider is
    unchanged: the pins still describe the backend being used.
    """
    harness = harnesses.get(harnesses.CLAUDE_HARNESS)
    if harness is None:
        raise RunnerError("the claude harness is not declared")
    try:
        if borrow is not None:
            borrow, _report = borrowing.require_allowed(
                profile,
                borrow.selector,
                entry=harness,
                provider_override=provider_override,
            )
        else:
            harness_policy.require(
                profile,
                harness.name,
                provider_override=provider_override,
            )
    except (borrowing.BorrowError, harness_policy.HarnessPolicyError) as exc:
        raise RunnerError(str(exc)) from exc
    managed = _managed_env_keys()
    env = {
        k: v
        for k, v in (os.environ if base_env is None else base_env).items()
        if k not in managed
    }
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
        # The provider's Claude translation is a low-priority backend
        # default: it sits above the shell but *below* the profile's own env,
        # applied next, which can override any provider key. The profile's
        # schema fields (models, context_window, auto_compact_at and
        # reasoning_effort) are folded into that translation; its model pins
        # only when the backend in play is the profile's own (see
        # _profile_backend_pins_apply).
        try:
            provider_env = providers.claude_env(
                profile,
                provider,
                overlay_models=_profile_backend_pins_apply(profile, provider),
            )
        except providers.ProviderError as exc:
            raise RunnerError(str(exc)) from exc
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
    # the point of an isolated profile. The exception is the backend keys when
    # this run is not on the profile's own backend: those name a backend that
    # is not the one being talked to (see the docstring).
    profile_env = _claude_profile_env(profile)
    if with_token and not _profile_backend_pins_apply(profile, provider):
        profile_env = {
            k: v for k, v in profile_env.items() if k not in BACKEND_ENV_KEYS
        }
    env.update(profile_env)
    if with_token:
        if not providers.uses_anthropic_oauth(provider):
            # A provider is overriding the backend: auth comes from the
            # profile's single set-token value (own, inherited, or borrowed),
            # which OVERRIDES any plaintext
            # ANTHROPIC_AUTH_TOKEN in the config file — so backend keys can
            # live in the per-machine 0600 token file instead of the yaml.
            stored = lineage.stored_auth_token(auth_source)
            if stored:
                if not harness.token_env:
                    raise RunnerError(
                        "the claude harness has no declared token_env"
                    )
                env[harness.token_env] = stored
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
    if with_token:
        # A backend that takes its routing in the request body (OpenRouter's
        # provider pinning) cannot be reached by environment alone. When the
        # provider declares one, the base URL is swung to a local shim that
        # merges the spec into every request — see :mod:`routing`.
        routing.apply(env, provider)
    # Claude gateways authenticate with the declared bearer-token route. When
    # it is active the packaged rule forces ANTHROPIC_API_KEY="", preventing
    # Claude Code from also emitting a competing X-Api-Key header.
    _finalize_declared_auth(harness, env)
    # Profile identity is not a user override. Set it last so neither a stale
    # shell value nor a synced ``env`` entry can escape this profile.
    env[config.CLAUDE_CONFIG_DIR_ENV] = str(profile.config_dir)
    return env


def harness_child_env(
    profile: Profile,
    harness: harnesses.Harness,
    *,
    base_env: Optional[dict] = None,
    borrow: Optional[Profile] = None,
    tools: Optional[Sequence[str]] = None,
) -> dict:
    """Environment for a non-Claude profile harness.

    Non-Claude-safe runtime-profile env remains available, while Claude Code's
    namespace is filtered below. OAuth CLIs read their own namespaced home;
    API-key harnesses receive the runtime profile's shared token, or the base
    lender's when ``borrow`` is given, through their declared route.
    """
    if harness.builtin:
        return child_env(
            profile, with_token=True, base_env=base_env, borrow=borrow
        )
    if borrow is not None:
        try:
            borrow, _report = borrowing.require_allowed(
                profile, borrow.selector, entry=harness
            )
        except borrowing.BorrowError as exc:
            raise RunnerError(str(exc)) from exc
    else:
        try:
            harness_policy.require(profile, harness.name)
        except harness_policy.HarnessPolicyError as exc:
            raise RunnerError(str(exc)) from exc
    env = dict(os.environ if base_env is None else base_env)
    env.update(harness.env)
    env.update(lineage.effective_env(profile))

    finalize_harness_env(profile, harness, env, borrow=borrow, tools=tools)
    return env


def harness_translation(
    profile: Profile,
    harness: harnesses.Harness,
    *,
    borrow: Optional[Profile] = None,
) -> translators.Translation:
    """The harness's translation of the backend this launch talks to."""
    auth_source = borrow if borrow is not None else profile
    try:
        own = providers.resolve_name(profile)
        provider = providers.resolve_name(auth_source)
        spec = providers.spec_for(
            profile, provider, overlay_models=(provider == own)
        )
    except providers.ProviderError as exc:
        raise RunnerError(str(exc)) from exc
    translation = translators.for_harness(harness.name, spec)
    if translation.missing:
        raise RunnerError(
            f"harness {harness.name!r} cannot translate configured provider "
            f"field(s): {', '.join(translation.missing)}"
        )
    return translation


def harness_launch_args(
    profile: Profile,
    harness: harnesses.Harness,
    args: Sequence[str],
    *,
    tools: Optional[Sequence[str]] = None,
) -> list[str]:
    """Prepend the harness's translated launch arguments, then its adapter.

    Translated arguments (Codex's ``-c`` overrides) go first so anything the
    session passes explicitly is seen later by the harness and wins.
    ``tools`` is a session's explicit builtin-tool choice; ``None`` takes the
    profile's default.
    """
    if harness.builtin:
        return list(args)
    translated = harness_translation(profile, harness).args
    try:
        return pi_provider.launch_args(
            profile, harness, [*translated, *args], tools=tools
        )
    except pi_provider.PiProviderError as exc:
        raise RunnerError(str(exc)) from exc


def finalize_harness_env(
    profile: Profile,
    harness: harnesses.Harness,
    env: dict,
    *,
    borrow: Optional[Profile] = None,
    tools: Optional[Sequence[str]] = None,
) -> None:
    """Enforce profile auth/storage boundaries after all environment layers.

    Existing profiles often carry many ``ANTHROPIC_*`` and
    ``CLAUDE_CODE_*`` values. Except for the packaged Claude auth boundary,
    they remain available to Claude Code (including Kimi-compatible backends),
    but are not sprayed into unrelated CLI agents. Pi receives the profile's
    one launcher-managed token again below.

    The daemon calls this a second time after applying per-session ``--env``;
    otherwise that dead customization path could escape the same boundary.
    """
    if harness.builtin:
        _finalize_declared_auth(harness, env)
        return
    token_env = harness.token_env
    auth_source = borrow if borrow is not None else profile
    managed_token = (
        _api_key_for(auth_source) if harness.auth == "api-key" else None
    )
    for key in list(env):
        if key.startswith(("CLAUDE_CODE_", "ANTHROPIC_")) and not (
            harness.auth == "api-key" and managed_token and key == token_env
        ):
            env.pop(key, None)
        elif (
            harness.auth == "api-key"
            and key.upper().endswith("API_KEY")
            and (not managed_token or key != token_env)
        ):
            # A Pi profile has one explicit token route. Do not let whatever
            # API keys happened to start claunch choose Pi's backend.
            env.pop(key, None)
    translation = harness_translation(profile, harness, borrow=borrow)
    # harness_options.<harness>.env is the declared raw channel for this
    # harness: applied after the Claude-namespace filter above (so it is the
    # one way to hand such a key to another harness on purpose) and before
    # the token route, which stays authoritative.
    env.update(translation.env)
    if managed_token:
        if not token_env:
            raise RunnerError(
                f"harness {harness.name!r} uses API-key auth but declares "
                "no token_env"
            )
        env[token_env] = managed_token
    _finalize_declared_auth(harness, env)
    try:
        pi_provider.apply_env(profile, harness, env, borrow=borrow)
        pi_provider.apply_tools_env(profile, harness, env, tools=tools)
    except pi_provider.PiProviderError as exc:
        raise RunnerError(str(exc)) from exc
    if harness.home_env:
        home = harness.profile_home(profile.config_dir)
        home.mkdir(parents=True, exist_ok=True)
        # Like CLAUDE_CONFIG_DIR, the storage boundary is launcher-owned.
        env[harness.home_env] = str(home)
        _write_translated_files(home, translation)


def _write_translated_files(home, translation: translators.Translation) -> None:
    """Merge translated keys into files under the harness's profile home.

    Only the named keys change; everything else in the file is kept, so a
    setting the person wrote by hand survives every launch.
    """
    for relative, values in translation.files.items():
        path = home / relative
        doc: dict = {}
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8") or "{}")
                doc = loaded if isinstance(loaded, dict) else {}
            except (OSError, ValueError):
                doc = {}
        before = json.dumps(doc, sort_keys=True)
        for key, value in values.items():
            translators.set_dotted(doc, key, value)
        if json.dumps(doc, sort_keys=True) == before:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


def _api_key_for(auth_source: Profile) -> Optional[str]:
    """The one secret an API-key harness receives for ``auth_source``.

    The profile's ``set-token`` secret (own or inherited) wins; without one,
    the key written in its provider's ``env`` is used, so a provider configured
    once for the Claude harness authenticates every other harness too. The
    same precedence the Claude harness applies to ``ANTHROPIC_AUTH_TOKEN``.
    """
    stored = lineage.stored_auth_token(auth_source)
    if stored:
        return stored
    try:
        return providers.auth_token(providers.resolve_name(auth_source))
    except providers.ProviderError as exc:
        raise RunnerError(str(exc)) from exc


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
    borrow: Optional[Profile] = None,
    tools: Optional[Sequence[str]] = None,
) -> int:
    cmd = [
        *harness.launch_command(),
        *harness_launch_args(
            profile, harness, [*harness.args, *args], tools=tools
        ),
    ]
    try:
        return subprocess.run(
            cmd,
            cwd=cwd,
            env=harness_child_env(profile, harness, borrow=borrow, tools=tools),
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
                    f"harness {harness.name!r} uses the profile token as an "
                    f"API key; store it with 'claunch set-token {profile.name}'"
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
    tools: Optional[Sequence[str]] = None,
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
        if borrow is not None and not harness.borrowable:
            raise RunnerError(borrowing.capability(harness)["message"])
        incompatible = []
        if provider:
            incompatible.append("--provider")
        if null_token:
            incompatible.append("--null")
        if incompatible:
            raise RunnerError(
                f"{', '.join(incompatible)} only applies to the claude harness; "
                f"profile {profile.selector!r} selects {harness.name!r}"
            )
        return _plain_spawn(
            profile, harness, list(args), cwd=cwd, borrow=borrow, tools=tools
        )

    auth_source = borrow if borrow is not None else profile
    if provider:
        name, source = provider, "--provider"
    else:
        name, source = providers.resolve_with_source(auth_source)
    if not providers.uses_anthropic_oauth(name):
        # Tell the user why auth behaves differently on this run: with a
        # provider overriding the backend, the stored profile token (if any) is
        # exported as ANTHROPIC_AUTH_TOKEN instead of the OAuth injection.
        stored = lineage.stored_auth_token(auth_source)
        via = (
            "auth: stored profile token exported as ANTHROPIC_AUTH_TOKEN"
            if stored
            else "auth: no stored profile token; using the provider's env as configured"
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
        runtime_args = harness_launch_args(
            profile, harness, [*harness.args, *harness.heartbeat_args, prompt]
        )
        cmd = [*harness.launch_command(), *runtime_args]
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
