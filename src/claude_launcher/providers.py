"""Named API providers, configured in the central launcher config file.

A *provider* names a backend: where it is, which models it serves, how much
context they carry. It is written once, in a vocabulary no harness owns
(:mod:`provider_spec`), and translated at launch into each harness's own
words (:mod:`translators`)::

    providers:
      deepseek:
        api_key: "sk-..."
        endpoints:
          anthropic: https://api.deepseek.com/anthropic
          openai:    https://api.deepseek.com
        models: {default: deepseek-flash, small: deepseek-flash, large: deepseek-v4-pro}
        context_window: 1000000
        auto_compact_at: 900000
        reasoning_effort: high
        openai_reasoning_format: deepseek
        harness_options:
          claude: {model_tag: "[1m]"}
    provider: deepseek                # global default (optional)
    profiles:
      work:
        provider: deepseek            # per-profile override (optional)
        models: {default: deepseek-v4-pro}
        reasoning_effort: high         # may override the provider default

The pre-schema form -- ``env:`` holding Claude's ``ANTHROPIC_*`` variables --
is still read (reverse-translated for other harnesses, passed verbatim to
Claude) until ``claunch migrate-config`` rewrites it.

The built-in ``default`` provider is plain Anthropic (no overrides). Any other
provider's Claude translation is a *low-priority backend default*: it sits
above the launching shell but below the profile's own ``env`` (applied
last), so a profile key always beats a provider key. Selecting a non-default
provider also swaps auth: the profile's single ``set-token`` secret (own,
inherited, or borrowed) is exported through the Claude harness's packaged
``ANTHROPIC_AUTH_TOKEN`` route instead of injecting ``CLAUDE_CODE_OAUTH_TOKEN``.
"""

from __future__ import annotations

import os
from typing import Dict, Optional, Tuple

from . import lineage, profile as profile_mod, provider_spec, store, template, translators
from .profile import Profile
from .provider_spec import ProviderSpec, SpecError

#: The built-in no-override provider — plain Anthropic, launcher injects token.
DEFAULT_PROVIDER = "default"

#: Explicit named Anthropic provider. It may carry policy metadata, but its
#: service identity remains Anthropic.
CLAUDE_PROVIDER = "claude"

#: Provider services are independent of harness names. They define
#: authentication and usage-reporting behaviour.
ANTHROPIC_SERVICE = "anthropic"
CUSTOM_SERVICE = "custom"


class ProviderError(Exception):
    """Raised for unknown providers or a malformed config file."""


def entries(doc: Optional[dict] = None) -> Dict[str, dict]:
    """Raw provider entries from the config file (``{}`` for the built-ins)."""
    doc = store.load() if doc is None else doc
    out: Dict[str, dict] = {DEFAULT_PROVIDER: {}, CLAUDE_PROVIDER: {}}
    raw = doc.get("providers")
    if isinstance(raw, dict):
        for name, entry in raw.items():
            out[str(name)] = dict(entry) if isinstance(entry, dict) else {}
    return out


def spec(name: str, doc: Optional[dict] = None) -> ProviderSpec:
    """The harness-neutral description of provider ``name``."""
    if name in (DEFAULT_PROVIDER, CLAUDE_PROVIDER):
        return ProviderSpec()
    reg = entries(doc)
    if name not in reg:
        raise ProviderError(f"unknown provider {name!r} (see 'claunch providers')")
    try:
        return provider_spec.from_entry(reg[name], f"providers.{name}")
    except SpecError as exc:
        raise ProviderError(str(exc)) from exc


