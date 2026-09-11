"""Per-harness translation of a :class:`~provider_spec.ProviderSpec`.

The spec says what the backend is; a translator says it in one harness's
words.  Each returns a :class:`Translation` -- environment variables, launch
arguments, and files under the harness's profile home -- and a list of
``notes`` for the launcher to print when a value could not be carried.

=================  ==================================  ========================
spec field         claude                              codex / pi
=================  ==================================  ========================
api_key            ANTHROPIC_AUTH_TOKEN                pi: token_env (runner)
endpoints          anthropic -> ANTHROPIC_BASE_URL     pi: openai -> extension
models             ANTHROPIC_MODEL & friends           codex: (session --model)
                                                       pi: extension model list
context_window     ``[1m]`` tag when >= 1M             codex: -c model_context_window
                                                       pi: extension contextWindow
auto_compact_at    CLAUDE_CODE_AUTO_COMPACT_WINDOW     codex: -c model_auto_compact_token_limit
                                                       pi: settings.json compaction.reserveTokens
harness_options    env, model_tag                      codex: env, config (-c)
                                                       pi: env, settings (file)
=================  ==================================  ========================

Pi's provider registration itself (endpoint, models, context window) lives in
:mod:`pi_provider`, which reads the same spec; :func:`pi` here carries the
remaining channels.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

from .provider_spec import (
    CLAUDE_AUTH_TOKEN,
    CLAUDE_BASE_URL,
    CLAUDE_COMPACT_WINDOW,
    CLAUDE_MODEL_VARS,
    MODEL_ROLES,
    ProviderSpec,
    overlay,
    split_model_tag,
)

#: ``context_window`` at or above which Claude Code's 1M-context tag applies.
CLAUDE_LONG_CONTEXT = 1_000_000
CLAUDE_LONG_CONTEXT_TAG = "[1m]"

#: Relative to the Pi profile home (``PI_CODING_AGENT_DIR``).
PI_SETTINGS_FILE = "settings.json"


@dataclass
class Translation:
    """What one harness receives for a spec."""

    env: Dict[str, str] = field(default_factory=dict)
    args: List[str] = field(default_factory=list)
    #: ``{relative file: {dotted.key: value}}`` -- merged into the file, the
    #: rest of it untouched.
    files: Dict[str, Dict[str, object]] = field(default_factory=dict)
    #: Human lines: a value that this harness has no way to carry.
    notes: List[str] = field(default_factory=list)
    #: Spec fields this harness cannot launch without.
    missing: List[str] = field(default_factory=list)


def for_harness(name: str, spec: ProviderSpec) -> Translation:
    """Dispatch on a packaged harness name; others get options.env only."""
    fn = _TRANSLATORS.get(name)
    if fn is not None:
        return fn(spec)
    return Translation(env=spec.option_env(name))


# --- claude -----------------------------------------------------------------


def claude_model_tag(spec: ProviderSpec) -> str:
    """The decoration Claude's model ids get: declared, else from the window."""
    declared = spec.options("claude").get("model_tag")
    if declared is not None:
        return str(declared)
    if spec.context_window and spec.context_window >= CLAUDE_LONG_CONTEXT:
        return CLAUDE_LONG_CONTEXT_TAG
    return ""


def claude(spec: ProviderSpec) -> Translation:
    """Claude Code's ``ANTHROPIC_*`` / ``CLAUDE_CODE_*`` environment."""
    out = Translation()
    if spec.legacy_env is not None:
        # Not yet migrated: the recorded variables, exactly as written.
        out.env.update(spec.legacy_env)
        out.env.update(spec.option_env("claude"))
        return out
    anthropic = spec.endpoint("anthropic")
    if anthropic:
        out.env[CLAUDE_BASE_URL] = anthropic
    if spec.api_key:
        out.env[CLAUDE_AUTH_TOKEN] = spec.api_key
    tag = claude_model_tag(spec)
    for role, keys in CLAUDE_MODEL_VARS.items():
        value = spec.model(role)
        if not value:
            continue
        name, own_tag = split_model_tag(value)
        decorated = value if own_tag else f"{name}{tag}"
        for key in keys:
            out.env[key] = decorated
    if spec.auto_compact_at:
        out.env[CLAUDE_COMPACT_WINDOW] = str(spec.auto_compact_at)
    out.env.update(spec.option_env("claude"))
    return out


def _claude_model_vars(ctx: ProviderSpec, roles: Iterable[str]) -> Dict[str, str]:
    tag = claude_model_tag(ctx)
    env: Dict[str, str] = {}
    for role in roles:
        value = ctx.model(role)
        if not value:
            continue
        name, own_tag = split_model_tag(value)
        decorated = value if own_tag else f"{name}{tag}"
        for key in CLAUDE_MODEL_VARS[role]:
            env[key] = decorated
    return env


