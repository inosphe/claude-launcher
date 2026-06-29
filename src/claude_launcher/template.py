"""Bootstrap template used to initialize ``~/.claunch.yaml`` the first time.

``~/.claunch.yaml`` is the launcher's source of truth (see :mod:`store`), but it
has to start from *something*. That seed is ``template.yaml`` in the launcher
home: a full config skeleton whose ``template.env`` block becomes the default env
for new profiles. Edit it to change the defaults a brand-new install starts with.

Once ``~/.claunch.yaml`` exists it is authoritative and read live; this file is
only consulted to create it (and as the source for ``claunch template --init``).
The *live* default-env (what new profiles actually get) is the ``template`` block
inside ``~/.claunch.yaml``, exposed here as :func:`env` / :func:`set_env`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict

import yaml

from . import config, settings, store
from .profile import Profile

TEMPLATE_FILENAME = "template.yaml"

#: Built-in defaults used until a ``template.yaml`` is written.
DEFAULT_ENV: Dict[str, str] = {
    "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "0",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "400000",
}


def template_path() -> Path:
    return config.launcher_home() / TEMPLATE_FILENAME


def default_document() -> dict:
    """The initial ``~/.claunch.yaml`` document (from ``template.yaml`` or built-in).

    This is a complete, valid config skeleton: a ``template.env`` block of
    defaults plus an empty ``profiles`` map. Providers/selections start absent.
    """
    path = template_path()
    if path.is_file():
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("version", store.VERSION)
                data.setdefault("template", {"env": dict(DEFAULT_ENV)})
                data.setdefault("profiles", {})
                return data
        except (OSError, yaml.YAMLError):
            pass
    return {
        "version": store.VERSION,
        "template": {"env": dict(DEFAULT_ENV)},
        "profiles": {},
    }


def ensure_file() -> Path:
    """Write a default ``template.yaml`` if one does not exist yet."""
    path = template_path()
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        text = yaml.safe_dump(
            default_document(),
            sort_keys=True,
            allow_unicode=True,
            default_flow_style=False,
        )
        path.write_text(text, encoding="utf-8")
    return path


def env() -> Dict[str, str]:
    """The live default env for new profiles (``template.env`` in the store)."""
    return store.template_env()


def set_env(env_map: Dict[str, str]) -> None:
    """Update the live template's ``env`` block in the store."""
    store.set_template_env({str(k): str(v) for k, v in env_map.items()})


def apply_to(profile: Profile) -> Dict[str, str]:
    """Merge the template's env defaults into ``profile`` and return the result."""
    template_env = env()
    if template_env:
        return settings.set_env(profile, template_env)
    return settings.get_env(profile)
