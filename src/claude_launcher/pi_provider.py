"""Project a claunch provider into the Pi CLI.

Pi reads API keys from environment variables, while custom endpoints and
models are registered through ``models.json`` or an extension.  claunch owns
the profile token and provider bundle, so the packaged extension is the
appropriate boundary: it registers an in-memory provider for this process and
does not modify the user's Pi settings or ``models.json``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from . import provider_spec, providers, routing
from .profile import Profile

ADAPTER = "pi"
PI_PROVIDER_NAME = "claunch-profile"
EXTENSION_FILE = "pi_provider.mjs"
#: The extension that adds claunch's own tools (``full_read``) to every Pi
#: session, whatever provider it runs on.
TOOLS_EXTENSION_FILE = "pi_tools.mjs"
ACTIVITY_EXTENSION_FILE = "pi_activity.mjs"
#: Tools the packaged extension knows; ``harness_options.pi.tools`` switches
#: them off by name.
BUILTIN_TOOLS = ("full_read",)

ENV_PROVIDER = "CLAUNCH_PI_PROVIDER"
ENV_BASE_URL = "CLAUNCH_PI_BASE_URL"
ENV_API = "CLAUNCH_PI_API"
ENV_MODELS = "CLAUNCH_PI_MODELS"
ENV_TOKEN_NAME = "CLAUNCH_PI_TOKEN_ENV"
ENV_AUTH_HEADER = "CLAUNCH_PI_AUTH_HEADER"
ENV_CONTEXT_WINDOW = "CLAUNCH_PI_CONTEXT_WINDOW"
#: Explicit output budget, declared through harness_options.pi.env.
ENV_MAX_TOKENS = "CLAUNCH_PI_MAX_TOKENS"
#: Explicit cross-harness effort and the OpenAI request encoding declared by
#: the effective provider spec.  Both are required together for a reasoning
#: model; the adapter never infers either from the model id or endpoint.
ENV_REASONING_EFFORT = "CLAUNCH_PI_REASONING_EFFORT"
ENV_REASONING_FORMAT = "CLAUNCH_PI_REASONING_FORMAT"
#: Comma-separated names of the builtin tools to register (unset = all).
ENV_TOOLS = "CLAUNCH_PI_TOOLS"
#: ``1`` when the base URL is claunch's metering shim: the extension then lets
#: Pi ask for ``stream_options.include_usage`` so the shim sees token counts.
ENV_STREAM_USAGE = "CLAUNCH_PI_STREAM_USAGE"
#: JSON object of extra request headers the extension registers with the
#: provider. The session's name is not one of them: it rides in the base URL
#: (``routing.SESSION_PATH_PREFIX``).
ENV_HEADERS = "CLAUNCH_PI_HEADERS"
PROJECTION_ENV = frozenset(
    {
        ENV_PROVIDER,
        ENV_BASE_URL,
        ENV_API,
        ENV_MODELS,
        ENV_TOKEN_NAME,
        ENV_AUTH_HEADER,
        ENV_CONTEXT_WINDOW,
        ENV_MAX_TOKENS,
        ENV_REASONING_EFFORT,
        ENV_REASONING_FORMAT,
        ENV_TOOLS,
        ENV_STREAM_USAGE,
        ENV_HEADERS,
    }
)

_SELECTION_FLAGS = ("--provider", "--model", "--models")

#: A session effort of this value says the caller supplies reasoning itself
#: (per request, from its own extension): claunch then projects none of its
#: own -- no ``CLAUNCH_PI_*`` reasoning, no ``--thinking`` and no
#: ``settings.json`` default. It is Pi's own name for "thinking disabled".
EFFORT_CALLER_MANAGES = "off"

log = logging.getLogger(__name__)


class PiProviderError(Exception):
    """Raised when a selected provider cannot be represented for Pi."""


@dataclass(frozen=True)
class Projection:
    """The non-secret provider values supplied to the packaged extension."""

    base_url: str
    api: str
    models: tuple[str, ...]
    default_model: str
    token_env: str
    auth_header: bool
    context_window: Optional[int] = None
    max_tokens: Optional[int] = None
    reasoning_effort: Optional[str] = None
    reasoning_format: Optional[str] = None
    #: Set when a declared effort could not be carried; ``apply_env`` logs it.
    #: The launch itself proceeds, without reasoning.
    warning: Optional[str] = None


def extension_path() -> Path:
    """Path to the provider extension shipped beside this module."""
    return _packaged(EXTENSION_FILE, "provider")


def tools_extension_path() -> Path:
    """Path to the tools extension shipped beside this module."""
    return _packaged(TOOLS_EXTENSION_FILE, "tools")


def activity_extension_path() -> Path:
    """Path to the managed turn-activity extension."""
    return _packaged(ACTIVITY_EXTENSION_FILE, "activity")


def _packaged(filename: str, what: str) -> Path:
    path = Path(__file__).with_name(filename)
    if not path.is_file():
        raise PiProviderError(f"the packaged Pi {what} extension is missing: {path}")
    return path


def enabled_tools(
    profile: Profile, harness, *, override: Optional[Sequence[str]] = None
) -> tuple[str, ...]:
    """Builtin tools this launch registers.

    A session's explicit choice (``override``) wins outright; otherwise the
    profile's default: every builtin tool minus ``harness_options.pi.tools:
    {name: false}``.
    """
    if harness.provider_adapter != ADAPTER:
        return ()
    if override is not None:
        return tuple(name for name in BUILTIN_TOOLS if name in set(override))
    return profile_default_tools(profile)


def profile_default_tools(profile: Profile) -> tuple[str, ...]:
    """The profile's default: all builtin tools minus the ones switched off."""
    try:
        declared = providers.spec_for(profile).options("pi").get("tools")
    except providers.ProviderError as exc:
        raise PiProviderError(str(exc)) from exc
    switches = declared if isinstance(declared, dict) else {}
    return tuple(
        name for name in BUILTIN_TOOLS if switches.get(name, True) is not False
    )


