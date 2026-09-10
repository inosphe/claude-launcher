"""The harness-neutral description of a backend: one set of values per provider.

A provider entry in ``~/.claunch.yaml`` describes *the backend* -- where it
is, which models it serves, what it costs in context -- in one vocabulary
that no harness owns.  At launch a per-harness translator
(:mod:`translators`) turns that description into the harness's own words:
Claude's ``ANTHROPIC_*`` environment, Codex's ``-c key=value`` overrides,
Pi's in-process provider registration.

::

    providers:
      deepseek:
        api_key: "sk-..."                  # below the profile's set-token secret
        endpoints:                         # per *protocol*, a fact of the backend
          anthropic: https://api.deepseek.com/anthropic
          openai:    https://api.deepseek.com
        models:                            # role -> the id the backend accepts
          default: deepseek-flash
          small:   deepseek-flash
          large:   deepseek-v4-pro
        context_window: 1000000
        auto_compact_at: 900000
        harness_options:                   # the one harness-keyed place
          claude:
            model_tag: "[1m]"              # only needed while context_window is unknown
            env: {CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS: "1"}
          codex:
            config: {model_reasoning_effort: xhigh}
          pi:
            settings: {hideThinkingBlock: true}
            tools: {full_read: false}      # switch a claunch builtin tool off

    profiles:
      work:
        provider: deepseek
        models: {default: deepseek-v4-pro}   # same field names, one layer up
        auto_compact_at: 600000
        harness_options: {...}

A profile overlays ``models``, ``context_window``, ``auto_compact_at`` and
``harness_options`` on its provider (root ancestor first, the profile itself
last).  ``api_key`` and ``endpoints`` identify the backend and stay on the
provider.

The pre-schema form -- a provider ``env:`` of Claude-vocabulary variables --
is still read: :func:`from_legacy_env` reverse-translates it into a spec so
every other harness can use such a provider, while the Claude harness keeps
receiving the recorded variables verbatim (``legacy_env``) until
``claunch migrate-config`` rewrites the file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Dict, Iterable, Optional, Tuple
from urllib.parse import urlsplit

#: Model roles, in the order harnesses that take a flat list receive them.
MODEL_ROLES: Tuple[str, ...] = ("default", "small", "large", "subagent")

#: Protocol names an ``endpoints`` map may carry.
PROTOCOLS: Tuple[str, ...] = ("anthropic", "openai")

#: Which ``harness_options.<harness>`` keys each packaged translator accepts.
HARNESS_OPTION_CHANNELS: Dict[str, Tuple[str, ...]] = {
    "claude": ("env", "model_tag"),
    "codex": ("env", "config"),
    "pi": ("env", "settings", "tools"),
}

#: Provider/profile fields that make up the spec (besides ``harness_options``).
SPEC_FIELDS: Tuple[str, ...] = (
    "api_key",
    "endpoints",
    "models",
    "context_window",
    "auto_compact_at",
)
#: The subset a profile entry may overlay.
PROFILE_SPEC_FIELDS: Tuple[str, ...] = (
    "models",
    "context_window",
    "auto_compact_at",
)
HARNESS_OPTIONS_FIELD = "harness_options"

#: Claude Code's context-window decoration on a model id (``name[1m]``).
MODEL_TAG_RE = re.compile(r"(\[[^\]]*\])\s*$")

# --- Claude's vocabulary, used by the reverse translation of legacy ``env``.
CLAUDE_BASE_URL = "ANTHROPIC_BASE_URL"
CLAUDE_AUTH_TOKEN = "ANTHROPIC_AUTH_TOKEN"
CLAUDE_COMPACT_WINDOW = "CLAUDE_CODE_AUTO_COMPACT_WINDOW"
#: role -> the Claude variables that carry it, primary first.
CLAUDE_MODEL_VARS: Dict[str, Tuple[str, ...]] = {
    "default": ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL"),
    "small": ("ANTHROPIC_DEFAULT_HAIKU_MODEL",),
    "large": ("ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_FABLE_MODEL"),
    "subagent": ("CLAUDE_CODE_SUBAGENT_MODEL",),
}
CLAUDE_MODEL_KEYS: Tuple[str, ...] = tuple(
    key for keys in CLAUDE_MODEL_VARS.values() for key in keys
) + ("ANTHROPIC_SMALL_FAST_MODEL",)
#: Empty pins the runner enforces itself; a legacy ``env`` carried them by
#: convention and the migration drops them.
CLAUDE_REDUNDANT_PINS: Dict[str, str] = {
    "ANTHROPIC_API_KEY": "",
    "CLAUDE_CODE_OAUTH_TOKEN": "",
}


class SpecError(Exception):
    """A provider or profile entry that does not fit the schema."""


@dataclass(frozen=True)
class ProviderSpec:
    """One backend, described without reference to any harness."""

    api_key: Optional[str] = None
    endpoints: Dict[str, str] = field(default_factory=dict)
    models: Dict[str, str] = field(default_factory=dict)
    context_window: Optional[int] = None
    auto_compact_at: Optional[int] = None
    #: ``{harness: {channel: value}}`` -- validated against
    #: :data:`HARNESS_OPTION_CHANNELS` for packaged harnesses.
    harness_options: Dict[str, Dict[str, object]] = field(default_factory=dict)
    #: The recorded Claude-vocabulary ``env`` of a not-yet-migrated provider.
    #: When set, the Claude translator returns it verbatim.
    legacy_env: Optional[Dict[str, str]] = None

    # -- convenience -------------------------------------------------------
    def endpoint(self, protocol: str) -> Optional[str]:
        value = str(self.endpoints.get(protocol) or "").strip()
        return value or None

    def model(self, role: str) -> Optional[str]:
        """The id for ``role`` with the role fallbacks applied."""
        value = str(self.models.get(role) or "").strip()
        if value:
            return value
        if role == "subagent":
            return self.model("small")
        return None

    def model_list(self) -> Tuple[str, ...]:
        """Distinct model ids, default first, in role order."""
        out = []
        for role in MODEL_ROLES:
            value = self.model(role)
            if value and value not in out:
                out.append(value)
        return tuple(out)

    def options(self, harness: str) -> Dict[str, object]:
        block = self.harness_options.get(harness)
        return dict(block) if isinstance(block, dict) else {}

    def option_env(self, harness: str) -> Dict[str, str]:
        env = self.options(harness).get("env")
        return (
            {str(k): str(v) for k, v in env.items()} if isinstance(env, dict) else {}
        )

    def is_empty(self) -> bool:
        return not (
            self.api_key
            or self.endpoints
            or self.models
            or self.context_window
            or self.auto_compact_at
            or self.harness_options
            or self.legacy_env
        )

    def describes_backend(self) -> bool:
        """Whether this spec points somewhere other than plain Anthropic."""
        return bool(self.api_key or self.endpoints or self.legacy_env)


# --- parsing ----------------------------------------------------------------


def _int_or_none(value, what: str) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise SpecError(f"{what} must be a whole number of tokens, not {value!r}")
    if number <= 0:
        raise SpecError(f"{what} must be positive, not {number}")
    return number


def _str_map(value, what: str, allowed: Optional[Iterable[str]] = None) -> Dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SpecError(f"{what} must be a mapping")
    out: Dict[str, str] = {}
    allowed_set = set(allowed) if allowed is not None else None
    for key, item in value.items():
        name = str(key)
        if allowed_set is not None and name not in allowed_set:
            raise SpecError(
                f"{what}.{name} is not a known key (expected one of "
                f"{', '.join(sorted(allowed_set))})"
            )
        text = str(item or "").strip()
        if text:
            out[name] = text
    return out


def parse_harness_options(value, what: str) -> Dict[str, Dict[str, object]]:
    """Validate a ``harness_options`` block; unknown channels are errors."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SpecError(f"{what} must be a mapping of harness name -> options")
    out: Dict[str, Dict[str, object]] = {}
    for harness, block in value.items():
        name = str(harness)
        if block is None:
            continue
        if not isinstance(block, dict):
            raise SpecError(f"{what}.{name} must be a mapping")
        channels = HARNESS_OPTION_CHANNELS.get(name)
        cleaned: Dict[str, object] = {}
        for channel, item in block.items():
            cname = str(channel)
            if channels is not None and cname not in channels:
                raise SpecError(
                    f"{what}.{name}.{cname} is not accepted by the {name} "
                    f"translator (it takes: {', '.join(channels)})"
                )
            if cname in ("env", "config", "settings"):
                if not isinstance(item, dict):
                    raise SpecError(f"{what}.{name}.{cname} must be a mapping")
                cleaned[cname] = dict(item)
            elif cname == "tools":
                # {tool name: on/off} -- the builtin tools of that harness.
                if not isinstance(item, dict) or not all(
                    isinstance(v, bool) for v in item.values()
                ):
                    raise SpecError(
                        f"{what}.{name}.{cname} must map tool names to true/false"
                    )
                cleaned[cname] = dict(item)
            elif cname == "model_tag":
                cleaned[cname] = "" if item is None else str(item)
            else:
                cleaned[cname] = item
        out[name] = cleaned
    return out


