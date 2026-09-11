"""Project a claunch Anthropic-compatible provider into the Pi CLI.

Pi reads API keys from environment variables, while custom endpoints and
models are registered through ``models.json`` or an extension.  claunch owns
the profile token and provider bundle, so the packaged extension is the
appropriate boundary: it registers an in-memory provider for this process and
does not modify the user's Pi settings or ``models.json``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from . import providers, routing
from .profile import Profile

ADAPTER = "pi"
PI_PROVIDER_NAME = "claunch-profile"
EXTENSION_FILE = "pi_provider.mjs"
#: The extension that adds claunch's own tools (``full_read``) to every Pi
#: session, whatever provider it runs on.
TOOLS_EXTENSION_FILE = "pi_tools.mjs"
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
#: Comma-separated names of the builtin tools to register (unset = all).
ENV_TOOLS = "CLAUNCH_PI_TOOLS"
#: ``1`` when the base URL is claunch's metering shim: the extension then lets
#: Pi ask for ``stream_options.include_usage`` so the shim sees token counts.
ENV_STREAM_USAGE = "CLAUNCH_PI_STREAM_USAGE"
#: JSON object of extra request headers the extension registers with the
#: provider; the daemon puts ``X-Claunch-Session`` here (see ``metering``).
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
        ENV_TOOLS,
        ENV_STREAM_USAGE,
        ENV_HEADERS,
    }
)

_SELECTION_FLAGS = ("--provider", "--model", "--models")


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


def extension_path() -> Path:
    """Path to the provider extension shipped beside this module."""
    return _packaged(EXTENSION_FILE, "provider")


def tools_extension_path() -> Path:
    """Path to the tools extension shipped beside this module."""
    return _packaged(TOOLS_EXTENSION_FILE, "tools")


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


def resolve(
    profile: Profile, harness, *, borrow: Optional[Profile] = None
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
        spec = providers.spec_for(
            profile,
            name,
            overlay_models=(name == providers.resolve_name(profile)),
        )
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
    models = list(spec.model_list())
    if not models:
        raise PiProviderError(
            f"provider {name!r} cannot launch Pi: no models are declared "
            f"(set providers.{name}.models.default)"
        )
    return Projection(
        base_url=_openai_base_url(openai_root),
        api="openai-completions",
        models=tuple(models),
        default_model=spec.model("default") or models[0],
        token_env=harness.token_env,
        # The OpenAI client derives Authorization: Bearer from this API key;
        # an explicit header would duplicate that native route.
        auth_header=False,
        context_window=spec.context_window,
    )


def apply_env(
    profile: Profile, harness, env: dict, *, borrow: Optional[Profile] = None
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
    projection = resolve(profile, harness, borrow=borrow)
    if projection is None:
        return
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
    if routing.is_shim_url(base_url):
        env[ENV_STREAM_USAGE] = "1"


def fronted_base_url(base_url: str, provider_name: str) -> str:
    """``base_url`` swung to the shim when the provider goes through one.

    The shim's URL comes back without a trailing slash: Pi hands it to the
    OpenAI client as ``baseURL``, which appends ``/chat/completions`` itself,
    and the shim forwards that path onto the upstream root (which already
    ends in ``/v1``).
    """
    fronted = routing.front(base_url, provider_name)
    return fronted.rstrip("/") if fronted != base_url else base_url


def apply_session_header(env: dict, session: str) -> None:
    """Name ``session`` in every request Pi sends through the shim.

    Same purpose as ``metering.apply_session_header`` for Claude Code: the
    shim is shared by every session on the same upstream, and the header is
    the only way a record gets the session's name. The extension registers
    the headers with the provider; the shim strips them before forwarding.
    """
    try:
        headers = json.loads(env.get(ENV_HEADERS) or "{}")
    except ValueError:
        headers = {}
    if not isinstance(headers, dict):
        headers = {}
    headers = {k: v for k, v in headers.items() if k.lower() != "x-claunch-session"}
    headers["X-Claunch-Session"] = session
    env[ENV_HEADERS] = json.dumps(headers, separators=(",", ":"))


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
) -> list[str]:
    """Prepend Pi's extensions and default model when required.

    The tools extension rides along on every Pi launch that has at least one
    builtin tool enabled; the provider extension only when a custom provider
    is projected. A caller's explicit ``--provider``, ``--model`` or
    ``--models`` owns model selection for that launch, including flags
    forwarded after ``claunch run PROFILE:pi``. Flags after ``--`` are prompt
    text and do not suppress the projection.
    """
    forwarded = list(args)
    if harness.provider_adapter != ADAPTER:
        return forwarded
    adapter_args: list[str] = []
    if enabled_tools(profile, harness, override=tools):
        adapter_args += ["--extension", str(tools_extension_path())]
    projection = resolve(profile, harness)
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


def _openai_base_url(base_url: str) -> str:
    """Translate an Anthropic-compatible root into its OpenAI ``/v1`` root."""
    normalized = base_url.rstrip("/")
    return normalized if normalized.endswith("/v1") else normalized + "/v1"
