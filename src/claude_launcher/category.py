"""Workspace category trees: a keyword index of what sessions and issues are about.

One SQLite file per registered workspace, beside the glossaries (and separate
from them: a category label is an index keyword, not a defined term). Each
node is one keyword; its label and aliases together are the tree's
vocabulary, so a normalized spelling names at most one node in a workspace.

Sessions and issues are *items*. An item sits in at most one node (claunch-
p2v4m, decided by the user): extract and merge move items, they never copy.

A person changes the tree directly. An agent may assign items to existing
nodes, and the new node it wants is created at once (:func:`create_now`).
A person who wants to review it first asks for the older path instead: the
new node is filed as a *proposal* (:func:`propose`) and the person accepts or
rejects it on the Category page. Until then the proposal's items wait in the
proposal's parent node (or stay where they were when the proposal is for a
top-level node); accepting moves the ones still waiting there. The choice is
per request -- the "Assign to tree" pane's proposal checkbox and
``category_assign``'s ``proposal`` argument -- and the default is to create.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from difflib import SequenceMatcher

from . import config, workspaces
from .glossary import normalize

KINDS = ("session", "issue")
#: Fuzzy similarity at or above this is reported as a near duplicate.
SIMILAR = 0.72


class CategoryError(Exception):
    def __init__(self, message, status=400, **extra):
        super().__init__(message)
        self.status = status
        self.extra = extra


def workspace(name=None, cwd=None):
    found = workspaces.get(name) if name else workspaces.owning(cwd or "")
    if found is None:
        raise CategoryError("choose a registered workspace", 404)
    return found


def db_path(ws):
    root = workspaces.normalize_path(ws.path)
    key = hashlib.sha256(os.path.normcase(root).encode("utf-8")).hexdigest()
    return config.launcher_home() / "categories" / (key + ".sqlite3")


@contextmanager
def connection(ws):
    path = db_path(ws)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(path), timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys = ON")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS nodes (
                    id TEXT PRIMARY KEY, parent_id TEXT REFERENCES nodes(id),
                    label TEXT NOT NULL, key TEXT NOT NULL UNIQUE,
                    aliases TEXT NOT NULL, description TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS items (
                    kind TEXT NOT NULL, ref TEXT NOT NULL,
                    node_id TEXT NOT NULL REFERENCES nodes(id),
                    assigned_by TEXT NOT NULL, reason TEXT NOT NULL,
                    assigned_at TEXT NOT NULL,
                    PRIMARY KEY (kind, ref)
                );
                CREATE TABLE IF NOT EXISTS proposals (
                    id TEXT PRIMARY KEY, parent_id TEXT,
                    label TEXT NOT NULL, key TEXT NOT NULL,
                    aliases TEXT NOT NULL, description TEXT NOT NULL,
                    reason TEXT NOT NULL, items TEXT NOT NULL,
                    proposed_by TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', node_id TEXT,
                    created_at TEXT NOT NULL, reviewed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    session TEXT PRIMARY KEY, mode TEXT NOT NULL,
                    fork TEXT, state TEXT NOT NULL, note TEXT NOT NULL,
                    result TEXT, started_at TEXT NOT NULL, finished_at TEXT
                );
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL,
                    actor TEXT NOT NULL, op TEXT NOT NULL, detail TEXT NOT NULL
                );
            """)
            # A job carries the choice made when it was requested: create the
            # new category at once (0) or file it as a proposal for a person
            # (1). Tables made before the column existed get it here.
            if "proposal" not in {row[1] for row in db.execute("PRAGMA table_info(jobs)")}:
                db.execute("ALTER TABLE jobs ADD COLUMN proposal INTEGER NOT NULL DEFAULT 0")
            with db:
                yield db
        finally:
            db.close()
    except sqlite3.IntegrityError as exc:
        raise CategoryError(f"category constraint failed: {exc}", 409) from exc
    except (OSError, sqlite3.Error) as exc:
        raise CategoryError(f"cannot access categories: {exc}", 500) from exc


# ---- validation ------------------------------------------------------------- #

