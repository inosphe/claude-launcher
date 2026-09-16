"""Live harness activity from terminal control messages, before rendering.

Claude and Codex animate their OSC window title while a turn runs. Pi's
packaged extension emits an OSC heartbeat from its agent lifecycle instead.
Neither depends on the screen geometry or on a viewer being attached. Only
fresh positive signals override quiescence; stale/unknown signals fall back
to the screen heuristic. Log replay must never feed this detector.
"""

from __future__ import annotations

import re
import time

_OSC = re.compile(rb"\x1b\]([^\x07\x1b]*)(?:\x07|\x1b\\)")
_TITLE_SPINNERS = {"claude": "◐◑", "codex": "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"}
_PI_PREFIX = b"777;claunch;activity;"
_MAX_PENDING = 2048


class Detector:
    def __init__(self, harness: str) -> None:
        self.harness = harness
        self._pending = b""
        self._last_busy: float | None = None

    def feed(self, chunk: bytes) -> None:
        if (self.harness not in _TITLE_SPINNERS and self.harness != "pi") or not chunk:
            return
        data = self._pending + chunk
        end = 0
        for match in _OSC.finditer(data):
            end = match.end()
            payload = match[1]
            if self.harness == "pi":
                if payload == _PI_PREFIX + b"busy":
                    self._last_busy = time.monotonic()
                elif payload == _PI_PREFIX + b"idle":
                    self._last_busy = None
            elif payload.startswith((b"0;", b"2;")):
                title = payload[2:].decode("utf-8", "replace")
                busy = len(title) >= 2 and title[0] in _TITLE_SPINNERS[self.harness] and title[1] == " "
                self._last_busy = time.monotonic() if busy else None
        # Retain only an unfinished escape, never an already-consumed title:
        # unrelated output must not refresh a previously observed busy signal.
        start = data.rfind(b"\x1b]", end)
        pending = data[start:] if start >= end else (b"\x1b" if data.endswith(b"\x1b") else b"")
        self._pending = pending if len(pending) <= _MAX_PENDING else b""

    def busy(self, fresh_for: float) -> bool:
        return self._last_busy is not None and time.monotonic() - self._last_busy <= fresh_for