def caller_manages_effort(harness, effort) -> bool:
    """Whether this session hands reasoning to its caller untouched."""
    return harness.provider_adapter == ADAPTER and (
        str(effort or "").strip().lower() == EFFORT_CALLER_MANAGES
    )


def projected_effort(
    spec, effort, *, provider: Optional[str] = None
) -> tuple[Optional[str], Optional[str]]:
    """The reasoning effort this launch carries, and a warning when dropped.

    The caller's choice comes first: a session that was launched with an
    explicit level keeps it, and one launched with ``off`` gets no effort at
    all because its caller encodes reasoning per request. Without a caller
    choice the profile's ``reasoning_effort`` applies.

    A provider that cannot encode the profile's effort (it declares no
    ``openai_reasoning_format``) is not a reason to refuse the session: the
    effort is dropped and the second value says why, so the launch proceeds
    without reasoning. A caller who named the level explicitly is refused
    instead -- that is a requirement we cannot meet, not a default we can
    leave out.
    """
    caller = str(effort or "").strip().lower() or None
    if caller == EFFORT_CALLER_MANAGES:
        return None, None
    if caller:
        if not spec.openai_reasoning_format:
            name = provider or "the selected provider"
            raise PiProviderError(
                f"provider {name!r} cannot launch Pi with reasoning effort "
                f"{caller!r}: openai_reasoning_format is not declared"
            )
        return caller, None
    if spec.reasoning_effort and not spec.openai_reasoning_format:
        name = provider or "the selected provider"
        # The effort may have come from the profile rather than from the
        # provider entry, so this names the missing field instead of saying
        # which layer set the level it cannot carry.
        return None, (
            f"provider {name!r} has no openai_reasoning_format, so the "
            f"reasoning effort {spec.reasoning_effort!r} is not sent; launching "
            "without it. Add openai_reasoning_format to the provider, or pass "
            "'--effort off' to run without a launcher-imposed effort."
        )
    return spec.reasoning_effort, None


