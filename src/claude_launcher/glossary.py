"""Workspace glossaries: reviewed terms, lexical matching and explicit references.

One SQLite file per registered directory, outside git worktrees. Workspace
renames preserve identity; different roots (even with the same term) are isolated.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

from . import config, workspaces


class GlossaryError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def normalize(text):
    """Case/width/spacing/punctuation variants share a lookup key."""
    return "".join(c for c in unicodedata.normalize("NFKC", text).casefold()
                   if c.isalnum())


def workspace(name=None, cwd=None):
    found = workspaces.get(name) if name else workspaces.owning(cwd or "")
    if found is None:
        raise GlossaryError("choose a registered workspace", 404)
    return found


def db_path(ws):
    root = workspaces.normalize_path(ws.path)
    # Match the registry's Windows case handling.
    import os
    key = hashlib.sha256(os.path.normcase(root).encode("utf-8")).hexdigest()
    return config.launcher_home() / "glossaries" / (key + ".sqlite3")


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
                CREATE TABLE IF NOT EXISTS terms (
                    id TEXT PRIMARY KEY, term TEXT NOT NULL,
                    key TEXT NOT NULL UNIQUE, definition TEXT NOT NULL,
                    aliases TEXT NOT NULL, source TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS suggestions (
                    id TEXT PRIMARY KEY, term TEXT NOT NULL,
                    definition TEXT NOT NULL, aliases TEXT NOT NULL,
                    source TEXT NOT NULL, proposed_by TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', term_id TEXT,
                    created_at TEXT NOT NULL, reviewed_at TEXT
                );
            """)
            with db:
                yield db
        finally:
            db.close()
    except sqlite3.IntegrityError as exc:
        raise GlossaryError("this workspace already has that normalized term", 409) from exc
    except (OSError, sqlite3.Error) as exc:
        raise GlossaryError(f"cannot access glossary: {exc}", 500) from exc


def _text(value, name, cap, required=False):
    if not isinstance(value, str):
        raise GlossaryError(f"{name} must be a string")
    value = value.strip()
    if len(value) > cap or (required and not value):
        raise GlossaryError(f"{name} must contain {'1' if required else '0'}..{cap} characters")
    return value


def clean(body):
    if not isinstance(body, dict):
        raise GlossaryError("body must be an object")
    term = _text(body.get("term", ""), "term", 200, True)
    if not normalize(term):
        raise GlossaryError("term must contain letters or digits")
    aliases = body.get("aliases", [])
    if not isinstance(aliases, list) or len(aliases) > 50:
        raise GlossaryError("aliases must be an array of at most 50 strings")
    kept, seen = [], {normalize(term)}
    for alias in aliases:
        alias = _text(alias, "alias", 200, True)
        key = normalize(alias)
        if not key:
            raise GlossaryError("alias must contain letters or digits")
        if key not in seen:
            kept.append(alias)
            seen.add(key)
    return {"term": term, "definition": _text(body.get("definition", ""), "definition", 10000, True),
            "aliases": kept, "source": _text(body.get("source", ""), "source", 2000)}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _row(row):
    out = dict(row)
    out.pop("key", None)
    out["aliases"] = json.loads(out["aliases"])
    if "status" not in out:
        out["reference"] = f"[[term:#{out['id']}]]"
    return out


def _get(db, table, ident):
    row = db.execute(f"SELECT * FROM {table} WHERE id=?", (ident,)).fetchone()
    if row is None:
        raise GlossaryError("glossary entry not found", 404)
    return _row(row)


def _insert(db, value):
    ident, at = uuid.uuid4().hex, _now()
    db.execute("INSERT INTO terms VALUES (?,?,?,?,?,?,?,?)", (
        ident, value["term"], normalize(value["term"]), value["definition"],
        json.dumps(value["aliases"], ensure_ascii=False), value["source"], at, at))
    return _get(db, "terms", ident)


def create(ws, body):
    value = clean(body)
    with connection(ws) as db:
        return _insert(db, value)


def update(ws, ident, body):
    with connection(ws) as db:
        current = _get(db, "terms", ident)
        if not isinstance(body, dict):
            raise GlossaryError("body must be an object")
        value = clean({**current, **body})
        db.execute("UPDATE terms SET term=?, key=?, definition=?, aliases=?, source=?, updated_at=? WHERE id=?", (
            value["term"], normalize(value["term"]), value["definition"],
            json.dumps(value["aliases"], ensure_ascii=False), value["source"], _now(), ident))
        return _get(db, "terms", ident)


def delete(ws, ident):
    with connection(ws) as db:
        _get(db, "terms", ident)
        db.execute("DELETE FROM terms WHERE id=?", (ident,))


