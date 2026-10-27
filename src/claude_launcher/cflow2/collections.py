"""cflow v2 collections: declarations and the pure state operations on them.

Spec: ``docs/cflow-v2.md`` 2.7 (collections) and 4.5 (execution). The
frontend calls :func:`normalize` for every declaration; the runtime keeps one
``col_state`` dict per collection (``{kind, items, seq}``) in ``state.json``
and changes it only through :func:`apply_push`, :func:`apply_set`,
:func:`apply_remove`, :func:`reset` and :func:`inherit`. Every function here is
free of I/O: the caller holds the run lock, writes the journal event the
operation returns, and saves the state.

A failed operation raises :class:`CollectionError` before it changes
``col_state`` -- an operation is all or nothing.

Normalized declaration (JSON-able; ``watch.on`` keys are strings so the
declaration survives a ``chart.json`` round trip unchanged)::

    {"name": "landing", "kind": "queue", "key": "issue",
     "item": {"issue": {"type": "str", "optional": False}, ...},
     "status": {"values": [...], "initial": "...", "settled": [...]} | None,
     "access": {"push": [...], "set": [...], "remove": [...], "set.landed": [...]},
     "env": {"TARGET": "master"},
     "watch": {"every": 60, "probe": "..." | [...], "on": {"0": {"set": {...}}}} | None,
     "lifecycle": {"reset_at": [...], "drop": "settled", "remap": {}, "inherit": False}}

Records: every record carries the declared item fields plus ``seq`` (the
collection's running counter at push time), ``at`` (the ``now`` passed to the
push) and ``by`` (the writer). A collection with ``status`` adds ``status``.
Those names are reserved and cannot be item fields.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Dict, List, Optional, Tuple

KINDS = ("queue", "log")
FIELD_TYPES = ("str", "int", "bool", "list", "map", "any")
PRINCIPALS = ("driver", "daemon", "child", "peer", "person")
OPS = ("push", "set", "remove")
DROP = ("settled", "all", "none")
RESERVED_FIELDS = ("seq", "at", "by", "status", "since")
DECL_KEYS = ("kind", "key", "item", "status", "access", "env", "watch", "lifecycle", "type")
WATCH_MIN_EVERY = 15
WATCH_DEFAULT_EVERY = 60
ADDRESS_TARGETS = ("self", "main", "side", "parent", "leader")
#: Targets only a trigger push may name (docs 2.8): another session's run.
TRIGGER_TARGETS = ("parent", "leader")

_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class CollectionError(ValueError):
    """A declaration or an operation that the collection rules reject."""


# ---------------------------------------------------------------- declaration

def _fail(name: str, msg: str) -> CollectionError:
    return CollectionError(f"collection {name!r}: {msg}")


def _str_list(name: str, value: Any, what: str) -> List[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise _fail(name, f"{what} must be a list of strings")
    return list(value)


def _parse_type(name: str, field: str, value: Any) -> Dict[str, Any]:
    if not isinstance(value, str):
        raise _fail(name, f"item.{field}: type must be a string, one of {', '.join(FIELD_TYPES)}")
    text = value.strip()
    optional = text.endswith("?")
    base = text[:-1].strip() if optional else text
    if base not in FIELD_TYPES:
        raise _fail(name, f"item.{field}: unknown type {value!r} "
                          f"(use {', '.join(FIELD_TYPES)}, with '?' for optional)")
    return {"type": base, "optional": optional}


def _type_ok(kind: str, value: Any) -> bool:
    if kind == "any":
        return True
    if kind == "str":
        return isinstance(value, str)
    if kind == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "bool":
        return isinstance(value, bool)
    if kind == "list":
        return isinstance(value, list)
    if kind == "map":
        return isinstance(value, dict)
    return False


def _no_template(name: str, command: Any) -> None:
    parts = [command] if isinstance(command, str) else list(command)
    for part in parts:
        if "${" in part:
            raise _fail(name, "watch.probe: '${...}' is not allowed in a command; read values "
                              "from the environment instead ($CFLOW_ITEM_<FIELD>, "
                              "$CFLOW_<NAME>, $CFLOW_SESSION)")


def _merge_type(name: str, spec: Dict[str, Any], types: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    tname = spec.get("type")
    if tname is None:
        return dict(spec)
    if not isinstance(tname, str) or not tname:
        raise _fail(name, "type must be a collection_types name")
    table = types or {}
    if tname not in table:
        known = ", ".join(sorted(table)) or "none"
        raise _fail(name, f"unknown collection type {tname!r} (known: {known})")
    base = table[tname]
    if not isinstance(base, dict):
        raise _fail(name, f"collection type {tname!r} must be a mapping")
    if "type" in base:
        raise _fail(name, f"collection type {tname!r} cannot itself use 'type:'")
    merged: Dict[str, Any] = copy.deepcopy(dict(base))
    for key, value in spec.items():
        if key == "type":
            continue
        if key == "env" and isinstance(value, dict) and isinstance(merged.get("env"), dict):
            # env merges by name, so a use that sets one name keeps the rest
            # and a required name (null in the type) can only be filled in.
            env = dict(merged["env"])
            env.update(value)
            merged["env"] = env
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def normalize(name: str, spec: Any, types: Optional[Dict[str, Any]] = None,
              *, allow_unset_env: bool = False) -> Dict[str, Any]:
    """Validate one collection declaration and expand its ``type:``.

    ``types`` maps a ``collection_types`` name to its raw declaration. In a
    type, an ``env`` value of ``null`` means "the use must set this"; a
    declaration that still has one after the merge is an error unless
    ``allow_unset_env`` (used to validate the ``collection_types`` entries
    themselves).
    """
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise CollectionError(f"collection name {name!r} must be a word "
                              "(letters, digits, '_' and '-')")
    if not isinstance(spec, dict):
        raise _fail(name, "declaration must be a mapping")
    unknown = sorted(set(spec) - set(DECL_KEYS))
    if unknown:
        raise _fail(name, f"unknown key(s) {', '.join(map(str, unknown))} "
                          f"(allowed: {', '.join(DECL_KEYS)})")
    raw = _merge_type(name, spec, types)

    kind = raw.get("kind")
    if kind not in KINDS:
        raise _fail(name, f"kind must be one of {', '.join(KINDS)}")

    item_raw = raw.get("item")
    if not isinstance(item_raw, dict) or not item_raw:
        raise _fail(name, "item must be a non-empty mapping of field -> type")
    item: Dict[str, Dict[str, Any]] = {}
    for field, ftype in item_raw.items():
        if not isinstance(field, str) or not _NAME_RE.match(field):
            raise _fail(name, f"item field {field!r} must be a word")
        if field in RESERVED_FIELDS:
            raise _fail(name, f"item field {field!r} is reserved "
                              f"({', '.join(RESERVED_FIELDS)} are added by the engine)")
        item[field] = _parse_type(name, field, ftype)

    key = raw.get("key")
    if kind == "queue":
        if not isinstance(key, str) or not key:
            raise _fail(name, "a queue needs key: <item field>")
        if key not in item:
            raise _fail(name, f"key {key!r} is not an item field")
        if item[key]["optional"]:
            raise _fail(name, f"key field {key!r} cannot be optional")
        if item[key]["type"] not in ("str", "int"):
            raise _fail(name, f"key field {key!r} must be str or int")
    elif key is not None:
        raise _fail(name, "key is only for kind: queue")

    status = None
    status_raw = raw.get("status")
    if status_raw is not None:
        if kind != "queue":
            raise _fail(name, "status is only for kind: queue (a log is append-only)")
        if not isinstance(status_raw, dict):
            raise _fail(name, "status must be a mapping {values, initial, settled}")
        extra = sorted(set(status_raw) - {"values", "initial", "settled"})
        if extra:
            raise _fail(name, f"status: unknown key(s) {', '.join(map(str, extra))}")
        values = _str_list(name, status_raw.get("values"), "status.values")
        if not values:
            raise _fail(name, "status.values must not be empty")
        if len(set(values)) != len(values):
            raise _fail(name, "status.values has duplicates")
        initial = status_raw.get("initial", values[0])
        if initial not in values:
            raise _fail(name, f"status.initial {initial!r} is not in status.values")
        settled = _str_list(name, status_raw.get("settled", []), "status.settled")
        bad = [v for v in settled if v not in values]
        if bad:
            raise _fail(name, f"status.settled {bad} is not a subset of status.values")
        status = {"values": values, "initial": initial, "settled": settled}

    access_raw = raw.get("access") or {}
    if not isinstance(access_raw, dict):
        raise _fail(name, "access must be a mapping of operation -> principals")
    ops = OPS if kind == "queue" else ("push",)
    access: Dict[str, List[str]] = {op: ["driver"] for op in ops}
    for op, who in access_raw.items():
        if not isinstance(op, str):
            raise _fail(name, f"access key {op!r} must be a string")
        if op.startswith("set."):
            value = op[4:]
            if kind != "queue":
                raise _fail(name, f"access.{op}: a log has no set")
            if status is None:
                raise _fail(name, f"access.{op}: the collection declares no status")
            if value not in status["values"]:
                raise _fail(name, f"access.{op}: {value!r} is not a declared status value")
        elif op not in OPS:
            raise _fail(name, f"access key {op!r} must be push, set, remove or set.<status>")
        elif op not in ops:
            raise _fail(name, f"access.{op}: a log only supports push")
        principals = _str_list(name, who, f"access.{op}")
        bad = [p for p in principals if p not in PRINCIPALS]
        if bad:
            raise _fail(name, f"access.{op}: unknown principal(s) {bad} "
                              f"(use {', '.join(PRINCIPALS)})")
        access[op] = list(dict.fromkeys(principals))

    env_raw = raw.get("env") or {}
    if not isinstance(env_raw, dict):
        raise _fail(name, "env must be a mapping of NAME -> string")
    env: Dict[str, Any] = {}
    for ename, evalue in env_raw.items():
        if not isinstance(ename, str) or not _ENV_RE.match(ename):
            raise _fail(name, f"env name {ename!r} must be letters, digits and '_'")
        if evalue is None:
            if not allow_unset_env:
                raise _fail(name, f"env.{ename} is required (the collection type leaves it "
                                  f"unset); set it, e.g. env: {{{ename}: ...}}")
            env[ename] = None
            continue
        if not isinstance(evalue, str):
            raise _fail(name, f"env.{ename} must be a string")
        env[ename] = evalue

    watch = None
    watch_raw = raw.get("watch")
    if watch_raw is not None:
        if kind != "queue":
            raise _fail(name, "watch is only for kind: queue")
        if not isinstance(watch_raw, dict):
            raise _fail(name, "watch must be a mapping {every, probe, on}")
        if True in watch_raw and "on" not in watch_raw:
            # a YAML 1.1 loader reads the key `on:` as the boolean true
            watch_raw = {("on" if k is True else k): v for k, v in watch_raw.items()}
        extra = sorted(set(watch_raw) - {"every", "probe", "on"})
        if extra:
            raise _fail(name, f"watch: unknown key(s) {', '.join(map(str, extra))}")
        every = watch_raw.get("every", WATCH_DEFAULT_EVERY)
        if isinstance(every, bool) or not isinstance(every, int) or every < WATCH_MIN_EVERY:
            raise _fail(name, f"watch.every must be an integer >= {WATCH_MIN_EVERY}")
        probe = watch_raw.get("probe")
        if isinstance(probe, str):
            if not probe.strip():
                raise _fail(name, "watch.probe must not be empty")
        elif isinstance(probe, list):
            if not probe or not all(isinstance(p, str) for p in probe):
                raise _fail(name, "watch.probe argv must be a non-empty list of strings")
            probe = list(probe)
        else:
            raise _fail(name, "watch.probe must be a command string or an argv list")
        _no_template(name, probe)
        on_raw = watch_raw.get("on")
        if not isinstance(on_raw, dict) or not on_raw:
            raise _fail(name, "watch.on must map exit codes to {set: {...}} or {remove: true}")
        on: Dict[str, Dict[str, Any]] = {}
        for code, action in on_raw.items():
            try:
                icode = int(code) if not isinstance(code, bool) else None
            except (TypeError, ValueError):
                icode = None
            if icode is None or not 0 <= icode <= 255:
                raise _fail(name, f"watch.on key {code!r} must be an exit code 0-255")
            on[str(icode)] = _watch_action(name, icode, action, item, key, status)
        watch = {"every": every, "probe": probe, "on": on}

    lc_raw = raw.get("lifecycle") or {}
    if not isinstance(lc_raw, dict):
        raise _fail(name, "lifecycle must be a mapping")
    extra = sorted(set(lc_raw) - {"reset_at", "drop", "remap", "inherit"})
    if extra:
        raise _fail(name, f"lifecycle: unknown key(s) {', '.join(map(str, extra))}")
    reset_at = _str_list(name, lc_raw.get("reset_at", []), "lifecycle.reset_at")
    drop = lc_raw.get("drop", "settled")
    if drop not in DROP:
        raise _fail(name, f"lifecycle.drop must be one of {', '.join(DROP)}")
    remap_raw = lc_raw.get("remap") or {}
    if not isinstance(remap_raw, dict):
        raise _fail(name, "lifecycle.remap must be a mapping status -> status")
    if remap_raw and status is None:
        raise _fail(name, "lifecycle.remap needs a status declaration")
    remap: Dict[str, str] = {}
    for src, dst in remap_raw.items():
        for v in (src, dst):
            if v not in status["values"]:  # type: ignore[index]
                raise _fail(name, f"lifecycle.remap: {v!r} is not a declared status value")
        remap[src] = dst
    inherit_flag = lc_raw.get("inherit", False)
    if not isinstance(inherit_flag, bool):
        raise _fail(name, "lifecycle.inherit must be true or false")

    return {
        "name": name, "kind": kind, "key": key if kind == "queue" else None,
        "item": item, "status": status, "access": access, "env": env, "watch": watch,
        "lifecycle": {"reset_at": reset_at, "drop": drop, "remap": remap,
                      "inherit": inherit_flag},
    }


def _watch_action(name: str, code: int, action: Any, item: Dict[str, Any],
                  key: Optional[str], status: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    where = f"watch.on.{code}"
    if not isinstance(action, dict) or len(action) != 1:
        raise _fail(name, f"{where} must be {{set: {{field: value}}}} or {{remove: true}}")
    if "remove" in action:
        if action["remove"] is not True:
            raise _fail(name, f"{where}.remove must be true")
        return {"remove": True}
    fields = action.get("set")
    if not isinstance(fields, dict) or not fields:
        raise _fail(name, f"{where} must be {{set: {{field: value}}}} or {{remove: true}}")
    try:
        _check_fields({"name": name, "item": item, "key": key, "status": status},
                      fields, settable=True)
    except CollectionError as exc:
        raise _fail(name, f"{where}: {_strip(name, exc)}") from None
    return {"set": copy.deepcopy(fields)}


def _strip(name: str, exc: Exception) -> str:
    prefix = f"collection {name!r}: "
    text = str(exc)
    return text[len(prefix):] if text.startswith(prefix) else text


# --------------------------------------------------------------- addresses

def parse_address(addr: Any) -> Tuple[str, str]:
    """``[target:]name`` -> ``(target, name)``.

    ``target`` is ``self`` (default), ``main``, ``parent``, ``leader`` or
    ``side:<run>``. Raises :class:`CollectionError` on a malformed address.
    """
    if not isinstance(addr, str) or not addr.strip():
        raise CollectionError(f"collection address {addr!r} must be a non-empty string")
    parts = addr.strip().split(":")
    if len(parts) == 1:
        target, name = "self", parts[0]
    elif len(parts) == 2 and parts[0] in ("self", "main", "parent", "leader"):
        target, name = parts
    elif len(parts) == 3 and parts[0] == "side" and parts[1]:
        target, name = f"side:{parts[1]}", parts[2]
    else:
        raise CollectionError(f"collection address {addr!r}: use [self:|main:|parent:|leader:|"
                              "side:<run>:]<name>")
    if not _NAME_RE.match(name):
        raise CollectionError(f"collection address {addr!r}: bad collection name {name!r}")
    return target, name


# ------------------------------------------------------------------- state

def empty(spec: Dict[str, Any]) -> Dict[str, Any]:
    """A new, empty ``col_state`` for a normalized declaration."""
    return {"kind": spec["kind"], "items": [], "seq": 0}


def access_ok(spec: Dict[str, Any], op: str, principal: str,
              status: Optional[str] = None) -> bool:
    """May ``principal`` perform ``op`` (push/set/remove)?

    For ``set`` with a target ``status``, a declared ``set.<status>`` decides
    and overrides ``set``.
    """
    access = spec.get("access") or {}
    if op == "set" and status is not None and f"set.{status}" in access:
        return principal in access[f"set.{status}"]
    if op not in access:
        return False
    return principal in access[op]


def _check_access(spec: Dict[str, Any], op: str, principal: str,
                  status: Optional[str] = None) -> None:
    if principal not in PRINCIPALS:
        raise _fail(spec["name"], f"unknown principal {principal!r}")
    if not access_ok(spec, op, principal, status):
        rule = f"set.{status}" if (op == "set" and status is not None
                                   and f"set.{status}" in spec["access"]) else op
        allowed = spec["access"].get(rule, [])
        raise _fail(spec["name"], f"access denied: {principal} may not {op}"
                                  + (f" status {status!r}" if status is not None and op == "set" else "")
                                  + f" (access.{rule}: {allowed or 'nobody'})")


def _check_fields(spec: Dict[str, Any], fields: Dict[str, Any], *, settable: bool) -> None:
    name = spec["name"]
    item = spec["item"]
    for field, value in fields.items():
        if field == "status":
            if not settable:
                raise _fail(name, "status is set by the engine on push (use set to change it)")
            if spec.get("status") is None:
                raise _fail(name, "the collection declares no status")
            if value not in spec["status"]["values"]:
                raise _fail(name, f"status {value!r} is not one of "
                                  f"{spec['status']['values']}")
            continue
        if field in RESERVED_FIELDS:
            raise _fail(name, f"field {field!r} is set by the engine")
        if field not in item:
            raise _fail(name, f"unknown field {field!r} (declared: {', '.join(item)})")
        if settable and field == spec.get("key"):
            raise _fail(name, f"the key field {field!r} cannot be changed; push a new record")
        decl = item[field]
        if value is None:
            if decl["optional"]:
                continue
            raise _fail(name, f"field {field!r} is required")
        if not _type_ok(decl["type"], value):
            raise _fail(name, f"field {field!r} must be {decl['type']}, "
                              f"got {type(value).__name__}")


def _find(col_state: Dict[str, Any], spec: Dict[str, Any], key: Any) -> int:
    kfield = spec["key"]
    for i, rec in enumerate(col_state.get("items", [])):
        if rec.get(kfield) == key:
            return i
    return -1


def _need_queue(spec: Dict[str, Any], op: str) -> None:
    if spec["kind"] != "queue":
        raise _fail(spec["name"], f"{op} is only for a queue (this is a {spec['kind']})")


#: The record field that names the trigger push that wrote it (doc 2.8).
TID = "_tid"


def apply_push(col_state: Dict[str, Any], spec: Dict[str, Any], item: Any, *,
               principal: str, by: str, now: Any, tid: Optional[str] = None) -> Dict[str, Any]:
    """Add a record. A queue replaces the record with the same key (in place)
    and resets its status to ``initial``. Returns a ``collection_pushed`` event.

    ``tid`` marks a daemon trigger push, which may run more than once (doc
    2.8): the record carries it as ``_tid``. When the record that push would
    write already carries the same ``_tid`` (a log: any record; a queue: the
    record with the same key) nothing changes and the event says
    ``replay``."""
    name = spec["name"]
    _check_access(spec, "push", principal)
    if not isinstance(item, dict):
        raise _fail(name, "push item must be a mapping")
    _check_fields(spec, item, settable=False)
    missing = [f for f, d in spec["item"].items() if not d["optional"] and item.get(f) is None]
    if missing:
        raise _fail(name, f"push is missing required field(s) {', '.join(missing)}")
    tid = str(tid) if tid else None
    if tid:
        if spec["kind"] == "queue":
            idx = _find(col_state, spec, item.get(spec["key"]))
            same = [col_state["items"][idx]] if idx >= 0 else []
        else:
            same = list(col_state.get("items", []))
        for rec in same:
            if rec.get(TID) == tid:
                return {"event": "collection_pushed", "collection": name,
                        "principal": principal, "by": by, "seq": rec.get("seq"),
                        "replay": True, "item": copy.deepcopy(rec)}
    seq = int(col_state.get("seq", 0)) + 1
    record = {f: copy.deepcopy(v) for f, v in item.items() if v is not None}
    record.update({"seq": seq, "at": now, "by": by})
    if tid:
        record[TID] = tid
    if spec.get("status") is not None:
        record["status"] = spec["status"]["initial"]
        record["since"] = now
    event: Dict[str, Any] = {"event": "collection_pushed", "collection": name,
                             "principal": principal, "by": by, "seq": seq}
    items_ = col_state.setdefault("items", [])
    if spec["kind"] == "queue":
        key = record[spec["key"]]
        idx = _find(col_state, spec, key)
        event["key"] = key
        event["replaced"] = idx >= 0
        if idx >= 0:
            event["previous"] = copy.deepcopy(items_[idx])
            items_[idx] = record
        else:
            items_.append(record)
    else:
        items_.append(record)
    col_state["seq"] = seq
    col_state.setdefault("kind", spec["kind"])
    event["item"] = copy.deepcopy(record)
    return event


def apply_set(col_state: Dict[str, Any], spec: Dict[str, Any], key: Any,
              fields: Any, *, principal: str, by: str, now: Any) -> Dict[str, Any]:
    """Change fields (or ``status``) of a queue record. Returns a
    ``collection_set`` event with the previous values."""
    name = spec["name"]
    _need_queue(spec, "set")
    if not isinstance(fields, dict) or not fields:
        raise _fail(name, "set needs a non-empty mapping of fields")
    _check_access(spec, "set", principal, fields.get("status") if "status" in fields else None)
    _check_fields(spec, fields, settable=True)
    idx = _find(col_state, spec, key)
    if idx < 0:
        raise _fail(name, f"no record with {spec['key']} = {key!r}")
    rec = col_state["items"][idx]
    before = {f: copy.deepcopy(rec.get(f)) for f in fields}
    if "status" in fields and fields["status"] != rec.get("status"):
        rec["since"] = now
    for f, v in fields.items():
        if v is None:
            rec.pop(f, None)
        else:
            rec[f] = copy.deepcopy(v)
    return {"event": "collection_set", "collection": name, "principal": principal,
            "by": by, "at": now, "key": key, "fields": copy.deepcopy(fields),
            "before": before}


def apply_remove(col_state: Dict[str, Any], spec: Dict[str, Any], key: Any, *,
                 principal: str, by: str, now: Any) -> Dict[str, Any]:
    """Delete a queue record. Returns a ``collection_removed`` event."""
    name = spec["name"]
    _need_queue(spec, "remove")
    _check_access(spec, "remove", principal)
    idx = _find(col_state, spec, key)
    if idx < 0:
        raise _fail(name, f"no record with {spec['key']} = {key!r}")
    rec = col_state["items"].pop(idx)
    return {"event": "collection_removed", "collection": name, "principal": principal,
            "by": by, "at": now, "key": key, "item": rec}


def _ident(spec: Dict[str, Any], rec: Dict[str, Any]) -> Any:
    return rec.get(spec["key"]) if spec["kind"] == "queue" else rec.get("seq")


def reset(col_state: Dict[str, Any], spec: Dict[str, Any], now: Any = None) -> Dict[str, Any]:
    """Run the lifecycle clean-up in place: drop records per ``lifecycle.drop``
    and remap the status of what stays. ``seq`` is kept, so log cursors stay
    valid. Returns a ``collection_reset`` summary."""
    lc = spec["lifecycle"]
    drop = lc["drop"]
    settled = set((spec.get("status") or {}).get("settled") or [])
    kept: List[Dict[str, Any]] = []
    dropped: List[Any] = []
    remapped: Dict[str, List[str]] = {}
    for rec in col_state.get("items", []):
        gone = drop == "all" or (drop == "settled" and rec.get("status") in settled)
        if gone:
            dropped.append(_ident(spec, rec))
            continue
        st = rec.get("status")
        if st in lc["remap"]:
            rec["status"] = lc["remap"][st]
            if now is not None:
                rec["since"] = now
            remapped[str(_ident(spec, rec))] = [st, rec["status"]]
        kept.append(rec)
    col_state["items"] = kept
    col_state.setdefault("seq", 0)
    col_state.setdefault("kind", spec["kind"])
    return {"event": "collection_reset", "collection": spec["name"], "dropped": dropped,
            "kept": [_ident(spec, r) for r in kept], "remapped": remapped}


def inherit_report(old_col_state: Optional[Dict[str, Any]],
                   spec: Dict[str, Any], now: Any = None) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """:func:`inherit` plus a ``collection_inherited`` summary."""
    summary: Dict[str, Any] = {"event": "collection_inherited", "collection": spec["name"],
                               "inherited": False, "dropped": [], "kept": [],
                               "remapped": {}, "invalid": []}
    if (not spec["lifecycle"]["inherit"] or not isinstance(old_col_state, dict)
            or old_col_state.get("kind") != spec["kind"]):
        return empty(spec), summary
    new = {"kind": spec["kind"], "items": copy.deepcopy(list(old_col_state.get("items") or [])),
           "seq": int(old_col_state.get("seq", 0) or 0)}
    # The declaration may have changed since the old run: keep only records
    # that still fit it, and pull an unknown status back to initial.
    valid: List[Dict[str, Any]] = []
    for rec in new["items"]:
        if not isinstance(rec, dict):
            continue
        # A trigger push marks its record with TID; it is not a declared field.
        fields = {f: v for f, v in rec.items() if f not in RESERVED_FIELDS and f != TID}
        try:
            _check_fields(spec, fields, settable=False)
            missing = [f for f, d in spec["item"].items()
                       if not d["optional"] and fields.get(f) is None]
            if missing:
                raise _fail(spec["name"], "missing fields")
        except CollectionError:
            summary["invalid"].append(_ident(spec, rec) if spec["kind"] == "log"
                                      else rec.get(spec["key"]))
            continue
        st = spec.get("status")
        if st is None:
            rec.pop("status", None)
        elif rec.get("status") not in st["values"]:
            rec["status"] = st["initial"]
            if now is not None:
                rec["since"] = now
        valid.append(rec)
    new["items"] = valid
    rs = reset(new, spec, now)
    summary.update(inherited=True, dropped=rs["dropped"], kept=rs["kept"],
                   remapped=rs["remapped"])
    return new, summary


def inherit(old_col_state: Optional[Dict[str, Any]], spec: Dict[str, Any],
            now: Any = None) -> Dict[str, Any]:
    """The ``col_state`` a new run starts with when the previous run in the
    same slot had ``old_col_state``: cleaned up (:func:`reset`) when the
    declaration has ``lifecycle.inherit: true``, empty otherwise."""
    return inherit_report(old_col_state, spec, now)[0]


# ------------------------------------------------------------------- reads

def items(col_state: Optional[Dict[str, Any]], status: Optional[str] = None) -> List[Dict[str, Any]]:
    """Copies of the records, optionally only those with ``status``."""
    recs = (col_state or {}).get("items") or []
    if status is not None:
        recs = [r for r in recs if r.get("status") == status]
    return copy.deepcopy(list(recs))


def count(col_state: Optional[Dict[str, Any]], status: Optional[str] = None) -> int:
    recs = (col_state or {}).get("items") or []
    if status is None:
        return len(recs)
    return sum(1 for r in recs if r.get("status") == status)


def fresh(col_state: Optional[Dict[str, Any]], cursor: Optional[int],
          name: Optional[str] = None) -> bool:
    """Is there a record after ``cursor`` (a seq; None = 0), optionally one
    whose ``name`` field equals ``name``?"""
    pos = int(cursor or 0)
    for rec in (col_state or {}).get("items") or []:
        if int(rec.get("seq", 0)) > pos and (name is None or rec.get("name") == name):
            return True
    return False


def end_cursor(col_state: Optional[Dict[str, Any]]) -> int:
    """The cursor value that ``consume`` moves to (the collection's seq)."""
    return int((col_state or {}).get("seq", 0) or 0)


def watch_action(spec: Dict[str, Any], code: int) -> Optional[Dict[str, Any]]:
    """The ``watch.on`` action for an exit code, or None."""
    watch = spec.get("watch") or {}
    return copy.deepcopy((watch.get("on") or {}).get(str(int(code))))


def unsettled(col_state: Optional[Dict[str, Any]], spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Records a watch probes: those whose status is not settled."""
    settled = set((spec.get("status") or {}).get("settled") or [])
    return [r for r in items(col_state) if r.get("status") not in settled]
