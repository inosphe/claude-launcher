"""Export/import all profile settings to a single YAML file (``~/.claunch.yaml``).

The synced document captures the launcher's *managed* state — the list of
profiles, each profile's ``env`` vars, and the default template — but **not**
login tokens, which are secrets and stay per-machine (run ``claunch login``).

Schema::

    version: 1
    template:
      env: {KEY: VALUE, ...}
    profiles:
      <name>:
        parent: <other profile>   # optional
        env: {KEY: VALUE, ...}
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import yaml

from . import config, lineage, profile, seed, settings, template

SYNC_VERSION = 1


class SyncError(Exception):
    """Raised for unreadable or malformed sync files."""


@dataclass
class ImportSummary:
    created: List[str] = field(default_factory=list)
    updated: List[str] = field(default_factory=list)
    removed: List[str] = field(default_factory=list)
    template_applied: bool = False


def _resolve(path: Optional[Path]) -> Path:
    return path or config.sync_file()


def _existing() -> dict:
    """Best-effort read of the current sync file (``{}`` if missing/invalid).

    Provider definitions (``providers``) and selections (``provider`` globally and
    per profile) live only in this file, so a rebuild must carry them forward.
    """
    path = config.sync_file()
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def _profile_entry(p, prev_profiles: dict) -> dict:
    entry: dict = {"env": settings.get_env(p)}
    parent = lineage.get_parent(p)
    if parent:
        entry["parent"] = parent
    prev = prev_profiles.get(p.name)
    if isinstance(prev, dict) and prev.get("provider"):
        entry["provider"] = str(prev["provider"])  # selection lives only here
    return entry


def build_document() -> dict:
    """Snapshot current profiles + template as a plain (YAML-ready) dict.

    Provider definitions and selections are preserved verbatim from the existing
    file, since the launcher reads them live and nothing else stores them.
    """
    prev = _existing()
    prev_profiles = prev.get("profiles")
    if not isinstance(prev_profiles, dict):
        prev_profiles = {}
    profiles = {p.name: _profile_entry(p, prev_profiles) for p in profile.list_all()}
    doc = {
        "version": SYNC_VERSION,
        "template": {"env": template.env()},
        "profiles": profiles,
    }
    if isinstance(prev.get("providers"), dict):
        doc["providers"] = prev["providers"]
    if prev.get("provider"):
        doc["provider"] = str(prev["provider"])
    return doc


def export_to(path: Optional[Path] = None) -> Path:
    """Write the current state to ``path`` (default ``~/.claunch.yaml``)."""
    path = _resolve(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(
        build_document(), sort_keys=True, allow_unicode=True, default_flow_style=False
    )
    path.write_text(text, encoding="utf-8")
    return path


def _load_document(path: Path) -> dict:
    if not path.is_file():
        raise SyncError(f"sync file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SyncError(f"could not read sync file {path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise SyncError(f"sync file {path} must be a mapping at the top level")
    return data


def _as_env(value, where: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SyncError(f"{where} must be a mapping of env vars")
    return {str(k): str(v) for k, v in value.items()}


def import_from(
    path: Optional[Path] = None,
    *,
    prune: bool = False,
    do_seed: bool = True,
) -> ImportSummary:
    """Make local profiles match ``path``. Creates missing profiles (seeded),
    sets each profile's env authoritatively, and optionally removes profiles
    absent from the file (``prune``)."""
    path = _resolve(path)
    data = _load_document(path)
    summary = ImportSummary()

    tmpl = data.get("template")
    if isinstance(tmpl, dict) and "env" in tmpl:
        template.set_env(_as_env(tmpl.get("env"), "template.env"))
        summary.template_applied = True

    raw_profiles = data.get("profiles") or {}
    if not isinstance(raw_profiles, dict):
        raise SyncError("'profiles' must be a mapping of name -> settings")

    existing = {p.name for p in profile.list_all()}
    wanted = set()

    # Pass 1: create/update every profile and set its own env.
    for name, pdata in raw_profiles.items():
        name = str(name)
        wanted.add(name)
        env = _as_env((pdata or {}).get("env"), f"profiles.{name}.env")
        if name in existing:
            p = profile.require(name)
            summary.updated.append(name)
        else:
            p = profile.create(name)
            if do_seed:
                seed.seed_profile(p)
            summary.created.append(name)
        settings.replace_env(p, env)

    # Pass 2: wire up parents now that all profiles exist.
    for name, pdata in raw_profiles.items():
        p = profile.require(str(name))
        parent = (pdata or {}).get("parent")
        if parent:
            lineage.set_parent(p, str(parent))
        else:
            lineage.clear_parent(p)

    if prune:
        for name in sorted(existing - wanted):
            profile.remove(name)
            summary.removed.append(name)

    return summary
