"""A session's conversation, paged for a reader who scrolls it themselves.

The terminal cannot answer "what did this session say an hour ago". A claude
session lives on the alternate screen and repaints the whole grid every frame
instead of scrolling it, so nothing scrolls off into any scrollback: measured
on this project's own sessions, four hundred kilobytes of PTY output leaves one
or two lines behind in the daemon's pyte history. Reconstructing the rest from
the byte stream does not work either — a grid-diff recorder run over those same
logs recovers zero rows, because successive grids share no prefix or suffix to
exploit. The content simply is not in the pipe.

It is on disk, though, in the form each harness keeps: Claude's conversation
jsonl under ``<config>/projects/<slug>/<id>.jsonl`` or Codex's rollout jsonl
under its profile home. These files are append-only, hold the whole
conversation rather than the last screenful, survive every daemon
restart, and are already located for other features (:mod:`briefing`,
:mod:`ctxsize`). This module turns it into pages a browser can scroll natively
— which is the point: a DOM scroller has a scrollbar, momentum, touch, PgUp,
find-in-page and selection, none of which a wheel-to-RPC control ever had.

The index is the part that makes paging cheap. A conversation reaches tens of
megabytes (16 MiB in this fleet), and re-reading one per page request would
cost more than the wheel it replaces. So each session keeps the byte offset of
every content record beside its log, extended in place as the transcript grows
and rebuilt only when the file it describes is no longer the one it indexed.
That file is the storage the daemon manages for a session, and it is why a
restart costs nothing: the offsets are still there, and the scan resumes at the
byte it stopped on.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from .. import atomic
from . import paths
from .briefing import locate_transcript

log = logging.getLogger(__name__)

#: Record types that carry conversation. The rest of the jsonl is harness
#: bookkeeping — mode flips, titles, queue operations, usage snapshots — which
#: is noise to a reader and would triple the page for nothing.
CONTENT_TYPES = ("user", "assistant", "response_item")
CODEX_CONTENT_TYPES = frozenset(
    {
        "message",
        "function_call",
        "function_call_output",
        "custom_tool_call",
        "custom_tool_call_output",
    }
)

#: How many records a page holds by default. Small enough that the first page
#: paints immediately, large enough that a reader flicking upward is not
#: fetching every other frame.
PAGE_DEFAULT = 40
PAGE_MAX = 200

#: Tool traffic is clipped; prose is not. A tool_result can be a megabyte of
#: file content, and a reader scrolling a conversation wants to see that a tool
#: ran and roughly what came back — not to have the page carry the whole of it.
#: Text and thinking blocks are what the reader came for, so they arrive whole.
TOOL_CLIP = 2000


# --------------------------------------------------------------------------- #
# index
# --------------------------------------------------------------------------- #
def index_path(name: str) -> Path:
    """Where a session's transcript offsets live, beside its output log."""
    return paths.session_dir(name) / "transcript.index"


