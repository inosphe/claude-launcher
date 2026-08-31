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

from . import lineage, providers
from .profile import Profile

ADAPTER = "pi"
PI_PROVIDER_NAME = "claunch-profile"
EXTENSION_FILE = "pi_provider.mjs"

ENV_PROVIDER = "CLAUNCH_PI_PROVIDER"
ENV_BASE_URL = "CLAUNCH_PI_BASE_URL"
ENV_API = "CLAUNCH_PI_API"
ENV_MODELS = "CLAUNCH_PI_MODELS"
ENV_TOKEN_NAME = "CLAUNCH_PI_TOKEN_ENV"
ENV_AUTH_HEADER = "CLAUNCH_PI_AUTH_HEADER"
PROJECTION_ENV = frozenset(
    {
        ENV_PROVIDER,
        ENV_BASE_URL,
        ENV_API,
        ENV_MODELS,
        ENV_TOKEN_NAME,
        ENV_AUTH_HEADER,
    }
)

_BASE_URL_KEY = "ANTHROPIC_BASE_URL"
_MODEL_KEYS = (
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_FABLE_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
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


def extension_path() -> Path:
    """Path to the extension shipped beside this module."""
    path = Path(__file__).with_name(EXTENSION_FILE)
    if not path.is_file():
        raise PiProviderError(
            f"the packaged Pi provider extension is missing: {path}"
        )
    return path


def resolve(profile: Profile, harness) -> Optional[Projection]:
    """Resolve the selected claunch provider into Pi's provider vocabulary."""
    if harness.provider_adapter != ADAPTER:
        return None
    name = providers.resolve_name(profile)
    if providers.uses_anthropic_oauth(name):
        return None

    backend = providers.provider_env(name)
    # Profile env is the final layer for its selected backend, matching
    # runner.child_env. Only the endpoint/model vocabulary is read here; auth
    # continues to come from the profile token file through token_env.
    profile_env = lineage.effective_env(profile)
    for key in (_BASE_URL_KEY, *_MODEL_KEYS):
        if key in profile_env:
            backend[key] = profile_env[key]

    anthropic_base_url = str(backend.get(_BASE_URL_KEY) or "").strip()
    if not anthropic_base_url:
        raise PiProviderError(
            f"provider {name!r} cannot be used by the Pi adapter: "
            f"{_BASE_URL_KEY} is not configured"
        )

    models = []
    for key in _MODEL_KEYS:
        model = str(backend.get(key) or "").strip()
        if model and model not in models:
            models.append(model)
    if not models:
        raise PiProviderError(
            f"provider {name!r} cannot be used by the Pi adapter: "
            "no Anthropic model is configured"
        )
    if not harness.token_env:
        raise PiProviderError(
            f"harness {harness.name!r} has no token_env for the Pi adapter"
        )
    return Projection(
        # The claunch provider vocabulary remains ANTHROPIC_*. Pi uses the
        # same backend through its OpenAI Chat Completions endpoint because
        # local Qwen-compatible Anthropic streams may complete without
        # producing content blocks Pi can render.
        base_url=_openai_base_url(anthropic_base_url),
        api="openai-completions",
        models=tuple(models),
        default_model=models[0],
        token_env=harness.token_env,
        # The OpenAI client derives Authorization: Bearer from this API key;
        # an explicit header would duplicate that native route.
        auth_header=False,
    )


def apply_env(profile: Profile, harness, env: dict) -> None:
    """Replace inherited adapter state with this profile's projection."""
    for key in PROJECTION_ENV:
        env.pop(key, None)
    projection = resolve(profile, harness)
    if projection is None:
        return
    env.update(
        {
            ENV_PROVIDER: PI_PROVIDER_NAME,
            ENV_BASE_URL: projection.base_url,
            ENV_API: projection.api,
            ENV_MODELS: json.dumps(
                projection.models, ensure_ascii=False, separators=(",", ":")
            ),
            ENV_TOKEN_NAME: projection.token_env,
            ENV_AUTH_HEADER: "1" if projection.auth_header else "0",
        }
    )


def launch_args(profile: Profile, harness, args: Sequence[str]) -> list[str]:
    """Prepend Pi's provider extension and default model when required.

    A caller's explicit ``--provider``, ``--model`` or ``--models`` owns model
    selection for that launch, including flags forwarded after
    ``claunch run PROFILE:pi``. Flags after ``--`` are prompt text and do not
    suppress the projection.
    """
    forwarded = list(args)
    projection = resolve(profile, harness)
    if projection is None:
        return forwarded
    adapter_args = ["--extension", str(extension_path())]
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
