"""Workspace knowledge: decisions, vision, needs and principles as records.

Issues and session transcripts carry the reasons behind the work only
indirectly; a session that wants to know *why* the workspace does something
has to reconstruct it from comments. This module keeps those reasons as
explicit records (claunch-269q1) from two sources, searched as one index:

- **Files** — ``<workspace>/.claunch/knowledge/*.md``, one record per file
  with a YAML front matter block. Tracked in git, so a record lands through
  review like code. Read only here: edit the file.
- **Database** — one SQLite file per registered workspace beside the
  glossary's (``<launcher home>/knowledge/<key>.sqlite3``), written by people
  on the Knowledge page and by agents through ``knowledge_record``. Lives
  outside git worktrees so a write after a round's tree is clean leaves the
  tree clean.

Database records keep their versions (claunch-qgdfc): every edit that changes
a field writes the record as it stood to ``revisions`` before the new value
lands, so no text is lost and ``history`` can show what the record said and
who changed it. The current row is the newest version; ``revisions`` holds the
older ones. File records are versioned by git instead.

Search is lexical (casefolded substrings over title, tags, refs and body),
which is enough for a few hundred records and keeps Korean spellings with
particles matchable without a tokenizer. Embedding search belongs to the
``rag`` service and is a follow-up.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import config, workspaces

KINDS = ("vision", "need", "decision", "principle", "lesson")
KIND_LABELS = {"vision": "비전", "need": "니즈", "decision": "의사결정",
               "principle": "원칙", "lesson": "교훈"}
STATUSES = ("active", "superseded")
FILES_DIR = Path(".claunch") / "knowledge"
_FRONT = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.S)


class KnowledgeError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def workspace(name=None, cwd=None):
    found = workspaces.get(name) if name else workspaces.owning(cwd or "")
    if found is None:
        raise KnowledgeError("choose a registered workspace", 404)
    return found


def db_path(ws):
    root = workspaces.normalize_path(ws.path)
    # Same key as the glossary's: the registry's Windows case handling.
    key = hashlib.sha256(os.path.normcase(root).encode("utf-8")).hexdigest()
    return config.launcher_home() / "knowledge" / (key + ".sqlite3")


def files_dir(ws):
    return Path(workspaces.normalize_path(ws.path)) / FILES_DIR


@contextmanager
def connection(ws):
    path = db_path(ws)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(path), timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS records (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL,
                    title TEXT NOT NULL, body TEXT NOT NULL,
                    tags TEXT NOT NULL, refs TEXT NOT NULL,
                    source TEXT NOT NULL, author TEXT NOT NULL,
                    status TEXT NOT NULL, supersedes TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS revisions (
                    record_id TEXT NOT NULL, version INTEGER NOT NULL,
                    kind TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,
                    tags TEXT NOT NULL, refs TEXT NOT NULL,
                    source TEXT NOT NULL, author TEXT NOT NULL,
                    status TEXT NOT NULL, supersedes TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    archived_at TEXT NOT NULL, archived_by TEXT NOT NULL,
                    PRIMARY KEY (record_id, version)
                );
            """)
            # Databases written before versions (claunch-qgdfc) have no column.
            columns = {row[1] for row in db.execute("PRAGMA table_info(records)")}
            if "version" not in columns:
                db.execute("ALTER TABLE records ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
            with db:
                yield db
        finally:
            db.close()
    except (OSError, sqlite3.Error) as exc:
        raise KnowledgeError(f"cannot access knowledge store: {exc}", 500) from exc


def _now():
    return datetime.now(timezone.utc).isoformat()


def _text(value, name, cap, required=False):
    if not isinstance(value, str):
        raise KnowledgeError(f"{name} must be a string")
    value = value.strip()
    if len(value) > cap or (required and not value):
        raise KnowledgeError(f"{name} must contain {'1' if required else '0'}..{cap} characters")
    return value


def _strings(value, name, cap=30):
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or len(value) > cap:
        raise KnowledgeError(f"{name} must be an array of at most {cap} strings")
    kept = []
    for item in value:
        item = _text(item, name, 200)
        if item and item not in kept:
            kept.append(item)
    return kept


def clean(body):
    if not isinstance(body, dict):
        raise KnowledgeError("body must be an object")
    kind = body.get("kind", "decision")
    if kind not in KINDS:
        raise KnowledgeError("kind must be one of " + ", ".join(KINDS))
    status = body.get("status", "active")
    if status not in STATUSES:
        raise KnowledgeError("status must be active or superseded")
    return {"kind": kind, "title": _text(body.get("title", ""), "title", 200, True),
            "body": _text(body.get("body", ""), "body", 20000, True),
            "tags": _strings(body.get("tags"), "tags"),
            "refs": _strings(body.get("refs"), "refs"),
            "source": _text(body.get("source", ""), "source", 2000),
            "status": status,
            "supersedes": _text(body.get("supersedes", ""), "supersedes", 200)}


def _row(row):
    out = dict(row)
    out["tags"] = json.loads(out["tags"])
    out["refs"] = json.loads(out["refs"])
    out["origin"] = "db"
    out.setdefault("version", 1)
    return out


def _get(db, ident):
    row = db.execute("SELECT * FROM records WHERE id=?", (ident,)).fetchone()
    if row is None:
        raise KnowledgeError("knowledge record not found", 404)
    return _row(row)


def _writable(ident):
    if ident.startswith("md:"):
        raise KnowledgeError("a file record is edited in git: change the file under .claunch/knowledge/", 409)


def _snapshot(db, row, editor):
    """Keep the record as it stands now as the version the next edit replaces."""
    db.execute(
        "INSERT OR REPLACE INTO revisions (record_id, version, kind, title, body, tags, refs, "
        "source, author, status, supersedes, created_at, updated_at, archived_at, archived_by) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            row["id"], row["version"], row["kind"], row["title"], row["body"],
            json.dumps(row["tags"], ensure_ascii=False),
            json.dumps(row["refs"], ensure_ascii=False),
            row["source"], row["author"], row["status"], row["supersedes"],
            row["created_at"], row["updated_at"], _now(), editor))