def resolve(
    profile: Profile,
    harness,
    *,
    borrow: Optional[Profile] = None,
    effort: Optional[str] = None,
) -> Optional[Projection]:
    """Translate the selected provider's spec into Pi's provider vocabulary.

    Pi speaks OpenAI Chat Completions, so it needs ``endpoints.openai``; the
    model list is the spec's roles, default first; ``context_window`` is
    handed to the extension as each model's ``contextWindow``. Nothing is
    derived from Claude's vocabulary any more -- a provider that only says
    where its Anthropic-compatible API is cannot launch Pi, and says so.
    """
    if harness.provider_adapter != ADAPTER:
        return None
    auth_source = borrow if borrow is not None else profile
    name = providers.resolve_name(auth_source)
    if providers.uses_anthropic_oauth(name):
        return None
    if not harness.token_env:
        raise PiProviderError(
            f"harness {harness.name!r} has no token_env for the Pi adapter"
        )
    try:
        # ``launch_spec`` reads the model table from ``borrow`` when there is
        # one, so a Pi session borrowing another profile lists that profile's
        # models; context and reasoning stay the running profile's.
        spec = providers.launch_spec(profile, name, borrow=borrow)
    except providers.ProviderError as exc:
        raise PiProviderError(str(exc)) from exc

    openai_root = spec.endpoint("openai")
    if not openai_root:
        raise PiProviderError(
            f"provider {name!r} cannot launch Pi: endpoints.openai is not "
            "declared (Pi talks OpenAI Chat Completions; set "
            f"providers.{name}.endpoints.openai to the backend's OpenAI-"
            "compatible root)"
        )
    try:
        urls = provider_spec.openai_urls(openai_root)
    except provider_spec.SpecError as exc:
        raise PiProviderError(str(exc)) from exc
    models = list(spec.model_list())
    if not models:
        raise PiProviderError(
            f"provider {name!r} cannot launch Pi: no models are declared "
            f"(set providers.{name}.models.default)"
        )
    if caller_manages_effort(harness, effort):
        # The caller projects reasoning itself; even the profile's format
        # metadata must not reach the extension, or it would encode a level
        # the caller never asked for.
        reasoning_effort, reasoning_format, warning = None, None, None
    else:
        reasoning_effort, warning = projected_effort(spec, effort, provider=name)
        reasoning_format = spec.openai_reasoning_format or None
        if reasoning_format and not reasoning_effort:
            # A provider that declares the request encoding but is never
            # asked for a level is a broken declaration: unlike a missing
            # format, it cannot be read as "run without reasoning".
            raise PiProviderError(
                f"provider {name!r} cannot launch Pi with explicit reasoning: "
                "reasoning_effort is not declared (reasoning_effort and "
                "openai_reasoning_format must be set together)"
            )
    max_tokens = None
    raw_max = spec.option_env("pi").get(ENV_MAX_TOKENS)
    if raw_max is not None:
        if not raw_max.isascii() or not raw_max.isdecimal() or int(raw_max) <= 0:
            raise PiProviderError(f"{ENV_MAX_TOKENS} must be a positive integer")
        max_tokens = int(raw_max)
        if max_tokens > (spec.context_window or 128000):
            raise PiProviderError(f"{ENV_MAX_TOKENS} cannot exceed context_window")
    return Projection(
        base_url=urls.base_url,
        api="openai-completions",
        models=tuple(models),
        default_model=spec.model("default") or models[0],
        token_env=harness.token_env,
        # The OpenAI client derives Authorization: Bearer from this API key;
        # an explicit header would duplicate that native route.
        auth_header=False,
        context_window=spec.context_window,
        max_tokens=max_tokens,
        reasoning_effort=reasoning_effort,
        reasoning_format=reasoning_format,
        warning=warning,
    )


