"""Progress a running session publishes about itself, kept per session name.

A session whose real work runs out of sight of its terminal -- a pi
extension driving a durable run, say -- has a busy/idle state that says
nothing about how far that work has got. This module gives it one small,
generic record to publish instead: a stage name, a ``done``/``total`` count,
how many items are in flight (``active``), one line of text, and where a
fuller status lives (``file``, ``url``). claunch attaches no meaning to any of
it; the list API, ``claunch sessions`` and the dashboard show it as given.
``file`` in particular is shown and never opened.

Two ways in, one store:

* **OSC**, for a program writing to the session's own terminal:
  ``ESC ] 777;claunch;progress;<JSON> BEL`` (``ESC \\`` also ends it). The
  daemon takes the sequence out of the output before the screen, the
  transcript-less viewers and the web terminal see it (:class:`OscFilter`),
  so it never shows and never reaches a renderer. ``{}`` clears. It is a key
  of its own beside ``777;claunch;activity;*`` and never feeds busy/idle or
  the delivery gate (claunch-74ic3, C6).
* **HTTP/CLI**, for anything outside the terminal:
  ``POST /api/sessions/{name}/progress`` with the same object (``null``
  clears), which ``claunch progress`` sends.

Each publish replaces the whole record and is stamped ``at`` when it differs
from the one before; an identical repeat is dropped without a write. The
records live in one daemon-local JSON file so a daemon restart, and an exited
session, still show the last thing a run said. Removing a session's record
removes its progress with it.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from .. import atomic

log = logging.getLogger(__name__)

#: The OSC payload prefix (what follows ``ESC ]``).
OSC_PREFIX = b"777;claunch;progress;"
_OSC_START = b"\x1b]" + OSC_PREFIX
#: The largest serialised record accepted, in bytes. It also bounds how much
#: unterminated sequence :class:`OscFilter` will hold back from the screen.
MAX_BYTES = 2048

_TEXT_FIELDS = {"stage": 64, "text": 200, "file": 1024, "url": 1024}
_COUNT_FIELDS = ("done", "total", "active")
FIELDS = tuple(_TEXT_FIELDS) + _COUNT_FIELDS


def clean(value) -> Optional[dict]:
    """The record ``value`` publishes, or ``None`` when it clears.

    ``None`` and ``{}`` clear. Anything else must be an object whose keys are
    all in :data:`FIELDS`; text fields are trimmed and length-capped, counts
    are non-negative integers, and empty values are dropped. Raises
    ``ValueError`` naming the rule that failed.
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("progress must be an object (or null to clear)")
    unknown = sorted(set(value) - set(FIELDS))
    if unknown:
        raise ValueError(
            f"unknown progress field(s) {', '.join(unknown)}; "
            f"allowed: {', '.join(FIELDS)}"
        )
    out: dict = {}
    for key, limit in _TEXT_FIELDS.items():
        raw = value.get(key)
        if raw is None:
            continue
        if not isinstance(raw, str):
            raise ValueError(f"progress '{key}' must be text")
        text = " ".join(raw.split()) if key in ("stage", "text") else raw.strip()
        if len(text) > limit:
            raise ValueError(f"progress '{key}' is at most {limit} characters")
        if text:
            out[key] = text
    for key in _COUNT_FIELDS:
        raw = value.get(key)
        if raw is None:
            continue
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            raise ValueError(f"progress '{key}' must be a non-negative integer")
        out[key] = raw
    if len(json.dumps(out, ensure_ascii=False).encode("utf-8")) > MAX_BYTES:
        raise ValueError(f"progress is at most {MAX_BYTES} bytes")
    return out or None


def parse_osc(payload: bytes) -> Optional[dict]:
    """The record an OSC body (the bytes after :data:`OSC_PREFIX`) carries.

    Raises ``ValueError`` for anything :func:`clean` refuses or that is not
    JSON at all.
    """
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"progress OSC is not JSON: {exc}") from None
    return clean(value)


