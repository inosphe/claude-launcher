"""Context items handed to a session beside its opening task (gds6-bfu7).

The create form's opening task is one text box, so everything the person
wants the session to have goes into it: the request, and under it the error
log the request is about, and under that a second log, a spec excerpt. The
agent then reads one block in which the instruction and its material run
together. A context item keeps each piece apart: a title and a text, as many
as :data:`MAX_ITEMS`, each its own file.

The create request carries them as ``opening_contexts`` -- a list of
``{"title": ..., "text": ...}`` -- from the web form (New session and Spawn
child are one form) and from the agents' MCP ``spawn`` tool. They are text in
the request body itself, so an agent's spawn request that waits on a person
(daemon/spawn_gate.py) keeps them in its spec with nothing to expire, unlike
a staged upload.

Where they live: ``<session dir>/contexts/NN-<slug>.txt``, with
``index.json`` beside them naming each item's number, title and file. The
session's state directory, never its working directory -- the same reason
as daemon/uploads.py. They live as long as that directory.

How the session learns of them: :func:`block` ends the opening (after the
task, before uploaded files) with one ``[claunch context]`` line per item,
naming its number, title, size and path, and the MCP tools ``context_list``
/ ``context_read`` (``claude_launcher/contexts_mcp.py``) that read them
through the daemon -- for a harness that cannot open a path on this machine.
The detail panel lists them under the Opening task and shows each on demand
(``GET /api/sessions/<name>/contexts[/<n>]``). They are read-only once the
session exists.

The ceilings keep the create request under the daemon's request body limit
(aiohttp's 1 MiB default): a body over it is not refused but read as empty.
A larger log belongs in the form's file attachments (daemon/uploads.py).
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from . import paths

#: How many context items one opening may carry.
MAX_ITEMS = 20

#: One item's text, in UTF-8 bytes.
MAX_ITEM_BYTES = 256 * 1024

#: All items' texts together, in UTF-8 bytes.
MAX_TOTAL_BYTES = 384 * 1024

#: A title longer than this is refused rather than cut.
MAX_TITLE = 120

#: What :func:`read` hands back at once when the caller names no limit.
DEFAULT_READ_CHARS = 60000

INDEX = "index.json"

_SLUG_DROP = re.compile(r"[^\w-]+", re.UNICODE)
_MAX_SLUG = 40


class ContextError(ValueError):
    """A request's context items cannot be used, or an item is not there."""


@dataclass(frozen=True)
class Item:
    title: str
    text: str


def contexts_dir(session: str) -> Path:
    return paths.session_dir(session) / "contexts"


def _size(text: str) -> int:
    return len(text.encode("utf-8"))


def check(raw) -> List[Item]:
    """``raw`` (a request's ``opening_contexts``) checked: the shape, the
    count and the sizes -- before anything is arranged, so a bad request
    leaves nothing behind. An item is ``{"title", "text"}`` or a bare string
    (its text); an item whose text is blank is refused, since a context the
    session is told to read and finds empty reads as a lost attachment."""
    if raw in (None, "", []):
        return []
    if not isinstance(raw, list):
        raise ContextError("opening_contexts must be a list of {title, text} items")
    if len(raw) > MAX_ITEMS:
        raise ContextError(f"an opening carries at most {MAX_ITEMS} context items ({len(raw)} given)")
    items: List[Item] = []
    total = 0
    for i, entry in enumerate(raw, 1):
        if isinstance(entry, str):
            entry = {"text": entry}
        if not isinstance(entry, dict):
            raise ContextError(f"context {i} must be an object with 'title' and 'text'")
        title, text = entry.get("title", ""), entry.get("text")
        if title is None:
            title = ""
        if not isinstance(title, str) or not isinstance(text, str):
            raise ContextError(f"context {i}: 'title' and 'text' must be text")
        title = " ".join(title.split())
        if len(title) > MAX_TITLE:
            raise ContextError(f"context {i}: a title is at most {MAX_TITLE} characters")
        if not text.strip():
            raise ContextError(f"context {i} ({title or 'untitled'}) is empty")
        size = _size(text)
        if size > MAX_ITEM_BYTES:
            raise ContextError(
                f"context {i} ({title or 'untitled'}) is {human_size(size)}; an item is at most "
                f"{human_size(MAX_ITEM_BYTES)} -- attach a larger log as a file"
            )
        total += size
        items.append(Item(title, text))
    if total > MAX_TOTAL_BYTES:
        raise ContextError(
            f"the context items come to {human_size(total)}; together they are at most "
            f"{human_size(MAX_TOTAL_BYTES)} -- attach larger logs as files"
        )
    return items