def apply_env(
    profile: Profile,
    harness,
    env: dict,
    *,
    borrow: Optional[Profile] = None,
    effort: Optional[str] = None,
) -> None:
    """Replace inherited adapter state with this profile's projection.

    The base URL goes through :func:`routing.front` like Claude Code's
    ``ANTHROPIC_BASE_URL`` does: an API-key provider is fronted by the
    metering shim (or the routing shim when it declares a ``routing``
    block), and the shim's loopback URL is what Pi registers. When that
    happened, ``CLAUNCH_PI_STREAM_USAGE=1`` tells the extension to let Pi
    request usage in the stream -- without it an OpenAI stream carries no
    token counts and the record would say ``counted: false``.
    """
    for key in PROJECTION_ENV:
        env.pop(key, None)
    projection = resolve(profile, harness, borrow=borrow, effort=effort)
    if projection is None:
        return
    if projection.warning:
        log.warning("%s", projection.warning)
    auth_source = borrow if borrow is not None else profile
    base_url = fronted_base_url(
        projection.base_url, providers.resolve_name(auth_source)
    )
    env.update(
        {
            ENV_PROVIDER: PI_PROVIDER_NAME,
            ENV_BASE_URL: base_url,
            ENV_API: projection.api,
            ENV_MODELS: json.dumps(
                projection.models, ensure_ascii=False, separators=(",", ":")
            ),
            ENV_TOKEN_NAME: projection.token_env,
            ENV_AUTH_HEADER: "1" if projection.auth_header else "0",
        }
    )
    if projection.context_window:
        env[ENV_CONTEXT_WINDOW] = str(projection.context_window)
    if projection.max_tokens:
        env[ENV_MAX_TOKENS] = str(projection.max_tokens)
    if projection.reasoning_effort:
        env[ENV_REASONING_EFFORT] = projection.reasoning_effort
        env[ENV_REASONING_FORMAT] = projection.reasoning_format or ""
    if routing.is_shim_url(base_url):
        env[ENV_STREAM_USAGE] = "1"


def fronted_base_url(base_url: str, provider_name: str,
                     profile: "Optional[str]" = None) -> str:
    """``base_url`` swung to the shim when the provider goes through one.

    The shim's URL comes back without a trailing slash: Pi hands it to the
    OpenAI client as ``baseURL``, which appends ``/chat/completions`` itself,
    and the shim forwards that path onto the backend's declared API base.
    """
    fronted = routing.front(base_url, provider_name, profile=profile)
    return fronted.rstrip("/") if fronted != base_url else base_url


def apply_tools_env(
    profile: Profile, harness, env: dict, *, tools: Optional[Sequence[str]] = None
) -> None:
    """Tell the tools extension which builtin tools to register."""
    env.pop(ENV_TOOLS, None)
    if harness.provider_adapter != ADAPTER:
        return
    enabled = enabled_tools(profile, harness, override=tools)
    if enabled != BUILTIN_TOOLS:
        env[ENV_TOOLS] = ",".join(enabled)


def launch_args(
    profile: Profile,
    harness,
    args: Sequence[str],
    *,
    tools: Optional[Sequence[str]] = None,
    effort: Optional[str] = None,
    borrow: Optional[Profile] = None,
) -> list[str]:
    """Prepend Pi's extensions and default model when required.

    The activity extension reports managed interactive turn lifecycle even
    with tools disabled. The tools extension rides along on launches with a
    builtin tool enabled; the provider extension only when a custom provider
    is projected. A caller's explicit ``--provider``, ``--model`` or
    ``--models`` owns model selection for that launch, including flags
    forwarded after ``claunch run PROFILE:pi``. Flags after ``--`` are prompt
    text and do not suppress the projection.

    ``borrow`` is the profile whose token the launch spends; the default
    model comes from the same projection the environment does, so it is that
    profile's, matching the rest of the launch.
    """
    forwarded = list(args)
    if harness.provider_adapter != ADAPTER:
        return forwarded
    adapter_args: list[str] = ["--extension", str(activity_extension_path())]
    if enabled_tools(profile, harness, override=tools):
        adapter_args += ["--extension", str(tools_extension_path())]
    projection = resolve(profile, harness, effort=effort, borrow=borrow)
    if projection is None:
        return [*adapter_args, *forwarded]
    adapter_args += ["--extension", str(extension_path())]
    if _has_explicit_selection(forwarded):
        return [*adapter_args, *forwarded]
    return [
        *adapter_args,
        "--provider",
        PI_PROVIDER_NAME,
        "--model",
        projection.default_model,
        *forwarded,
    ]


def _has_explicit_selection(args: Sequence[str]) -> bool:
    for arg in args:
        if arg == "--":
            break
        if arg in _SELECTION_FLAGS:
            return True
        if any(arg.startswith(flag + "=") for flag in _SELECTION_FLAGS):
            return True
    return False
