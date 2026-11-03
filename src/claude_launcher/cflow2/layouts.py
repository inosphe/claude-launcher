"""Layout metadata for cflow v2, as a separate file (claunch-yklgu.1).

A layout is not part of a workflow definition. The definition carries only a
reference -- ``layout: <id>@<version>``, at the top level or on a task -- and
the layout itself lives beside the workflow, in ``layouts/<id>.yaml`` under
the same three layer directories :mod:`cflow2.layers` searches (project,
global, package). A reference is a name, never a path.

Nothing here changes what a workflow means. The compiler resolves each
reference once and embeds the result in ``chart.meta`` (``layout``,
``node_layouts``, ``layouts``), so a run's ``chart.json`` snapshot carries the
layout it started with: editing the file later does not move a live run.

Serving a run is :func:`view_model` over that snapshot. The payload is the
cross-repository contract gds6-df2s "공유 계약 초안" B fixes::

    {"layout": "<id>@<version>", "api": 1, "title": str,
     "blocks": [{"type": <component>, ...}]}

Two things never stop a run:

- a block whose ``type`` the layout's ``components`` allowlist does not name
  comes back as ``{"type": "unknown", "layout": ..., "unknown_type": ...,
  "note": ...}`` -- the page draws the rest and shows that block as a
  placeholder (계약 정정 #1 item 2: 모르는 블록은 조용히 건너뛰지 않는다).
- a reference that does not resolve, or a layout file that declares an
  ``api`` this build does not speak, is a compile *warning* and a placeholder
  block. A layout problem is never a compile error and never a refused run.

The registry (:func:`registry`) is the index over those files: each entry is
a layout id, its version, its ``api`` and the component types it allows.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import yaml

from . import ir

#: The layout contract version this build serves (gds6-df2s contract B).
SUPPORTED_API = 1

#: The directory a layer keeps its layout files in (beside its definitions).
LAYOUT_DIR = "layouts"

LAYOUT_SUFFIXES = (".yaml", ".yml")

#: Keys a layout file may carry; anything else is a typo, not a new idea.
LAYOUT_KEYS = ("layout", "version", "api", "title", "description", "components", "blocks")

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

NOTE_UNKNOWN = "이 블록은 이 화면 버전이 모릅니다"
NOTE_MISSING = "이 레이아웃을 찾을 수 없습니다"
NOTE_API = "이 화면은 api {have}만 읽습니다 — 레이아웃은 api {want}를 요구합니다"

ResolveLayout = Callable[[str], Optional[Path]]


class LayoutError(Exception):
    """A layout file that cannot be read (caught by :func:`resolve`)."""


# ---------------------------------------------------------------------------
# references


def parse_reference(ref: Any) -> Tuple[str, int]:
    """``"<id>@<version>"`` -> ``(id, version)``. Raises ``ValueError``."""
    text = str(ref or "").strip()
    ident, sep, ver = text.partition("@")
    if not sep or not ver:
        raise ValueError(f"a layout reference is '<id>@<version>', got {text!r}")
    if not _ID_RE.match(ident):
        raise ValueError(
            f"layout id must be letters, digits, '.', '_' or '-', got {ident!r}")
    try:
        version = int(ver)
    except ValueError:
        raise ValueError(f"layout version must be an integer, got {ver!r}") from None
    if version < 1:
        raise ValueError("layout version must be 1 or more")
    return ident, version


def is_reference(value: Any) -> bool:
    """Whether ``value`` is a well-formed ``<id>@<version>`` reference."""
    try:
        parse_reference(value)
    except ValueError:
        return False
    return True


# ---------------------------------------------------------------------------
# files


def layout_dir(base: Union[str, Path]) -> Path:
    """A layer directory's ``layouts/`` subdirectory."""
    return Path(base) / LAYOUT_DIR


def layout_files(cwd: Optional[str] = None) -> List[Tuple[str, Path]]:
    """``(layer, path)`` for every layout file the layers offer, nearest
    layer first (the order :func:`cflow2.layers.search_dirs` gives)."""
    from . import layers

    out: List[Tuple[str, Path]] = []
    seen = set()
    for layer, base in layers.search_dirs(cwd):
        directory = layout_dir(base)
        try:
            resolved = directory.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        try:
            names = sorted(n for n in os.listdir(directory)
                           if n.endswith(LAYOUT_SUFFIXES))
        except OSError:
            continue
        for name in names:
            out.append((layer, directory / name))
    return out


