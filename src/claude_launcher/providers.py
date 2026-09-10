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
        harness_options:
          claude: {model_tag: "[1m]"}
    provider: deepseek                # global default (optional)
    profiles:
      work:
        provider: deepseek            # per-profile override (optional)
        models: {default: deepseek-v4-pro}

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

from typing import Dict, Optional, Tuple

from . import lineage, profile as profile_mod, provider_spec, store, translators
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


def profile_layers(
    profile: Profile, doc: Optional[dict] = None, *, overlay_models: bool = True
) -> list:
    """The spec layer of each profile in the chain, root ancestor first."""
    doc = store.load() if doc is None else doc
    layers = []
    for p in lineage.chain(profile, doc):
        entry = store.profile_entry(p.name, doc)
        try:
            layer = provider_spec.from_entry(
                entry, f"profiles.{p.name}", profile=True
            )
        except SpecError as exc:
            raise ProviderError(str(exc)) from exc
        if not overlay_models:
            layer = provider_spec.replace(layer, models={})
        layers.append(layer)
    return layers


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
    """Provider spec with the profile chain's overlay applied.

    ``name`` defaults to the profile's resolved provider. ``overlay_models``
    is switched off by the runner when the backend in play is not the
    profile's own (a borrow across providers, an explicit ``--provider``):
    the profile's model pins describe *its* backend and must not be asked
    of another one, while its context/compaction values still apply.
    """
    doc = store.load() if doc is None else doc
    merged = spec(resolve_name(profile, doc) if name is None else name, doc)
    for layer in profile_layers(profile, doc, overlay_models=overlay_models):
        merged = provider_spec.overlay(merged, layer)
    return merged


def claude_env(
    profile: Profile,
    name: Optional[str] = None,
    doc: Optional[dict] = None,
    *,
    overlay_models: bool = True,
) -> Dict[str, str]:
    """What the Claude harness receives for ``profile`` on provider ``name``.

    The provider's translation, then each profile layer's additions
    (:func:`translators.claude_layered`). On the built-in ``default``
    provider only the profile layers contribute (compaction, model pins).
    """
    doc = store.load() if doc is None else doc
    base = spec(resolve_name(profile, doc) if name is None else name, doc)
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