def _layers(
    profile: Profile, doc: dict, *, overlay_models: bool = True
) -> Tuple[ProviderSpec, list]:
    """The template's spec layer and each chain profile's, clears applied.

    The chain is root ancestor first, the profile itself last. A key written
    with no value in a chain entry clears what the template and the
    ancestors would give (see :func:`template.resolve_layers`).
    """
    raw = [("template", template.live_layer(doc))] + [
        (f"profiles.{p.name}", store.profile_entry(p.name, doc))
        for p in lineage.chain(profile, doc)
    ]
    # Normalize the key alias before applying null clears across ancestors.
    normalized = []
    for what, entry in raw:
        entry = dict(entry)
        if "api-key" in entry:
            entry.setdefault("api_key", entry.pop("api-key"))
        normalized.append((what, entry))
    raw = normalized
    resolved = template.resolve_layers([entry for _, entry in raw])
    layers = []
    for (what, _), entry in zip(raw, resolved):
        try:
            layer = provider_spec.from_entry(entry, what, profile=True)
        except SpecError as exc:
            raise ProviderError(str(exc)) from exc
        if not overlay_models and what != "template":
            layer = provider_spec.replace(layer, models={})
        layers.append(layer)
    return layers[0], layers[1:]


def profile_layers(
    profile: Profile, doc: Optional[dict] = None, *, overlay_models: bool = True
) -> list:
    """The spec layer of each profile in the chain, root ancestor first.

    The template is not among them: it sits *under* the provider (see
    :func:`base_spec`).
    """
    doc = store.load() if doc is None else doc
    return _layers(profile, doc, overlay_models=overlay_models)[1]


def template_layer(profile: Profile, doc: Optional[dict] = None) -> ProviderSpec:
    """The template's spec layer as ``profile`` sees it (its clears applied)."""
    doc = store.load() if doc is None else doc
    return _layers(profile, doc)[0]


def base_spec(
    profile: Profile, name: Optional[str] = None, doc: Optional[dict] = None
) -> ProviderSpec:
    """The provider's spec over the template: what the profile chain sits on.

    Order, lowest first: template < provider < root ancestor < ... < profile.
    The template is the general default and fills only what the provider
    leaves unset; a provider describes one backend and its values win.
    """
    doc = store.load() if doc is None else doc
    provider = spec(resolve_name(profile, doc) if name is None else name, doc)
    return provider_spec.overlay(template_layer(profile, doc), provider)


def profile_overlay(profile: Profile, doc: Optional[dict] = None) -> ProviderSpec:
    """The spec fields a profile chain adds, merged root ancestor first."""
    merged = ProviderSpec()
    for layer in profile_layers(profile, doc):
        merged = provider_spec.overlay(merged, layer)
    return merged


def spec_for(
    profile: Profile,
    name: Optional[str] = None,
    doc: Optional[dict] = None,
    *,
    overlay_models: bool = True,
) -> ProviderSpec:
    """Provider spec with the template under it and the profile chain over it.

    ``name`` defaults to the profile's resolved provider. ``overlay_models``
    is switched off by the runner when the backend in play is not the
    profile's own (a borrow across providers, an explicit ``--provider``):
    the profile's model pins describe *its* backend and must not be asked
    of another one, while its context/compaction values still apply.
    """
    doc = store.load() if doc is None else doc
    merged = base_spec(profile, name, doc)
    for layer in profile_layers(profile, doc, overlay_models=overlay_models):
        merged = provider_spec.overlay(merged, layer)
    return merged


def launch_spec(
    profile: Profile,
    name: Optional[str] = None,
    doc: Optional[dict] = None,
    *,
    borrow: Optional[Profile] = None,
) -> ProviderSpec:
    """The spec a launch acts on: the running profile's, with a borrow's models.

    Every field the running profile contributes — context window, compaction
    threshold, reasoning effort — stays its own: a borrow swaps whose
    credential and backend the launch uses, not how much context it keeps.
    The model table is the exception, because those ids name the backend.

    Without ``borrow`` this is :func:`spec_for` unchanged. With one, the table
    is read from the lender, role for role — what makes ``haiku`` reach the
    lender's ``small`` and ``opus`` its ``large``. Declared harnesses (Pi,
    Codex) go through this too, so their model lists follow one rule.

    ``lender``'s own pins apply only while the backend in play is the
    lender's own provider, exactly as they would for a launch of that profile:
    an explicit ``--provider`` swings the backend away from both profiles and
    leaves the provider's table to answer.
    """
    doc = store.load() if doc is None else doc
    own = resolve_name(profile, doc)
    provider = (resolve_name(borrow, doc) if borrow is not None else own) if name is None else name
    spec = spec_for(profile, provider, doc, overlay_models=(provider == own))
    if borrow is None:
        return spec
    models = spec_for(
        borrow,
        provider,
        doc,
        overlay_models=(provider == resolve_name(borrow, doc)),
    ).models
    return provider_spec.replace(spec, models=dict(models))