def find(layout_id: str, cwd: Optional[str] = None) -> Optional[Path]:
    """The file for a layout id, nearest layer first; ``None`` for an id that
    is not a bare id or has no file."""
    ident = str(layout_id or "")
    if not _ID_RE.match(ident):
        return None
    for _layer, path in layout_files(cwd):
        if path.stem == ident:
            return path
    return None


def resolver(cwd: Optional[str] = None) -> ResolveLayout:
    """A ``resolve_layout`` for :mod:`cflow2.frontend` bound to ``cwd``."""
    return lambda layout_id: find(layout_id, cwd)


# ---------------------------------------------------------------------------
# reading one file


def load(path: Union[str, Path]) -> Dict[str, Any]:
    """One layout file, normalized: ``{"id", "version", "api", "title",
    "components", "blocks"}``. Raises :class:`LayoutError` (or ``OSError``/
    ``yaml.YAMLError``) when it cannot be read as one."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise LayoutError(f"{path}: cannot read: {exc}") from None
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise LayoutError(f"{path}: YAML error: {exc}") from None
    if not isinstance(raw, dict):
        raise LayoutError(f"{path}: a layout file is a mapping")
    extra = sorted(str(k) for k in raw if k not in LAYOUT_KEYS)
    if extra:
        raise LayoutError(f"{path}: unknown keys {extra} (a layout file takes "
                          f"{', '.join(LAYOUT_KEYS)})")
    ident = raw.get("layout")
    if not isinstance(ident, str) or not _ID_RE.match(ident):
        raise LayoutError(f"{path}: 'layout' must be a layout id")
    version = raw.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise LayoutError(f"{path}: 'version' must be an integer of 1 or more")
    api = raw.get("api", SUPPORTED_API)
    if isinstance(api, bool) or not isinstance(api, int) or api < 1:
        raise LayoutError(f"{path}: 'api' must be an integer of 1 or more")
    title = raw.get("title", ident)
    if not isinstance(title, str) or not title:
        raise LayoutError(f"{path}: 'title' must be a non-empty string")
    raw_blocks = raw.get("blocks") or []
    if not isinstance(raw_blocks, list):
        raise LayoutError(f"{path}: 'blocks' must be a list")
    blocks: List[Dict[str, Any]] = []
    for i, block in enumerate(raw_blocks):
        if not isinstance(block, dict):
            raise LayoutError(f"{path}: block {i} is not a mapping")
        kind = block.get("type")
        if not isinstance(kind, str) or not kind:
            raise LayoutError(f"{path}: block {i} needs a 'type'")
        blocks.append(dict(block))
    components = raw.get("components")
    if components is None:
        components = [b["type"] for b in blocks]
    if not isinstance(components, list) or not all(
            isinstance(c, str) and c for c in components):
        raise LayoutError(f"{path}: 'components' must be a list of component type names")
    description = raw.get("description", "")
    if not isinstance(description, str):
        raise LayoutError(f"{path}: 'description' must be a string")
    return {
        "id": ident,
        "version": version,
        "api": api,
        "title": title,
        "description": description,
        "components": list(dict.fromkeys(components)),
        "blocks": blocks,
    }


# ---------------------------------------------------------------------------
# registry


def registry(cwd: Optional[str] = None) -> List[Dict[str, Any]]:
    """Every layout file the layers offer, as registry entries: ``id``,
    ``version``, ``api``, ``title`` and the allowed ``components``. A file
    that does not read as a layout is listed with ``error`` set rather than
    dropped."""
    out: List[Dict[str, Any]] = []
    for layer, path in layout_files(cwd):
        try:
            entry = load(path)
        except (OSError, yaml.YAMLError, LayoutError) as exc:
            out.append({"file": str(path), "layer": layer, "error": str(exc)})
            continue
        entry["ref"] = f"{entry['id']}@{entry['version']}"
        entry["file"] = str(path)
        entry["layer"] = layer
        out.append(entry)
    return out


def refs(cwd: Optional[str] = None) -> List[str]:
    """Every usable ``<id>@<version>`` the layers offer."""
    return [entry["ref"] for entry in registry(cwd) if entry.get("ref")]


def resolve(ref: str, cwd: Optional[str] = None,
            resolve_layout: Optional[ResolveLayout] = None) -> Dict[str, Any]:
    """The registry entry a reference names. Never raises: an unreachable or
    malformed reference comes back as ``{"ref", "error", "why"}`` so that a
    workflow still compiles (a layout problem must not stop a run)."""
    text = str(ref or "").strip()
    try:
        ident, version = parse_reference(text)
    except ValueError as exc:
        return {"ref": text, "error": "bad_reference", "why": str(exc)}
    finder = resolve_layout or resolver(cwd)
    path = finder(ident)
    if path is None:
        return {"ref": text, "error": "not_found",
                "why": f"no layout file for {ident!r} (looked for "
                       f"{LAYOUT_DIR}/{ident}.yaml in the project, global and "
                       f"package layers)"}
    try:
        entry = load(path)
    except (OSError, yaml.YAMLError, LayoutError) as exc:
        return {"ref": text, "error": "invalid", "why": str(exc)}
    if entry["id"] != ident or entry["version"] != version:
        return {"ref": text, "error": "mismatch",
                "why": f"{path} declares {entry['id']}@{entry['version']}, "
                       f"not {ident}@{version}"}
    entry["ref"] = text
    entry["source"] = str(path)
    return entry


def resolve_all(refs_: Any, cwd: Optional[str] = None,
                resolve_layout: Optional[ResolveLayout] = None) -> Dict[str, Dict[str, Any]]:
    """Resolve every reference once; the map the compiler stores in
    ``chart.meta['layouts']``."""
    out: Dict[str, Dict[str, Any]] = {}
    for ref in sorted({str(r).strip() for r in refs_ if r}):
        out[ref] = resolve(ref, cwd, resolve_layout)
    return out


# ---------------------------------------------------------------------------
# view model


def ref_for(meta: Optional[dict], node_id: Optional[str] = None) -> Optional[str]:
    """The layout reference that applies to ``node_id``: the node's own when
    it declares one, else the workflow's. ``None`` when neither does."""
    meta = meta or {}
    nodes = meta.get("node_layouts") or {}
    if node_id and isinstance(nodes, dict):
        ref = nodes.get(node_id)
        if isinstance(ref, str) and ref:
            return ref
    ref = meta.get("layout")
    return ref if isinstance(ref, str) and ref else None


