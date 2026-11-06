"""Procedure packs: v2 procedures shipped and installed as one unit
(``docs/cflow-v2.md`` 2.12.1, claunch-8t5r0).

A pack is a directory ``packs/<id>/`` in a v2 layer: a manifest
``pack.yaml`` and the pack's procedures as ordinary v2 files (``cflow: 2``,
``kind: procedure``). A procedure of a pack is named ``<pack>:<file stem>``
(``unreal:build-check``) -- the colon keeps it apart from the plain
definition names the layers resolve, so a pack cannot shadow a workflow.

Where packs live, in the order a name is resolved:

- project layer ``<repo>/.claunch/workflows/v2/packs/<id>/`` -- installed
  into that workspace;
- global layer ``~/.claude-launcher/workflows/v2/packs/<id>/`` -- installed
  for every workspace of this launcher home;
- package ``src/claude_launcher/workflows/v2/packs/<id>/`` -- the catalog.
  A pack that is only in the package is offered for installation and is
  **not callable** until it is installed: installing is the workspace's
  decision that this pack's procedures belong to its sessions, the same
  two-step model as prompt packs (``prompt_packs.py``).

The manifest::

    pack: 1
    title: Unreal 작업 규약
    description: ...
    roles: {type: whitelist, roles: [unreal]}   # optional; absent = every role

``roles`` uses the ``filter_roles`` shape and is judged the same way
(:class:`claude_launcher.cflow.model.RoleFilter`, primary role and subroles).
A procedure's own ``filter_roles`` applies too: a call passes both.

The layer listing (:func:`layers._candidates`) globs ``*.yaml`` of a layer
directory only, so pack files never appear as plain definitions.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import yaml

from . import ir, layers

PACKS_DIR = "packs"
MANIFEST = "pack.yaml"
SEP = ":"
MANIFEST_VERSION = 1

#: the layers a pack is callable from (installed); the package is the catalog
INSTALLED_LAYERS = (layers.LAYER_PROJECT, layers.LAYER_GLOBAL)


class PackError(ValueError):
    """A manifest or a pack operation that cannot be carried out."""


@dataclass
class Pack:
    id: str
    layer: str
    path: Path
    title: str = ""
    description: str = ""
    #: ``{"type": whitelist|blacklist, "roles": [...]}`` or ``None`` (every role)
    roles: Optional[Dict[str, Any]] = None
    procedures: List[str] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def installed(self) -> bool:
        return self.layer in INSTALLED_LAYERS

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "layer": self.layer, "path": str(self.path),
                "title": self.title, "description": self.description,
                "roles": self.roles, "installed": self.installed,
                "procedures": [qualify(self.id, p) for p in self.procedures],
                "error": self.error}


# -- names -------------------------------------------------------------------

def qualify(pack_id: str, proc: str) -> str:
    return f"{pack_id}{SEP}{proc}"


def split(name: Any) -> Optional[Tuple[str, str]]:
    """``(pack, procedure)`` for a qualified name, ``None`` otherwise."""
    text = str(name or "")
    if text.count(SEP) != 1:
        return None
    pack_id, proc = text.split(SEP)
    if not (layers.is_name(pack_id) and layers.is_name(proc)):
        return None
    if SEP in pack_id or SEP in proc:
        return None
    return pack_id, proc


def is_qualified(name: Any) -> bool:
    return split(name) is not None


def check_id(pack_id: str) -> str:
    pack_id = str(pack_id or "").strip()
    if not pack_id or SEP in pack_id or not layers.is_name(pack_id) or pack_id.startswith("."):
        raise PackError(f"{pack_id!r} is not a pack id (a directory name, no '/', ':' or '.yaml')")
    return pack_id


# -- the manifest -------------------------------------------------------------

def check_roles(raw: Any, where: str = "roles") -> Optional[Dict[str, Any]]:
    """A ``filter_roles``-shaped value, normalised (lower-cased roles), or
    ``None`` for absent. Raises :class:`PackError` on a malformed one."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise PackError(f"{where} must be a mapping {{type, roles}}")
    extra = set(raw) - {"type", "roles"}
    if extra:
        raise PackError(f"{where}: unknown key(s) {', '.join(sorted(extra))}")
    kind = raw.get("type")
    if kind not in ("whitelist", "blacklist"):
        raise PackError(f"{where}.type must be whitelist or blacklist")
    roles = raw.get("roles")
    if not isinstance(roles, list) or not all(isinstance(r, str) and r.strip() for r in roles):
        raise PackError(f"{where}.roles must be a list of role names")
    return {"type": kind, "roles": sorted({r.strip().lower() for r in roles})}


