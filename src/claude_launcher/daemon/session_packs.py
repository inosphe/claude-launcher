"""The prompt packs of one running session, as the web UI's packs pane shows them.

The pane (``web/static/packs-pane.js``, claunch-6esft.1) answers three
questions about a session: which packs its opening carried, which others it
could still be given, and which a person has sent it since. It also sends
one -- typed into the terminal once, like the briefing the ↻ button used to
type straight away (:mod:`daemon.rebrief`).

What is sent is the block the session would have read in any other way, so
its text id is the one an agent already knows:

* a pack the opening carried -- the recorded opening block again
  (:func:`prompt_packs.opening`), for a session that lost it to a compaction;
* an mcp- pack -- the block ``rebrief`` loads (:func:`prompt_packs.loaded_block`);
* an on-demand pack -- the block ``claunch retro`` sends
  (:func:`prompt_packs.on_demand_block`);
* anything else -- :func:`prompt_packs.sent_block`.

The session definition is not written: a sent pack is not part of the
opening. What makes it survive a compaction is the same door the opening's
packs use. A delivered send of a pack the opening did not carry is *held*
in the session's own directory, text included, until a person drops it;
:func:`held_entries` feeds the session reminder's Prompt packs section and
:mod:`daemon.rebrief` serves the text back by its id. The text is kept as it
was sent, so the id the agent was handed keeps answering after the file it
came from is edited.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Dict, List, Optional, Tuple

from .. import atomic, prompt_packs
from . import paths

#: How many sends the log keeps; the pane shows the newest first.
LOG_LIMIT = 100

#: The source word the reminder prints beside a held pack.
SENT_SOURCE = "sent"

GIVEN = "given"
MCP = "mcp"
ON_DEMAND = "on-demand"
OTHER = "sent"

_lock = threading.RLock()
#: path -> ((mtime_ns, size), parsed): the reminder reads every live
#: session's record on each pass, and the file almost never changes.
_cache: Dict[str, tuple] = {}


def _path(name: str):
    return paths.session_dir(name) / "packs-sent.json"


def _load(name: str) -> dict:
    path = _path(name)
    try:
        stat = os.stat(path)
    except OSError:
        return {"log": [], "held": {}}
    stamp = (stat.st_mtime_ns, stat.st_size)
    with _lock:
        hit = _cache.get(str(path))
        if hit and hit[0] == stamp:
            return hit[1]
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    raw = raw if isinstance(raw, dict) else {}
    log = [r for r in raw.get("log") or () if isinstance(r, dict)]
    held = {
        k: v for k, v in (raw.get("held") or {}).items()
        if isinstance(v, dict) and isinstance(v.get("text"), str) and v["text"].strip()
    }
    out = {"log": log, "held": held}
    with _lock:
        _cache[str(path)] = (stamp, out)
    return out


def _save(name: str, data: dict) -> None:
    path = _path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    with atomic.scratch(path) as tmp:
        tmp.write_text(json.dumps(data), encoding="utf-8")
        atomic.replace(tmp, path)


def sent(name: str) -> List[dict]:
    """The sends logged for ``name``, oldest first."""
    return list(_load(name)["log"])


def held(name: str) -> Dict[str, dict]:
    """Packs sent to ``name`` and kept for recovery: id -> {text, text_id, how, at}."""
    return dict(_load(name)["held"])


def held_entries(name: str) -> List[dict]:
    """The held packs as :func:`session_reminder.pack_entries` lists them."""
    return [
        {"id": pack_id, "digest": row.get("text_id") or prompt_packs.text_id(row["text"]),
         "source": SENT_SOURCE, "required_by": []}
        for pack_id, row in sorted(held(name).items())
    ]


def record_sent(name: str, row: dict, text: Optional[str] = None) -> None:
    """Log one send; a delivered one with ``text`` is also held."""
    with _lock:
        data = _load(name)
        data = {"log": (data["log"] + [row])[-LOG_LIMIT:], "held": dict(data["held"])}
        if text and row.get("delivered"):
            data["held"][row["pack"]] = {
                "text": text.strip(), "text_id": row.get("text_id"),
                "how": row.get("how"), "at": row.get("at"),
            }
        _save(name, data)


def drop(name: str, pack_id: str) -> bool:
    """Stop holding ``pack_id`` for ``name``; False when it was not held."""
    with _lock:
        data = _load(name)
        if pack_id not in data["held"]:
            return False
        rest = {k: v for k, v in data["held"].items() if k != pack_id}
        _save(name, {"log": list(data["log"]), "held": rest})
        return True


def _workspace(manager, name: str):
    from .rebrief import _workspace as workspace_of  # rebrief imports this module

    return workspace_of(manager, name)


def _given(sdef) -> List[dict]:
    return [r for r in getattr(sdef, "prompt_packs", ()) or () if isinstance(r, dict)]


def _kind(pack: prompt_packs.Pack) -> str:
    if prompt_packs.is_mcp(pack.id) and not pack.on_demand:
        return MCP
    return ON_DEMAND if pack.on_demand else pack.delivery


def view(name: str, *, manager) -> dict:
    """What the packs pane draws for ``name``. Raises for an unknown session."""
    sdef = manager.get(name).sdef
    workspace = _workspace(manager, name)
    memo: dict = {}
    given = []
    for rec in _given(sdef):
        text = rec.get("text") or ""
        row = {
            "id": rec.get("id"),
            "text_id": prompt_packs.text_id(text),
            "source": rec.get("source") or prompt_packs.WORKSPACE,
            "lines": len(text.strip().splitlines()),
        }
        for key in ("required_by", "ticked", "always"):
            if rec.get(key):
                row[key] = rec[key]
        current = prompt_packs.current_copy(workspace, rec, memo=memo)
        if current is not None:
            row["title"] = current.title
            # The file it follows was edited since the opening was written:
            # re-sending gives the recorded text, not the edit.
            row["changed"] = prompt_packs.text_id(current.body) != row["text_id"]
        given.append(row)
    given_ids = {r["id"] for r in given}
    kept = held(name)
    ids = {p.id for p in prompt_packs.catalog()}
    if workspace is not None:
        ids.update(p.id for p in prompt_packs.installed(workspace))
    available = []
    for pack_id in sorted(ids - given_ids):
        try:
            pack = prompt_packs.pack_copy(workspace, pack_id)
        except prompt_packs.PackError:
            continue
        row = {
            **pack.to_dict(),
            "kind": _kind(pack),
            "source": pack.source,
            "text_id": prompt_packs.text_id(pack.body),
            "lines": len(pack.body.strip().splitlines()),
        }
        if pack_id in kept:
            row["held"] = {k: kept[pack_id].get(k) for k in ("text_id", "how", "at")}
        available.append(row)
    return {
        "session": name,
        "workspace": str(workspace) if workspace is not None else None,
        "given": given,
        "available": available,
        "sent": list(reversed(sent(name))),
    }


def block_for(name: str, pack_id: str, *, manager) -> Tuple[str, str, str]:
    """``(block, how, body)`` -- what sending ``pack_id`` types, which kind,
    and the pack text a held send keeps.

    Raises :class:`prompt_packs.PackError` for an unknown pack, and whatever
    ``manager.get`` raises for an unknown session.
    """
    sdef = manager.get(name).sdef
    pack_id = prompt_packs.check_id((pack_id or "").strip().lower())
    for rec in _given(sdef):
        if rec.get("id") == pack_id:
            return prompt_packs.opening([rec]), GIVEN, rec.get("text") or ""
    workspace = _workspace(manager, name)
    if prompt_packs.is_mcp(pack_id) and not prompt_packs.is_on_demand(pack_id, workspace):
        pack = prompt_packs.loadable_copy(workspace, pack_id)
        return prompt_packs.loaded_block(pack), MCP, pack.body
    if prompt_packs.is_on_demand(pack_id, workspace):
        pack = prompt_packs.pack_copy(workspace, pack_id)
        return prompt_packs.on_demand_block(workspace, pack_id), ON_DEMAND, pack.body
    pack = prompt_packs.pack_copy(workspace, pack_id)
    return prompt_packs.sent_block(pack), OTHER, pack.body


def text_id_of(block: str) -> Optional[str]:
    """The ``text id:`` line of a block built above."""
    for line in block.splitlines()[:4]:
        if line.startswith("text id: "):
            return line[len("text id: "):].strip()
    return None


def sent_row(pack_id: str, how: str, block: str, delivered: bool) -> dict:
    return {
        "pack": pack_id, "how": how, "text_id": text_id_of(block),
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "delivered": bool(delivered),
    }