def suggest(ws, body, proposed_by=""):
    value = clean(body)
    proposed_by = _text(proposed_by, "proposed_by", 200)
    with connection(ws) as db:
        # Hold the write lock before reading: concurrent retries must observe
        # the preceding caller's insert instead of each creating a proposal.
        db.execute("BEGIN IMMEDIATE")
        # Repeated tool calls for the same proposal do not fill the review queue.
        encoded = json.dumps(value["aliases"], ensure_ascii=False)
        existing = db.execute("SELECT * FROM suggestions WHERE status='pending' AND term=? AND definition=? AND aliases=? AND source=? AND proposed_by=?", (
            value["term"], value["definition"], encoded, value["source"], proposed_by)).fetchone()
        if existing:
            return _row(existing)
        ident = uuid.uuid4().hex
        db.execute("INSERT INTO suggestions VALUES (?,?,?,?,?,?,'pending',NULL,?,NULL)", (
            ident, value["term"], value["definition"], encoded, value["source"], proposed_by, _now()))
        return _get(db, "suggestions", ident)


def suggestions(ws, status="pending"):
    if status not in ("pending", "accepted", "rejected", "all"):
        raise GlossaryError("invalid suggestion status")
    with connection(ws) as db:
        rows = db.execute("SELECT * FROM suggestions " + ("" if status == "all" else "WHERE status=? ") + "ORDER BY created_at DESC", () if status == "all" else (status,))
        return [_row(row) for row in rows]


def review(ws, ident, decision):
    if decision not in ("accept", "reject"):
        raise GlossaryError("decision must be accept or reject")
    with connection(ws) as db:
        # Serialize reviewers before checking pending: one proposal creates one term.
        db.execute("BEGIN IMMEDIATE")
        proposal = _get(db, "suggestions", ident)
        if proposal["status"] != "pending":
            raise GlossaryError("suggestion has already been reviewed", 409)
        term = _insert(db, clean(proposal)) if decision == "accept" else None
        db.execute("UPDATE suggestions SET status=?, term_id=?, reviewed_at=? WHERE id=?", (
            "accepted" if term else "rejected", term["id"] if term else None, _now(), ident))
        return {"suggestion": _get(db, "suggestions", ident), "term": term}


def entries(ws):
    with connection(ws) as db:
        return [_row(row) for row in db.execute("SELECT * FROM terms ORDER BY term COLLATE NOCASE, id")]


def _match(query, label):
    key = normalize(label)
    if query == key:
        return 1.0, "exact"
    if query in key:
        return 0.85 + 0.1 * len(query) / len(key), "substring"
    # Hangul decomposed for typo matching within syllables; Unicode NFC remains
    # the storage/display spelling. Short queries need actual character overlap.
    ratio = SequenceMatcher(None, unicodedata.normalize("NFD", query),
                            unicodedata.normalize("NFD", key), autojunk=False).ratio()
    return 0.8 * ratio, "fuzzy"


def search(ws, query="", limit=50):
    query = _text(query, "query", 200)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise GlossaryError("limit must be an integer 1..100")
    rows = entries(ws)
    key = normalize(query)
    if not query:
        return rows[:limit]
    if not key:
        return []
    hits = []
    for row in rows:
        labels = [row["term"], *row["aliases"]]
        score, kind, label = max((*_match(key, label), label) for label in labels)
        if kind == "fuzzy" and (len(key) < 3 or score < 0.48):
            continue
        hits.append({**row, "score": round(score, 4), "match": kind, "matched_label": label})
    return sorted(hits, key=lambda r: (-r["score"], r["term"], r["id"]))[:limit]


_REFERENCE = re.compile(r"(?<!\\)\[\[term:([^\[\]\n]{1,200})\]\]")


def resolve(ws, text):
    """A quasi-quotation inspired template: literals plus explicit term nodes.

    No evaluation, recursive expansion or fuzzy substitution. Unresolved names
    retain their literal text and supply suggestions for a person's choice.
    """
    # Validate without stripping: offsets refer to the caller's literal text.
    _text(text, "text", 20000)
    if len(text) > 20000:
        raise GlossaryError("text must contain 0..20000 characters")
    rows = entries(ws)
    refs = []
    for match in _REFERENCE.finditer(text):
        name = match.group(1).strip()
        hits = [r for r in rows if (r["id"] == name[1:] if name.startswith("#")
                else normalize(name) in [normalize(r["term"]), *map(normalize, r["aliases"])])]
        node = {"literal": match.group(0), "start": match.start(), "end": match.end(),
                "status": "resolved" if len(hits) == 1 else "ambiguous" if hits else "missing"}
        if len(hits) == 1:
            node["term"] = hits[0]
        else:
            node["candidates"] = hits if hits else ([] if name.startswith("#") else search(ws, name, 5))
        refs.append(node)
    return {"text": text, "references": refs}