def _text(value, name, cap, required=False):
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise CategoryError(f"{name} must be a string")
    value = value.strip()
    if len(value) > cap or (required and not value):
        raise CategoryError(f"{name} must contain {'1' if required else '0'}..{cap} characters")
    return value


def _label(value, name="label"):
    label = _text(value, name, 120, True)
    if not normalize(label):
        raise CategoryError(f"{name} must contain letters or digits")
    return label


def _aliases(value, label):
    if value is None:
        value = []
    if not isinstance(value, list) or len(value) > 30:
        raise CategoryError("aliases must be an array of at most 30 strings")
    kept, seen = [], {normalize(label)}
    for alias in value:
        alias = _label(alias, "alias")
        key = normalize(alias)
        if key not in seen:
            kept.append(alias)
            seen.add(key)
    return kept


def _items(value):
    """``[{kind, ref}]`` (or ``[[kind, ref]]``) -> unique ``[(kind, ref)]``."""
    if not isinstance(value, list) or len(value) > 500:
        raise CategoryError("items must be an array of at most 500 {kind, ref}")
    out = []
    for entry in value:
        if isinstance(entry, dict):
            kind, ref = entry.get("kind"), entry.get("ref")
        elif isinstance(entry, (list, tuple)) and len(entry) == 2:
            kind, ref = entry
        else:
            raise CategoryError("each item must be {kind, ref}")
        if kind not in KINDS:
            raise CategoryError(f"item kind must be one of {', '.join(KINDS)}")
        ref = _text(ref, "item ref", 200, True)
        if (kind, ref) not in out:
            out.append((kind, ref))
    return out


def _now():
    return datetime.now(timezone.utc).isoformat()


def _event(db, actor, op, **detail):
    db.execute("INSERT INTO events (at, actor, op, detail) VALUES (?,?,?,?)",
               (_now(), actor or "", op, json.dumps(detail, ensure_ascii=False)))


# ---- rows -------------------------------------------------------------------- #

def _node(row):
    out = dict(row)
    out.pop("key", None)
    out["aliases"] = json.loads(out["aliases"])
    return out


def _proposal(row):
    out = dict(row)
    out.pop("key", None)
    out["aliases"] = json.loads(out["aliases"])
    # Each item keeps the node it sat in when it joined the proposal (after
    # parking): accepting moves only the items still there.
    out["items"] = [{"kind": k, "ref": r, "waiting_in": w} for k, r, w in json.loads(out["items"])]
    return out


def _get_node(db, ident):
    row = db.execute("SELECT * FROM nodes WHERE id=?", (ident,)).fetchone()
    if row is None:
        raise CategoryError("category node not found", 404)
    return _node(row)


def _parent_of(db, ident):
    row = db.execute("SELECT parent_id FROM nodes WHERE id=?", (ident,)).fetchone()
    return row["parent_id"] if row else None


def _ancestors(db, ident):
    """``ident`` and its ancestors, nearest first (``None`` = the root)."""
    chain, seen = [], set()
    while ident is not None and ident not in seen:
        seen.add(ident)
        chain.append(ident)
        ident = _parent_of(db, ident)
    return chain + [None]


def _path(db, ident):
    labels = []
    for node in _ancestors(db, ident)[:-1]:
        row = db.execute("SELECT label FROM nodes WHERE id=?", (node,)).fetchone()
        labels.append(row["label"])
    return list(reversed(labels))


def _lca(db, anchors):
    """Deepest node that is an ancestor-or-self of every anchor (None = root)."""
    chains = [_ancestors(db, a) for a in anchors]
    if not chains:
        return None
    common = set(chains[0])
    for chain in chains[1:]:
        common &= set(chain)
    return next(n for n in chains[0] if n in common)


def _vocabulary(db, exclude=()):
    """normalized spelling -> node id, for every label and alias."""
    out = {}
    for row in db.execute("SELECT id, label, aliases FROM nodes"):
        if row["id"] in exclude:
            continue
        for spelling in [row["label"], *json.loads(row["aliases"])]:
            out.setdefault(normalize(spelling), row["id"])
    return out


