"""One-time, idempotent migration of legacy scattered config into the store.

Earlier versions kept launcher config in three places: each profile's ``env`` in
its native ``settings.json``, the ``parent`` in ``<config_dir>/.launcher.json``,
and the default template in ``<launcher home>/template.json``. The source of
truth is now ``~/.claunch.yaml`` (see :mod:`store`), so on startup we absorb any
of those legacy stores into it and remove the duplicates.

:func:`run` is safe to call before every command: once migrated there is nothing
left to absorb (env stripped from ``settings.json``, ``.launcher.json`` and
``template.json`` deleted), so it returns after a few cheap checks.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import config, profile, seed, store, template

LEGACY_META_FILENAME = ".launcher.json"
LEGACY_SETTINGS_FILENAME = "settings.json"
LEGACY_TEMPLATE_FILENAME = "template.json"


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _legacy_template_path() -> Path:
    return config.launcher_home() / LEGACY_TEMPLATE_FILENAME


def _profile_has_legacy(p) -> bool:
    if (p.config_dir / LEGACY_META_FILENAME).is_file():
        return True
    env = _read_json(p.config_dir / LEGACY_SETTINGS_FILENAME).get("env")
    return isinstance(env, dict) and bool(env)


def reconcile() -> None:
    """Materialize profiles the store declares but that have no directory here.

    The store is the source of truth, but a profile only *works* with a real
    ``CLAUDE_CONFIG_DIR``. So a config copied from another machine (or a
    hand-edited ``~/.claunch.yaml``) can name a profile whose directory does not
    exist yet — create and seed it on demand, so commands work without an
    explicit ``apply``. Idempotent: once the directory exists this does nothing.
    """
    for name in store.profiles():
        p = profile.resolve(name)
        if not p.exists():
            profile.create(name)
            seed.seed_profile(p)


def run() -> None:
    """Migrate legacy config, then materialize any not-yet-created profiles."""
    _migrate_legacy()
    reconcile()


def _migrate_legacy() -> None:
    """Absorb any legacy scattered config into ``~/.claunch.yaml``."""
    store_exists = store.path().is_file()
    legacy_template = _legacy_template_path()
    profiles = profile.list_all()

    if (
        store_exists
        and not legacy_template.is_file()
        and not any(_profile_has_legacy(p) for p in profiles)
    ):
        return  # already migrated (or a clean install with nothing to absorb)

    if not store_exists:
        # Seed the source of truth from the bootstrap template before absorbing.
        store.save(template.default_document())

    def _mutate(doc: dict) -> None:
        if legacy_template.is_file():
            env = _read_json(legacy_template).get("env")
            if isinstance(env, dict) and env:
                doc.setdefault("template", {})["env"] = {
                    str(k): str(v) for k, v in env.items()
                }
        section = doc.setdefault("profiles", {})
        if not isinstance(section, dict):
            section = {}
            doc["profiles"] = section
        for p in profiles:
            entry = section.get(p.name)
            entry = dict(entry) if isinstance(entry, dict) else {}
            if "env" not in entry:
                env = _read_json(p.config_dir / LEGACY_SETTINGS_FILENAME).get("env")
                if isinstance(env, dict) and env:
                    entry["env"] = {str(k): str(v) for k, v in env.items()}
            if "parent" not in entry:
                parent = _read_json(p.config_dir / LEGACY_META_FILENAME).get("parent")
                if parent:
                    entry["parent"] = str(parent)
            if entry:
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
