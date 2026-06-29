"""The launcher's single source of truth: ``~/.claunch.yaml``, read live.

Every launcher-managed setting lives here in one YAML file the launcher reads at
each launch — each profile's ``env``/``parent``/``provider``, the default
``template`` env, and provider definitions plus the active selection. Profile
*existence* is still the directory on disk (Claude Code needs a real
``CLAUDE_CONFIG_DIR``); this file only holds the config attached to a profile.

Login tokens are deliberately **not** here: they are secrets, kept per-profile
and per-machine (see :mod:`credentials`).

Schema::

    version: 1
    template:
      env: {KEY: VALUE, ...}
    provider: <name>            # global default provider (optional)
    providers:
      <name>: {env: {KEY: VALUE, ...}}
    profiles:
      <name>:
        parent: <other>         # optional
        provider: <name>        # optional
        env: {KEY: VALUE, ...}

The on-disk file is first created from a bootstrap *template* (``template.yaml``,
see :mod:`template`); after that this file is authoritative and is read live —
nothing else stores these settings, so there is no separate "export" step.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict, Optional

import yaml

from . import config

VERSION = 1


class StoreError(Exception):
    """Raised for an unreadable or malformed config file."""


def path() -> Path:
    """The config file backing the store (``~/.claunch.yaml`` by default)."""
    return config.sync_file()


def load() -> dict:
    """Return the live config document (an empty default if the file is absent).

    Reads fresh each call — the file is small and the CLI is short-lived, so this
    keeps every command seeing the current state without a cache to invalidate.

    A *missing* file is fine (a fresh install). A file that is present but
    unparseable raises :class:`StoreError` rather than being silently treated as
    empty — this is now the only state file, so a transient parse error must not
    let the next write clobber it.
    """
    p = path()
    if not p.is_file():
        return {"version": VERSION}
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise StoreError(f"cannot read config file {p}: {exc}") from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise StoreError(f"config file {p} must be a mapping at the top level")
    data.setdefault("version", VERSION)
    return data


def save(doc: dict) -> None:
    """Persist ``doc`` as the config file (stable key order, like the old export)."""
    doc.setdefault("version", VERSION)
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(
        doc, sort_keys=True, allow_unicode=True, default_flow_style=False
    )
    p.write_text(text, encoding="utf-8")


def update(mutator: Callable[[dict], None]) -> dict:
    """Load, apply ``mutator(doc)`` in place, then save. Returns the saved doc."""
    doc = load()
    mutator(doc)
    save(doc)
    return doc


# --------------------------------------------------------------------------- #
# profiles section
# --------------------------------------------------------------------------- #
def profiles(doc: Optional[dict] = None) -> Dict[str, dict]:
    """The ``profiles`` mapping (read-only view; ``{}`` if absent)."""
    doc = load() if doc is None else doc
    section = doc.get("profiles")
    if not isinstance(section, dict):
        return {}
    return {str(k): v for k, v in section.items() if isinstance(v, dict)}


def profile_entry(name: str, doc: Optional[dict] = None) -> dict:
    """A single profile's config entry (``{}`` if it has none)."""
    return profiles(doc).get(name, {})


def _writable_entry(doc: dict, name: str) -> dict:
    section = doc.get("profiles")
    if not isinstance(section, dict):
        section = {}
        doc["profiles"] = section
    entry = section.get(name)
    if not isinstance(entry, dict):
        entry = {}
        section[name] = entry
    return entry


def set_profile_field(name: str, key: str, value) -> None:
    """Set (or, when ``value`` is ``None``/empty, clear) one field on a profile."""

    def _mutate(doc: dict) -> None:
        entry = _writable_entry(doc, name)
        if value in (None, "", {}):
            entry.pop(key, None)
        else:
            entry[key] = value

    update(_mutate)


def ensure_profile(name: str) -> None:
    """Register ``name`` in the store (empty entry) so it is a known profile.

    Existence is still the directory on disk, but the store's ``profiles`` map is
    the authoritative *list* of profiles the launcher manages — so a profile with
    no config yet still gets an entry (and is never mistaken for an orphan dir).
    """

    def _mutate(doc: dict) -> None:
        section = doc.get("profiles")
        if not isinstance(section, dict):
            section = {}
            doc["profiles"] = section
        section.setdefault(name, {})

    update(_mutate)


def remove_profile(name: str) -> None:
    """Drop a profile's config entry entirely (used by ``remove``)."""

    def _mutate(doc: dict) -> None:
        section = doc.get("profiles")
        if isinstance(section, dict):
            section.pop(name, None)

    update(_mutate)


# --------------------------------------------------------------------------- #
# template section
# --------------------------------------------------------------------------- #
def template_env(doc: Optional[dict] = None) -> Dict[str, str]:
    """The default env applied to new profiles (live ``template.env`` block)."""
    doc = load() if doc is None else doc
    tmpl = doc.get("template")
    block = tmpl.get("env") if isinstance(tmpl, dict) else None
    return {str(k): str(v) for k, v in block.items()} if isinstance(block, dict) else {}


def set_template_env(env: Dict[str, str]) -> None:
    def _mutate(doc: dict) -> None:
        tmpl = doc.get("template")
        if not isinstance(tmpl, dict):
            tmpl = {}
            doc["template"] = tmpl
        tmpl["env"] = {str(k): str(v) for k, v in env.items()}

    update(_mutate)
