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

from . import config, provider_spec, settings, store
from .profile import Profile

TEMPLATE_FILENAME = "template.yaml"

#: Built-in defaults used until a ``template.yaml`` is written. The template
#: is a profile *layer* (see :mod:`provider_spec`): the same fields a profile
#: entry may carry, copied into every new Claude profile.
DEFAULT_TEMPLATE: dict = {
    "auto_compact_at": 400000,
    "harness_options": {
        "claude": {"env": {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "0"}}
    },
}

#: Fields :func:`apply_to` copies from the template into a new profile entry.
LAYER_FIELDS = (*provider_spec.PROFILE_SPEC_FIELDS, provider_spec.HARNESS_OPTIONS_FIELD)


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
                data.setdefault("template", _copy(DEFAULT_TEMPLATE))
                data.setdefault("profiles", {})
                return data
        except (OSError, yaml.YAMLError):
            pass
    return {
        "version": store.VERSION,
        "template": _copy(DEFAULT_TEMPLATE),
        "profiles": {},
    }


def _copy(doc: dict) -> dict:
    return yaml.safe_load(yaml.safe_dump(doc))


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
    """The live raw default env (a pre-schema ``template.env`` block, if any)."""
    return store.template_env()


def set_env(env_map: Dict[str, str]) -> None:
    """Update the live template's ``env`` block in the store."""
    store.set_template_env({str(k): str(v) for k, v in env_map.items()})


def layer() -> dict:
    """The template's spec-layer fields (``models``, ``auto_compact_at``, ...)."""
    block = store.template_block()
    return {k: block[k] for k in LAYER_FIELDS if k in block}


def apply_to(profile: Profile) -> Dict[str, str]:
    """Copy the template into ``profile`` and return what the profile now carries.

    Spec-layer fields are merged into the profile entry key by key (a field
    the profile already sets is kept); a pre-schema ``env`` block is merged
    into the profile's raw ``env`` as before. The returned mapping lists the
    applied fields and env keys, for the ``create`` summary line.
    """
    applied: Dict[str, str] = {}
    fields = layer()
    if fields:
        entry = store.profile_entry(profile.name)
        for key, value in fields.items():
            current = entry.get(key)
            if isinstance(value, dict) and isinstance(current, dict):
                merged = _merge(value, current)
            elif current is not None and current != "":
                continue
            else:
                merged = value
            store.set_profile_field(profile.name, key, merged)
            applied[key] = str(merged)
    template_env = env()
    if template_env:
        applied.update(settings.set_env(profile, template_env))
    return applied


def _merge(defaults: dict, own: dict) -> dict:
    """``own`` over ``defaults``, one level of nesting at a time."""
    out = dict(defaults)
    for key, value in own.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out
