"""Named API providers, configured in the central launcher config file.

A *provider* is a named bundle of environment variables — typically an
``ANTHROPIC_BASE_URL`` plus model overrides and an auth token — that points
Claude Code at a particular API backend (e.g. a third-party GLM endpoint).
Providers are defined and selected in ``~/.claunch.yaml`` (the launcher's source
of truth, see :mod:`store`), which the launcher reads live at launch:

    providers:
      fireworks-glm5p2:
        allowed_harnesses: [claude]
        env:
          ANTHROPIC_BASE_URL: "https://api.fireworks.ai/inference"
          ANTHROPIC_MODEL: "accounts/fireworks/models/glm-5p2"
          ANTHROPIC_AUTH_TOKEN: "fw_..."
          ...
    provider: fireworks-glm5p2        # global default (optional)
    profiles:
      work:
        provider: fireworks-glm5p2    # per-profile override (optional)

The built-in ``default`` provider is plain Anthropic (no overrides). Any other
provider contributes its env as a *low-priority backend default*: it sits above
the launching shell but below the profile's own ``env`` (applied last), so a
profile key always beats a provider key — only keys a profile never sets fall
through to the provider's value. Selecting a non-default provider also swaps
auth: the profile's single ``set-token`` secret (own, inherited, or borrowed)
is exported through the Claude harness's packaged ``ANTHROPIC_AUTH_TOKEN``
route instead of injecting ``CLAUDE_CODE_OAUTH_TOKEN``.
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

from . import lineage, profile as profile_mod, store
from .profile import Profile

#: The built-in "no override" provider — plain Anthropic, launcher injects token.
DEFAULT_PROVIDER = "default"


class ProviderError(Exception):
    """Raised for unknown providers or a malformed config file."""


def registry(doc: Optional[dict] = None) -> Dict[str, Dict[str, str]]:
    """Map of provider name -> env, from the config file plus built-in default."""
    doc = store.load() if doc is None else doc
    out: Dict[str, Dict[str, str]] = {DEFAULT_PROVIDER: {}}
    raw = doc.get("providers")
    if isinstance(raw, dict):
        for name, spec in raw.items():
            env = spec.get("env") if isinstance(spec, dict) else None
            out[str(name)] = (
                {str(k): str(v) for k, v in env.items()}
                if isinstance(env, dict)
                else {}
            )
    return out


def provider_env(name: str, doc: Optional[dict] = None) -> Dict[str, str]:
    """Env vars a provider contributes (``{}`` for ``default``)."""
    if name == DEFAULT_PROVIDER:
        return {}
    reg = registry(doc)
    if name not in reg:
        raise ProviderError(f"unknown provider {name!r} (see 'claunch providers')")
    return dict(reg[name])


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
