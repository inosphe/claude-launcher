"""Path-addressed reads and writes of the config file, for ``/api/config/doc``.

A remote settings page (mux-relay's ``/sync``, claunch-gr62q) compares the
config document it keeps with this machine's and copies single entries across.
It needs three things the rest of the store does not offer: the file's hash to
detect that someone else wrote in between, a list of ``set``/``delete`` ops on
key paths, and the existing load checks run on the result *before* anything is
written.

An op is ``{"op": "set", "path": [...], "value": <JSON>}`` or
``{"op": "delete", "path": [...]}``. A path is a list of keys: a string names a
mapping key, an integer a sequence index. ``set`` creates the mappings missing
along the way; it does not create sequence slots (an index must exist, or be
exactly the length to append).

Secrets are not part of this document: login tokens live in each profile's
directory (:mod:`credentials`), and :func:`credential_kinds` only reports
whether one is there.
"""

from __future__ import annotations

import copy
import datetime
import hashlib
from typing import Any, Dict, List, Optional

import yaml

from . import store


class OpError(ValueError):
    """An op list that cannot be applied (bad shape, path into a scalar)."""


class InvalidDoc(ValueError):
    """The document the ops produce fails the store's own load checks."""


def file_bytes() -> bytes:
    """The config file's bytes (empty when it does not exist yet)."""
    p = store.path()
    try:
        return p.read_bytes()
    except FileNotFoundError:
        return b""


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def jsonable(value: Any) -> Any:
    """``value`` with what YAML can hold and JSON cannot made into text.

    The safe loader produces dates and timestamps for unquoted ISO values;
    the wire carries them as their ISO text.
    """
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


def _path(raw: Any) -> List[Any]:
    if not isinstance(raw, list) or not raw:
        raise OpError("path must be a non-empty list of keys")
    for key in raw:
        # bool is an int subclass; a true/false key is not an index.
        if isinstance(key, bool) or not isinstance(key, (str, int)):
            raise OpError(f"path key {key!r} must be a string or an integer")
    return list(raw)


def _step(node: Any, key: Any, where: List[Any]) -> Any:
    if isinstance(key, str):
        if not isinstance(node, dict):
            raise OpError(f"{where} is not a mapping")
        return node.get(key)
    if not isinstance(node, list):
        raise OpError(f"{where} is not a sequence")
    if not -len(node) <= key < len(node):
        raise OpError(f"index {key} is out of range at {where}")
    return node[key]


def _set(doc: dict, path: List[Any], value: Any) -> None:
    node: Any = doc
    for depth, key in enumerate(path[:-1]):
        where = path[:depth]
        child = _step(node, key, where)
        if child is None and isinstance(key, str):
            child = {}
            node[key] = child
        elif child is None or not isinstance(child, (dict, list)):
            raise OpError(f"{path[:depth + 1]} is not a mapping or a sequence")
        node = child
    last = path[-1]
    if isinstance(last, str):
        if not isinstance(node, dict):
            raise OpError(f"{path[:-1]} is not a mapping")
        node[last] = value
        return
    if not isinstance(node, list):
        raise OpError(f"{path[:-1]} is not a sequence")
    if last == len(node):
        node.append(value)
    elif -len(node) <= last < len(node):
        node[last] = value
    else:
        raise OpError(f"index {last} is out of range at {path[:-1]}")


def _delete(doc: dict, path: List[Any]) -> None:
    node: Any = doc
    for depth, key in enumerate(path[:-1]):
        node = _step(node, key, path[:depth])
        if node is None:
            return  # nothing there to delete
    last = path[-1]
    if isinstance(last, str):
        if not isinstance(node, dict):
            raise OpError(f"{path[:-1]} is not a mapping")
        node.pop(last, None)
        return
    if not isinstance(node, list):
        raise OpError(f"{path[:-1]} is not a sequence")
    if -len(node) <= last < len(node):
        del node[last]


