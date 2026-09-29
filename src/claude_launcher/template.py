"""The profile template: a live base layer under every profile, and its bootstrap seed.

The ``template`` block of ``~/.claunch.yaml`` is a profile *layer* (the same
fields a profile entry may carry -- ``models``, ``context_window``,
``auto_compact_at``, ``reasoning_effort``, ``harness_options`` -- plus the
common ``env``). It is read at launch as the bottom of every profile's parent
chain: a profile (or an ancestor) that leaves a field unset gets the
template's value, and one that sets it wins. Nothing is copied into a profile
when it is created, so a new profile's entry stays empty and a later change to
the template reaches every profile that does not override it.

A layer takes a value away from the layers below it by writing the key with no
value (``KEY:`` in YAML, i.e. ``null``) -- see :func:`resolve_layers`. That is
how a profile opts out of a template default.

``template.yaml`` in the launcher home is only the bootstrap seed: it is
consulted to create ``~/.claunch.yaml`` the first time (and written by
``claunch template --init``). Once ``~/.claunch.yaml`` exists, its ``template``
block is authoritative.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import yaml

from . import config, provider_spec, store

TEMPLATE_FILENAME = "template.yaml"

#: Built-in defaults used until a ``template.yaml`` is written. The template
#: is a profile *layer* (see :mod:`provider_spec`): the same fields a profile
#: entry may carry, applied at launch under every profile's parent chain.
DEFAULT_TEMPLATE: dict = {
    "auto_compact_at": 400000,
    "harness_options": {
        "claude": {"env": {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "0"}}
    },
}

#: Spec fields the template contributes to every profile (:func:`live_layer`).
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
    """The live template's common ``env`` block (clears left out)."""
    return store.template_env()


def set_env(env_map: Dict[str, str]) -> None:
    """Update the live template's ``env`` block in the store."""
    store.set_template_env({str(k): str(v) for k, v in env_map.items()})


def layer() -> dict:
    """The template's spec-layer fields (``models``, ``auto_compact_at``, ...)."""
    return live_layer()


def live_layer(doc: Optional[dict] = None) -> dict:
    """The template's spec fields, as the bottom layer of a profile chain."""
    block = store.template_block(doc)
    return {k: copy.deepcopy(block[k]) for k in LAYER_FIELDS if k in block}


def live_env(doc: Optional[dict] = None) -> dict:
    """The template's raw common ``env``, as the bottom layer of a profile chain."""
    raw = store.template_block(doc).get("env")
    return copy.deepcopy(raw) if isinstance(raw, dict) else {}


def resolve_layers(layers: Sequence[Optional[dict]]) -> List[dict]:
    """Apply explicit clears across ``layers`` (bottom first, top last).

    A key whose value is ``None`` (written ``KEY:`` in YAML) removes that key
    from every layer below it -- the template and the ancestors of the layer
    that writes it. Nested mappings are followed key by key, so
    ``harness_options: {claude: {env: {X: null}}}`` removes only ``X``. The
    clears themselves are dropped from the returned layers, which are deep
    copies: the caller's documents are never modified.

    Only the layers passed in are affected. A provider's own values sit
    outside the chain and are not cleared this way.
    """
    out = [copy.deepcopy(item) if isinstance(item, dict) else {} for item in layers]
    for index in range(len(out) - 1, 0, -1):
        for lower in out[:index]:
            _clear(lower, out[index])
    return [_drop_clears(item) for item in out]


def _clear(lower: dict, top: dict) -> None:
    for key, value in top.items():
        if value is None:
            lower.pop(key, None)
        elif isinstance(value, dict) and isinstance(lower.get(key), dict):
            _clear(lower[key], value)


def _drop_clears(layer: dict) -> dict:
    return {
        key: _drop_clears(value) if isinstance(value, dict) else value
        for key, value in layer.items()
        if value is not None
    }
