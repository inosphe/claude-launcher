"""One-time migration of legacy scattered config into the store, then reconcile.

Earlier versions kept launcher config in three places: each profile's ``env`` in
its native ``settings.json``, the ``parent`` in ``<config_dir>/.launcher.json``,
and the default template in ``<launcher home>/template.json``. The source of
truth is now ``~/.claunch.yaml`` (see :mod:`store`), so on first run we absorb any
of those legacy stores into it and remove the duplicates.

Migration runs **exactly once**, gated by a marker file. That matters for two
reasons: (1) a legacy profile dir must be registered in the store even if it had
no env/parent (a token-only profile), so ``prune`` never mistakes it for an
orphan; and (2) registering every directory on *every* run would defeat ``prune``
(whose job is to remove dirs the store no longer lists). The marker draws that
line — one bulk registration at upgrade, then steady state.

Legacy *live* values win over a pre-existing ``~/.claunch.yaml``: before this
change that file was an export snapshot while the profile directory was the live
source, so an old export must not clobber newer ``settings.json`` / ``.launcher.json``
values.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import config, profile, seed, store, template

LEGACY_META_FILENAME = ".launcher.json"
LEGACY_SETTINGS_FILENAME = "settings.json"
LEGACY_TEMPLATE_FILENAME = "template.json"
MARKER_FILENAME = ".migrated-v1"


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _legacy_template_path() -> Path:
    return config.launcher_home() / LEGACY_TEMPLATE_FILENAME


def _marker_path() -> Path:
    return config.launcher_home() / MARKER_FILENAME


def reconcile() -> None:
    """Materialize profiles the store declares but that have no directory here.

    The store is the source of truth, but a profile only *works* with a real
    ``CLAUDE_CONFIG_DIR``. So a config copied from another machine (or a
    hand-edited ``~/.claunch.yaml``) can name a profile whose directory does not
    exist yet — create and seed it on demand, so commands work without an
    explicit step. Idempotent: once the directory exists this does nothing.
    """
    for name in store.profiles():
        p = profile.resolve(name)
        if not p.exists():
            profile.create(name)
            seed.seed_profile(p)


def run() -> None:
    """Migrate legacy config (once), then materialize any not-yet-created profiles."""
    _migrate_legacy()
    reconcile()


def _migrate_legacy() -> None:
    """Absorb legacy scattered config into ``~/.claunch.yaml`` (once)."""
    marker = _marker_path()
    if marker.is_file():
        return

    if not store.path().is_file():
        # Seed the source of truth from the bootstrap template before absorbing.
        store.save(template.default_document())

    profiles = profile.list_all()
    legacy_template = _legacy_template_path()

    def _mutate(doc: dict) -> None:
        if legacy_template.is_file():
            env = _read_json(legacy_template).get("env")
            if isinstance(env, dict) and env:
                doc.setdefault("template", {})["env"] = {
                    str(k): str(v) for k, v in env.items()
                }
        section = doc.get("profiles")
        if not isinstance(section, dict):
            section = {}
            doc["profiles"] = section
        for p in profiles:
            # Preserve any existing entry (e.g. a provider selection), but let
            # legacy *live* values win over a stale snapshot for env/parent.
            entry = dict(section.get(p.name) or {})
            env = _read_json(p.config_dir / LEGACY_SETTINGS_FILENAME).get("env")
            if isinstance(env, dict) and env:
                entry["env"] = {str(k): str(v) for k, v in env.items()}
            parent = _read_json(p.config_dir / LEGACY_META_FILENAME).get("parent")
            if parent:
                entry["parent"] = str(parent)
            # Always register the profile, even with no config, so a token-only
            # profile is never treated as an orphan by ``prune``.
            section[p.name] = entry

    store.update(_mutate)

    # Filesystem cleanup, only after the store write succeeded.
    if legacy_template.is_file():
        legacy_template.unlink()
    for p in profiles:
        meta = p.config_dir / LEGACY_META_FILENAME
        if meta.is_file():
            meta.unlink()
        settings_path = p.config_dir / LEGACY_SETTINGS_FILENAME
        data = _read_json(settings_path)
        if "env" in data:
            data.pop("env", None)
            settings_path.write_text(
                json.dumps(data, indent=2) + "\n", encoding="utf-8"
            )

    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("migrated\n", encoding="utf-8")