class OscFilter:
    """Take progress sequences out of a PTY stream, chunk by chunk.

    :meth:`feed` returns the chunk without them, and the bodies it removed.
    A sequence cut across two reads is held back until it ends; so is a
    chunk tail that could still turn into one (a lone ``ESC``, ``ESC ]7``
    ...). Anything else -- other OSCs included -- passes through in the same
    call. A held sequence that grows past :data:`MAX_BYTES` without ending is
    let through as it was: it is not one this module could accept, and the
    screen must not wait on it forever.
    """

    def __init__(self) -> None:
        self._held = b""

    def feed(self, chunk: bytes) -> Tuple[bytes, List[bytes]]:
        data = self._held + chunk if self._held else chunk
        self._held = b""
        if b"\x1b" not in data:
            return data, []
        out: List[bytes] = []
        bodies: List[bytes] = []
        pos = 0
        while True:
            start = data.find(_OSC_START, pos)
            if start < 0:
                break
            body_at = start + len(_OSC_START)
            end, term_len = _terminator(data, body_at)
            if end < 0:
                if len(data) - start > MAX_BYTES + len(_OSC_START) + 2:
                    # Never ending: give it back and stop looking at it.
                    out.append(data[pos:])
                    return b"".join(out), bodies
                out.append(data[pos:start])
                self._held = data[start:]
                return b"".join(out), bodies
            out.append(data[pos:start])
            bodies.append(data[body_at:end])
            pos = end + term_len
        rest = data[pos:]
        cut = _possible_start(rest)
        if cut is not None:
            self._held = rest[cut:]
            rest = rest[:cut]
        out.append(rest)
        return b"".join(out), bodies


def _terminator(data: bytes, at: int) -> Tuple[int, int]:
    """Index and length of the first BEL or ST at/after ``at``, or (-1, 0)."""
    bel = data.find(b"\x07", at)
    st = data.find(b"\x1b\\", at)
    if bel < 0 and st < 0:
        return -1, 0
    if st < 0 or (0 <= bel < st):
        return bel, 1
    return st, 2


def _possible_start(tail: bytes) -> Optional[int]:
    """Where in ``tail`` an unfinished :data:`_OSC_START` begins, if any."""
    last = tail.rfind(b"\x1b")
    if last < 0:
        return None
    piece = tail[last:]
    if len(piece) < len(_OSC_START) and _OSC_START.startswith(piece):
        return last
    return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ProgressStore:
    """The per-session records, in memory and in one JSON file.

    Read from the session-list worker thread and written from the event loop,
    so every access takes the lock. The file is read once, at construction,
    and rewritten whole on every change.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._records: Dict[str, dict] = self._load()

    def _load(self) -> Dict[str, dict]:
        try:
            if not self._path.is_file():
                return {}
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("cannot read session progress %s; starting empty", self._path)
            return {}
        out: Dict[str, dict] = {}
        if isinstance(data, dict):
            for name, rec in data.items():
                if not isinstance(rec, dict):
                    continue
                at = str(rec.get("at") or "")
                try:
                    body = clean({k: v for k, v in rec.items() if k != "at"})
                except ValueError:
                    continue
                if body:
                    out[str(name)] = {**body, "at": at}
        return out

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with atomic.scratch(self._path) as tmp:
                tmp.write_text(
                    json.dumps(self._records, ensure_ascii=False, indent=1),
                    encoding="utf-8",
                )
                atomic.replace(tmp, self._path)
        except OSError:
            # The in-memory record still answers; only a restart loses it.
            log.warning("cannot write session progress %s", self._path, exc_info=True)

    def get(self, name: str) -> Optional[dict]:
        with self._lock:
            rec = self._records.get(name)
            return dict(rec) if rec else None

    def snapshot(self, names: Iterable[str]) -> Dict[str, dict]:
        with self._lock:
            return {n: dict(self._records[n]) for n in names if n in self._records}

    def set(self, name: str, value) -> Optional[dict]:
        """Publish ``value`` (already :func:`clean`-ed or raw) for ``name``.

        Returns the stored record, ``None`` once cleared. Raises
        ``ValueError`` for a value :func:`clean` refuses.
        """
        body = clean(value)
        with self._lock:
            current = self._records.get(name)
            if body is None:
                if current is None:
                    return None
                del self._records[name]
                self._save()
                return None
            if current is not None and {k: v for k, v in current.items() if k != "at"} == body:
                return dict(current)
            rec = {**body, "at": _now()}
            self._records[name] = rec
            self._save()
            return dict(rec)

    def forget(self, names: Iterable[str]) -> None:
        with self._lock:
            dropped = [n for n in names if self._records.pop(n, None) is not None]
            if dropped:
                self._save()