def _supersede(db, old, editor=""):
    # Only database records can be marked; a file record names its own status.
    # The status change is itself a version, so the history says who replaced it.
    if not old or old.startswith("md:"):
        return
    row = db.execute("SELECT * FROM records WHERE id=?", (old,)).fetchone()
    if row is None:
        return
    current = _row(row)
    if current["status"] == "superseded":
        return
    _snapshot(db, current, editor)
    db.execute("UPDATE records SET status='superseded', updated_at=?, version=? WHERE id=?",
               (_now(), current["version"] + 1, old))


def create(ws, body, author=""):
    value = clean(body)
    author = _text(author or body.get("author", ""), "author", 200)
    ident, at = uuid.uuid4().hex[:12], _now()
    with connection(ws) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            ident, value["kind"], value["title"], value["body"],
            json.dumps(value["tags"], ensure_ascii=False),
            json.dumps(value["refs"], ensure_ascii=False),
            value["source"], author, value["status"], value["supersedes"], at, at, 1))
        _supersede(db, value["supersedes"], author or "knowledge_record")
        return _get(db, ident)


def update(ws, ident, body, author=""):
    _writable(ident)
    if not isinstance(body, dict):
        raise KnowledgeError("body must be an object")
    editor = _text(author or body.get("author", "") or "web", "author", 200)
    with connection(ws) as db:
        # Read and write under one write lock: two editors at once must not both
        # snapshot the same version and lose one of the changes.
        db.execute("BEGIN IMMEDIATE")
        current = _get(db, ident)
        value = clean({**current, **body})
        changed = any(value[key] != current[key] for key in
                      ("kind", "title", "body", "tags", "refs", "source", "status", "supersedes"))
        if changed:
            _snapshot(db, current, editor)
        db.execute("UPDATE records SET kind=?, title=?, body=?, tags=?, refs=?, source=?, "
                   "status=?, supersedes=?, updated_at=?, version=? WHERE id=?", (
                       value["kind"], value["title"], value["body"],
                       json.dumps(value["tags"], ensure_ascii=False),
                       json.dumps(value["refs"], ensure_ascii=False),
                       value["source"], value["status"], value["supersedes"], _now(),
                       current["version"] + (1 if changed else 0), ident))
        if value["supersedes"] != current["supersedes"]:
            _supersede(db, value["supersedes"], editor)
        return _get(db, ident)