def _claim_spellings(db, label, aliases, exclude=()):
    """Refuse a label/alias some other node already uses (409 with the owners)."""
    vocab = _vocabulary(db, exclude)
    taken = []
    for spelling in [label, *aliases]:
        owner = vocab.get(normalize(spelling))
        if owner and owner not in taken:
            taken.append(owner)
    if taken:
        owners = [{**_get_node(db, t), "path": _path(db, t)} for t in taken]
        raise CategoryError(
            "that keyword already names a category: "
            + ", ".join(" / ".join(o["path"]) for o in owners),
            409, candidates=owners)


def _place(db, kind, ref, node_id, actor, reason):
    db.execute(
        "INSERT INTO items VALUES (?,?,?,?,?,?) ON CONFLICT(kind, ref) DO UPDATE SET "
        "node_id=excluded.node_id, assigned_by=excluded.assigned_by, "
        "reason=excluded.reason, assigned_at=excluded.assigned_at",
        (kind, ref, node_id, actor or "", reason or "", _now()))


def _repoint(db, old, new):
    """Everything that pointed at node ``old`` points at ``new`` (None = root:
    its items become unassigned) -- items, child nodes, pending proposals and
    the place each proposal item is waiting in."""
    if new is None:
        db.execute("DELETE FROM items WHERE node_id=?", (old,))
    else:
        db.execute("UPDATE items SET node_id=? WHERE node_id=?", (new, old))
    db.execute("UPDATE nodes SET parent_id=? WHERE parent_id=?", (new, old))
    for row in db.execute("SELECT id, parent_id, items FROM proposals WHERE status='pending'").fetchall():
        entries = [[k, r, new if w == old else w] for k, r, w in json.loads(row["items"])]
        db.execute("UPDATE proposals SET parent_id=?, items=? WHERE id=?", (
            new if row["parent_id"] == old else row["parent_id"],
            json.dumps(entries, ensure_ascii=False), row["id"]))


def _insert_node(db, parent_id, label, aliases, description, actor):
    if parent_id is not None:
        _get_node(db, parent_id)
    _claim_spellings(db, label, aliases)
    ident, at = uuid.uuid4().hex, _now()
    db.execute("INSERT INTO nodes VALUES (?,?,?,?,?,?,?,?,?)", (
        ident, parent_id, label, normalize(label),
        json.dumps(aliases, ensure_ascii=False), description, actor or "", at, at))
    _event(db, actor, "create", node=ident, label=label, parent=parent_id)
    return _get_node(db, ident)


def _parent_arg(value):
    if value in (None, "", "root"):
        return None
    return _text(value, "parent_id", 64, True)


# ---- reads -------------------------------------------------------------------- #

def tree(ws):
    """The whole tree as flat lists; the caller nests them by ``parent_id``."""
    with connection(ws) as db:
        nodes = [_node(r) for r in db.execute(
            "SELECT * FROM nodes ORDER BY label COLLATE NOCASE, id")]
        items = [dict(r) for r in db.execute(
            "SELECT * FROM items ORDER BY assigned_at DESC")]
        proposals = [_proposal(r) for r in db.execute(
            "SELECT * FROM proposals WHERE status='pending' ORDER BY created_at")]
    paths, by_id = {}, {n["id"]: n for n in nodes}

    def path(ident):
        if ident not in paths:
            node = by_id[ident]
            parent = node["parent_id"]
            paths[ident] = (path(parent) if parent in by_id else []) + [node["label"]]
        return paths[ident]

    for node in nodes:
        node["path"] = path(node["id"])
    return {"nodes": nodes, "items": items, "proposals": proposals}


def _score(query, label):
    key = normalize(label)
    if query == key:
        return 1.0, "exact"
    if query in key or key in query:
        shorter, longer = sorted((len(query), len(key)))
        return 0.8 + 0.15 * shorter / longer, "substring"
    ratio = SequenceMatcher(None, unicodedata.normalize("NFD", query),
                            unicodedata.normalize("NFD", key), autojunk=False).ratio()
    return 0.8 * ratio, "fuzzy"