def from_entry(entry: dict, what: str, *, profile: bool = False) -> ProviderSpec:
    """Parse the schema fields of one provider (or profile) entry.

    ``profile=True`` restricts the accepted fields to
    :data:`PROFILE_SPEC_FIELDS` and never reads ``env`` (a profile's legacy
    ``env`` is applied by the runner as Claude-vocabulary overrides, exactly
    as before the schema).
    """
    entry = entry if isinstance(entry, dict) else {}
    allowed = PROFILE_SPEC_FIELDS if profile else SPEC_FIELDS
    for name in SPEC_FIELDS:
        if name in entry and name not in allowed:
            raise SpecError(f"{what}.{name} belongs on the provider, not a profile")
    spec = ProviderSpec(
        api_key=(str(entry.get("api_key") or "").strip() or None)
        if "api_key" in allowed
        else None,
        endpoints=_str_map(entry.get("endpoints"), f"{what}.endpoints", PROTOCOLS)
        if "endpoints" in allowed
        else {},
        models=_str_map(entry.get("models"), f"{what}.models", MODEL_ROLES),
        context_window=_int_or_none(
            entry.get("context_window"), f"{what}.context_window"
        ),
        auto_compact_at=_int_or_none(
            entry.get("auto_compact_at"), f"{what}.auto_compact_at"
        ),
        harness_options=parse_harness_options(
            entry.get(HARNESS_OPTIONS_FIELD), f"{what}.{HARNESS_OPTIONS_FIELD}"
        ),
    )
    if (
        spec.context_window
        and spec.auto_compact_at
        and spec.auto_compact_at > spec.context_window
    ):
        raise SpecError(
            f"{what}.auto_compact_at ({spec.auto_compact_at}) exceeds "
            f"context_window ({spec.context_window})"
        )
    if not profile:
        legacy = entry.get("env")
        if isinstance(legacy, dict) and legacy:
            legacy_env = {str(k): str(v) for k, v in legacy.items()}
            reverse = from_legacy_env(legacy_env)
            # Schema fields written next to a legacy env win over what the
            # env implies; the env itself still reaches Claude verbatim.
            spec = overlay(reverse, spec)
            spec = replace(spec, legacy_env=legacy_env)
    return spec