def history(ws, ident, limit=20):
    """One record's versions, oldest first; the current row is the last entry.

    At most ``limit`` entries come back: the current row plus the newest
    ``limit - 1`` older versions. A file record is a single version here —
    git holds its history.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise KnowledgeError("limit must be an integer 1..100")
    if ident.startswith("md:"):
        match = next((row for row in file_records(ws) if row["id"] == ident), None)
        if match is None:
            raise KnowledgeError("knowledge record not found", 404)
        return [{**match, "version": 1, "current": True}]
    with connection(ws) as db:
        current = _get(db, ident)
        old = [] if limit == 1 else [_row(row) for row in db.execute(
            "SELECT * FROM revisions WHERE record_id=? ORDER BY version DESC LIMIT ?",
            (ident, limit - 1))]
    old.reverse()
    for row in old:
        row["current"] = False
    current["current"] = True
    # The current row was not replaced by anything, but every entry carries the keys.
    current.setdefault("archived_by", "")
    current.setdefault("archived_at", "")
    return old + [current]


def delete(ws, ident):
    _writable(ident)
    with connection(ws) as db:
        db.execute("BEGIN IMMEDIATE")
        _get(db, ident)
        db.execute("DELETE FROM records WHERE id=?", (ident,))
        db.execute("DELETE FROM revisions WHERE record_id=?", (ident,))


def parse_file(path):
    """One ``.md`` file as a record, or None when it has no usable front matter."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    match = _FRONT.match(text)
    if not match:
        return None
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(meta, dict):
        return None
    try:
        value = clean({**meta, "body": match.group(2),
                       "title": str(meta.get("title") or path.stem)})
    except KnowledgeError:
        return None
    date = str(meta.get("date") or "")
    return {"id": "md:" + path.stem, **value, "author": str(meta.get("author") or ""),
            "created_at": date, "updated_at": date, "origin": "file",
            "path": str(FILES_DIR / path.name).replace(os.sep, "/")}


def file_records(ws):
    folder = files_dir(ws)
    if not folder.is_dir():
        return []
    rows = (parse_file(p) for p in sorted(folder.glob("*.md")))
    return [r for r in rows if r is not None]


def entries(ws, kind="", status="active"):
    if kind and kind not in KINDS:
        raise KnowledgeError("kind must be one of " + ", ".join(KINDS))
    if status not in (*STATUSES, "all"):
        raise KnowledgeError("status must be active, superseded or all")
    with connection(ws) as db:
        rows = [_row(r) for r in db.execute("SELECT * FROM records")]
    rows += file_records(ws)
    rows = [r for r in rows if (not kind or r["kind"] == kind)
            and (status == "all" or r["status"] == status)]
    return sorted(rows, key=lambda r: (r["updated_at"], r["id"]), reverse=True)


def _fold(text):
    return unicodedata.normalize("NFKC", text).casefold()


def search(ws, query="", kind="", limit=20, status="active"):
    query = _text(query, "query", 500)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise KnowledgeError("limit must be an integer 1..100")
    rows = entries(ws, kind, status)
    words = [w for w in _fold(query).split() if w]
    if not words:
        return rows[:limit]
    hits = []
    for row in rows:
        title, tags = _fold(row["title"]), _fold(" ".join(row["tags"] + row["refs"]))
        body = _fold(row["body"])
        # Title over tags over body; every word that hits anywhere counts once.
        score, matched = 0, []
        for word in words:
            weight = 3 if word in title else 2 if word in tags else 1 if word in body else 0
            if weight:
                score += weight
                matched.append(word)
        if score:
            hits.append({**row, "score": score, "matched": matched})
    # rows are newest first; a stable sort keeps that order among equal scores.
    hits.sort(key=lambda r: (-len(r["matched"]), -r["score"]))
    return hits[:limit]


def export_markdown(ws):
    """Every active record as one markdown document, grouped by kind."""
    rows = entries(ws)
    out = [f"# {ws.name} knowledge", ""]
    for kind in KINDS:
        group = [r for r in rows if r["kind"] == kind]
        if not group:
            continue
        out += [f"## {KIND_LABELS[kind]} ({kind})", ""]
        for row in group:
            out += [f"### {row['title']}", ""]
            meta = [f"id `{row['id']}`"]
            if row["tags"]:
                meta.append("tags " + ", ".join(row["tags"]))
            if row["refs"]:
                meta.append("refs " + ", ".join(row["refs"]))
            if row["updated_at"]:
                meta.append(row["updated_at"][:10])
            out += ["- " + " · ".join(meta), "", row["body"], ""]
    return "\n".join(out).rstrip() + "\n"