def search(ws, query="", limit=20):
    """Nodes whose label or alias matches ``query`` (normalized, substring, fuzzy)."""
    query = _text(query, "query", 200)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise CategoryError("limit must be an integer 1..100")
    nodes = tree(ws)["nodes"]
    key = normalize(query)
    if not query:
        return nodes[:limit]
    if not key:
        return []
    hits = []
    for node in nodes:
        score, kind, label = max((*_score(key, s), s) for s in [node["label"], *node["aliases"]])
        if kind == "fuzzy" and (len(key) < 2 or score < 0.45):
            continue
        hits.append({**node, "score": round(score, 4), "match": kind, "matched_label": label})
    return sorted(hits, key=lambda r: (-r["score"], r["label"], r["id"]))[:limit]


def items_of(ws, node_id, descendants=True):
    """Items in ``node_id`` (and, by default, everything below it)."""
    with connection(ws) as db:
        _get_node(db, node_id)
        wanted = {node_id}
        if descendants:
            frontier = [node_id]
            while frontier:
                rows = db.execute(
                    f"SELECT id FROM nodes WHERE parent_id IN ({','.join('?' * len(frontier))})",
                    frontier).fetchall()
                frontier = [r["id"] for r in rows if r["id"] not in wanted]
                wanted.update(frontier)
        rows = db.execute(
            f"SELECT * FROM items WHERE node_id IN ({','.join('?' * len(wanted))}) "
            "ORDER BY assigned_at DESC", sorted(wanted)).fetchall()
        return [{**dict(r), "path": _path(db, r["node_id"])} for r in rows]


def where(ws, kind, ref):
    """The node an item sits in (with its path), or None."""
    (kind, ref), = _items([{"kind": kind, "ref": ref}])
    with connection(ws) as db:
        row = db.execute("SELECT * FROM items WHERE kind=? AND ref=?", (kind, ref)).fetchone()
        if row is None:
            pending = [_proposal(p) for p in db.execute(
                "SELECT * FROM proposals WHERE status='pending'")]
            return {"item": None, "node": None, "proposals": _with_item(pending, kind, ref)}
        pending = [_proposal(p) for p in db.execute(
            "SELECT * FROM proposals WHERE status='pending'")]
        return {"item": dict(row),
                "node": {**_get_node(db, row["node_id"]), "path": _path(db, row["node_id"])},
                "proposals": _with_item(pending, kind, ref)}


def _with_item(pending, kind, ref):
    return [p for p in pending if any(i["kind"] == kind and i["ref"] == ref for i in p["items"])]


def events(ws, limit=100):
    with connection(ws) as db:
        rows = db.execute("SELECT * FROM events ORDER BY seq DESC LIMIT ?", (int(limit),))
        return [{**dict(r), "detail": json.loads(r["detail"])} for r in rows]


# ---- a person's edits ---------------------------------------------------------- #

def create_node(ws, body, actor=""):
    if not isinstance(body, dict):
        raise CategoryError("body must be an object")
    label = _label(body.get("label"))
    aliases = _aliases(body.get("aliases"), label)
    description = _text(body.get("description"), "description", 2000)
    with connection(ws) as db:
        db.execute("BEGIN IMMEDIATE")
        return _insert_node(db, _parent_arg(body.get("parent_id")), label, aliases,
                            description, actor)


def update_node(ws, ident, body, actor=""):
    """Rename, re-alias, re-describe or move (``parent_id``) one node."""
    if not isinstance(body, dict):
        raise CategoryError("body must be an object")
    with connection(ws) as db:
        db.execute("BEGIN IMMEDIATE")
        current = _get_node(db, ident)
        label = _label(body.get("label", current["label"]))
        aliases = _aliases(body.get("aliases", current["aliases"]), label)
        description = _text(body.get("description", current["description"]), "description", 2000)
        _claim_spellings(db, label, aliases, exclude={ident})
        parent = current["parent_id"]
        if "parent_id" in body:
            parent = _parent_arg(body.get("parent_id"))
            if parent is not None:
                _get_node(db, parent)
                if ident in _ancestors(db, parent):
                    raise CategoryError("a category cannot move under itself or its descendants")
        db.execute("UPDATE nodes SET parent_id=?, label=?, key=?, aliases=?, description=?, "
                   "updated_at=? WHERE id=?", (
                       parent, label, normalize(label), json.dumps(aliases, ensure_ascii=False),
                       description, _now(), ident))
        _event(db, actor, "update", node=ident, label=label, parent=parent,
               moved=parent != current["parent_id"],
               renamed=label != current["label"])
        return _get_node(db, ident)