def slug(title: str) -> str:
    """A file name part for ``title``: word characters and dashes."""
    out = _SLUG_DROP.sub("-", title.strip().lower()).strip("-_")
    return out[:_MAX_SLUG].rstrip("-_") or "context"


def label(rec: dict) -> str:
    """How an item is named to a reader: its title, or ``context N``."""
    return rec.get("title") or f"context {rec.get('n')}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def store(session: str, items: List[Item]) -> List[dict]:
    """Write ``items`` into session ``session``'s contexts, in order; the
    index records (``n``, ``title``, ``file``, ``bytes``, ``lines``,
    ``path``). A directory left by an earlier session of the same name is
    replaced -- the new session's opening names these items and no others."""
    folder = contexts_dir(session)
    if folder.exists():
        shutil.rmtree(folder)
    if not items:
        return []
    folder.mkdir(parents=True)
    records = []
    for n, item in enumerate(items, 1):
        name = f"{n:02d}-{slug(item.title)}.txt"
        (folder / name).write_text(item.text, encoding="utf-8", newline="")
        records.append({
            "n": n, "title": item.title, "file": name,
            "bytes": _size(item.text), "chars": len(item.text),
            "lines": item.text.count("\n") + (0 if item.text.endswith("\n") else 1),
        })
    (folder / INDEX).write_text(
        json.dumps({"at": _now(), "items": records}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    return [{**rec, "path": str(folder / rec["file"])} for rec in records]


def listing(session: str) -> List[dict]:
    """Session ``session``'s context items as recorded, each with its
    ``path``; ``missing: true`` on one whose file is gone. Empty when the
    session has none."""
    folder = contexts_dir(session)
    try:
        doc = json.loads((folder / INDEX).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    recs = doc.get("items") if isinstance(doc, dict) else None
    out = []
    for rec in recs if isinstance(recs, list) else []:
        if not isinstance(rec, dict) or not isinstance(rec.get("file"), str):
            continue
        # The index is the daemon's, but a name read back off disk is still
        # kept inside the folder before it becomes a path.
        if Path(rec["file"]).name != rec["file"]:
            continue
        path = folder / rec["file"]
        item = {**rec, "path": str(path)}
        if not path.is_file():
            item["missing"] = True
        out.append(item)
    return out


def read(session: str, n: int, *, offset: int = 0, max_chars: Optional[int] = None) -> dict:
    """Item ``n``'s text from character ``offset``, at most ``max_chars``
    characters (:data:`DEFAULT_READ_CHARS` when unset); ``next_offset`` is
    where the following read starts, or None at the end."""
    rec = next((r for r in listing(session) if r.get("n") == n), None)
    if rec is None:
        raise ContextError(f"session {session} has no context item {n}")
    if rec.get("missing"):
        raise ContextError(f"context item {n} of {session} is no longer on disk")
    text = Path(rec["path"]).read_text(encoding="utf-8")
    offset = max(0, int(offset or 0))
    limit = DEFAULT_READ_CHARS if max_chars is None else max(1, int(max_chars))
    part = text[offset:offset + limit]
    end = offset + len(part)
    return {
        **rec, "text": part, "offset": offset, "total_chars": len(text),
        "truncated": end < len(text), "next_offset": end if end < len(text) else None,
    }


def human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KiB", "MiB"):
        if size < 1024 or unit == "MiB":
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def line(rec: dict) -> str:
    """The line a session reads one item from."""
    return (
        f"[claunch context] {rec['n']}. {label(rec)} "
        f"({human_size(rec['bytes'])}, {rec['lines']} lines) {rec['path']}"
    )


def block(records: List[dict]) -> str:
    """The lines for ``records``, under a note of what they are and how to
    read them."""
    if not records:
        return ""
    count = len(records)
    head = (
        f"The user attached {count} context item{'s' if count != 1 else ''} to this "
        "opening, kept apart from the task text. Read them before you act on the task. "
        "Read one with the MCP tool `context_read` (n = its number; `context_list` "
        "lists them), or open the file at the end of its line (it is on this machine, "
        "not in your working directory):"
    )
    return "\n".join([head] + [line(rec) for rec in records])