def compact_problem(
    profile: Profile, name: Optional[str] = None, doc: Optional[dict] = None
) -> Optional[str]:
    """Why ``profile`` on provider ``name`` cannot compact where it is told to.

    ``None`` when ``auto_compact_at`` fits in ``context_window`` (or either is
    not set). Otherwise one sentence naming both values and the layer each
    came from, so the fix is obvious: the threshold would never be reached,
    or reached only after the backend has already run out of context.
    """
    doc = store.load() if doc is None else doc
    provider = resolve_name(profile, doc) if name is None else name
    merged = spec_for(profile, provider, doc)
    window, at = merged.context_window, merged.auto_compact_at
    if not (window and at and at > window):
        return None
    chain = lineage.chain(profile, doc)
    tmpl, layers = _layers(profile, doc)
    named = [("the template", tmpl), (f"provider {provider!r}", spec(provider, doc))] + [
        (f"profile {p.name!r}", layer) for p, layer in zip(chain, layers)
    ]

    def source(field: str) -> str:
        for what, layer in reversed(named):
            if getattr(layer, field):
                return what
        return "unknown"

    return (
        f"profile {profile.name!r}: auto_compact_at {at} (from "
        f"{source('auto_compact_at')}) exceeds context_window {window} (from "
        f"{source('context_window')}); lower auto_compact_at, or write "
        "'auto_compact_at:' with no value on the profile to drop the "
        "template's"
    )


def compact_problems(doc: Optional[dict] = None) -> Dict[str, str]:
    """:func:`compact_problem` for every profile, keyed by profile name.

    A profile whose configuration cannot be read at all is reported by the
    commands that use it; it is left out here.
    """
    doc = store.load() if doc is None else doc
    out: Dict[str, str] = {}
    for p in profile_mod.list_all():
        try:
            problem = compact_problem(p, doc=doc)
        except (ProviderError, lineage.LineageError):
            continue
        if problem:
            out[p.name] = problem
    return out


def claude_env(
    profile: Profile,
    name: Optional[str] = None,
    doc: Optional[dict] = None,
    *,
    overlay_models: bool = True,
) -> Dict[str, str]:
    """What the Claude harness receives for ``profile`` on provider ``name``.

    The translation of the provider over the template, then each profile
    layer's additions (:func:`translators.claude_layered`).
    """
    doc = store.load() if doc is None else doc
    base = base_spec(profile, name, doc)
    return translators.claude_layered(
        base, profile_layers(profile, doc, overlay_models=overlay_models)
    )


def registry(doc: Optional[dict] = None) -> Dict[str, Dict[str, str]]:
    """Map of provider name -> its Claude-vocabulary env (built-ins: ``{}``).

    Kept as the shape every listing and key-collecting caller already reads;
    the values are now the Claude translation of each provider's spec.
    """
    doc = store.load() if doc is None else doc
    out: Dict[str, Dict[str, str]] = {}
    for name in entries(doc):
        try:
            out[name] = translators.claude(spec(name, doc)).env
        except ProviderError:
            out[name] = {}
    return out


