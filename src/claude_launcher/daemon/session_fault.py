"""Session faults: what a harness says when the account, not the work, stops it.

A *session fault* is a message a harness paints when it cannot go on for a
reason the agent cannot fix by working differently -- an exhausted usage
plan, a rejected login, an API status error, a network outage, a policy
refusal. It is not a task failure (a failing test, a tool error): it is the
harness standing still until a person acts.

The catalogue is data (:data:`CATALOG_RESOURCE`, ``session_faults.yaml``)
keyed by harness and by fault kind, because the messages are collected as
they are seen on live terminals. Adding one is a YAML entry plus a
``sample`` line; :class:`Catalog` compiles them and :class:`Detector`
matches them.

Where the reading comes from
----------------------------
The *bottom of the rendered screen*, not the raw output stream. A message
that stops a session stays where a person would look for it, while a phrase
in the conversation (a commit subject, this module's own documentation)
scrolls away. Two row shapes are skipped before matching: diff rows (a
document that quotes a message must not fault the session that wrote it)
and harness tool-call headers (``⏺ Bash(...)`` lines, where the command
itself may quote one). This is a heuristic, and the doc says so: a false
positive expires when its kind's window does.

The lifetime is a kind's ``window`` unless the message names a reset time
("resets Oct 26, 9am (Asia/Seoul)"), which is parsed with :mod:`zoneinfo`
and becomes the deadline. A fault is *active* until that deadline; it is
never cleared early by later output, because a recovery is not visible in
the text that follows (a re-login prints nothing that says so).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import resources
from typing import Dict, List, Optional, Tuple
from typing import Pattern as Regex
from zoneinfo import ZoneInfo

#: The packaged catalogue. The file, not this module, is where messages are
#: added; see its header comment.
CATALOG_RESOURCE = "session_faults.yaml"

#: How many rows up from the bottom of the screen are read as the live
#: region. A fault message is painted as the last thing before the terminal
#: goes quiet, so it sits inside this window when it is scanned; 16 rows
#: covers a wrapped quota sentence plus the turn summary under it.
SCAN_ROWS = 16

#: The shortest gap between two scans of one session. The session samples
#: its screen every 0.4s for idle detection; running the catalogue on every
#: sample would spend time on a screen that cannot have changed. One second
#: still catches a message that is up for two or three samples.
SCAN_INTERVAL = 1.0

#: A row is skipped when it is a diff line: ``+ text``, ``- text``, or the
#: line-numbered form a tool result uses (``  12 + text``). Writing a
#: document that quotes a fault message must not fault the writer.
_DIFF_ROW = re.compile(r"^\s*(?:\d+\s+)?[+-](?:\s|$)")

#: A row is skipped when it is a harness tool-call header: ``⏺ Bash(rg ...)``.
#: The command line can quote a message (searching for one, as the session
#: that built this module did) without the harness being in a fault.
_TOOL_ROW = re.compile(r"^\s*[⏺●]\s*[A-Za-z_][\w.-]*\(")

#: A row is skipped when it is drawn decoration: a horizontal rule under a
#: box or a notice. It carries no words, and the message kept for the tooltip
#: takes the rows under the matched one until its length limit -- so a rule
#: below the last line of a fault ended up inside the recorded message
#: (pi, s1736, 2026-10-25: ``Error: Retry failed after 3 attempts: Request
#: timed out.`` followed by 140 ``─``).
_RULE_ROW = re.compile(r"^[\s─━═\-=_*~]{3,}$")

#: The absolute form of a reset time: "resets Oct 26, 9am (Asia/Seoul)".
_RESET_ABSOLUTE = re.compile(
    r"resets?\s+(?:at\s+|on\s+)?"
    r"(?P<month>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+"
    r"(?P<day>\d{1,2}),?\s+"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>[ap]m)?\s*"
    r"\((?P<tz>[^)]+)\)",
    re.IGNORECASE,
)

#: The relative form: "resets in 3h".
_RESET_RELATIVE = re.compile(
    r"resets?\s+in\s+(?P<count>\d+)\s*(?P<unit>[mhd])\b", re.IGNORECASE
)

#: The HTTP status in "API Error: 412 ...". Reported beside the kind so a
#: status that has no pattern of its own is still visible, and a kind that
#: stands for one code (412 -> account_suspended) says which it was.
_STATUS_CODE = re.compile(r"API Error:\s*(\d{3})\b", re.IGNORECASE)

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

_UNIT_SECONDS = {"m": 60.0, "h": 3600.0, "d": 86400.0}

#: What unblocks a stop, which is the axis a person triages by. The kind is
#: the diagnosis (an exhausted plan, a 529, a prompt); this says who or what
#: clears it: ``input`` -- someone presses a key on that terminal;
#: ``account`` -- someone fixes the plan, the payment or the login;
#: ``provider`` -- the provider's state recovers, or time passes;
#: ``policy`` -- the session changes how it asks.
#:
#: A prompt that waits for a keystroke is not an account or service fault,
#: and reading it as one loses the difference between "nothing here can
#: move until a person looks" and "waiting will fix it" (claunch-pczoc.3).
RESOLVED_BY = ("input", "account", "provider", "policy")


class CatalogError(Exception):
    """The catalogue document is not usable; its message names the entry."""


@dataclass(frozen=True)
class Kind:
    """One class of fault: how it reads, what a person can do, how long it lasts.

    ``resolved_by`` is the coarse grade of that action (see
    :data:`RESOLVED_BY`): it says who or what clears the stop, so a reader
    can tell a prompt waiting for a keystroke apart from an exhausted plan
    without reading the message.
    """

    name: str
    label: str
    action: str
    window: float
    resolved_by: str


@dataclass(frozen=True)
class Pattern:
    """One message pattern.

    ``fallback`` marks a reading that names no cause -- a bare status code,
    or the client saying it gave up. It decides only when nothing else in the
    scanned window matched; see :meth:`Catalog.match`. Catalogue order is the
    tie-break between two entries that start at the same character.
    """

    kind: str
    regex: Regex
    source: str
    sample: str = ""
    fallback: bool = False


class Catalog:
    """A parsed ``session_faults.yaml``: kinds, common patterns, per-harness ones."""

    def __init__(self, document: dict) -> None:
        if not isinstance(document, dict):
            raise CatalogError("session faults: the document must be a mapping")
        version = document.get("version")
        if version != 1:
            raise CatalogError(f"session faults: unsupported version {version!r}")
        self.kinds: Dict[str, Kind] = {}
        raw_kinds = document.get("kinds")
        if not isinstance(raw_kinds, dict) or not raw_kinds:
            raise CatalogError("session faults: 'kinds' must be a non-empty mapping")
        for name, row in raw_kinds.items():
            if not isinstance(row, dict):
                raise CatalogError(f"session faults: kind {name!r} must be a mapping")
            try:
                window = float(row.get("window"))
            except (TypeError, ValueError):
                raise CatalogError(f"session faults: kind {name!r} needs a numeric window")
            if window <= 0:
                raise CatalogError(f"session faults: kind {name!r} window must be positive")
            resolved_by = str(row.get("resolved_by") or "").strip()
            if resolved_by not in RESOLVED_BY:
                raise CatalogError(
                    f"session faults: kind {name!r} needs 'resolved_by', one of "
                    + ", ".join(RESOLVED_BY)
                )
            self.kinds[str(name)] = Kind(
                name=str(name),
                label=str(row.get("label") or name),
                action=str(row.get("action") or ""),
                window=window,
                resolved_by=resolved_by,
            )
        self.common = self._patterns(document.get("common"), "common")
        raw_harnesses = document.get("harnesses") or {}
        if not isinstance(raw_harnesses, dict):
            raise CatalogError("session faults: 'harnesses' must be a mapping")
        self.harnesses: Dict[str, Tuple[Pattern, ...]] = {}
        for harness, rows in raw_harnesses.items():
            self.harnesses[str(harness)] = self._patterns(rows, f"harness {harness!r}")

    def _patterns(self, rows, where: str) -> Tuple[Pattern, ...]:
        if rows is None:
            return ()
        if not isinstance(rows, list):
            raise CatalogError(f"session faults: {where} must be a list of patterns")
        out: List[Pattern] = []
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise CatalogError(f"session faults: {where}[{index}] must be a mapping")
            kind = str(row.get("kind") or "")
            if kind not in self.kinds:
                raise CatalogError(
                    f"session faults: {where}[{index}] names unknown kind {kind!r}"
                )
            source = str(row.get("pattern") or "").strip()
            if not source:
                raise CatalogError(f"session faults: {where}[{index}] has no pattern")
            try:
                compiled = re.compile(source, re.IGNORECASE)
            except re.error as exc:
                raise CatalogError(
                    f"session faults: {where}[{index}] does not compile: {exc}"
                ) from exc
            fallback = row.get("fallback", False)
            if not isinstance(fallback, bool):
                raise CatalogError(
                    f"session faults: {where}[{index}] 'fallback' must be true or false"
                )
            out.append(Pattern(
                kind=kind,
                regex=compiled,
                source=source,
                sample=str(row.get("sample") or "").strip(),
                fallback=fallback,
            ))
        return tuple(out)

    def patterns_for(self, harness: str) -> Tuple[Pattern, ...]:
        """The patterns read for one harness: common first, then its own.

        The order breaks a tie -- two entries matching at the same character.
        Which entry decides is otherwise settled by :meth:`match`, so putting
        a message in ``common`` does not by itself outrank the harness's own
        wording painted beside it.
        """
        return self.common + self.harnesses.get(harness, ())

    def match(self, harness: str, text: str) -> Optional[Tuple[Kind, Pattern, re.Match]]:
        """The match that decides, with its kind.

        ``text`` is the scanned window with its rows joined top to bottom, so
        a match starting later is a row painted later -- the line the session
        stopped on. A pattern that matches more than once counts at its last
        hit. An entry marked ``fallback`` (a bare status code, or the client
        saying it gave up) only decides when no other entry matched in the
        same window, wherever the two sit; catalogue order breaks a tie
        between two entries starting at the same character.
        """
        best: Optional[Tuple[Tuple[bool, int], Kind, Pattern, re.Match]] = None
        for pattern in self.patterns_for(harness):
            deepest = None
            for found in pattern.regex.finditer(text):
                deepest = found
            if deepest is None:
                continue
            rank = (not pattern.fallback, deepest.start())
            if best is None or rank > best[0]:
                best = (rank, self.kinds[pattern.kind], pattern, deepest)
        if best is None:
            return None
        return best[1], best[2], best[3]


def load(text: str) -> Catalog:
    """Parse a catalogue document. Tests and the packaged file both come here."""
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - PyYAML is a hard dependency
        raise CatalogError(f"session faults: PyYAML is unavailable: {exc}") from exc
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise CatalogError(f"session faults: the YAML does not parse: {exc}") from exc
    return Catalog(document)


_default: Optional[Catalog] = None


def default() -> Catalog:
    """The packaged catalogue, parsed once per process."""
    global _default
    if _default is None:
        text = resources.files("claude_launcher").joinpath(CATALOG_RESOURCE).read_text(
            encoding="utf-8"
        )
        _default = load(text)
    return _default


# --------------------------------------------------------------------------- #
# reset times
# --------------------------------------------------------------------------- #
def reset_hint(text: str) -> str:
    """The human-readable reset phrase, as the harness wrote it ("" if none)."""
    found = _RESET_ABSOLUTE.search(text)
    if found:
        return re.sub(r"^resets?\s+", "", found.group(0), flags=re.IGNORECASE).strip()
    found = _RESET_RELATIVE.search(text)
    return found.group(0) if found else ""


def reset_deadline(text: str, now: Optional[float] = None) -> Optional[float]:
    """Epoch seconds the message names as its reset, or ``None``.

    Absolute times carry their zone ("resets Oct 26, 9am (Asia/Seoul)"), so
    they are parsed with :mod:`zoneinfo`. A date that already passed by more
    than six hours is read as next year's. An unknown zone, a missing minute
    or an impossible date answers ``None`` and the kind's window is used
    instead -- a message that cannot be read must not make a session fault
    for longer than its window.
    """
    now = time.time() if now is None else now
    found = _RESET_ABSOLUTE.search(text)
    if found:
        try:
            zone = ZoneInfo(found.group("tz").strip())
        except Exception:  # noqa: BLE001 - an unknown zone is a fallback, not a crash
            return None
        hour = int(found.group("hour")) % 12
        if (found.group("ampm") or "").lower() == "pm":
            hour += 12
        minute = int(found.group("minute") or 0)
        month = _MONTHS[found.group("month")[:3].lower()]
        day = int(found.group("day"))
        year = datetime.fromtimestamp(now, zone).year
        for candidate_year in (year, year + 1):
            try:
                candidate = datetime(
                    candidate_year, month, day, hour, minute, tzinfo=zone
                )
            except ValueError:
                return None
            stamp = candidate.timestamp()
            if stamp > now - 6 * 3600:
                return stamp
        return None
    found = _RESET_RELATIVE.search(text)
    if found:
        return now + int(found.group("count")) * _UNIT_SECONDS[found.group("unit").lower()]
    return None


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# the detector
# --------------------------------------------------------------------------- #
def _clean_row(row: str) -> bool:
    """Whether a screen row may be read as harness message text."""
    if not row.strip():
        return False
    if _DIFF_ROW.match(row) or _TOOL_ROW.match(row) or _RULE_ROW.match(row):
        return False
    return True


def _message_from(text: str, rows: List[str], start: int, limit: int = 400) -> str:
    """The matched row (and what follows it) as one readable line.

    The match itself may be the middle of the sentence -- a pattern for
    "weekly limit" starts there -- so the message kept for the tooltip and
    the API begins at the row that contains the match rather than at the
    pattern's first character. It is cut at ``limit`` characters; a wrapped
    quota sentence is the case this exists for.
    """
    begin = 0
    position = 0
    for row in rows:
        if position + len(row) >= start:
            begin = position
            break
        position += len(row) + 1
    return " ".join(text[begin: begin + limit].split())


class Detector:
    """One session's fault reading, computed from its rendered screen.

    :meth:`scan` is called from the session's sample loop. It reads only the
    bottom ``SCAN_ROWS`` rows, at most once per :data:`SCAN_INTERVAL`, and
    keeps the last match as this session's fault until the kind's window (or
    the message's own reset time) runs out. Seeing the same kind again inside
    that window does not extend it; seeing it after the window opens a new
    run and counts a strike.
    """

    def __init__(
        self,
        harness: str,
        catalog: Optional[Catalog] = None,
        clock=time.time,
    ) -> None:
        self.harness = harness
        self._catalog = catalog if catalog is not None else default()
        self._clock = clock
        self._record: Optional[dict] = None
        self._last_scan = 0.0

    @property
    def catalog(self) -> Catalog:
        return self._catalog

    def scan(self, screen, now: Optional[float] = None) -> bool:
        """Read the bottom of ``screen``; False when this call was throttled.

        The caller keeps its "screen unchanged" fingerprint only when this
        answers True, so a scan skipped by :data:`SCAN_INTERVAL` is retried
        on the next sample instead of waiting for the next screen change.
        """
        if not self._catalog.patterns_for(self.harness):
            return True
        now = self._clock() if now is None else now
        if now - self._last_scan < SCAN_INTERVAL:
            return False
        self._last_scan = now
        rows = [row for row in screen.tail_lines(SCAN_ROWS) if _clean_row(row)]
        if not rows:
            return True
        text = " ".join(rows)
        found = self._catalog.match(self.harness, text)
        if found is None:
            return True
        kind, _pattern, match = found
        message = _message_from(text, rows, match.start())
        status = _STATUS_CODE.search(text)
        hint = reset_hint(text)
        deadline = reset_deadline(text, now)
        previous = self._record
        same_kind = previous is not None and previous["kind"] == kind.name
        # A run is one continuous sighting of one kind: it starts at the
        # first match and ends when its window does. A re-match inside that
        # window updates what was seen -- the message, the last sighting --
        # and leaves the window where it was. Pushing ``until`` out on every
        # scan is what kept a fault on the rail after the session had moved
        # on: the message kept for the tooltip is an excerpt of the rows
        # under the match, so a paragraph scrolling through the scanned rows
        # changed it every second, and a changed message started a new
        # record with a fresh window (s1812, 2026-10-25: the session quoted
        # "Request timed out" in its own analysis and the badge stayed 15
        # minutes past the last real sighting, again and again).
        #
        # ``reset`` is the text the window was measured from, so it belongs
        # to the run exactly as ``since`` and ``until`` do. Re-reading it on
        # a re-match let one row show two observations at once -- a screen
        # drawing both a weekly and a five-hour limit line keeps the same
        # kind, so the tooltip said "resets: Oct 25, 2pm" beside a window
        # that ended Oct 26 09:00.
        #
        # A message can also name a reset time that has already passed, and
        # then ``until`` is behind ``since`` from the first scan: ``now <
        # until`` never holds, every scan would be a new run, and ``strikes``
        # would count scans rather than sightings (1 per second while the
        # screen keeps changing). Such a record has no window to end, so a
        # sighting like it is folded into the record instead.
        #
        # Both sides of that comparison have to be about the same observation.
        # Reading it from the previous record alone held the window shut for
        # good: a session that once drew a stale reset stayed inactive when a
        # later screen named a future one (claunch-5ldt8 review).
        previous_expired = (
            previous is not None and previous["until"] <= previous["since"]
        )
        this_expired = deadline is not None and deadline <= now
        running = same_kind and (
            now < previous["until"] or (previous_expired and this_expired)
        )
        if running:
            since = previous["since"]
            until = previous["until"]
            reset = previous.get("reset", "")
            strikes = previous.get("strikes", 1)
        else:
            since = now
            until = deadline if deadline is not None else now + kind.window
            reset = hint
            if not same_kind:
                # A different kind opens its own first run.
                strikes = 1
            elif previous_expired:
                # The run being replaced never had a window, so nothing closed
                # and this is not a second sighting of it.
                strikes = previous.get("strikes", 1)
            else:
                # Coming back after the window closed is a second sighting.
                strikes = previous.get("strikes", 0) + 1
        self._record = {
            "harness": self.harness,
            "kind": kind.name,
            "label": kind.label,
            "action": kind.action,
            "resolved_by": kind.resolved_by,
            "message": message,
            "code": status.group(1) if status else "",
            "reset": reset,
            "since": since,
            "last_seen": now,
            "until": until,
            "strikes": strikes,
        }
        return True

    def view(self, now: Optional[float] = None) -> Optional[dict]:
        """The last fault with its ``active`` flag (``None`` when never faulted)."""
        if self._record is None:
            return None
        now = self._clock() if now is None else now
        record = self._record
        return {
            "harness": record["harness"],
            "kind": record["kind"],
            "label": record["label"],
            "action": record["action"],
            "resolved_by": record.get("resolved_by", ""),
            "message": record["message"],
            "code": record.get("code", ""),
            "reset": record["reset"],
            "since": _iso(record["since"]),
            "last_seen": _iso(record["last_seen"]),
            "until": _iso(record["until"]),
            "strikes": record.get("strikes", 1),
            "active": now < record["until"],
            "source": "screen",
        }

    def forget(self) -> None:
        """Drop the record (a session redefined onto another harness, tests)."""
        self._record = None
