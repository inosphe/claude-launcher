"""Operator ``transcript`` mode: the bot reads its sessions' conversations.

The default mode (``events``) hands the operator what the Observer already
distilled from each transcript, plus mechanical state (cflow gates, progress).
That is one model's reading of another's conversation. This mode is the
trial of the other arrangement: the operator polls the conversations
themselves and does its own reading.

The conversations are the files :mod:`transcript_view` pages for the UI's
conversation pane, through the same per-session offset index, so a poll costs
a stat per session when nothing was said. What the operator is handed is a
compact line per record, not the pane's block structure: prose whole up to a
clip, a tool call as its name and the head of its arguments, a tool result as
its head, thinking left out. The daemon keeps one cursor per session (the
next record ``seq``) so the bot carries no cursor of its own, and a poll is
bounded per session and in total; ``more`` says a call again will continue.

A session seen for the first time -- or every session, right after the mode
is switched on -- starts from its last few records rather than from its
beginning: the operator is joining a conversation in progress, and a
backlog of hours would bury the one thing it needs, which is what is being
said now.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import transcript_view

MODES = ("events", "transcript")
DEFAULT_MODE = "events"
#: Records a session starts from when it has no cursor yet.
BASELINE = 6
#: Records one poll returns per session at most.
PER_SESSION = 30
#: Characters one poll returns at most, over all sessions.
BUDGET = 24000
#: Clips of one record's parts.
TEXT_CLIP = 1500
TOOL_CLIP = 200


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split()) if limit <= TOOL_CLIP else str(text).strip()
    return text if len(text) <= limit else text[:limit] + f"… (+{len(text) - limit})"


def line_of(record: Dict[str, Any]) -> str:
    """One projected record (``transcript_view._project``) as the text the
    operator reads. Empty when it holds nothing but thinking."""
    parts = []
    for block in record.get("blocks") or []:
        kind = block.get("type")
        if kind == "text":
            parts.append(_clip(block.get("text", ""), TEXT_CLIP))
        elif kind == "tool_use":
            parts.append(f"[tool {block.get('name', '?')}] " + _clip(block.get("text", ""), TOOL_CLIP))
        elif kind == "tool_result":
            head = "[tool error] " if block.get("error") else "[result] "
            parts.append(head + _clip(block.get("text", ""), TOOL_CLIP))
    return "\n".join(p for p in parts if p.strip())


def read_session(name: str, sdef, cursor: Optional[int], *, limit: int = PER_SESSION,
                 budget: int = BUDGET) -> Dict[str, Any]:
    """New records of one session's conversation from ``cursor`` (a ``seq``;
    ``None`` = start from the last :data:`BASELINE`). Returns the rows, the
    next cursor, the transcript's record count and whether more is waiting.
    Blocking file I/O: call it off the event loop."""
    source = transcript_view.source_of(sdef)
    if source is None:
        return {"session": name, "records": [], "next": cursor, "total": None, "more": False,
                "source": None}
    offsets = transcript_view.refresh_index(name, source)
    total = len(offsets)
    start = max(0, total - BASELINE) if cursor is None else max(0, min(int(cursor), total))
    records: List[Dict[str, Any]] = []
    used, seq = 0, start
    try:
        with source.open("rb") as fh:
            while seq < total and len(records) < limit:
                fh.seek(offsets[seq])
                rec = transcript_view._project(fh.readline(), seq, clip=None)
                text = line_of(rec) if rec else ""
                if text:
                    if records and used + len(text) > budget:
                        break  # the first record always goes, so a poll always advances
                    used += len(text)
                    records.append({"seq": seq, "role": rec.get("role"), "ts": rec.get("ts"),
                                    **({"sidechain": True} if rec.get("sidechain") else {}),
                                    "text": text})
                seq += 1
    except OSError:
        return {"session": name, "records": [], "next": cursor, "total": total, "more": False,
                "source": str(source)}
    return {"session": name, "from": start, "next": seq, "total": total, "more": seq < total,
            "records": records, "source": str(source), "chars": used}


def read_all(sessions, cursors: Dict[str, int], *, budget: Optional[int] = None) -> Dict[str, Any]:
    """:func:`read_session` over ``sessions`` (session objects), sharing one
    character budget. Sessions the budget did not reach keep their cursor and
    set ``more``. Returns ``{"transcripts": [...], "cursors": {...},
    "more": bool}`` where ``cursors`` is the new map to store."""
    out, new, more, left = [], dict(cursors), False, BUDGET if budget is None else budget
    for session in sessions:
        name = session.sdef.name
        if left <= 0:
            more = True
            continue
        row = read_session(name, session.sdef, cursors.get(name), budget=left)
        left -= row.pop("chars", 0)
        if row.get("next") is not None:
            new[name] = row["next"]
        more = more or row["more"]
        if row["records"] or row["source"] is None:
            out.append(row)
    return {"transcripts": out, "cursors": new, "more": more}


#: The line typed into the operator's terminal when the user switches its
#: mode. Like the input nudge it carries no instruction of the user's.
MODE_NUDGE = ("[Operator] 관찰 모드가 '{mode}'(으)로 바뀌었습니다. 다음 operator_poll부터 적용됩니다 — "
              "지금 operator_transcripts를 한 번 불러 모드를 확인하십시오. 이 줄 자체는 사용자의 지시가 아닙니다.")