def parse_manifest(text: str, pack_id: str) -> Dict[str, Any]:
    try:
        doc = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise PackError(f"pack {pack_id}: {MANIFEST} is not YAML: {exc}") from None
    if not isinstance(doc, dict):
        raise PackError(f"pack {pack_id}: {MANIFEST} must be a mapping")
    if doc.get("pack") != MANIFEST_VERSION:
        raise PackError(f"pack {pack_id}: {MANIFEST} must start with 'pack: {MANIFEST_VERSION}'")
    extra = set(doc) - {"pack", "title", "description", "roles"}
    if extra:
        raise PackError(f"pack {pack_id}: unknown key(s) {', '.join(sorted(extra))} in {MANIFEST}")
    return {"title": str(doc.get("title") or ""),
            "description": str(doc.get("description") or ""),
            "roles": check_roles(doc.get("roles"), f"pack {pack_id}: roles")}


def dump_manifest(meta: Dict[str, Any]) -> str:
    doc: Dict[str, Any] = {"pack": MANIFEST_VERSION}
    if meta.get("title"):
        doc["title"] = meta["title"]
    if meta.get("description"):
        doc["description"] = meta["description"]
    if meta.get("roles"):
        doc["roles"] = {"type": meta["roles"]["type"], "roles": list(meta["roles"]["roles"])}
    return yaml.safe_dump(doc, allow_unicode=True, sort_keys=False)


# -- where packs are -----------------------------------------------------------

def package_packs_dir() -> Path:
    # the live package directory: the pin (layers.pin_package) copies only a
    # layer's top-level *.yaml, and the catalog is read, never run
    return layers.live_package_dir() / PACKS_DIR


def pack_dirs(cwd: Optional[str] = None) -> List[Tuple[str, Path]]:
    """``(layer, packs dir)`` in resolution order: project, global, package."""
    return [(layers.LAYER_PROJECT, layers.project_dir(cwd) / PACKS_DIR),
            (layers.LAYER_GLOBAL, layers.global_dir() / PACKS_DIR),
            (layers.LAYER_PACKAGE, package_packs_dir())]


def _read_pack(layer: str, path: Path) -> Pack:
    pack = Pack(id=path.name, layer=layer, path=path)
    try:
        meta = parse_manifest((path / MANIFEST).read_text(encoding="utf-8"), path.name)
    except OSError as exc:
        pack.error = f"{MANIFEST} unreadable: {exc}"
        meta = {}
    except PackError as exc:
        pack.error = str(exc)
        meta = {}
    pack.title = meta.get("title", "")
    pack.description = meta.get("description", "")
    pack.roles = meta.get("roles")
    try:
        pack.procedures = sorted(p.stem for p in path.glob("*.yaml") if p.name != MANIFEST)
    except OSError:
        pack.procedures = []
    return pack


def all_packs(cwd: Optional[str] = None) -> List[Pack]:
    """Every pack visible from ``cwd``, the nearest copy of each id; a pack
    shadowed by a nearer layer is not listed again."""
    seen: Dict[str, Pack] = {}
    resolved_seen = set()
    for layer, base in pack_dirs(cwd):
        try:
            if not base.is_dir():
                continue
            resolved = base.resolve()
        except OSError:
            continue
        if resolved in resolved_seen:
            continue
        resolved_seen.add(resolved)
        for path in sorted(p for p in base.iterdir() if p.is_dir()):
            if path.name in seen or SEP in path.name or path.name.startswith("."):
                continue
            seen[path.name] = _read_pack(layer, path)
    return [seen[k] for k in sorted(seen)]


def installed(cwd: Optional[str] = None) -> List[Pack]:
    return [p for p in all_packs(cwd) if p.installed]


def get(pack_id: str, cwd: Optional[str] = None) -> Optional[Pack]:
    for pack in all_packs(cwd):
        if pack.id == pack_id:
            return pack
    return None


def find(name: str, cwd: Optional[str] = None, *,
         installed_only: bool = True) -> Optional[Tuple[Pack, Path]]:
    """The pack and file a qualified name resolves to (``None`` when not a
    qualified name, no such pack or procedure, or -- ``installed_only`` --
    the pack is only in the catalog)."""
    parts = split(name)
    if parts is None:
        return None
    pack = get(parts[0], cwd)
    if pack is None or (installed_only and not pack.installed):
        return None
    path = pack.path / f"{parts[1]}.yaml"
    if parts[1] not in pack.procedures or not path.is_file():
        return None
    return pack, path


