"""What the dashboard's notices were done with, shared by every browser
(claunch-mfzlv).

Each browser keeps its own notification history (app.js, localStorage), and
every browser looking at the same daemon puts up the same cards: a QA
request, a thread reply. A card read or put away in one browser stayed up in
all the others. This store is the one place those browsers tell each other
what they did with a notice, keyed by the notice's sync id -- an id the page
derives from the event itself (``qa:<question>:open``,
``thread:<thread>:<reply seq>``), so every browser names the same event the
same way.

Two fields per id, with different rules:

- ``dismissed`` only goes one way. Once any browser put the card away it is
  away everywhere; a ``false`` is not a change and is ignored. Two browsers
  dismissing the same card is the same write twice.
- ``read`` can be set and cleared (Mark read / Mark unread in the panel), so
  two browsers can disagree. Every change is stamped with the store's
  sequence number (``read_seq``) and a writer says which ``base`` it last
  saw. A write whose value differs from the stored one while the stored one
  is newer than ``base`` was made without knowing about the other browser's
  change: it is refused and the stored record comes back in ``conflicts``,
  which the page adopts. A write of the value already stored always passes.

The page polls (``POST /api/notices/sync``) every few seconds: it sends the
changes it has queued and the last ``seq`` it applied, and gets back every
record changed after that. It is not real-time and does not need to be.

The records live in one daemon-local JSON file so a daemon restart does not
bring back cards that were already read. ``epoch`` names the file: a page
whose ``epoch`` differs (a fresh file) starts again from ``seq`` 0. The
oldest records beyond :data:`MAX_RECORDS` are dropped; a notice that old is
no longer on any card.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .. import atomic

log = logging.getLogger(__name__)

#: Records kept; the ones changed longest ago go first.
MAX_RECORDS = 2000
#: The longest sync id accepted.
MAX_ID = 200
#: The most changes one request may carry.
MAX_CHANGES = 500


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean_change(raw) -> dict:
    """One change as the page sent it, checked: ``{id, read?, dismissed?,
    base?}``. Raises ``ValueError`` naming the rule that failed."""
    if not isinstance(raw, dict):
        raise ValueError("a change must be an object")
    nid = raw.get("id")
    if not isinstance(nid, str) or not nid.strip() or len(nid) > MAX_ID:
        raise ValueError(f"a change needs an id of 1..{MAX_ID} characters")
    out: dict = {"id": nid}
    if "read" in raw and raw["read"] is not None:
        if not isinstance(raw["read"], bool):
            raise ValueError("read must be true or false")
        out["read"] = raw["read"]
    if raw.get("dismissed") is True:
        out["dismissed"] = True
    base = raw.get("base", 0)
    if isinstance(base, bool) or not isinstance(base, int) or base < 0:
        raise ValueError("base must be a non-negative integer")
    out["base"] = base
    return out


class NoticeSyncStore:
    """The records, in memory and in one JSON file rewritten on change."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self.epoch = ""
        self.seq = 0
        self._records: Dict[str, dict] = {}
        self._load()
        if not self.epoch:
            self.epoch = secrets.token_hex(6)

    def _load(self) -> None:
        try:
            if not self._path.is_file():
                return
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("cannot read notice sync %s; starting empty", self._path)
            return
        if not isinstance(data, dict):
            return
        self.epoch = str(data.get("epoch") or "")
        seq = data.get("seq")
        self.seq = seq if isinstance(seq, int) and seq >= 0 else 0
        for nid, rec in (data.get("records") or {}).items():
            if not isinstance(rec, dict) or not isinstance(rec.get("seq"), int):
                continue
            read = rec.get("read")
            self._records[str(nid)] = {
                "read": read if isinstance(read, bool) else None,
                "read_seq": rec.get("read_seq") if isinstance(rec.get("read_seq"), int) else 0,
                "dismissed": rec.get("dismissed") is True,
                "seq": rec["seq"],
                "at": str(rec.get("at") or ""),
            }
            self.seq = max(self.seq, rec["seq"])

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with atomic.scratch(self._path) as tmp:
                tmp.write_text(
                    json.dumps({"epoch": self.epoch, "seq": self.seq, "records": self._records},
                               ensure_ascii=False),
                    encoding="utf-8",
                )
                atomic.replace(tmp, self._path)
        except OSError:
            # The in-memory records still answer; only a restart loses them.
            log.warning("cannot write notice sync %s", self._path, exc_info=True)

    def _trim(self) -> None:
        over = len(self._records) - MAX_RECORDS
        if over <= 0:
            return
        for nid, _ in sorted(self._records.items(), key=lambda kv: kv[1]["seq"])[:over]:
            del self._records[nid]

    @staticmethod
    def view(nid: str, rec: dict) -> dict:
        return {"id": nid, **rec}

    def apply(self, changes: List[dict]) -> Tuple[List[dict], List[dict]]:
        """Write :func:`clean_change`-ed ``changes`` in order.

        Returns ``(applied, conflicts)``: the records a change moved, and the
        stored record for every ``read`` change refused as stale (see the
        module docstring) -- a record can be in both when the same change
        also dismissed it. A change that moves nothing is in neither list.
        """
        applied: Dict[str, dict] = {}
        conflicts: Dict[str, dict] = {}
        with self._lock:
            for ch in changes:
                nid = ch["id"]
                rec = self._records.get(nid)
                fresh = rec is None
                if fresh:
                    rec = {"read": None, "read_seq": 0, "dismissed": False, "seq": 0, "at": ""}
                moved = False
                if ch.get("dismissed") and not rec["dismissed"]:
                    rec["dismissed"] = True
                    moved = True
                read_moved = False
                if "read" in ch and rec["read"] != ch["read"]:
                    if rec["read"] is not None and rec["read_seq"] > ch["base"]:
                        conflicts[nid] = rec
                    else:
                        rec["read"] = ch["read"]
                        moved = read_moved = True
                if not moved:
                    continue
                self.seq += 1
                if read_moved:
                    rec["read_seq"] = self.seq
                rec["seq"] = self.seq
                rec["at"] = _now()
                if fresh:
                    self._records[nid] = rec
                applied[nid] = rec
            if applied:
                self._trim()
                self._save()
            return ([self.view(n, dict(r)) for n, r in applied.items()],
                    [self.view(n, dict(r)) for n, r in conflicts.items()])

    def since(self, seq: int) -> List[dict]:
        """Every record changed after ``seq``, oldest change first."""
        with self._lock:
            rows = [self.view(n, dict(r)) for n, r in self._records.items() if r["seq"] > seq]
        rows.sort(key=lambda r: r["seq"])
        return rows

    def sync(self, body: dict) -> dict:
        """One page's poll: apply its ``changes``, answer what changed after
        its ``since`` (from 0 when its ``epoch`` is another file's).
        Raises ``ValueError`` for a malformed body."""
        if not isinstance(body, dict):
            raise ValueError("body must be an object")
        raw = body.get("changes") or []
        if not isinstance(raw, list):
            raise ValueError("changes must be a list")
        if len(raw) > MAX_CHANGES:
            raise ValueError(f"at most {MAX_CHANGES} changes per request")
        changes = [clean_change(c) for c in raw]
        since = body.get("since", 0)
        if isinstance(since, bool) or not isinstance(since, int) or since < 0:
            raise ValueError("since must be a non-negative integer")
        if body.get("epoch") != self.epoch:
            since = 0
        _, conflicts = self.apply(changes)
        return {
            "epoch": self.epoch,
            "seq": self.seq,
            "changes": self.since(since),
            "conflicts": conflicts,
        }


def install(app, store: Optional[NoticeSyncStore] = None) -> NoticeSyncStore:
    from aiohttp import web

    from . import paths

    notices = store or NoticeSyncStore(paths.notice_sync_json())
    app["notice_sync"] = notices

    async def h_sync(request):
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 -- not JSON
            return web.json_response({"error": "body must be JSON"}, status=400)
        try:
            return web.json_response(notices.sync(body))
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)

    app.router.add_post("/api/notices/sync", h_sync)
    return notices