def _load_index(name: str) -> Dict[str, Any]:
    try:
        doc = json.loads(index_path(name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(doc, dict) or not isinstance(doc.get("offsets"), list):
        return {}
    return doc


def _save_index(name: str, doc: Dict[str, Any]) -> None:
    path = index_path(name)
    try:
        with atomic.scratch(path) as tmp:
            tmp.write_text(json.dumps(doc), encoding="utf-8")
            atomic.replace(tmp, path)
    except OSError:  # an index we cannot persist is rebuilt next time, not fatal
        log.debug("could not write transcript index for %s", name, exc_info=True)


def refresh_index(name: str, source: Path) -> List[int]:
    """The byte offset of every content record in ``source``, oldest first.

    Extended in place: the transcript is append-only, so a scan that stopped
    at byte N last time resumes there. The index is thrown away and rebuilt
    only when the file underneath is no longer the one it describes — a
    different path (the conversation was relocated), or a file that has shrunk
    below what was already scanned (truncated, or replaced by a shorter one).
    """
    doc = _load_index(name)
    try:
        size = source.stat().st_size
    except OSError:
        return []

    scanned = int(doc.get("scanned") or 0)
    offsets: List[int] = doc.get("offsets") or []
    if doc.get("source") != str(source) or scanned > size:
        scanned, offsets = 0, []
    if scanned == size and offsets is not None and doc.get("source") == str(source):
        return offsets

    try:
        with source.open("rb") as fh:
            fh.seek(scanned)
            pos = scanned
            for raw in fh:
                # A final line without its newline is a record still being
                # written. Stop before it and leave `scanned` short, so the
                # next refresh reads it whole rather than indexing a fragment.
                if not raw.endswith(b"\n"):
                    break
                if raw.strip():
                    kind = _peek_type(raw)
                    if kind in CONTENT_TYPES:
                        offsets.append(pos)
                pos += len(raw)
    except OSError:
        return offsets

    _save_index(name, {"source": str(source), "scanned": pos, "offsets": offsets})
    return offsets


def _peek_type(raw: bytes) -> str:
    """The record's ``type``, without paying for the whole object.

    The index scan touches every line of a file that reaches tens of
    megabytes, and nine records in ten are bookkeeping it will discard. A
    substring test first means only the candidates are parsed.
    """
    for kind in CONTENT_TYPES:
        needle = b'"type":"%s"' % kind.encode()
        if needle in raw or needle.replace(b'":"', b'": "') in raw:
            break
    else:
        return ""
    try:
        doc = json.loads(raw)
    except ValueError:
        return ""
    if not isinstance(doc, dict):
        return ""
    kind = doc.get("type") or ""
    if kind != "response_item":
        return kind
    payload = doc.get("payload")
    if not isinstance(payload, dict) or payload.get("type") not in CODEX_CONTENT_TYPES:
        return ""
    if payload.get("type") == "message" and payload.get("role") not in (
        "user",
        "assistant",
    ):
        return ""
    return kind


# --------------------------------------------------------------------------- #
# projection
# --------------------------------------------------------------------------- #
def _clip(text: str, limit: int = TOOL_CLIP) -> Dict[str, Any]:
    text = str(text)
    if len(text) <= limit:
        return {"text": text, "clipped": False}
    return {"text": text[:limit], "clipped": True, "full": len(text)}


def _blocks(content: Any) -> List[Dict[str, Any]]:
    """One record's content, flattened to what a reader is shown.

    Prose and thinking arrive whole; tool calls and their results arrive
    clipped, with the full length reported so the page can say how much it is
    not showing rather than pretending that was all of it.
    """
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content.strip() else []
    if not isinstance(content, list):
        return []
    out: List[Dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            text = str(block.get("text") or "")
            if text.strip():
                out.append({"type": "text", "text": text})
        elif kind == "thinking":
            text = str(block.get("thinking") or "")
            if text.strip():
                out.append({"type": "thinking", "text": text})
        elif kind == "tool_use":
            out.append({
                "type": "tool_use",
                "name": str(block.get("name") or "?"),
                "id": str(block.get("id") or ""),
                **_clip(json.dumps(block.get("input"), ensure_ascii=False,
                                   default=str)),
            })
        elif kind == "tool_result":
            body = block.get("content")
            if isinstance(body, list):
                # A structured result: the text parts are the readable half,
                # and an image block has nothing to show in a text pane.
                body = "\n".join(
                    str(b.get("text") or "")
                    for b in body
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            out.append({
                "type": "tool_result",
                "id": str(block.get("tool_use_id") or ""),
                "error": bool(block.get("is_error")),
                **_clip("" if body is None else str(body)),
            })
    return out


def _project(raw: bytes, seq: int) -> Optional[Dict[str, Any]]:
    try:
        doc = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(doc, dict):
        return None
    if doc.get("type") == "response_item":
        return _project_codex(doc, seq)
    msg = doc.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    blocks = _blocks(content)
    if not blocks:
        return None
    return {
        "seq": seq,
        "role": (msg or {}).get("role") or doc.get("type") or "?",
        "ts": doc.get("timestamp") or "",
        "sidechain": bool(doc.get("isSidechain")),
        "blocks": blocks,
    }


def _project_codex(doc: dict, seq: int) -> Optional[Dict[str, Any]]:
    """Project one Codex rollout ``response_item`` into the shared UI shape."""
    payload = doc.get("payload")
    if not isinstance(payload, dict):
        return None
    kind = payload.get("type")
    blocks: List[Dict[str, Any]] = []
    role = "assistant"
    if kind == "message":
        role = str(payload.get("role") or "?")
        for block in payload.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") in ("text", "input_text", "output_text"):
                value = str(block.get("text") or "")
                if value.strip():
                    blocks.append({"type": "text", "text": value})
    elif kind in ("function_call", "custom_tool_call"):
        body = payload.get("arguments", payload.get("input", ""))
        blocks.append(
            {
                "type": "tool_use",
                "name": str(payload.get("name") or "?"),
                "id": str(payload.get("call_id") or ""),
                **_clip("" if body is None else str(body)),
            }
        )
    elif kind in ("function_call_output", "custom_tool_call_output"):
        blocks.append(
            {
                "type": "tool_result",
                "id": str(payload.get("call_id") or ""),
                "error": False,
                **_clip(str(payload.get("output") or "")),
            }
        )
    if not blocks:
        return None
    return {
        "seq": seq,
        "role": role,
        "ts": doc.get("timestamp") or "",
        "sidechain": False,
        "blocks": blocks,
    }


# --------------------------------------------------------------------------- #
# paging
# --------------------------------------------------------------------------- #
def page(
    name: str,
    sdef,
    *,
    before: Optional[int] = None,
    limit: int = PAGE_DEFAULT,
) -> Dict[str, Any]:
    """One page of the conversation, newest-last, ending just before ``before``.

    ``before`` is a record's ``seq`` — the reader's cursor, walking backwards
    as they scroll up. Omitted, the page is the tail: what a pane shows when
    it opens. ``has_more`` says whether scrolling further up will find
    anything, so the scroller knows when to stop asking.
    """
    limit = max(1, min(int(limit or PAGE_DEFAULT), PAGE_MAX))
    source = locate_transcript(sdef)
    if source is None:
        return {"records": [], "has_more": False, "total": 0, "source": None}

    offsets = refresh_index(name, source)
    total = len(offsets)
    end = total if before is None else max(0, min(int(before), total))
    start = max(0, end - limit)

    records: List[Dict[str, Any]] = []
    try:
        with source.open("rb") as fh:
            for seq in range(start, end):
                fh.seek(offsets[seq])
                rec = _project(fh.readline(), seq)
                if rec is not None:
                    records.append(rec)
    except OSError:
        return {"records": [], "has_more": False, "total": total,
                "source": str(source)}

    return {
        "records": records,
        # Against `start`, not against what survived projection: a page whose
        # records all held nothing readable still has older ones behind it.
        "has_more": start > 0,
        "cursor": start,
        "total": total,
        "source": str(source),
    }