def load(name: str, cwd: Optional[str] = None, *, installed_only: bool = True) -> ir.Chart:
    """Compile a pack procedure. Its includes resolve by name in the layers
    of ``cwd`` like any definition's."""
    from . import frontend

    hit = find(name, cwd, installed_only=installed_only)
    if hit is None:
        parts = split(name)
        if parts is not None:
            pack = get(parts[0], cwd)
            if pack is not None and not pack.installed:
                raise frontend.FrontendError(
                    f"procedure {name!r}: pack {parts[0]!r} is in the catalog only; "
                    f"install it into this workspace first")
        raise frontend.FrontendError(f"no installed pack procedure named {name!r}")
    return layers.load_file(hit[1], cwd)


# -- roles -------------------------------------------------------------------

def _filter(spec: Optional[Dict[str, Any]]):
    from ..cflow.model import RoleFilter

    if not spec:
        return None
    return RoleFilter(type=spec["type"], roles=tuple(spec.get("roles") or ()))


def admits(spec: Optional[Dict[str, Any]], roles: Optional[Sequence[str]]) -> bool:
    """Whether a session holding ``roles`` (primary + subroles) passes the
    ``filter_roles``-shaped ``spec``. Unknown roles (``None``) are judged as
    the role of a session in no mesh, "": a whitelist turns it away, a
    blacklist lets it pass (``RoleFilter.allows_any``)."""
    flt = _filter(spec)
    if flt is None:
        return True
    return flt.allows_any(list(roles or ()))


def describe(spec: Optional[Dict[str, Any]]) -> str:
    flt = _filter(spec)
    return flt.describe() if flt is not None else "every role"


def held_roles(session: str, mesh: str = "", cwd: Optional[str] = None
               ) -> Optional[Tuple[str, ...]]:
    """Every role ``session`` holds in ``mesh`` (primary first), or ``None``
    when it cannot be read (no daemon, not enrolled). The same read the v1
    ``filter_roles`` check stands on (``cflow.engine._enforce_role_filter``)."""
    if not session:
        return None
    try:
        from ..cflow import responders

        reach = responders.pool(session=session, mesh=mesh or "", cwd=cwd)
    except Exception:  # noqa: BLE001 - not known is the answer
        return None
    if getattr(reach, "problem", None) or not getattr(reach, "me", None):
        return None
    held = tuple(getattr(reach, "me_roles", ()) or (getattr(reach, "me_role", ""),))
    return tuple(r for r in held if r) or None


# -- install / remove / edit ----------------------------------------------------

def install(workspace: str, pack_id: str, *, force: bool = False) -> Pack:
    """Copy the catalog's pack into ``workspace``'s project layer."""
    pack_id = check_id(pack_id)
    src = package_packs_dir() / pack_id
    if not (src / MANIFEST).is_file():
        raise PackError(f"no pack {pack_id!r} in the catalog")
    dest = layers.project_dir(workspace) / PACKS_DIR / pack_id
    if dest.exists():
        if not force:
            raise PackError(f"pack {pack_id!r} is already installed in {dest} "
                            f"(force replaces it)")
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__", ".*"))
    return _read_pack(layers.LAYER_PROJECT, dest)


def remove(workspace: str, pack_id: str) -> None:
    pack_id = check_id(pack_id)
    dest = layers.project_dir(workspace) / PACKS_DIR / pack_id
    if not dest.is_dir():
        raise PackError(f"pack {pack_id!r} is not installed in this workspace")
    shutil.rmtree(dest)


def set_roles(workspace: str, pack_id: str, roles: Any) -> Pack:
    """Rewrite the ``roles`` of the pack installed in ``workspace``'s project
    layer (``None`` clears it: every role). The catalog copy and the global
    layer are not edited here."""
    pack_id = check_id(pack_id)
    dest = layers.project_dir(workspace) / PACKS_DIR / pack_id
    manifest = dest / MANIFEST
    if not manifest.is_file():
        raise PackError(f"pack {pack_id!r} is not installed in this workspace; "
                        f"install it before editing its roles")
    meta = parse_manifest(manifest.read_text(encoding="utf-8"), pack_id)
    meta["roles"] = check_roles(roles)
    tmp = manifest.with_name(f".{MANIFEST}.{os.getpid()}.tmp")
    tmp.write_text(dump_manifest(meta), encoding="utf-8")
    os.replace(tmp, manifest)
    return _read_pack(layers.LAYER_PROJECT, dest)