def delete_node(ws, ident, actor=""):
    """Delete a node without children; its items move up to its parent
    (or become unassigned when it was a top-level node)."""
    with connection(ws) as db:
        db.execute("BEGIN IMMEDIATE")
        node = _get_node(db, ident)
        if db.execute("SELECT 1 FROM nodes WHERE parent_id=?", (ident,)).fetchone():
            raise CategoryError("move or delete its child categories first", 409)
        parent = node["parent_id"]
        _repoint(db, ident, parent)
        db.execute("DELETE FROM nodes WHERE id=?", (ident,))
        _event(db, actor, "delete", node=ident, label=node["label"], parent=parent)
        return {"deleted": ident, "items_to": parent}


def assign(ws, items, node_id, actor="", reason=""):
    """Put items into an existing node (moving them from wherever they were)."""
    items = _items(items)
    if not items:
        raise CategoryError("no items to assign")
    reason = _text(reason, "reason", 2000)
    with connection(ws) as db:
        db.execute("BEGIN IMMEDIATE")
        node = _get_node(db, _text(node_id, "node_id", 64, True))
        for kind, ref in items:
            _place(db, kind, ref, node["id"], actor, reason)
        _event(db, actor, "assign", node=node["id"], items=items, reason=reason)
        return {"node": {**node, "path": _path(db, node["id"])},
                "items": [{"kind": k, "ref": r} for k, r in items]}


def unassign(ws, items, actor=""):
    items = _items(items)
    with connection(ws) as db:
        db.execute("BEGIN IMMEDIATE")
        for kind, ref in items:
            db.execute("DELETE FROM items WHERE kind=? AND ref=?", (kind, ref))
        _event(db, actor, "unassign", items=items)
        return {"items": [{"kind": k, "ref": r} for k, r in items]}


def _new_node_body(body):
    label = _label(body.get("label"))
    return label, _aliases(body.get("aliases"), label), _text(
        body.get("description"), "description", 2000)


def extract(ws, node_id, items, body, actor=""):
    """Move ``items`` (all inside ``node_id``'s subtree) into a new child of ``node_id``."""
    items = _items(items)
    if not items:
        raise CategoryError("select the items to extract")
    if not isinstance(body, dict):
        raise CategoryError("body must be an object")
    label, aliases, description = _new_node_body(body)
    with connection(ws) as db:
        db.execute("BEGIN IMMEDIATE")
        parent = _get_node(db, _text(node_id, "node_id", 64, True))["id"]
        for kind, ref in items:
            row = db.execute("SELECT node_id FROM items WHERE kind=? AND ref=?",
                             (kind, ref)).fetchone()
            if row is None or parent not in _ancestors(db, row["node_id"]):
                raise CategoryError(f"{kind} {ref} is not inside the category being split")
        node = _insert_node(db, parent, label, aliases, description, actor)
        for kind, ref in items:
            _place(db, kind, ref, node["id"], actor, "extract")
        _event(db, actor, "extract", node=node["id"], parent=parent, items=items)
        return {"node": {**node, "path": _path(db, node["id"])},
                "items": [{"kind": k, "ref": r} for k, r in items]}