def claude_layer(ctx: ProviderSpec, layer: ProviderSpec) -> Dict[str, str]:
    """The variables one profile layer adds over what lower layers produced.

    ``ctx`` is the spec with this layer already overlaid (for the tag and
    the role fallbacks); only the roles *this* layer sets are re-emitted, so
    a profile that pins ``small`` does not silently move ``subagent`` too --
    that stays whatever the provider translated it to. A layer that changes
    the tag (``context_window`` or ``model_tag``) re-emits every role.
    """
    retag = bool(layer.context_window) or "model_tag" in layer.options("claude")
    if retag:
        roles = MODEL_ROLES
    else:
        # ``xlarge`` that nobody set follows ``large`` wherever it goes, so a
        # layer moving ``large`` moves Fable's variable too (as one ``large``
        # used to). ``subagent`` deliberately does not follow ``small``.
        follow = "xlarge" if "large" in layer.models and not ctx.models.get("xlarge") else None
        roles = tuple(r for r in MODEL_ROLES if r in layer.models or r == follow)
    env = _claude_model_vars(ctx, roles)
    if layer.auto_compact_at:
        env[CLAUDE_COMPACT_WINDOW] = str(layer.auto_compact_at)
    env.update(layer.option_env("claude"))
    return env


def claude_layered(base: ProviderSpec, layers: Iterable[ProviderSpec]) -> Dict[str, str]:
    """Provider translation, then each profile layer's additions in order.

    Translating layer by layer (instead of overlaying the specs and
    translating once) keeps the precedence the raw ``env`` form had: a
    provider's kept-verbatim variable sits *below* a profile's role field,
    and a profile's own raw variable sits above everything.
    """
    env = dict(claude(base).env)
    ctx = base
    for layer in layers:
        ctx = overlay(ctx, layer)
        env.update(claude_layer(ctx, layer))
    return env


# --- codex ------------------------------------------------------------------


def toml_literal(value) -> str:
    """Render a scalar the way Codex's ``-c key=value`` expects it."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def codex(spec: ProviderSpec) -> Translation:
    """Codex's ``-c`` overrides and environment.

    Model selection stays with the session's own ``--model`` (the harness
    declaration's ``model_args``); a custom backend for Codex is not
    translated yet, so ``api_key``/``endpoints`` are noted, not applied.
    """
    out = Translation(env=spec.option_env("codex"))
    if spec.context_window:
        out.args += ["-c", f"model_context_window={spec.context_window}"]
    if spec.auto_compact_at:
        out.args += ["-c", f"model_auto_compact_token_limit={spec.auto_compact_at}"]
    config = spec.options("codex").get("config")
    if isinstance(config, dict):
        for key, value in config.items():
            out.args += ["-c", f"{key}={toml_literal(value)}"]
    if spec.endpoints or spec.api_key:
        out.notes.append(
            "codex: api_key/endpoints are not translated for this harness "
            "(it runs on its own login); models, context_window and "
            "auto_compact_at are"
        )
    return out


# --- pi ---------------------------------------------------------------------


def pi_reserve_tokens(spec: ProviderSpec) -> Optional[int]:
    """Pi compacts when ``tokens > contextWindow - reserveTokens``."""
    if not (spec.context_window and spec.auto_compact_at):
        return None
    reserve = spec.context_window - spec.auto_compact_at
    return reserve if reserve > 0 else None


def pi(spec: ProviderSpec) -> Translation:
    """Pi's environment and ``settings.json`` keys (registration is elsewhere)."""
    out = Translation(env=spec.option_env("pi"))
    settings: Dict[str, object] = {}
    reserve = pi_reserve_tokens(spec)
    if reserve is not None:
        settings["compaction.reserveTokens"] = reserve
    elif spec.auto_compact_at and not spec.context_window:
        out.notes.append(
            "pi: auto_compact_at needs context_window to be translated "
            "(compaction.reserveTokens = context_window - auto_compact_at)"
        )
    declared = spec.options("pi").get("settings")
    if isinstance(declared, dict):
        for key, value in declared.items():
            settings[str(key)] = value
    if settings:
        out.files[PI_SETTINGS_FILE] = settings
    return out


_TRANSLATORS = {"claude": claude, "codex": codex, "pi": pi}


def set_dotted(doc: dict, key: str, value) -> None:
    """``set_dotted(d, "compaction.reserveTokens", 5)`` -> ``d["compaction"]["reserveTokens"] = 5``."""
    parts = [p for p in str(key).split(".") if p]
    if not parts:
        return
    node = doc
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value
