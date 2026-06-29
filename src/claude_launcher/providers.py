"""Named API providers, configured in the central launcher config file.

A *provider* is a named bundle of environment variables — typically an
``ANTHROPIC_BASE_URL`` plus model overrides and an auth token — that points
Claude Code at a particular API backend (e.g. a third-party GLM endpoint).
Providers are defined and selected in ``~/.claunch.yaml`` (the launcher's source
of truth, see :mod:`store`), which the launcher reads live at launch:

    providers:
      fireworks-glm5p2:
        env:
          ANTHROPIC_BASE_URL: "https://api.fireworks.ai/inference"
          ANTHROPIC_MODEL: "accounts/fireworks/models/glm-5p2"
          ANTHROPIC_AUTH_TOKEN: "fw_..."
          ...
    provider: fireworks-glm5p2        # global default (optional)
    profiles:
      work:
        provider: fireworks-glm5p2    # per-profile override (optional)

The built-in ``default`` provider is plain Anthropic (no overrides); selecting
any other provider layers its env over the profile's own and lets the provider
supply auth instead of the launcher injecting the profile's OAuth token.
"""

from __future__ import annotations

from typing import Dict, Optional

from . import lineage, store
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


def _require_known(name: str) -> str:
    if name != DEFAULT_PROVIDER and name not in registry():
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
    doc = store.load() if doc is None else doc
    for p in reversed(lineage.chain(profile)):  # self first, then up to the root
        sel = _profile_selection(p.name, doc)
        if sel:
            return sel
    return active(doc) or DEFAULT_PROVIDER


def effective_env(profile: Profile) -> Dict[str, str]:
    """Provider env vars that ``run`` should layer on for ``profile``."""
    doc = store.load()
    return provider_env(resolve_name(profile, doc), doc)


# --------------------------------------------------------------------------- #
# writing the selection back to the config file (used by `set-provider`)
# --------------------------------------------------------------------------- #
def set_active(name: str) -> None:
    """Set the global provider. ``default`` resets it (there is no higher level)."""
    _require_known(name)

    def _mutate(doc: dict) -> None:
        if name == DEFAULT_PROVIDER:
            doc.pop("provider", None)
        else:
            doc["provider"] = name

    store.update(_mutate)


def clear_active() -> None:
    """Remove the global provider selection (back to the built-in default)."""
    store.update(lambda doc: doc.pop("provider", None))


def set_profile_selection(profile: Profile, name: str) -> None:
    """Pin ``profile`` to ``name``.

    ``name`` may be any provider including ``default`` — selecting ``default``
    *pins* the profile to plain Anthropic, overriding an ancestor's or the global
    provider. To instead drop the override and inherit, use
    :func:`clear_profile_selection`.
    """
    _require_known(name)
    store.set_profile_field(profile.name, "provider", name)


def clear_profile_selection(profile: Profile) -> None:
    """Remove ``profile``'s provider override so it inherits global/default."""
    store.set_profile_field(profile.name, "provider", None)