def merge(ws, body, actor=""):
    """Gather items and/or whole nodes spread over the tree into one new node
    under their lowest common ancestor.

    ``items`` move into the new node (an unassigned item counts as sitting at
    the root). Each of ``nodes`` is folded in: its items and child categories
    move to the new node, its label and aliases become the new node's aliases,
    and it is deleted. A selected node may not contain another selected one.
    """
    if not isinstance(body, dict):
        raise CategoryError("body must be an object")
    items = _items(body.get("items") or [])
    folded = body.get("nodes") or []
    if not isinstance(folded, list) or len(folded) > 100:
        raise CategoryError("nodes must be an array of at most 100 ids")
    folded = list(dict.fromkeys(_text(n, "node id", 64, True) for n in folded))
    if len(items) + len(folded) < 2:
        raise CategoryError("select at least two items or categories to merge")
    label, aliases, description = _new_node_body(body)
    with connection(ws) as db:
        db.execute("BEGIN IMMEDIATE")
        nodes = [_get_node(db, n) for n in folded]
        for node in nodes:
            above = _ancestors(db, node["parent_id"])
            clash = [o["label"] for o in nodes if o["id"] in above]
            if clash:
                raise CategoryError(
                    f"{node['label']} is inside {clash[0]}, which is also selected")
        anchors = [n["parent_id"] for n in nodes]
        for kind, ref in items:
            row = db.execute("SELECT node_id FROM items WHERE kind=? AND ref=?",
                             (kind, ref)).fetchone()
            anchors.append(row["node_id"] if row else None)
        parent = _lca(db, anchors)
        # The folded nodes' spellings move to the new node; free them first.
        absorbed = []
        for node in nodes:
            for spelling in [node["label"], *node["aliases"]]:
                if normalize(spelling) not in {normalize(s) for s in [label, *aliases, *absorbed]}:
                    absorbed.append(spelling)
        ident, at = uuid.uuid4().hex, _now()
        exclude = {n["id"] for n in nodes}
        _claim_spellings(db, label, aliases + absorbed, exclude=exclude)
        # Insert under a placeholder key, re-point, then drop the folded nodes
        # (their keys are UNIQUE and still taken until they are gone).
        db.execute("INSERT INTO nodes VALUES (?,?,?,?,?,?,?,?,?)", (
            ident, parent, label, "\x00" + ident, "[]", description, actor or "", at, at))
        for node in nodes:
            _repoint(db, node["id"], ident)
            db.execute("DELETE FROM nodes WHERE id=?", (node["id"],))
        db.execute("UPDATE nodes SET key=?, aliases=? WHERE id=?", (
            normalize(label), json.dumps(aliases + absorbed, ensure_ascii=False), ident))
        for kind, ref in items:
            _place(db, kind, ref, ident, actor, "merge")
        _event(db, actor, "merge", node=ident, parent=parent, items=items,
               folded=[{"id": n["id"], "label": n["label"]} for n in nodes])
        return {"node": {**_get_node(db, ident), "path": _path(db, ident)},
                "items": [{"kind": k, "ref": r} for k, r in items],
                "folded": [n["id"] for n in nodes]}


# ---- an agent's proposals ------------------------------------------------------- #

def similar(ws, label, limit=5):
    """Existing nodes whose spelling is close to ``label`` (for the proposer to see)."""
    key = normalize(label)
    out = []
    for node in tree(ws)["nodes"]:
        score = max(_score(key, s)[0] for s in [node["label"], *node["aliases"]])
        if score >= SIMILAR:
            out.append({**node, "score": round(score, 4)})
    return sorted(out, key=lambda n: -n["score"])[:limit]


def create_now(ws, body, items, actor="", reason=""):
    """Create the category and place ``items`` in it at once -- no review card.

    This is the agent's default (``category_assign`` without ``proposal``),
    and what the pane asks for while its "제안으로 등록" box is unchecked. A
    spelling that already names a node is still refused (409, the owners as
    candidates), so the tree keeps one node per keyword; the caller assigns
    to that node instead of inventing a synonym. ``reason`` is kept on the
    placement, as :func:`assign` keeps it.
    """
    if not isinstance(body, dict):
        raise CategoryError("body must be an object")
    label = _label(body.get("label"))
    aliases = _aliases(body.get("aliases"), label)
    description = _text(body.get("description"), "description", 2000)
    reason = _text(reason or body.get("reason"), "reason", 2000)
    placed = _items(items)
    with connection(ws) as db:
        db.execute("BEGIN IMMEDIATE")
        node = _insert_node(db, _parent_arg(body.get("parent_id")), label, aliases,
                            description, actor)
        for kind, ref in placed:
            _place(db, kind, ref, node["id"], actor, reason)
        if placed:
            _event(db, actor, "assign", node=node["id"],
                   items=[{"kind": k, "ref": r} for k, r in placed], reason=reason)
        return {**node, "path": _path(db, node["id"])}