def overlay(base: ProviderSpec, top: ProviderSpec) -> ProviderSpec:
    """``top``'s set fields over ``base``; mappings merge key by key."""
    options: Dict[str, Dict[str, object]] = {
        k: dict(v) for k, v in base.harness_options.items()
    }
    for harness, block in top.harness_options.items():
        merged = options.setdefault(harness, {})
        for channel, value in block.items():
            if isinstance(value, dict) and isinstance(merged.get(channel), dict):
                merged[channel] = {**merged[channel], **value}
            else:
                merged[channel] = value
    return ProviderSpec(
        api_key=top.api_key or base.api_key,
        endpoints={**base.endpoints, **top.endpoints},
        models={**base.models, **top.models},
        context_window=top.context_window or base.context_window,
        auto_compact_at=top.auto_compact_at or base.auto_compact_at,
        harness_options=options,
        legacy_env=top.legacy_env if top.legacy_env is not None else base.legacy_env,
    )


# --- reverse translation of a legacy Claude env ------------------------------


def split_model_tag(model: str) -> Tuple[str, str]:
    """``deepseek-flash[1m]`` -> ``("deepseek-flash", "[1m]")``."""
    text = str(model or "").strip()
    match = MODEL_TAG_RE.search(text)
    if not match:
        return text, ""
    return text[: match.start()].rstrip(), match.group(1)


def url_has_path(url: str) -> bool:
    try:
        path = urlsplit(url).path
    except ValueError:
        return True
    return bool(path.strip("/"))


def from_legacy_env(env: Dict[str, str]) -> ProviderSpec:
    """Read a Claude-vocabulary ``env`` back into the neutral vocabulary.

    Only what the env states is recorded.  ``endpoints.openai`` is filled
    from the Anthropic URL solely when that URL has no path -- one host
    serving both protocols under the same root is then the only reading;
    a path (DeepSeek's ``/anthropic``) may or may not be shared, so the
    field is left for a person to declare.  A bracket tag shared by every
    model id is lifted into ``harness_options.claude.model_tag``.
    """
    env = {str(k): str(v) for k, v in (env or {}).items()}
    endpoints: Dict[str, str] = {}
    base_url = env.get(CLAUDE_BASE_URL, "").strip()
    if base_url:
        endpoints["anthropic"] = base_url
        if not url_has_path(base_url):
            endpoints["openai"] = base_url
    models: Dict[str, str] = {}
    tags = []
    for role, keys in CLAUDE_MODEL_VARS.items():
        for key in keys:
            value = env.get(key, "").strip()
            if value:
                name, tag = split_model_tag(value)
                models[role] = name
                tags.append(tag)
                break
    options: Dict[str, Dict[str, object]] = {}
    # The role ids are always the bare names: no backend accepts the tag.
    # One tag shared by every id is the Claude decoration and is recorded
    # as such; a mixed set is recorded as "no tag" and the migration pins
    # the tagged variables verbatim for Claude (its outcome check does).
    tag = tags[0] if tags and all(t == tags[0] for t in tags) else ""
    if models.get("subagent") and models["subagent"] == models.get("small"):
        del models["subagent"]
    if tag:
        options.setdefault("claude", {})["model_tag"] = tag
    api_key = env.get(CLAUDE_AUTH_TOKEN, "").strip() or None
    compact = None
    try:
        compact = _int_or_none(env.get(CLAUDE_COMPACT_WINDOW), CLAUDE_COMPACT_WINDOW)
    except SpecError:
        compact = None
    return ProviderSpec(
        api_key=api_key,
        endpoints=endpoints,
        models=models,
        auto_compact_at=compact,
        harness_options=options,
    )