def placeholder(ref: str, unknown_type: str, note: str) -> Dict[str, Any]:
    """The block a reader draws where it cannot draw the real one (계약 정정
    #1 item 2: the rest of the page still renders)."""
    return {"type": "unknown", "layout": ref, "unknown_type": unknown_type,
            "note": note}


def view_model(chart: ir.Chart, node_id: Optional[str] = None) -> Dict[str, Any]:
    """A run's layout as the contract B payload: ``{layout, api, title,
    blocks}``. ``node_id`` selects a step's layout when the chart declares one
    for that node."""
    meta = chart.meta or {}
    ref = ref_for(meta, node_id)
    if ref is None:
        return {"layout": None, "api": SUPPORTED_API, "title": chart.name, "blocks": []}
    layouts = meta.get("layouts") or {}
    entry = layouts.get(ref) if isinstance(layouts, dict) else None
    if not isinstance(entry, dict):
        return {"layout": ref, "api": SUPPORTED_API, "title": chart.name,
                "blocks": [placeholder(ref, "layout", NOTE_MISSING)]}
    if entry.get("error"):
        return {"layout": ref, "api": SUPPORTED_API, "title": chart.name,
                "blocks": [placeholder(ref, str(entry["error"]),
                                       str(entry.get("why") or NOTE_MISSING))]}
    api = entry.get("api", SUPPORTED_API)
    title = entry.get("title") or chart.name
    if api != SUPPORTED_API:
        return {"layout": ref, "api": SUPPORTED_API, "title": title,
                "blocks": [placeholder(ref, f"api:{api}",
                                       NOTE_API.format(have=SUPPORTED_API, want=api))]}
    allowed = set(entry.get("components") or ())
    blocks: List[Dict[str, Any]] = []
    for block in entry.get("blocks") or ():
        kind = block.get("type")
        if kind in allowed:
            blocks.append(dict(block))
        else:
            blocks.append(placeholder(ref, str(kind), NOTE_UNKNOWN))
    return {"layout": ref, "api": SUPPORTED_API, "title": title, "blocks": blocks}


def active_node(chart: ir.Chart, state: Optional[dict]) -> Optional[str]:
    """The deepest node with a live instance in ``state`` -- what a
    step-level layout follows. ``None`` when nothing is live."""
    inst = (state or {}).get("inst") or {}
    live = [rec.get("node") for rec in inst.values() if isinstance(rec, dict)]
    live = [nid for nid in live if isinstance(nid, str) and nid in chart.nodes]
    if not live:
        return None

    def depth(nid: str) -> int:
        d, cur = 0, nid
        while cur is not None and cur in chart.nodes and d < 10000:
            d += 1
            cur = chart.nodes[cur].parent
        return d

    return sorted(live, key=lambda nid: (-depth(nid), nid))[0]