def propose(ws, body, proposed_by=""):
    """File a new category for a person to review.

    An exact spelling clash with an existing node is refused (409, the owner
    as candidate: assign to it instead). A pending proposal for the same
    spelling under the same parent absorbs the new items instead of queueing
    a second card. When ``parent_id`` is given the items are placed in that
    parent now, so they are findable while the proposal waits.
    """
    if not isinstance(body, dict):
        raise CategoryError("body must be an object")
    label = _label(body.get("label"))
    aliases = _aliases(body.get("aliases"), label)
    description = _text(body.get("description"), "description", 2000)
    reason = _text(body.get("reason"), "reason", 2000, True)
    items = _items(body.get("items") or [])
    proposed_by = _text(proposed_by, "proposed_by", 200)
    parent = _parent_arg(body.get("parent_id"))
    with connection(ws) as db:
        db.execute("BEGIN IMMEDIATE")
        if parent is not None:
            _get_node(db, parent)
        _claim_spellings(db, label, aliases)
        if parent is not None:
            for kind, ref in items:
                _place(db, kind, ref, parent, proposed_by, f"waiting for proposal {label}")
        entries = []
        for kind, ref in items:
            at = db.execute("SELECT node_id FROM items WHERE kind=? AND ref=?",
                            (kind, ref)).fetchone()
            entries.append([kind, ref, at["node_id"] if at else None])
        row = db.execute(
            "SELECT * FROM proposals WHERE status='pending' AND key=? AND "
            "COALESCE(parent_id, '')=?", (normalize(label), parent or "")).fetchone()
        if row is not None:
            ident = row["id"]
            merged = [e for e in json.loads(row["items"])
                      if (e[0], e[1]) not in {(k, r) for k, r, _ in entries}] + entries
            db.execute("UPDATE proposals SET items=? WHERE id=?",
                       (json.dumps(merged, ensure_ascii=False), ident))
        else:
            ident = uuid.uuid4().hex
            db.execute("INSERT INTO proposals VALUES (?,?,?,?,?,?,?,?,?,'pending',NULL,?,NULL)", (
                ident, parent, label, normalize(label), json.dumps(aliases, ensure_ascii=False),
                description, reason, json.dumps(entries, ensure_ascii=False), proposed_by, _now()))
        _event(db, proposed_by, "propose", proposal=ident, label=label, parent=parent,
               items=items, reason=reason)
        out = _proposal(db.execute("SELECT * FROM proposals WHERE id=?", (ident,)).fetchone())
    out["similar"] = [n for n in similar(ws, label) if normalize(n["label"]) != normalize(label)]
    return out


def proposals(ws, status="pending"):
    if status not in ("pending", "accepted", "rejected", "all"):
        raise CategoryError("invalid proposal status")
    with connection(ws) as db:
        rows = db.execute("SELECT * FROM proposals " + ("" if status == "all" else "WHERE status=? ")
                          + "ORDER BY created_at DESC", () if status == "all" else (status,))
        return [_proposal(r) for r in rows]


