"""Named API providers, configured in the global launcher config file.

A *provider* is a named bundle of environment variables — typically an
``ANTHROPIC_BASE_URL`` plus model overrides and an auth token — that points
Claude Code at a particular API backend (e.g. a third-party GLM endpoint).
Providers are defined and selected entirely in the launcher's global config YAML
(``~/.claunch.yaml`` by default — the same file ``claunch export``/``import``
use), which the launcher reads live at launch:

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

import yaml

from . import config, lineage, settings
from .profile import Profile

#: The built-in "no override" provider — plain Anthropic, launcher injects token.
DEFAULT_PROVIDER = "default"


class ProviderError(Exception):
    """Raised for unknown providers or a malformed config file."""


def _load_config() -> dict:
    """Parse the global launcher YAML, tolerating a missing/invalid file."""
    path = config.sync_file()
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def registry(doc: Optional[dict] = None) -> Dict[str, Dict[str, str]]:
    """Map of provider name -> env, from the config file plus built-in default."""
    doc = _load_config() if doc is None else doc
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
    doc = _load_config() if doc is None else doc
    name = doc.get("provider")
    return str(name) if name else None


def _profile_selection(profile_name: str, doc: dict) -> Optional[str]:
    profiles = doc.get("profiles")
    if isinstance(profiles, dict):
        entry = profiles.get(profile_name)
        if isinstance(entry, dict) and entry.get("provider"):
            return str(entry["provider"])
    return None


def resolve_name(profile: Profile, doc: Optional[dict] = None) -> str:
    """Effective provider for ``profile``: own → ancestor → global → default."""
    doc = _load_config() if doc is None else doc
    for p in reversed(lineage.chain(profile)):  # self first, then up to the root
        sel = _profile_selection(p.name, doc)
        if sel:
            return sel
    return active(doc) or DEFAULT_PROVIDER


def effective_env(profile: Profile) -> Dict[str, str]:
    """Provider env vars that ``run`` should layer on for ``profile``."""
    doc = _load_config()
    return provider_env(resolve_name(profile, doc), doc)


# --------------------------------------------------------------------------- #
# writing the selection back to the config file (used by `set-provider`)
# --------------------------------------------------------------------------- #
def _write_config(doc: dict) -> None:
    """Persist the config doc with the same formatting ``export`` uses."""
    path = config.sync_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(
        doc, sort_keys=True, allow_unicode=True, default_flow_style=False
    )
    path.write_text(text, encoding="utf-8")


def set_active(name: str) -> None:
    """Set (or clear, with ``default``) the global provider in the config file."""
    _require_known(name)
    doc = _load_config()
    if name == DEFAULT_PROVIDER:
        doc.pop("provider", None)
    else:
        doc["provider"] = name
    doc.setdefault("version", 1)
    _write_config(doc)


def set_profile_selection(profile: Profile, name: str) -> None:
    """Set (or clear, with ``default``) a profile's provider in the config file."""
    _require_known(name)
    doc = _load_config()
    profiles = doc.get("profiles")
    if not isinstance(profiles, dict):
        profiles = {}
    entry = profiles.get(profile.name)
    if not isinstance(entry, dict):
        # Seed a full entry from live state so a later `import` (which sets env
        # authoritatively) won't wipe this profile's env down to empty.
        entry = {"env": settings.get_env(profile)}
        parent = lineage.get_parent(profile)
        if parent:
            entry["parent"] = parent
    if name == DEFAULT_PROVIDER:
        entry.pop("provider", None)
    else:
        entry["provider"] = name
    profiles[profile.name] = entry
    doc["profiles"] = profiles
    doc.setdefault("version", 1)
    _write_config(doc)