def service(name: str, doc: Optional[dict] = None) -> str:
    """Return a provider's service identity.

    ``default`` and the named ``claude`` provider use Anthropic. Other
    providers may declare ``service: NAME`` in their provider specification;
    missing metadata retains the historical custom-backend behaviour. Future
    services, such as OpenAI, therefore extend this axis without being
    conflated with a harness selection.
    """
    if name in (DEFAULT_PROVIDER, CLAUDE_PROVIDER):
        return ANTHROPIC_SERVICE
    doc = store.load() if doc is None else doc
    raw = doc.get("providers")
    spec = raw.get(name) if isinstance(raw, dict) else None
    if not isinstance(spec, dict):
        raise ProviderError(f"unknown provider {name!r} (see 'claunch providers')")
    value = str(spec.get("service") or CUSTOM_SERVICE).strip()
    return value or CUSTOM_SERVICE


def uses_anthropic_oauth(name: str, doc: Optional[dict] = None) -> bool:
    """Whether a provider authenticates through the Anthropic OAuth route."""
    return service(name, doc) == ANTHROPIC_SERVICE


#: Claude Code's switch for how long a prompt-cache entry lives (its setting
#: name is ``promptCacheTtl``; the environment variable wins over the setting).
#: The harness applies it to the main conversation; subagents and background
#: helpers have ``CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL`` of their own. Read
#: from the installed harness (2.1.306), not from a launcher default.
PROMPT_CACHE_TTL_ENV = "CLAUDE_CODE_PROMPT_CACHE_TTL"

#: The two values the harness accepts.
PROMPT_CACHE_TTL_VALUES = ("5m", "1h")

#: What the harness picks when nothing sets the value: a Claude subscription
#: gets the 1-hour cache, an API key (and Bedrock, Vertex, Foundry) the
#: 5-minute one. A subscription in overage falls back to 5 minutes.
PROMPT_CACHE_TTL_SUBSCRIPTION = "1h"
PROMPT_CACHE_TTL_API_KEY = "5m"


def prompt_cache_ttl(
    name: str, doc: Optional[dict] = None, env: Optional[dict] = None
) -> Dict[str, str]:
    """The prompt-cache TTL sessions inherit on ``name``, and why.

    The launcher does not set this value; it reports the one the harness
    resolves, so an operator can tell a 5-minute cache from a 1-hour one
    without reading the harness. Precedence: the environment variable as this
    process sees it (the launcher hands its own environment to sessions as the
    base layer) over the harness's automatic choice for the provider's service.
    A malformed value is ignored, which is what the harness does with it too.

    This reads one environment, not every layer a launch can have: a profile's
    own ``env`` sits *above* the base environment, and the harness's settings
    file (``promptCacheTtl``) can set the value as well, so a session on a
    profile that sets either can still use a different TTL than this answers.
    """
    env = os.environ if env is None else env
    wanted = str(env.get(PROMPT_CACHE_TTL_ENV) or "").strip().lower()
    if wanted in PROMPT_CACHE_TTL_VALUES:
        return {"ttl": wanted, "source": f"environment ({PROMPT_CACHE_TTL_ENV})"}
    if service(name, doc) == ANTHROPIC_SERVICE:
        return {
            "ttl": PROMPT_CACHE_TTL_SUBSCRIPTION,
            "source": "automatic (Claude subscription)",
        }
    return {"ttl": PROMPT_CACHE_TTL_API_KEY, "source": "automatic (API key)"}


def provider_env(name: str, doc: Optional[dict] = None) -> Dict[str, str]:
    """Env vars a provider contributes (``{}`` for ``default``)."""
    if name == DEFAULT_PROVIDER:
        return {}
    reg = registry(doc)
    if name not in reg:
        raise ProviderError(f"unknown provider {name!r} (see 'claunch providers')")
    return dict(reg[name])


def auth_token(name: str, doc: Optional[dict] = None) -> Optional[str]:
    """The provider's declared ``api_key`` (or legacy ``ANTHROPIC_AUTH_TOKEN``).

    This is the *fallback* below a profile's own ``set-token`` secret, so one
    key in the config file serves every harness the provider is used with.
    """
    if name == DEFAULT_PROVIDER:
        return None
    return spec(name, doc).api_key


def _require_known(name: str, doc: Optional[dict] = None) -> str:
    if name != DEFAULT_PROVIDER and name not in registry(doc):
        raise ProviderError(f"unknown provider {name!r} (see 'claunch providers')")
    return name