def apply_ops(doc: dict, ops: Any) -> dict:
    """A copy of ``doc`` with ``ops`` applied in order (``doc`` is untouched)."""
    if not isinstance(ops, list):
        raise OpError("ops must be a list")
    out = copy.deepcopy(doc)
    for index, op in enumerate(ops):
        if not isinstance(op, dict):
            raise OpError(f"ops[{index}] must be an object")
        kind = op.get("op")
        path = _path(op.get("path"))
        if kind == "set":
            if "value" not in op:
                raise OpError(f"ops[{index}]: set needs a value")
            _set(out, path, copy.deepcopy(op["value"]))
        elif kind == "delete":
            _delete(out, path)
        else:
            raise OpError(f"ops[{index}]: op must be 'set' or 'delete', not {kind!r}")
    return out


def validate(doc: dict) -> None:
    """Run the store's load checks, and the harness parser, on ``doc``.

    The document is written out the way :func:`store.save` writes it and read
    back with :func:`store.parse_text`, so what is accepted here is exactly
    what the next load accepts. The ``harnesses`` block goes through the strict
    :func:`harnesses.parse`: the live registry skips a broken entry quietly,
    which would let a copied declaration vanish without a word.
    """
    from . import harnesses

    if not isinstance(doc, dict):
        raise InvalidDoc("the config document must be a mapping at the top level")
    try:
        text = store.dump_text(doc)
    except yaml.YAMLError as exc:
        raise InvalidDoc(f"cannot write the document as YAML: {exc}") from exc
    try:
        parsed = store.parse_text(text, store.path())
    except store.StoreError as exc:
        raise InvalidDoc(str(exc)) from exc
    for section in ("profiles", "providers"):
        block = parsed.get(section)
        if block is None:
            continue
        if not isinstance(block, dict):
            raise InvalidDoc(f"'{section}' must be a mapping of name -> entry")
        for name, entry in block.items():
            if entry is not None and not isinstance(entry, dict):
                raise InvalidDoc(f"{section}.{name} must be a mapping")
    if parsed.get("harnesses") is not None:
        try:
            harnesses.parse({"harnesses": parsed["harnesses"]})
        except harnesses.HarnessConfigError as exc:
            raise InvalidDoc(str(exc)) from exc


def credential_kind(harness: Any) -> str:
    """``token``, ``oauth`` or ``none`` for a harness declaration.

    ``token``: the harness reads the profile's ``.launcher-token`` (Claude's
    setup-token, an ``api-key`` harness's key). ``oauth``: it keeps its own
    interactive login in the harness home, which this API does not carry.
    """
    if harness is None:
        return "none"
    if harness.auth == "claude" or (harness.auth == "api-key" and harness.token_env):
        return "token"
    if harness.auth == "oauth":
        return "oauth"
    return "none"


def profile_kind(prof, doc: Optional[dict] = None) -> str:
    from . import harnesses, lineage

    try:
        name = lineage.effective_harness(prof, doc)
    except Exception:  # noqa: BLE001 -- an unresolvable harness has no credential to carry
        return "none"
    return credential_kind(harnesses.get(name, doc))


def credential_kinds(doc: dict) -> Dict[str, Dict[str, Any]]:
    """``{profile: {"kind", "has_token"}}`` for every profile directory or entry."""
    from . import credentials, profile as profile_mod

    names = {p.name for p in profile_mod.list_all()}
    names.update(store.profiles(doc))
    out: Dict[str, Dict[str, Any]] = {}
    for name in sorted(names):
        try:
            prof = profile_mod.resolve(name)
        except profile_mod.ProfileError:
            continue
        kind = profile_kind(prof, doc)
        has = False
        if kind == "token":
            try:
                has = credentials.stored_token(prof) is not None
            except OSError:
                has = False
        out[name] = {"kind": kind, "has_token": has}
    return out


def read() -> Dict[str, Any]:
    """The ``GET /api/config/doc`` answer, minus the machine name."""
    data = file_bytes()
    text = data.decode("utf-8") if data else ""
    doc = store.parse_text(text, store.path()) if text else {"version": store.VERSION}
    return {"doc": doc, "hash": digest(data)}


def machine() -> str:
    import socket

    return socket.gethostname()