def review(ws, ident, decision, actor="", body=None):
    """Accept (create the node, move the proposal's items still waiting) or reject.

    ``body`` may override the label, aliases, description and parent on accept.
    Items a person has meanwhile moved elsewhere are left where they are.
    """
    if decision not in ("accept", "reject"):
        raise CategoryError("decision must be accept or reject")
    body = body if isinstance(body, dict) else {}
    with connection(ws) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM proposals WHERE id=?", (ident,)).fetchone()
        if row is None:
            raise CategoryError("proposal not found", 404)
        proposal = _proposal(row)
        if proposal["status"] != "pending":
            raise CategoryError("proposal has already been reviewed", 409)
        node = None
        if decision == "accept":
            label = _label(body.get("label", proposal["label"]))
            aliases = _aliases(body.get("aliases", proposal["aliases"]), label)
            description = _text(body.get("description", proposal["description"]),
                                "description", 2000)
            parent = (_parent_arg(body.get("parent_id")) if "parent_id" in body
                      else proposal["parent_id"])
            if parent is not None and db.execute(
                    "SELECT 1 FROM nodes WHERE id=?", (parent,)).fetchone() is None:
                parent = None
            node = _insert_node(db, parent, label, aliases, description, actor)
            for item in proposal["items"]:
                current = db.execute("SELECT node_id FROM items WHERE kind=? AND ref=?",
                                     (item["kind"], item["ref"])).fetchone()
                # Still where it was when it joined the proposal: it follows.
                # Moved by someone since: it stays.
                if (current["node_id"] if current else None) == item["waiting_in"]:
                    _place(db, item["kind"], item["ref"], node["id"],
                           proposal["proposed_by"], proposal["reason"])
            node = {**node, "path": _path(db, node["id"])}
        db.execute("UPDATE proposals SET status=?, node_id=?, reviewed_at=? WHERE id=?", (
            "accepted" if node else "rejected", node["id"] if node else None, _now(), ident))
        _event(db, actor, "review", proposal=ident, decision=decision,
               node=node["id"] if node else None)
        out = _proposal(db.execute("SELECT * FROM proposals WHERE id=?", (ident,)).fetchone())
        return {"proposal": out, "node": node}


# ---- assignment jobs ------------------------------------------------------------ #
# One "assign to tree" request per session at a time. The row outlives a daemon
# restart, which is what lets the merge of a quick-fork that was already
# running be recognised afterwards (daemon/category_assign.py).

RUNNING, ASSIGNED, DONE, FAILED = "running", "assigned", "done", "failed"


def _job(row):
    if row is None:
        return None
    out = dict(row)
    out["result"] = json.loads(out["result"]) if out["result"] else None
    out["proposal"] = bool(out.get("proposal"))
    return out


def job(ws, session):
    with connection(ws) as db:
        return _job(db.execute("SELECT * FROM jobs WHERE session=?", (session,)).fetchone())


def job_for_fork(ws, fork):
    with connection(ws) as db:
        return _job(db.execute("SELECT * FROM jobs WHERE fork=? AND state IN (?,?)",
                               (fork, RUNNING, ASSIGNED)).fetchone())


def job_start(ws, session, mode, fork=None, replace_inplace=False, proposal=False):
    """Open a job; 409 while the session already has one running.
    ``replace_inplace`` lets a new request replace a RUNNING in-place job
    (one the session itself was asked to do and has not done yet).
    ``proposal`` is what the person asked for: file a new category for review
    instead of creating it (the pane's "제안으로 등록" checkbox)."""
    with connection(ws) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM jobs WHERE session=?", (session,)).fetchone()
        stale = (replace_inplace and row is not None and row["mode"] == "inplace"
                 and row["state"] == RUNNING)
        if row is not None and row["state"] in (RUNNING, ASSIGNED) and not stale:
            raise CategoryError(f"{session} already has a category assignment running"
                                + (f" ({row['fork']})" if row["fork"] else ""), 409,
                                job=_job(row))
        db.execute("INSERT OR REPLACE INTO jobs (session, mode, fork, state, note, "
                   "result, started_at, finished_at, proposal) VALUES (?,?,?,?,?,NULL,?,NULL,?)",
                   (session, mode, fork, RUNNING, "", _now(), 1 if proposal else 0))
        return _job(db.execute("SELECT * FROM jobs WHERE session=?", (session,)).fetchone())


def job_update(ws, session, state, note="", result=None):
    with connection(ws) as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM jobs WHERE session=?", (session,)).fetchone() is None:
            return None
        finished = _now() if state in (DONE, FAILED) else None
        db.execute("UPDATE jobs SET state=?, note=?, result=COALESCE(?, result), finished_at=? "
                   "WHERE session=?", (state, note or "", json.dumps(result, ensure_ascii=False)
                                       if result is not None else None, finished, session))
        return _job(db.execute("SELECT * FROM jobs WHERE session=?", (session,)).fetchone())