def active(doc: Optional[dict] = None) -> Optional[str]:
    """The global default provider (top-level ``provider:``), or ``None``."""
    doc = store.load() if doc is None else doc
    name = doc.get("provider")
    return str(name) if name else None


def _profile_selection(profile_name: str, doc: dict) -> Optional[str]:
    sel = store.profile_entry(profile_name, doc).get("provider")
    return str(sel) if sel else None


def resolve_name(profile: Profile, doc: Optional[dict] = None) -> str:
    """Effective provider for ``profile``: own → ancestor → global → default."""
    return resolve_with_source(profile, doc)[0]


def resolve_with_source(
    profile: Profile, doc: Optional[dict] = None
) -> Tuple[str, str]:
    """Effective provider plus a human-readable note on where it came from."""
    doc = store.load() if doc is None else doc
    for p in reversed(lineage.chain(profile, doc)):  # self first, then root
        sel = _profile_selection(p.name, doc)
        if sel:
            if p.name == profile.name:
                return sel, f"set on profile {p.name!r}"
            return sel, f"inherited from profile {p.name!r}"
    name = active(doc)
    if name:
        return name, "global default"
    return DEFAULT_PROVIDER, "built-in default"


def effective_env(profile: Profile) -> Dict[str, str]:
    """Provider env vars that ``run`` should layer on for ``profile``."""
    doc = store.load()
    return provider_env(resolve_name(profile, doc), doc)


# --------------------------------------------------------------------------- #
# writing the selection back to the config file (used by `set-provider`)
# --------------------------------------------------------------------------- #
def set_active(name: str) -> None:
    """Set the global provider. ``default`` resets it (there is no higher level)."""
    doc = store.load()
    _require_known(name, doc)
    if name == DEFAULT_PROVIDER:
        doc.pop("provider", None)
    else:
        doc["provider"] = name
    _validate_profiles(doc, "global provider change")
    store.save(doc)


def clear_active() -> None:
    """Remove the global provider selection (back to the built-in default)."""
    doc = store.load()
    doc.pop("provider", None)
    _validate_profiles(doc, "clearing the global provider")
    store.save(doc)


def set_profile_selection(profile: Profile, name: str) -> None:
    """Pin ``profile`` to ``name``.

    ``name`` may be any provider including ``default`` — selecting ``default``
    *pins* the profile to plain Anthropic, overriding an ancestor's or the global
    provider. To instead drop the override and inherit, use
    :func:`clear_profile_selection`.
    """
    doc = store.load()
    _require_known(name, doc)
    _profile_entry(doc, profile.name)["provider"] = name
    _validate_profile(profile, doc, f"provider {name!r}")
    store.save(doc)


def clear_profile_selection(profile: Profile) -> None:
    """Remove ``profile``'s provider override so it inherits global/default."""
    doc = store.load()
    _profile_entry(doc, profile.name).pop("provider", None)
    _validate_profile(profile, doc, "clearing its provider")
    store.save(doc)


def _profile_entry(doc: dict, name: str) -> dict:
    section = doc.get("profiles")
    if not isinstance(section, dict):
        section = {}
        doc["profiles"] = section
    entry = section.get(name)
    if not isinstance(entry, dict):
        entry = {}
        section[name] = entry
    return entry


def _validate_profile(profile: Profile, doc: dict, action: str) -> None:
    try:
        lineage.effective_harness(profile, doc)
    except lineage.LineageError as exc:
        raise ProviderError(
            f"cannot apply {action} to profile {profile.name!r}: {exc}"
        ) from exc


def _validate_profiles(doc: dict, action: str) -> None:
    failures = []
    for profile in profile_mod.list_all():
        try:
            lineage.effective_harness(profile, doc)
        except lineage.LineageError as exc:
            failures.append(f"{profile.name}: {exc}")
    if failures:
        raise ProviderError(
            f"cannot apply {action}; it would deny active harnesses: "
            + "; ".join(failures)
        )
