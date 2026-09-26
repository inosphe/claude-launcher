"""A live managed session: one PTY child plus everything observed about it.

Concurrency model: all mutable state lives on the daemon's asyncio event loop.
The only other thread is a per-session *reader* pumping blocking PTY reads into
the loop via ``call_soon_threadsafe`` — identical on Windows (ConPTY reads have
no fd to select on) and Unix (kept symmetric on purpose).

Each output chunk is fed to the pyte screen, appended to the on-disk raw log,
and fanned out to attached subscribers (WebSocket viewers). A sampler task
fingerprints the rendered screen a few times a second so the
:class:`~claude_launcher.daemon.idle.IdleTracker` can tell "the program is
painting a spinner" apart from "the program printed something new".
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Set, Tuple, Union

from .. import atomic
from .. import harnesses as harness_registry
from . import compacting, keys as keys_mod, process_priority
from . import paths, pty_backend
from .harness import CLAUDE_HARNESS, SessionDef
from .idle import IdleTracker
from .activity import Detector as ActivityDetector
from .screen import BACKGROUND_HISTORY, RenderBudget, ScreenFeeder, ScreenState

#: Screen sampling cadence for idle detection (seconds).
SAMPLE_INTERVAL = 0.4

#: Rotate the raw output log beyond this size (a single .1 backup is kept).
LOG_MAX_BYTES = 10 * 1024 * 1024

#: The most PTY output one session may have posted to the event loop and not
#: yet had processed there. The reader thread posts one callback per read;
#: with fourteen pi job sessions writing ~800 KiB/s between them (2026-09-11
#: 14:08) the loop fell behind, its ready queue reached 17,000 callbacks and
#: 4 GB, and it never got back to accept(). Past this cap the reader thread
#: keeps logging to disk (that write no longer happens on the loop at all)
#: and stops posting until the loop has caught up; what is skipped is the
#: live render and the viewer broadcast, and the person watching is told.
INBOUND_MAX = 4 * 1024 * 1024

#: How much of a session's log is replayed through pyte to rebuild its screen
#: — for an exited record on first capture, and for a restored session getting
#: its scrollback back (:meth:`Session.seed_screen_from_log`). Large enough for
#: a full-screen TUI repaint, small enough that neither attaching nor a daemon
#: restart stalls on it.
REPLAY_TAIL_BYTES = 256 * 1024

#: Gap between a paste and its submitting Enter (seconds), so the CR arrives in
#: its own PTY read — see :meth:`Session.paste`. Measured against Claude Code:
#: 0 never submits, 20ms already does; 150ms leaves room for a busy renderer.
PASTE_ENTER_DELAY = float(os.environ.get("CLAUNCH_PASTE_ENTER_DELAY") or 0.15)

#: Bound on waiting for a TUI to repaint after a paste. A repaint is positive
#: evidence that the consumer processed the paste; the timeout preserves
#: delivery for TUIs that update their composer without changing the sampled
#: screen (or whose repaint is hidden behind an animation).
PASTE_RENDER_TIMEOUT = float(
    os.environ.get("CLAUNCH_PASTE_RENDER_TIMEOUT") or 2.0
)

#: How long a TUI has, from spawn, to become deliverable-to before
#: :meth:`Session.deliver` stops waiting and writes anyway. Generous: it is
#: paid at most once per session, and being late is free while being early
#: loses the message.
INPUT_READY_TIMEOUT = float(os.environ.get("CLAUNCH_INPUT_READY_TIMEOUT") or 30.0)

#: How long a TUI must stay idle *after* taking the keyboard before it is
#: considered done starting up. On top of the idle threshold, so the real wait
#: is longer; measured against Claude Code, whose input mounts a few seconds
#: before it finishes loading and starts accepting a submit.
INPUT_SETTLE = float(os.environ.get("CLAUNCH_INPUT_SETTLE") or 3.0)

#: How long the keyboard must have been quiet before an automated delivery may
#: type into a terminal (seconds). A human composing a message pauses to think
#: for a couple of seconds — long enough for the *screen* to read as idle —
#: and a paste-plus-Enter landing in that pause submits their half-typed line
#: with the delivery folded into it. Keystrokes are a signal the screen
#: sampler cannot see, so they are tracked separately (see
#: :meth:`Session.note_human_input`).
TYPING_GUARD = float(os.environ.get("CLAUNCH_TYPING_GUARD") or 5.0)

#: Bound on how long :meth:`Session.deliver` waits for the keyboard to go
#: quiet. Someone typing continuously holds a message at most this long — a
#: delayed delivery is recoverable, an interleaved one already went wrong, but
#: the message itself must never be dropped (the INPUT_READY_TIMEOUT
#: reasoning, applied to the reader's other half: the human).
TYPING_HOLD_TIMEOUT = float(os.environ.get("CLAUNCH_TYPING_HOLD_TIMEOUT") or 30.0)

#: How long an *unsent draft* keeps the keyboard held (seconds). TYPING_GUARD
#: asks "was a key pressed in the last few seconds", which a human composing a
#: prompt answers "no" every time they stop to think — and a five-second pause
#: is not the end of a message, it is the middle of one. So the composer's
#: state is tracked as well as its timing: while characters are sitting in it
#: unsent (see :func:`draft_state_from_bytes`) the keyboard counts as busy for
#: this much longer, and it is released the moment the human submits (Enter)
#: or clears the line — not on a timer. The cap is only for the person who
#: types one character and walks away.
DRAFT_GUARD = float(os.environ.get("CLAUNCH_DRAFT_GUARD") or 180.0)

#: The same cap for a draft that exists only as a composing mark — an IME
#: syllable the terminal has reported but whose bytes have not arrived.
#: While a composition is alive the client re-marks it at ~1s cadence, so
#: silence this long means it ended without a commit: cancelled, or the
#: mark was the last thing it ever sent. Text in the composer gets
#: DRAFT_GUARD; text that never reached the composer gets this.
COMPOSING_GUARD = float(os.environ.get("CLAUNCH_COMPOSING_GUARD") or 10.0)

#: How long a FORCED delivery ("deliver now", pressed by a person) waits for
#: the keyboard before it stops waiting. The ordinary wait is
#: :data:`TYPING_HOLD_TIMEOUT`, which is right for a background sender and
#: wrong here twice over: the caller is an HTTP request somebody is watching,
#: and parking it for half a minute is the same non-answer as refusing. Kept
#: long enough to slip between two keystrokes of ordinary typing, short
#: enough that "now" means now.
FORCE_TYPING_GRACE = float(os.environ.get("CLAUNCH_FORCE_TYPING_GRACE") or 1.5)

#: After a forced delivery submits somebody's unsent line to get it out of the
#: way, how long to let the TUI clear its composer before pasting. Too short
#: and the paste lands in a composer that still holds the line it is meant to
#: follow -- the splice this whole path exists to avoid.
FORCE_DRAFT_SETTLE = float(os.environ.get("CLAUNCH_FORCE_DRAFT_SETTLE") or 0.4)

log = logging.getLogger(__name__)


class _SubmittedLineTracker:
    """Recover submitted terminal lines from the raw input byte stream.

    The tracker covers the input needed to recognize an explicitly typed
    ``/new`` before that command reaches the child: ordinary text,
    erase/clear keys and ANSI escape sequences. Bracketed-paste newlines stay
    inside the buffered composer content.
    """

    _MAX_BYTES = 4096

    def __init__(self) -> None:
        self._line = bytearray()
        self._escape: Optional[bytearray] = None
        self._bracketed_paste = False
        self._overflow = False
        self._last_was_cr = False

    def _append(self, value: int) -> None:
        if self._overflow:
            return
        if len(self._line) >= self._MAX_BYTES:
            self._line.clear()
            self._overflow = True
            return
        self._line.append(value)

    def _submit(self) -> Optional[str]:
        if self._overflow:
            result = None
        else:
            result = self._line.decode("utf-8", errors="replace")
        self._line.clear()
        self._overflow = False
        return result

    def feed(self, data: bytes) -> List[str]:
        """Return complete lines submitted by ``data`` in wire order."""
        submitted: List[str] = []
        for byte in data:
            if self._escape is not None:
                # ESC + Enter is the TUI's multiline spelling.  It changes
                # the composer content and does not submit it.
                if not self._escape and byte in (0x0D, 0x0A):
                    self._append(0x0A)
                    self._escape = None
                    self._last_was_cr = False
                    continue
                self._escape.append(byte)
                if len(self._escape) == 1 and byte not in (0x5B, 0x4F):
                    self._escape = None  # Alt-key chord
                    continue
                if len(self._escape) > 1 and 0x40 <= byte <= 0x7E:
                    sequence = bytes(self._escape)
                    if sequence == b"[200~":
                        self._bracketed_paste = True
                    elif sequence == b"[201~":
                        self._bracketed_paste = False
                    self._escape = None
                continue

            if byte == 0x1B:
                self._escape = bytearray()
                self._last_was_cr = False
            elif byte in (0x0D, 0x0A):
                if self._bracketed_paste:
                    self._append(0x0A)
                elif not (byte == 0x0A and self._last_was_cr):
                    line = self._submit()
                    if line is not None:
                        submitted.append(line)
                self._last_was_cr = byte == 0x0D
            elif byte in (0x03, 0x15):  # C-c / C-u clear the composer
                self._line.clear()
                self._overflow = False
                self._last_was_cr = False
            elif byte in (0x08, 0x7F):  # Backspace
                if self._line and not self._overflow:
                    self._line.pop()
                self._last_was_cr = False
            elif byte >= 0x20:
                self._append(byte)
                self._last_was_cr = False
        return submitted


def draft_state_from_bytes(data: bytes) -> Optional[bool]:
    """What ``data`` — keystrokes a human just sent — did to their composer.

    ``True`` = there is now an unsent draft in it, ``False`` = it was just
    submitted or cleared, ``None`` = neither (nothing about the draft
    changed). The last one matters as much as the others: an arrow key, a
    lone Escape or a backspace must leave a draft *open*, because a message
    typed into the terminal at that moment is still typed into a line
    somebody is writing.

    Read from the bytes, not from the screen. A composer's contents are a
    guess to make from a rendered grid — placeholder hints look exactly like
    text once the colours are gone — but they are a fact on the wire: this is
    the keyboard, and these are the keys.

    Rules, in the order they are tested:

    * an escape sequence (arrows, function keys, a bracketed-paste *marker*)
      changes nothing — its bytes are not characters, whatever they look like;
    * ``ESC`` + Enter and backslash + Enter insert a newline *into* the
      composer rather than submitting it (Alt/Shift-Enter, the continuation
      most TUIs take), so they open a draft rather than closing one;
    * a bare Enter submits, and ``C-c`` / ``C-u`` discard: the composer is
      empty after either, so the draft is closed;
    * anything printable — including the multi-byte UTF-8 of a committed
      Hangul syllable, and text arriving inside a bracketed paste — opens one.
    """
    state: Optional[bool] = None
    i = 0
    n = len(data)
    while i < n:
        b = data[i]
        if b == 0x1B:  # ESC: a chord or a control sequence, not typing
            nxt = data[i + 1] if i + 1 < n else None
            if nxt in (0x5B, 0x4F):  # CSI / SS3 — skip to the final byte
                i += 2
                while i < n and not (0x40 <= data[i] <= 0x7E):
                    i += 1
                i += 1
                continue
            if nxt in (0x0D, 0x0A):  # Alt/Shift-Enter: a newline in the draft
                state = True
                i += 2
                continue
            i += 2 if nxt is not None else 1  # ESC alone, or Alt-<key>
            continue
        if b in (0x0D, 0x0A):
            # Backslash-Enter is the other "newline, don't send" spelling.
            state = True if (i and data[i - 1] == 0x5C) else False
        elif b in (0x03, 0x15):  # C-c, C-u — the line is gone
            state = False
        elif b >= 0x20 and b != 0x7F:  # printable (0x7F is backspace)
            state = True
        i += 1
    return state

STATUS_STARTING = "starting"
STATUS_BUSY = "busy"
STATUS_IDLE = "idle"
STATUS_EXITED = "exited"

#: The lifecycle partition every reader filters a record by. An exited record
#: is ``killed`` unless it carries the pause marker, and an archived one is
#: neither however it exited — so a record lands in exactly one of the four
#: and the counts of the filters built on this add up. Defined once, here:
#: the session rail (``daemon/api.py``) and the mesh roster
#: (``daemon/mesh.py``) both ask this question, and two copies of the answer
#: would let the same record be filed two ways on one page.
CATEGORY_RUNNING = "running"
CATEGORY_KILLED = "killed"
CATEGORY_PAUSED = "paused"
CATEGORY_ARCHIVED = "archived"


def session_category(session) -> str:
    """Which of the four partitions ``session`` belongs to."""
    if getattr(session, "archived_at", None):
        return CATEGORY_ARCHIVED
    if session.exited:
        return (
            CATEGORY_PAUSED
            if getattr(session, "paused_at", None)
            else CATEGORY_KILLED
        )
    return CATEGORY_RUNNING

#: A positive "a turn is in flight" marker the claude TUI paints into its
#: footer iff a turn is running, and omits when it is awaiting input. The
#: idle heuristic below is built to ignore exactly the rows that move during
#: a turn (the spinner + elapsed/token counter), so without this marker it
#: reads "idle" mid-turn and the status dot goes green while the agent works.
#: The marker is the one signal on those otherwise-ignored rows.
#:
#: VERSION-FRAGILE: this is an upstream UI string and *will* change across
#: claude versions. It is only consulted as an override when the quiescence
#: heuristic already reads idle, and then only on the *footer* row (the
#: bottom of the grid — see :meth:`_claude_turn_in_flight`), so a changed or
#: missing marker — or one that appears only in transcript content — degrades
#: to the old heuristic behaviour, never to a false "busy".
_CLAUDE_TURN_MARKER = "esc to interrupt"

#: Maximum age of a live activity signal or the Claude footer's spinner.
#: Native titles animate throughout a turn; Pi emits a heartbeat every 5s.
#: Expiry prevents a frozen process from retaining a busy status indefinitely.
#: The environment override also applies to the existing footer fallback.
TURN_MARKER_FRESH_FOR = float(
    os.environ.get("CLAUNCH_TURN_MARKER_FRESH_FOR") or 15.0
)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


#: How much of a queued message the rail shows. Long enough to tell a cflow
#: nudge from a mesh message from a briefing, short enough that a row stays a
#: row. The message itself is read in the terminal it is going to.
DELIVERY_PREVIEW_CHARS = 90


def _delivery_preview(text: str) -> str:
    """A single line standing for a message that has not been typed yet.

    Machine-generated deliveries open with a header line (``---``, a title,
    ``mesh:``), so the first line alone often says nothing about which one
    this is. The first line that carries words is the one worth showing.
    """
    for line in text.splitlines():
        stripped = line.strip().strip("#").strip()
        if stripped and stripped != "---":
            if len(stripped) > DELIVERY_PREVIEW_CHARS:
                return stripped[:DELIVERY_PREVIEW_CHARS - 1] + "…"
            return stripped
    return ""


def delivery_stamp() -> str:
    """The wall-clock line prefixed to every :meth:`Session.deliver` message.

    Machine-local time with its UTC offset, taken at the moment the paste is
    about to land (so the holds in :meth:`Session.deliver` are already behind
    it). One format, one place: agents and humans reading a transcript date
    an automated delivery from this line, so nothing else should invent its
    own variant of it.
    """
    return datetime.now().astimezone().strftime(
        "[claunch delivered %Y-%m-%d %H:%M:%S %z]"
    )


class Session:
    """Owns one PTY child; created and torn down by the SessionManager.

    Constructed and *started* in two steps. Between them the session exists —
    it is in the registry, it has a name, it can be joined into a mesh — but
    nothing is running yet and its command line is not fixed. That gap is what
    lets a session be arranged before it runs: its mesh membership and its
    cflow run are settled first, and the opening message they compose is handed
    to the harness as an argument instead of typed into a terminal that may not
    be reading yet (see :meth:`_await_readable`). Only
    :class:`~claude_launcher.daemon.manager.SessionManager` should hold an
    unstarted one.
    """

    def __init__(
        self,
        sdef: SessionDef,
        *,
        idle_threshold: float,
        scrollback: int,
        created_at: Optional[str] = None,
        last_visited_at: Optional[str] = None,
        last_input_at: Optional[str] = None,
        delivery_hold: bool = False,
        focused_session_scheduling: bool = True,
        background_render_delay: float = 0.05,
        render_budget: Optional[RenderBudget] = None,
    ) -> None:
        self.sdef = sdef
        self.argv: List[str] = []
        self.pty = None
        self.pid: Optional[int] = None
        self.idle_threshold = idle_threshold
        self._scrollback = scrollback
        self.screen = ScreenState(sdef.cols, sdef.rows, history=scrollback)
        # Nobody is attached to a new session: it starts on the background
        # scrollback and grows to the configured one when a viewer focuses it.
        self.screen.set_history_limit(min(scrollback, BACKGROUND_HISTORY))
        entry = harness_registry.get(sdef.harness)
        #: Harness-declared: whether the grid is kept current while nobody is
        #: attached (see ``Harness.background_render``).
        self.background_render = entry.background_render if entry else True
        #: ``time.monotonic()`` of the last PTY chunk; the idle signal for a
        #: parked (attached-only, unattended) grid, see ``_heuristic_status``.
        self._last_output_mono = 0.0
        self._focused_subscribers: Set[object] = set()
        self._focused_session_scheduling = focused_session_scheduling
        self._cpu_background: Optional[bool] = None
        self._feeder = ScreenFeeder(
            self.screen,
            foreground=self.is_focused,
            background_delay=background_render_delay,
            on_overflow=self._on_render_overflow,
            budget=render_budget,
            background_render=self.background_render,
        )
        self.tracker = IdleTracker()
        #: Compaction-notice scanner (see :mod:`compacting`): fed every pty
        #: chunk in :meth:`_on_output`, read by the dashboard row as the
        #: session's ``compacting`` flag.
        self._compacting = compacting.Detector(sdef.harness)
        self._activity = ActivityDetector(sdef.harness)
        #: When this session was *first* made, not when this object was.
        #: A relaunch that keeps the name — a daemon restart's restore, a
        #: respawn, a redefine — is the same session continuing, and the
        #: manager hands the old value back in so listings stay in the order
        #: the sessions were actually created (see SessionManager.list).
        self.created_at = created_at or _utcnow()
        self.last_output_at: Optional[str] = None
        #: The last moment a person was *here* — a viewer socket attached to
        #: this session (the web terminal, or ``claunch attach``). Stamped
        #: when the socket opens and again when it closes, so a tab left open
        #: for an hour is a visit that ended an hour later rather than one
        #: that ended the second it began; while a socket is still open,
        #: :meth:`viewers` says so and the reader should trust that over the
        #: stamp. Wall clock rather than monotonic because it is shown to a
        #: human and survives a daemon restart through
        #: :meth:`SessionManager.persist`.
        #:
        #: Carried in on the relaunch paths for the same reason
        #: ``created_at`` is: a restore, a respawn or a redefine is this
        #: session continuing, and starting it over with "never visited"
        #: would quietly wipe the very reading these exist to give.
        self.last_visited_at: Optional[str] = last_visited_at
        #: The last moment a person *typed* here, at a terminal they were
        #: sitting at. Deliberately not ``send-keys`` and not a delivery: the
        #: question this answers is "when did I last say something to this
        #: agent", and a script or another session typing into it is not an
        #: answer to that. The monotonic twin of this
        #: (``_last_terminal_input``) is what the delivery gate reads; this
        #: one exists to be read by a person, and is persisted with the visit.
        self.last_input_at: Optional[str] = last_input_at
        self.exit_code: Optional[int] = None
        self.exited_at: Optional[str] = None
        #: Set by the manager when Windows ended this process at a logoff or
        #: shutdown (manager.ended_by_os): the exit is recorded, but the
        #: record stays one the next boot restores.
        self.ended_by_os = False
        # Archiving is a retained record's lifecycle marker. A live session
        # always starts outside the archive; respawn therefore clears it by
        # constructing a new Session from the retained definition.
        self.archived_at: Optional[str] = None
        #: The other lifecycle marker: set by :meth:`pause` just before the
        #: child is terminated, so the record this session leaves reads as
        #: *paused* rather than killed. The process side is identical to a
        #: kill (the program is gone; the record stays respawnable) — the
        #: marker exists for the reader, and for the bulk resume that brings
        #: back exactly the ones that were paused. Cleared the same way
        #: ``archived_at`` is: respawn constructs a fresh Session.
        self.paused_at: Optional[str] = None
        #: When the board sweep this incarnation's ending owed was made
        #: (:meth:`beads.Board.sweep_many`). Persisted with the record, so a
        #: restart that retires it does not sweep the same ending again
        #: (claunch-fh8u1.2); a respawn starts a new incarnation at None.
        self.swept_at: Optional[str] = None
        #: Set by :meth:`kill` just before the child is signalled: somebody
        #: asked for this ending (a person, a parent, the beads wind-down,
        #: kill-on-end, a pause). The exit code cannot say so -- a signalled
        #: harness and one that crashed both leave 2 on Windows -- and the
        #: run event clock tells a parent only about the endings nobody
        #: asked for. In memory only; a respawn constructs a fresh Session.
        self.kill_requested = False
        #: Set by :meth:`SessionManager.respawn` right after this instance is
        #: constructed: a person explicitly asked for this incarnation, as
        #: opposed to :meth:`SessionManager.restore_all` starting it back up
        #: unattended after a daemon restart. In memory only, and
        #: deliberately not carried across a respawn's own relaunch or a
        #: restart's — each new incarnation starts False, so the deference
        #: this buys a session (see ``cflow kill-on-end`` in
        #: :mod:`cflow_clock`) lasts only until the daemon that saw the
        #: person's request goes down; the next boot re-judges from
        #: scratch, same as it always did.
        self.resumed_by_human = False
        self.exited = False
        self._started_mono = time.monotonic()
        self._subscribers: Set[asyncio.Queue] = set()
        self._loop = asyncio.get_running_loop()
        #: One writer into the PTY at a time; see :meth:`write_bytes`.
        self._write_lock = asyncio.Lock()
        self._status = STATUS_STARTING
        self._saw_output = False
        #: Latched once the harness has been seen ready to take a message; see
        #: :meth:`_await_readable`.
        self._input_ready = False
        #: Automated deliveries paste a block and then a separate Enter.  A
        #: newly created session can receive its mesh briefing and a cflow
        #: start nudge together; serialising the complete delivery keeps the
        #: two paste/Enter pairs whole and ordered.
        self._delivery_lock = asyncio.Lock()
        self._deferred_deliveries: Set[asyncio.Task] = set()
        #: One record per queued delivery that has not been typed yet, in the
        #: order they will go in. The tasks above are the delivery; this is
        #: the only thing anyone outside can see of it. Everything that comes
        #: through here is cflow's (the clock's reminders and stall pings, the
        #: dashboard's nudges); mesh messages take their own path and are
        #: already published by the queued view. Without it a person who
        #: presses a cflow button has nothing to read between pressing it and
        #: the message appearing in the session minutes later, and no way to
        #: tell a wait from a message that was dropped
        #: (claunch-restart-disconnect-banner-12p2). Not durable: a daemon
        #: restart loses the queue, and this list with it, which is the
        #: truth about the queue rather than a shortcoming of the list.
        self._pending_deliveries: list = []
        self._delivery_seq = 0
        self._deferred_delivery_lock = asyncio.Lock()
        #: Monotonic time a human last typed here (attach/web keystrokes,
        #: ``claunch send-keys``); 0.0 = never. See :meth:`keyboard_busy`.
        self._last_human_input = 0.0
        #: The subset of that which came from a *terminal* someone is sitting
        #: at (attach/web keystroke frames and the web terminal's composing
        #: marks) — never ``send-keys``. What the passthrough itself waits on;
        #: see :meth:`send_keys`.
        self._last_terminal_input = 0.0
        #: Whether characters typed at such a terminal are still sitting in
        #: the composer unsent. Opened and closed by the keys themselves (see
        #: :func:`draft_state_from_bytes`), never by a timer: a delivery typed
        #: into a half-written line is the corruption all of this exists to
        #: prevent, and the human's own Enter is what says it is safe again.
        self._draft_open = False
        #: Whether the open draft exists only as a composing mark — an IME
        #: syllable or a phone word that lives in the terminal's textarea
        #: and has sent no bytes yet. The composer itself is still empty in
        #: that case, so the hold is a shorter one (:data:`COMPOSING_GUARD`):
        #: a composition abandoned mid-syllable (Esc, focus lost) produces
        #: no closing bytes at all, and DRAFT_GUARD would hold this
        #: session's mail for minutes over a draft that never existed here.
        self._draft_uncommitted = False
        #: A person's standing "don't type anything in here" — set from the
        #: dashboard, not derived from the keyboard. Every hold above is the
        #: daemon *guessing* from timing that now is a bad moment; this one
        #: is somebody saying so, and it does not expire, because a guess
        #: that lapses after five seconds is right and a decision that
        #: lapses after five seconds is broken. Read by the delivery gate
        #: (:meth:`MeshManager._deliver_to`) and reported by the queued view;
        #: cleared by :meth:`set_delivery_hold` and by nothing else.
        #:
        #: Persisted with the session record (:meth:`SessionManager.persist`)
        #: and handed back in here on every relaunch that keeps the name — a
        #: daemon restart's restore, a respawn, a redefine. It used to be kept
        #: in memory on the reading that it means "I am at this keyboard right
        #: now"; that reading is wrong for the case it is actually set in. A
        #: person pins a session shut and walks away, the daemon restarts for
        #: reasons that have nothing to do with them, and the session they had
        #: shut is taking mail again with nothing on screen to say the setting
        #: they made is gone. The automatic holds above still expire on their
        #: own — a guess should lapse; this one is a decision, and a decision
        #: that a restart silently reverses is the failure this fixes.
        self._delivery_hold = bool(delivery_hold)
        #: Called once, with this session, when the child is gone for good —
        #: whatever ended it (see :meth:`_finish`). Set by the manager, which
        #: fans it out to whoever asked (the board sweep in
        #: :mod:`claude_launcher.daemon.beads`). Synchronous: a hook that has
        #: work to do schedules it.
        self.on_exit: Optional[Callable[["Session"], None]] = None
        #: Called before a complete terminal line reaches the child.  The
        #: manager uses the one launcher-relevant command, ``/new``, to
        #: snapshot conversation ownership before the harness creates the
        #: replacement (Codex its rollout, pi its session file). Other
        #: submitted lines are intentionally ignored by the manager.
        self.on_command_submitted: Optional[
            Callable[["Session", str], None]
        ] = None
        self._submitted_lines = _SubmittedLineTracker()

        session_dir = paths.session_dir(sdef.name)
        session_dir.mkdir(parents=True, exist_ok=True)
        # The session's own scratch space, named to it in ``CLAUNCH_SCRATCH``
        # (see ``harness.build_command``). It is created here so that the
        # variable names a directory that exists from the session's first
        # command, rather than one its first writer has to remember to make.
        paths.session_scratch_dir(sdef.name).mkdir(parents=True, exist_ok=True)
        self._log_path = paths.session_log(sdef.name)
        self._log = open(self._log_path, "ab")
        #: Bytes posted to the loop by the reader thread and not yet consumed
        #: by :meth:`_on_output` (see :data:`INBOUND_MAX`). Shared between
        #: the two threads, hence the lock.
        self._inflight = 0
        self._inflight_lock = threading.Lock()
        self._inbound_overflowing = False
        #: Bytes the reader thread logged but did not post (cumulative).
        self.inbound_dropped_bytes = 0

    def start(self, argv: List[str], env: Dict[str, str], cwd: str) -> None:
        """Spawn the child and begin reading it. Called once, by the manager."""
        self.argv = argv
        self._started_mono = time.monotonic()
        self._attach(pty_backend.spawn(
            argv, env=env, cwd=cwd, cols=self.sdef.cols, rows=self.sdef.rows
        ))

    async def start_async(
        self, argv: List[str], env: Dict[str, str], cwd: str
    ) -> None:
        """:meth:`start`, with the spawn on a worker thread.

        Creating the process and its pseudo console took 250-350 ms per
        session on this machine, all of it on the event loop and so every
        socket's (claunch-y9ax9.1). The native part of that releases the GIL
        -- measured: a busy thread beside the spawn never waited longer than
        16 ms -- so a thread is enough to take it off the loop. Callers
        already see a staged session with no ``pty`` while an HTTP handler
        awaits between staging and launch, and every writer checks for it.
        """
        self.argv = argv
        self._started_mono = time.monotonic()
        self._attach(await asyncio.to_thread(
            pty_backend.spawn,
            argv, env=env, cwd=cwd, cols=self.sdef.cols, rows=self.sdef.rows,
        ))

    def _attach(self, pty) -> None:
        self.pty = pty
        self.pid = self.pty.pid
        self._apply_cpu_priority()

        # A dedicated *daemon* thread, NOT the loop's default executor: at
        # daemon exit asyncio joins executor threads, and a reader stuck in a
        # blocking ConPTY read would hold the whole process (and its singleton
        # lock) hostage. A daemon thread can never block interpreter exit.
        self._reader = threading.Thread(
            target=self._read_pump, name=f"pty-read-{self.sdef.name}", daemon=True
        )
        self._reader.start()
        self._sampler = self._loop.create_task(self._sample_loop())

    # ------------------------------------------------------------------ #
    # output pipeline (reader thread -> loop)
    # ------------------------------------------------------------------ #
    def _read_pump(self) -> None:  # runs on the reader thread
        try:
            while True:
                chunk = self.pty.read()
                if not chunk:
                    break
                # The transcript is written here, on this thread, before the
                # loop hears of the chunk: disk I/O per chunk was 90% of what
                # the loop did per callback, and the loop is the one resource
                # every session shares. ``self._log`` is this thread's alone
                # until EOF (``_finish`` closes it after ``_on_eof``, which is
                # posted below, after the last write).
                self._append_log(chunk)
                self._post_output(chunk)
            self._loop.call_soon_threadsafe(self._on_eof)
        except RuntimeError:
            pass  # event loop already closed (daemon teardown)

    def _post_output(self, chunk: bytes) -> None:  # runs on the reader thread
        """Hand ``chunk`` to the loop, unless the loop is already behind by
        :data:`INBOUND_MAX` bytes of this session's output -- then it is
        logged only, and the owner is told once per episode."""
        with self._inflight_lock:
            over = self._inflight >= INBOUND_MAX
            if over:
                self.inbound_dropped_bytes += len(chunk)
                first = not self._inbound_overflowing
                self._inbound_overflowing = True
            else:
                self._inflight += len(chunk)
        if over:
            if first:
                self._loop.call_soon_threadsafe(self._on_input_overflow)
            return
        self._loop.call_soon_threadsafe(self._on_output, chunk)

    def _on_input_overflow(self) -> None:
        """The loop fell :data:`INBOUND_MAX` behind this session's output."""
        log.warning(
            "session %r writes faster than the loop takes it: output is being "
            "logged but not rendered or broadcast until the loop catches up "
            "(inbound cap %d)",
            self.sdef.name, INBOUND_MAX,
        )
        try:
            self.notify(
                "output arrives faster than the daemon can take it; the screen "
                "is skipping ahead (the log has everything)",
                ttl=30, level="warn",
            )
        except Exception:  # a notice must never break the output path
            log.debug("inbound overflow notice for %r failed", self.sdef.name, exc_info=True)

    def _on_render_overflow(self, dropped: int) -> None:
        """The render queue hit its cap and shed its oldest bytes.

        Once per overflow episode (see ``ScreenFeeder.submit``). The
        transcript on disk is complete; only the live grid skipped ahead.
        Logged at WARNING so the outage trail names the session, and shown
        to whoever is watching it — the person at the terminal is the one
        who would otherwise wonder why the screen jumped.
        """
        log.warning(
            "session %r writes faster than it renders: dropped %d unrendered "
            "bytes (queue cap %d); the transcript log is unaffected",
            self.sdef.name, dropped, self._feeder._max_pending,
        )
        try:
            self.notify(
                f"output arrives faster than it can be rendered; {dropped} "
                "bytes skipped on screen (the log has them)",
                ttl=30, level="warn",
            )
        except Exception:  # a notice must never break the output path
            log.debug("overflow notice for %r failed", self.sdef.name, exc_info=True)

    def _on_output(self, chunk: bytes) -> None:
        with self._inflight_lock:
            self._inflight = max(0, self._inflight - len(chunk))
            # Half-way back is the hysteresis: one more post is not a new
            # episode, but the reader must not flap on every byte either.
            if self._inbound_overflowing and self._inflight <= INBOUND_MAX // 2:
                self._inbound_overflowing = False
        if self.exited:
            return
        self._saw_output = True
        self.last_output_at = _utcnow()
        self._last_output_mono = time.monotonic()
        # Queued, not rendered here: pyte is CPU-bound and this runs on the
        # event loop, where a flooding session used to stall accept() (see
        # ScreenFeeder). Logging and the viewer broadcast stay inline — both
        # are cheap, and attached terminals must not lag behind the PTY.
        prev_alt = self.screen.alt_screen
        prev_mouse = self.screen.mouse_tracking
        self._feeder.submit(chunk)
        # ScreenFeeder.submit runs the mode tracker synchronously — only the
        # render is deferred — so alt_screen is already current right here.
        new_alt = self.screen.alt_screen
        new_mouse = self.screen.mouse_tracking
        # The transcript was already written by the reader thread
        # (``_read_pump``); nothing touches ``self._log`` on the loop.
        # The compaction notice rides the same stream the log does; scanning
        # here (and nowhere shown to the user) is what lets the dashboard
        # label a compacting session without asking the screen to.
        self._compacting.feed(chunk)
        self._activity.feed(chunk)
        self._broadcast(("data", chunk))
        if new_alt != prev_alt:
            # The program entered or left the alternate screen. Queued after
            # the data frame that carried the mode change: live viewers learn
            # the new buffer after their xterm has consumed the escape, and a
            # viewer scrolled back into history is unfrozen against this.
            self._broadcast(("buffer", new_alt))
        if new_mouse != prev_mouse:
            # The program took the mouse, or gave it back. Queued after the
            # data frame for the same reason: the viewer's own terminal learns
            # the mode from the bytes, and this frame only tells the *page*
            # whose wheel it is now — which affordance to show, and whether
            # the daemon still owes this socket a scrollback.
            self._broadcast(("mouse", new_mouse))

    def seed_screen_from_log(self) -> None:
        """Give a restored session back the scrollback its predecessor had.

        The daemon's pyte history is the only scrollback the web terminal has
        — the browser's xterm is built with ``scrollback: 0`` because a viewer
        that just attached has nothing in it, so the wheel is served entirely
        by :meth:`ScreenState.repaint_sequence` windowing over this history.
        A restart used to start that history at nothing: the relaunched
        session got a brand-new :class:`ScreenState` and nobody replayed the
        log into it, so the wheel had nothing to scroll while megabytes of it
        sat on disk. (``DeadSession`` has replayed its log all along, which is
        why an *exited* record could be scrolled and a restored one could not.)

        The tail is replayed, the grid it ends on is rolled up into history
        (the program that drew it is gone), and the modes it asserted are
        dropped — see :meth:`ScreenState.forget_modes`. Called before the
        harness is started, so the first bytes of the new program land on a
        blank grid with the old lines behind it.
        """
        path = paths.session_log(self.sdef.name)
        try:
            size = path.stat().st_size
            with open(path, "rb") as fh:
                if size > REPLAY_TAIL_BYTES:
                    fh.seek(size - REPLAY_TAIL_BYTES)
                data = fh.read()
        except OSError:
            return  # no log (or unreadable): an empty screen is honest enough
        if not data:
            return
        self.screen.feed(data)
        self.screen.scroll_grid_into_history()
        self.screen.forget_modes()

    async def screen_synced(self) -> None:
        """Wait for the grid to catch up with the bytes received so far.

        For the readers that must be exact rather than merely current — a
        capture, the repaint an attaching viewer gets.
        """
        self._render_parked_tail()
        await self._feeder.drained()

    def _render_parked_tail(self) -> None:
        """An attached-only harness (``background_render: false``) parks its
        output while nobody is looking; a capture is somebody looking. The
        tail is bounded (BACKGROUND_PENDING_MAX), so this is one short
        synchronous render, not the per-byte cost the flag switches off."""
        if not self.background_render and not self.is_focused():
            self._feeder.drain_now()

    def _append_log(self, chunk: bytes) -> None:
        try:
            if self._log.tell() > LOG_MAX_BYTES:
                self._log.close()
                backup = self._log_path.with_suffix(".log.1")
                if backup.exists():
                    backup.unlink()
                self._log_path.rename(backup)
                self._log = open(self._log_path, "ab")
            self._log.write(chunk)
            self._log.flush()
        except OSError:
            pass

    def append_wal(self, text: str) -> bool:
        """Durably append one block to this session's own transcript.

        The record-before-end act for a session the daemon is about to end:
        the ending must survive the termination, so the block is written
        through an append handle of its own and flushed, and a write that
        could not be made durable is reported as failure — the caller is
        forbidden to end the session behind a record that did not land.

        Opened fresh rather than through the pump's ``self._log`` because the
        callers (the run-event clock's scan) run on a different thread than
        the session's reader task; two threads sharing one buffered file
        object is the interleaving this avoids. ``self._log_path`` is the
        live transcript either way — rotation re-targets it, never renames
        the archive over it.
        """
        if self.exited:
            return False
        try:
            with self._log_path.open("ab") as log:
                log.write(text.encode("utf-8"))
                log.flush()
            return True
        except OSError:
            return False

    def _on_eof(self) -> None:
        if self.exited:
            return
        self._finish()

    def _finish(self) -> None:
        self.exited = True
        for task in self._deferred_deliveries:
            task.cancel()
        self.exit_code = self.pty.exit_code()
        self.exited_at = _utcnow()
        self._status = STATUS_EXITED
        # The last words of a session are the ones somebody reads afterwards,
        # so the queue is rendered out (synchronously — the loop has nothing
        # left to starve for this session) rather than dropped with the pump.
        self._feeder.drain_now()
        self._feeder.close()
        if self._sampler:
            self._sampler.cancel()
        try:
            self._log.close()
        except OSError:
            pass
        self._write_meta()
        self._broadcast(("exit", self.exit_code))
        self.pty.close()
        if self.on_exit is not None:
            try:
                self.on_exit(self)
            except Exception:  # a hook must never break the exit itself
                log.exception("on_exit hook for %r failed", self.sdef.name)

    def _write_meta(self) -> None:
        meta = {
            "name": self.sdef.name,
            "argv": self.argv,
            "created_at": self.created_at,
            "exited_at": self.exited_at,
            "exit_code": self.exit_code,
        }
        try:
            path = paths.session_dir(self.sdef.name).joinpath("meta.json")
            with atomic.scratch(path) as tmp:
                tmp.write_text(json.dumps(meta, indent=2), encoding="utf-8")
                atomic.replace(tmp, path)
        except OSError:
            pass

    # ------------------------------------------------------------------ #
    # idle sampling
    # ------------------------------------------------------------------ #
    async def _sample_loop(self) -> None:
        try:
            while not self.exited:
                await asyncio.sleep(SAMPLE_INTERVAL)
                if self.exited:
                    break
                self.tracker.sample(self.screen.line_hashes(), time.monotonic())
                new = self._compute_status(self.idle_threshold)
                if new != self._status:
                    self._status = new
                    self._broadcast(("state", new))
                # ConPTY can delay reader EOF past child exit; poll liveness
                # as a safety net.
                if not self.pty.isalive():
                    self._finish()
                    break
        except asyncio.CancelledError:
            pass

    def _heuristic_status(self, threshold: float) -> str:
        """Quiescence-based status from the idle tracker alone.

        STARTING before any output, IDLE once non-animated content has been
        still past ``threshold``, BUSY otherwise. This is the fallback when no
        positive turn-marker is on screen, and the readiness check for
        delivery (:meth:`_await_readable`): it answers "is the TUI settled",
        not "is a turn in flight" — a freshly started session that has not
        begun a turn is settled even if claude has not painted its footer yet,
        so delivery-to-a-starting-session must not depend on the marker.
        """
        if self.exited:
            return STATUS_EXITED
        if not self._saw_output:
            return STATUS_STARTING
        if not self.background_render and not self.is_focused():
            # The grid is parked (attached-only harness, nobody attached), so
            # its line hashes say nothing. Output timing is the signal: a
            # program that has written nothing for ``threshold`` is settled.
            # Animations while idle would read as busy here; that is the
            # trade the harness declaration makes.
            since = time.monotonic() - self._last_output_mono
            return STATUS_IDLE if since >= threshold else STATUS_BUSY
        idle_for = self.tracker.idle_for(time.monotonic())
        if idle_for is not None and idle_for >= threshold:
            return STATUS_IDLE
        return STATUS_BUSY

    def _claude_turn_in_flight(self) -> bool:
        """True iff the claude *footer* carries a *live* in-turn marker.

        The marker is an ordinary English phrase, so it legitimately appears
        in transcript content — a commit subject, a quoted docstring, this
        very marker's documentation. Only the footer row counts, and the
        footer is the bottom row of claude's TUI (verified on live panes);
        a phrase anywhere above it is content, not a turn, and must never pin
        a genuinely idle session "busy" forever. :meth:`ScreenState.bottom_line`
        reads exactly that row without materializing the whole grid.

        Presence is not enough: a crashed or dead claude leaves that same
        phrase frozen on its last frame. So a footer hit is trusted only
        while the turn's own animation is still moving — see
        :meth:`_turn_marker_fresh`. A stale marker degrades to the heuristic
        like a missing one: never to a false "busy".

        Only meaningful for the claude harness, and only consulted when the
        quiescence heuristic already reads idle — the one case where the
        marker changes the answer. Never let a missing marker alone mean
        busy, and never apply this to a non-claude harness: both fall back to
        :meth:`_heuristic_status`. See :data:`_CLAUDE_TURN_MARKER` for the
        version-fragility caveat (a claude version that moves the footer off
        the bottom row silently degrades to the heuristic — never to a false
        "busy").
        """
        if self.sdef.harness != CLAUDE_HARNESS:
            return False
        if _CLAUDE_TURN_MARKER not in self.screen.bottom_line():
            return False
        return self._turn_marker_fresh()

    def _turn_marker_fresh(self) -> bool:
        """Whether the marker on screen is from a *live* turn, not a fossil.

        A genuine turn animates one row above the footer region: the spinner
        and elapsed-time/token counter spin there. A TUI that died or wedged
        mid-turn leaves the same ``esc to interrupt`` text frozen on its last
        frame — and that frame's marker must NOT keep a dead session reading
        busy forever (see the exited panes that still carry the phrase).

        We must NOT ask "did the footer row change": the footer is static for
        long stretches in a *genuinely idle* session too (a held cflow prompt,
        a context-low notice), so a frozen footer would misread a live,
        awaiting-input session. The discriminating signal is narrower — the
        spinner row at ``rows - 2``, which moves only while a turn is in
        flight. Ask the idle tracker (which timestamps every row's last
        change) whether that row moved within ``TURN_MARKER_FRESH_FOR``; if
        not, the marker is a fossil and the session falls back to the
        heuristic.
        """
        now = time.monotonic()
        spinner_row = self.screen.rows - 2
        if spinner_row < 0:
            return False
        last = self.tracker.last_change_at(spinner_row)
        return last is not None and (now - last) <= TURN_MARKER_FRESH_FOR

    def _compute_status(self, threshold: float) -> str:
        heur = self._heuristic_status(threshold)
        if heur == STATUS_IDLE and self._turn_in_flight():
            return STATUS_BUSY
        return heur

    def _turn_in_flight(self) -> bool:
        return self._activity.busy(TURN_MARKER_FRESH_FOR) or self._claude_turn_in_flight()

    # ------------------------------------------------------------------ #
    # commands
    # ------------------------------------------------------------------ #
    def status(self, threshold: Optional[float] = None) -> str:
        return self._compute_status(self.idle_threshold if threshold is None else threshold)

    def idle_since(self) -> Optional[float]:
        """Seconds the session has been idle (None when not idle).

        Agrees with :meth:`status`: fresh harness activity or a live footer
        marker prevents an idle duration even when screen content is quiet.
        A parked screen uses output timing, just like the status heuristic.
        """
        if self.status() != STATUS_IDLE:
            return None
        if not self.background_render and not self.is_focused():
            return max(0.0, time.monotonic() - self._last_output_mono)
        idle_for = self.tracker.idle_for(time.monotonic())
        if idle_for is None or idle_for < self.idle_threshold:
            return None
        return idle_for

    async def _deliver(
        self, text: Union[str, Callable[[], str]], *, force: bool = False, wait_for_draft: bool = False,
    ) -> Optional[bool]:
        """Put ``text`` in front of the agent running here, as a user message.

        **The** way anything automated hands an agent something to act on —
        cflow nudges, mesh deliveries and policy nudges, a spawned child's
        opening task. Call this rather than assembling the write yourself:
        submitting a message correctly means a paste plus a *separately
        written* Enter (see :meth:`paste`), and every hand-rolled variant of
        that has eventually gotten the chunking wrong and left the message
        typed into the composer but never sent.

        Best-effort by design — every caller is a background sender with
        nothing to tell a user. Returns whether it landed, so a caller that
        must not lose the message (mesh delivery advancing its cursor) can
        hold its position and retry on the next tick. Nothing is stored here.
        A draft refusal writes nothing; a failure during PTY I/O can have
        written part of the message, so callers cannot assume every False
        permits safe replay.

        With ``wait_for_draft``, a pre-write draft refusal returns ``None``
        internally so :meth:`deliver` can retry after releasing its lock.
        An I/O failure returns ``False`` and is never retried: a write may
        have partially succeeded. Public :meth:`deliver` always returns bool.

        A human writing a prompt in this terminal is one of the reasons for
        that ``False`` (see :meth:`await_keyboard_quiet`). The message waits
        for their Enter — seconds away, since they are typing — and the next
        attempt goes in behind it.

        ``force`` is a person at a dashboard pressing "deliver now", and it
        is the one caller whose message must not come back undelivered: the
        hold it overrules is a hold that person can see, and answering them
        with "still waiting" is answering with nothing. It changes two things
        and no others.

        * The keyboard wait shortens to :data:`FORCE_TYPING_GRACE`. The full
          :data:`TYPING_HOLD_TIMEOUT` is right for a background sender with
          nowhere to be and wrong for an HTTP request somebody is watching —
          half a minute of parking is the same non-answer as a refusal.
        * An open composer no longer refuses the delivery. It is still not
          spliced into: the unsent line is **submitted first**, as its own
          message, and the delivery is pasted behind it. Both texts survive,
          in the order they were written, each one whole — which is the part
          of the refusal worth keeping once somebody has said "now".

        What ``force`` does not touch is :meth:`_await_readable`: a TUI that
        cannot yet take input is not a person holding the message back, and
        typing into one delivers nothing at all rather than delivering sooner.

        Every message is stamped with the wall-clock time it actually lands
        (after the readiness/keyboard holds, in the machine's local zone), so
        the receiving agent — and anyone reading its transcript — can tell
        *when* an automated delivery arrived, not just that it did.
        """
        try:
            await self._await_readable()
            quiet = await self.await_keyboard_quiet(
                timeout=FORCE_TYPING_GRACE if force else None
            )
            if self.exited:
                return False
            if not quiet and self.draft_open():
                if not force:
                    # Not a failure to report to anyone: somebody is
                    # mid-sentence at this keyboard. Said out loud all the
                    # same, because from a sender's side "held behind a
                    # human's prompt" and "the TUI never came up" look
                    # identical — both are just an undelivered message — and
                    # only one of them resolves on its own.
                    log.info(
                        "deliver to %r held: an unsent line is in that "
                        "terminal's composer; nothing was typed, and the "
                        "message stays with its sender until the human sends "
                        "or clears it",
                        self.sdef.name,
                    )
                    # Only this pre-write refusal is safe to retry. False
                    # also covers I/O failures that may have written bytes.
                    return None if wait_for_draft else False
                # Forced past it. The line is submitted rather than typed
                # over: a bare CR is the keypress the composer was waiting
                # for, so the human's text reaches the agent as they wrote it
                # and the paste below starts on an empty composer. Clearing
                # the line instead would throw somebody's writing away to
                # make room for a message that can simply follow it.
                log.info(
                    "deliver to %r forced past an unsent line: submitting "
                    "that line first, so the delivery follows it instead of "
                    "landing inside it",
                    self.sdef.name,
                )
                await self.write_bytes(b"\r")
                await asyncio.sleep(FORCE_DRAFT_SETTLE)
            # State-dependent reminders resolve after readiness and draft
            # waits, so a newer user rating can replace or cancel them.
            if callable(text):
                text = text()
                if not text:
                    return False
            await self.paste(f"{delivery_stamp()}\n{text}", enter=True)
        except Exception as exc:  # noqa: BLE001 — SessionGone, PTY write, ...
            log.debug("deliver to %r failed: %s", self.sdef.name, exc)
            return False
        return True

    async def deliver(
        self, text: Union[str, Callable[[], str]], *, force: bool = False, wait_for_draft: bool = False
    ) -> bool:
        """Deliver without interleaving input. A text factory can refresh state
        after the keyboard wait; returning an empty string cancels the send.
        """
        while True:
            async with self._delivery_lock:
                result = await self._deliver(
                    text, force=force, wait_for_draft=wait_for_draft
                )
            if result is not None:
                return result
            # Release the lock between safe retries, so an explicit forced
            # delivery can still submit the draft and release this wait.
            await asyncio.sleep(0.2)

    def queue_delivery(self, text: str) -> bool:
        """Accept a notification for delivery after an input draft is released.

        Acceptance is not a delivery receipt. Tasks belong to this live
        session, preserve delivery ordering, and are cancelled on exit; they
        are not a durable mailbox across daemon restarts. Waiting in a task
        keeps HTTP handlers and other sessions' clock ticks responsive.
        """
        if self.exited:
            return False

        # Lazily, and inline rather than through the helper, because this
        # method is also borrowed by doubles that are not Sessions at all:
        # a queue that needs __init__ (or a second method) to have run would
        # make those raise where the real session simply records.
        queue = getattr(self, "_pending_deliveries", None)
        if queue is None:
            queue = []
            self._pending_deliveries = queue
        self._delivery_seq = getattr(self, "_delivery_seq", 0) + 1
        record = {
            "id": self._delivery_seq,
            "at": _utcnow(),
            "chars": len(text),
            "preview": _delivery_preview(text),
        }
        queue.append(record)

        async def send() -> None:
            try:
                async with self._deferred_delivery_lock:
                    delivered = await self.deliver(text, wait_for_draft=True)
                log.info(
                    "queued notification to %r %s",
                    self.sdef.name, "delivered" if delivered else "failed",
                )
            finally:
                # Cancelled, failed or delivered, it is no longer waiting --
                # a queue that only ever grows would be worse than none.
                try:
                    queue.remove(record)
                except ValueError:
                    pass

        task = asyncio.create_task(send())
        self._deferred_deliveries.add(task)
        task.add_done_callback(self._deferred_deliveries.discard)
        return True

    async def await_input_ready(self) -> None:
        """Public face of :meth:`_await_readable`, for the operator lines a
        session queued while it was exited (``session_input.flush``)."""
        await self._await_readable()

    async def prepare_operator_input(self) -> None:
        """Clear the way for one operator line the way a forced web send
        does: wait out the short typing grace and, if a human's line is still
        in the composer, submit it first rather than typing into it."""
        quiet = await self.await_keyboard_quiet(
            terminal_only=True, timeout=FORCE_TYPING_GRACE
        )
        if not quiet and self.draft_open():
            await self.submit_open_draft()

    async def _await_readable(self) -> None:
        """Block until a starting TUI can actually take a message.

        Quiet is not ready. A just-started Claude Code prints its banner and
        pauses long enough to read as idle, mounts its input a few seconds
        after that, and only finishes loading a few seconds after *that*.
        Written into any of those gaps, a paste and its separately-written
        Enter come back out of one read with the CR folded into the text: the
        message is typed and never sent — the exact failure :meth:`paste`
        splits the writes to avoid, reintroduced by the reader rather than the
        writer. No delay between the two writes can fix it.

        Ready is two things together, and neither alone is enough (both were
        measured against Claude Code):

        * **DECSET 2004 is on.** A program enables bracketed paste when it
          takes over the keyboard, so this is the input existing at all.
        * **...and it has been quiet since.** The mode goes on partway through
          startup, several seconds before the first submit is accepted; the
          burst of work that follows it is what has to finish.

        Latched: once a session has been seen ready it never waits again, so
        this is paid at most once. Only a harness known to set the mode is
        waited on — a shell never does and would stall here forever — and the
        whole wait is bounded from the moment the session was spawned, so a
        harness that never settles delays a message rather than losing it.
        """
        if self._input_ready or self.exited:
            return
        entry = harness_registry.get(self.sdef.harness)
        readiness = entry.input_readiness if entry is not None else "immediate"
        if readiness != "bracketed-paste":
            self._input_ready = True  # not a TUI we know; nothing to wait for
            return
        deadline = self._started_mono + INPUT_READY_TIMEOUT
        quiet_since = None
        while time.monotonic() < deadline and not self.exited:
            # Readiness is quiescence, not "no turn in flight": a starting
            # session that has not begun a turn is settled even before claude
            # paints its footer, so gate on the heuristic, not the marker-augmented
            # status() (which would read busy mid-turn and stall the first
            # delivery until the deadline — see :meth:`_heuristic_status`).
            if self.screen.bracketed_paste and self._heuristic_status(
                self.idle_threshold
            ) == STATUS_IDLE:
                if quiet_since is None:
                    quiet_since = time.monotonic()
                elif time.monotonic() - quiet_since >= INPUT_SETTLE:
                    break
            else:
                quiet_since = None  # still starting; begin the count again
            await asyncio.sleep(0.2)
        self._input_ready = True

    def note_human_input(
        self,
        *,
        at_terminal: bool = False,
        data: Optional[bytes] = None,
        composing: bool = False,
    ) -> None:
        """Record a human keystroke aimed at this terminal.

        Called by the raw keyboard passthroughs — the WebSocket bridge behind
        the web terminal and ``claunch attach`` (for every keystroke frame
        *and* for the web terminal's ``typing`` marks, which cover the keys
        that produce no bytes yet: an IME composing Hangul, a virtual
        keyboard mid-word), and :meth:`send_keys` — and never by
        :meth:`deliver`: telling the two apart is the whole point.

        ``at_terminal`` says the keystroke came from a terminal a person is
        sitting at (the WebSocket bridge) rather than from ``send-keys``.
        Both count as "a human typed here" for a delivery; only the former
        counts for another ``send-keys``, so a script driving a session line
        by line does not wait TYPING_GUARD behind its own previous line.

        ``data`` is the keystroke itself when the caller has it, and it is
        what moves the draft state: the timing above says *when* somebody
        last touched the keyboard, ``data`` says whether what they touched it
        for is still sitting unsent in the composer (see
        :func:`draft_state_from_bytes`). ``composing`` is the web terminal's
        equivalent claim for keys that produced no bytes yet — an IME
        mid-syllable is a draft being written even though the wire is quiet.
        A draft it opens is marked *uncommitted*, though: nothing is in the
        composer until the composition's bytes arrive, so a mark that is
        never followed by bytes (a cancelled syllable) lapses on
        :data:`COMPOSING_GUARD` rather than DRAFT_GUARD.
        Both are terminal-only: ``send-keys`` types a line and its Enter
        together, and holding *itself* behind its own text would deadlock.
        """
        on_command = getattr(self, "on_command_submitted", None)
        if data and on_command is not None:
            for line in self._submitted_lines.feed(data):
                on_command(self, line)
        now = time.monotonic()
        self._last_human_input = now
        if not at_terminal:
            return
        self._last_terminal_input = now
        # ...and the same fact on a clock a person can read. Only here, in
        # the terminal branch: `send-keys` types into this session too, but
        # it is another agent doing it, and a card that answered "you last
        # typed here 20 seconds ago" because a script did would be worse
        # than saying nothing.
        self.last_input_at = _utcnow()
        if composing:
            # A mark only *opens* a draft as uncommitted: one arriving after
            # the commit's own bytes must not downgrade text that is
            # already sitting in the composer.
            if not self._draft_open:
                self._draft_uncommitted = True
            self._draft_open = True
        if data:
            state = draft_state_from_bytes(data)
            if state is not None:
                self._draft_open = state
                # Bytes settle the question either way: a committed
                # character is a real draft now, a submit/clear closed it.
                self._draft_uncommitted = False

    def draft_open(self) -> bool:
        """Whether a human has an unsent line in this terminal's composer.

        Capped by DRAFT_GUARD since the last keystroke — not because the
        draft stops existing, but because the *person* may have stopped: one
        character typed before walking away would otherwise hold this
        session's mail forever, and nobody is around for the interleaving to
        harm. Everything short of that is released by the keyboard instead —
        Enter, ``C-c`` — so an actual composer is protected for exactly as
        long as it is being written.

        A draft opened by a composing mark alone gets the shorter
        :data:`COMPOSING_GUARD`: its bytes have not reached the composer, so
        there is nothing to splice into, and the marks stop entirely the
        moment the composition is abandoned rather than submitted.
        """
        if not self._draft_open:
            return False
        guard = (
            COMPOSING_GUARD
            if getattr(self, "_draft_uncommitted", False)
            else DRAFT_GUARD
        )
        if time.monotonic() - self._last_terminal_input >= guard:
            return False
        return True

    def delivery_held(self) -> bool:
        """Whether a person has pinned this session shut.

        Distinct in kind from every other hold in this class, not in degree:
        those are inferred from timing and release themselves, this one was
        chosen and releases when it is un-chosen. Which is also why it is the
        only one worth putting a button on — a guess does not need an
        override, a decision needs a way back.
        """
        return self._delivery_hold

    def set_delivery_hold(self, held: bool) -> bool:
        """Pin this session shut, or let it go again; returns the new state.

        Nothing is dropped either way. Held messages stay in their mesh log
        with the recipient's cursor where it was — exactly as they do while
        the daemon waits out a busy turn — so resuming types in the backlog
        that built up rather than resuming from the next arrival.
        """
        self._delivery_hold = bool(held)
        return self._delivery_hold

    def reminders_paused(self) -> bool:
        """Whether repeating Session reminders are paused for this session."""
        return bool(self.sdef.reminder_paused)

    def set_reminder_pause(self, paused: bool) -> bool:
        """Pause or resume repeating Role and Cflow reminders.

        The service owns source clocks and re-arms them when this changes.
        The session owns the durable choice because it must survive process
        replacement and daemon restart with the rest of the definition.
        """
        self.sdef = dataclasses.replace(self.sdef, reminder_paused=bool(paused))
        return self.sdef.reminder_paused

    def keyboard_busy(
        self, guard: Optional[float] = None, *, terminal_only: bool = False
    ) -> bool:
        """Whether a human has typed here within the last ``guard`` seconds.

        A composer mid-edit is invisible to the screen sampler — a thinking
        pause reads exactly like idle — so anything about to type into this
        terminal asks about the keyboard directly. ``terminal_only`` asks
        about keystrokes from an attached terminal alone, leaving out
        ``send-keys`` (see :meth:`note_human_input`).

        Timing alone is not enough, and this is where several rounds of "the
        delivery still spliced into my prompt" ended: ``guard`` is a few
        seconds, and a person writing a paragraph pauses longer than that to
        think, to re-read, to look something up. So an unsent draft counts as
        a busy keyboard too (:meth:`draft_open`) — during those pauses the
        line is still half-written, and that, not the recency of a keypress,
        is what makes a paste destructive.
        """
        if guard is None:
            guard = TYPING_GUARD
        if self.draft_open():
            return True
        last = self._last_terminal_input if terminal_only else self._last_human_input
        if last <= 0:
            return False
        return time.monotonic() - last < guard

    async def await_keyboard_quiet(
        self, *, terminal_only: bool = False, timeout: Optional[float] = None
    ) -> bool:
        """Hold a write while a human is typing into this terminal, and say
        whether the keyboard ever went quiet.

        The idle-gates upstream cannot catch this by watching the screen:
        typing keeps it changing, but the pauses inside composing a message
        outlast the idle threshold, and a paste-plus-Enter injected into one
        submits the human's half-typed line with the delivery folded into it.
        So the last thing before the paste is the question the screen cannot
        answer — has the keyboard itself been quiet for a moment.

        Used by :meth:`deliver` and by the text-carrying forms of the raw
        passthrough (:meth:`send_keys` with text, ``send-keys --paste``):
        those are how one agent hands another a line, and a line typed over
        a human's half-written one is the same corruption whichever door it
        came through.

        The wait is bounded — a caller must not be parked forever — and the
        return says which way it ended: ``True`` the keyboard went quiet,
        ``False`` it never did. That answer is not the decision, though. What
        callers do with a ``False`` depends on :meth:`draft_open`:

        * **a draft is open** — refuse. A message typed into a half-written
          line is not a late delivery, it is a broken one, and it breaks the
          human's prompt along with itself. The sender keeps the message
          (mesh delivery leaves its cursor where it is, and the queued banner
          shows the hold) and it goes in behind their Enter.
        * **no draft** — write anyway, as this has always done. Thirty
          seconds of keys that left nothing in the composer is somebody
          holding a modifier or leaning on an arrow key, and there is no
          half-written line for the paste to land in.

        ``timeout`` shortens the bound for a caller who cannot afford the
        default one (a forced delivery: see :data:`FORCE_TYPING_GRACE`). It
        changes how long the wait is, never what the answer means — a
        ``False`` from a short wait is the same "still typing" as a ``False``
        from a long one, and the decision above stays with the caller.
        """
        deadline = time.monotonic() + (
            TYPING_HOLD_TIMEOUT if timeout is None else timeout
        )
        while not self.exited and time.monotonic() < deadline:
            if not self.keyboard_busy(terminal_only=terminal_only):
                return True
            await asyncio.sleep(0.2)
        return not self.keyboard_busy(terminal_only=terminal_only)

    async def send_keys(
        self,
        args: List[str],
        *,
        literal: bool = False,
        force: bool = False,
    ) -> bytes:
        """Raw keystrokes — the passthrough for a human at a keyboard (the
        web terminal, ``claunch send-keys``). To hand an agent a *message*,
        use :meth:`deliver` instead.

        Text (anything that is not a named key) is held while someone is
        typing at an attached terminal — ``claunch send-keys s "do X" Enter``
        from a script or another agent landing mid-composition splices its
        line into the human's — so it queues behind their keystrokes the way
        a delivery does. Only *terminal* keystrokes hold it (attach, web):
        an earlier ``send-keys`` does not, so a script driving a session line
        by line is not paced to TYPING_GUARD. Bare keys (``Enter``, ``C-c``,
        ``Escape``, arrows) are never held: an interrupt or a submit is
        wanted the instant it was sent, and holding an Enter would separate
        it from the text it was sent for.

        ``force`` is reserved for an operator explicitly pressing the web
        session input. It uses the short forced-delivery grace period and
        submits an open draft before writing the requested line, preserving
        both messages while avoiding the normal 30-second wait.

        Raises :class:`KeyboardHeld` when the hold runs out with an unsent
        line still in the composer. Refusing is the point: this path has no
        queue to fall back on, so the two available answers are "tell the
        sender it did not go" and "type it into somebody's half-written
        sentence", and only the first leaves both lines intact. The sender
        gets an error it can retry; the person at the keyboard sees nothing,
        which is right.
        """
        if self.exited:
            raise SessionGone(f"session {self.sdef.name!r} has exited")
        has_text = keys_mod.has_text(args, literal=literal)
        if has_text:
            # A text-bearing send-keys call is a message even though it uses
            # the raw keyboard path. During a restore, Codex can paint a quiet
            # frame before its composer is mounted; encoding and writing in
            # that interval observes bracketed-paste as disabled and sends
            # the text plus Enter as one premature write. Use the same
            # harness-declared readiness gate as deliver(), then encode with
            # the terminal modes that are current after startup completes.
            await self._await_readable()
            if self.exited:
                raise SessionGone(f"session {self.sdef.name!r} has exited")
        data = keys_mod.encode_keys(
            args, literal=literal, app_cursor=self.screen.app_cursor_keys
        )
        if has_text:
            quiet = await self.await_keyboard_quiet(
                terminal_only=True,
                timeout=FORCE_TYPING_GRACE if force else None,
            )
            if not quiet and self.draft_open():
                if force:
                    await self.submit_open_draft()
                else:
                    raise KeyboardHeld(
                        f"session {self.sdef.name!r}: someone is typing there "
                        f"right now — nothing was sent. Retry in a moment, or "
                        f"use 'claunch mesh send' / the deliver API, which keeps "
                        f"the message and types it in when the line is free."
                    )
            if self.exited:
                raise SessionGone(f"session {self.sdef.name!r} has exited")
        self.note_human_input(data=data)
        head, submit = keys_mod.split_submit(data)
        if submit and self.screen.bracketed_paste:
            # Text and its submitting CR in one write is the same trap
            # :meth:`paste` documents: a bracketed-paste TUI reads the chunk
            # as one paste and folds the CR into the text, so the line is
            # typed but never sent. Split it here too — this path is reached
            # by hand ('claunch send-keys ... Enter') where the caller has no
            # way to know the difference.
            await self.write_bytes(head)
            entry = harness_registry.get(self.sdef.harness)
            delay = (
                entry.paste_enter_delay
                if entry is not None and entry.paste_enter_delay is not None
                else PASTE_ENTER_DELAY
            )
            await asyncio.sleep(delay)
            await self.write_bytes(submit)
            if entry is not None and entry.submit_strategy == "screen":
                # Keep the web "type for this session" path and CLI
                # send-keys aligned with automated delivery: Codex may make
                # the first Enter a newline after text, so give it a second
                # paced Enter to submit the resulting draft.
                await asyncio.sleep(delay)
                await self.write_bytes(submit)
            return data
        await self.write_bytes(data)
        return data

    async def submit_open_draft(self) -> None:
        """Submit the line sitting unsent in the composer, for an operator
        forcing a send past it.

        The line is submitted rather than typed over: a bare CR is the
        keypress the composer was waiting for, so the human's text reaches
        the agent as they wrote it and what follows starts on an empty
        composer. The CR is generated here, so there is no terminal
        websocket frame to close the tracked draft state for us.
        """
        await self.write_bytes(b"\r")
        self._draft_open = False
        self._draft_uncommitted = False
        await asyncio.sleep(FORCE_DRAFT_SETTLE)

    async def paste(self, text: str, *, enter: bool = False) -> bytes:
        """Inject multiline text as one paste (bracketed when the program
        opted in via DECSET 2004), so newlines don't submit once per line.

        The submitting Enter is a *separate*, delayed write. A bracketed-paste
        TUI (Claude Code and Ink-based prompts generally) treats one read as
        one paste: a CR sitting in the same chunk right after the ``ESC[201~``
        end marker is folded into the pasted text, so the block lands in the
        composer and is never submitted. Landing the CR in its own read is
        what makes it a keypress again.
        """
        if self.exited:
            raise SessionGone(f"session {self.sdef.name!r} has exited")
        data = keys_mod.encode_paste(text, bracketed=self.screen.bracketed_paste)
        entry = harness_registry.get(self.sdef.harness)
        strategy = entry.submit_strategy if entry is not None else "fixed"
        delay = (
            entry.paste_enter_delay
            if entry is not None and entry.paste_enter_delay is not None
            else PASTE_ENTER_DELAY
        )
        before = (
            self.tracker.last_meaningful_change()
            if strategy == "screen" and self.screen.bracketed_paste
            else None
        )
        await self.write_bytes(data)
        if not enter:
            return data
        if strategy == "screen" and self.screen.bracketed_paste:
            # Codex acknowledges a large paste by rendering its compact
            # placeholder. Waiting for that repaint keeps Enter out of the
            # consumer's own paste-suppression window; a producer-side sleep
            # alone cannot, because ConPTY may batch two separate writes into
            # one read. Bounded fallback keeps an unobservable repaint from
            # losing the message.
            deadline = time.monotonic() + PASTE_RENDER_TIMEOUT
            while time.monotonic() < deadline and not self.exited:
                changed = self.tracker.last_meaningful_change()
                if changed is not None and changed != before:
                    break
                await asyncio.sleep(0.05)
        await asyncio.sleep(delay)
        await self.write_bytes(b"\r")
        if strategy == "screen" and self.screen.bracketed_paste:
            # Codex can interpret the first Enter after a pasted delivery as
            # a newline in its composer. A second, separately paced Enter
            # submits that draft; on versions that submit on the first one,
            # the second reaches an empty composer.
            await asyncio.sleep(delay)
            await self.write_bytes(b"\r")
            return data + b"\r\r"
        return data + b"\r"

    async def write_bytes(self, data: bytes, writer: object = None) -> None:
        """Write ``data`` into the PTY on behalf of ``writer``.

        ``writer`` says who is typing: a viewer socket passes itself, and the
        paths this class owns (delivery, send-keys, paste) leave it as the
        default. It matters because on Windows the backend decodes as it
        writes and holds a character whose bytes are split between two calls
        until the rest arrives, and that held fragment belongs to the writer
        that sent it. A viewer whose frame ends mid-syllable and a delivery
        that lands before the next frame are two writers, and sharing one
        decoder made the delivery's first bytes the end of the viewer's
        character -- the syllable was lost, and the delivery was prefixed
        with a replacement character. See
        ``claunch-pty-shared-decoder-across-writers-o3cy4``.
        """
        if self.exited or self.pty is None:
            raise SessionGone(f"session {self.sdef.name!r} has exited")
        # One writer at a time. A session has several -- each viewer, the
        # delivery queue, send-keys -- and the executor below runs them on
        # different threads, so without this they reach the PTY interleaved.
        async with self._write_lock:
            # PTY writes can block briefly (ConPTY pipe backpressure); keep
            # the event loop responsive by writing from the thread pool.
            await self._loop.run_in_executor(
                None, functools.partial(self.pty.write, data, writer)
            )

    def forget_writer(self, writer: object) -> None:
        """A writer has gone; drop the partial character it never finished.

        Held indefinitely it would be handed to whoever next writes under the
        same key, and it keeps a socket object alive besides.
        """
        if self.pty is not None:
            self.pty.forget_writer(writer)

    def resize(self, cols: int, rows: int) -> None:
        if self.exited or self.pty is None:
            raise SessionGone(f"session {self.sdef.name!r} has exited")
        if cols == self.sdef.cols and rows == self.sdef.rows:
            # Same size, nothing changed — and a broadcast here has teeth now:
            # a viewer that scrolled back into history treats a resize frame
            # as "the grid shape changed", unfreezes, and loses its place.
            # The web terminal re-asserts the size on focus regain, so this is
            # not a rare corner; keep the no-op a no-op.
            return
        self.pty.resize(cols, rows)
        self.screen.resize(cols, rows)
        self.sdef = dataclasses.replace(self.sdef, cols=cols, rows=rows)
        self._broadcast(("resize", (cols, rows)))

    def capture(self, *, history: bool = False) -> List[str]:
        self._render_parked_tail()
        lines = self.screen.render_history() if history else self.screen.render_screen()
        return lines

    async def wait_for(self, state: str, *, timeout: float, threshold: float) -> str:
        """Poll until the session reaches ``state`` (``idle`` or ``exited``).

        Returns the final status; raises :class:`asyncio.TimeoutError` on
        timeout. Waiting for idle also completes if the session exits — the
        caller inspects the returned status.
        """
        deadline = time.monotonic() + timeout
        while True:
            current = self._compute_status(threshold)
            if state == "exited" and current == STATUS_EXITED:
                return current
            if state == "idle" and current in (STATUS_IDLE, STATUS_EXITED):
                return current
            if time.monotonic() >= deadline:
                raise asyncio.TimeoutError()
            await asyncio.sleep(0.2)

    def kill(self, *, force: bool = False) -> None:
        if self.exited or self.pty is None:
            return
        self.kill_requested = True
        self.pty.terminate(force=force)

    def pause(self, *, force: bool = False) -> None:
        """Kill, and mark the record as paused rather than killed.

        The marker is written *before* the signal: the exit that follows is
        asynchronous (the reader task sees EOF and calls :meth:`_finish`),
        and a marker written after it could race a viewer reading the fresh
        exited record as a kill. A session that ignores the signal keeps
        running with the marker set, which the rail draws as "pausing" — the
        same window a plain kill has, made visible.
        """
        if self.exited or self.pty is None:
            return
        self.paused_at = _utcnow()
        self.kill(force=force)

    async def shutdown(self, grace: float = 5.0) -> None:
        """Terminate the child and wait briefly; force-kill stragglers."""
        pending = tuple(self._deferred_deliveries)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if self.exited or self.pty is None:
            return
        self.pty.terminate(force=False)
        deadline = time.monotonic() + grace
        while not self.exited and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        if not self.exited:
            self.pty.terminate(force=True)
            await asyncio.sleep(0.2)
            if not self.exited:
                self._finish()

    # ------------------------------------------------------------------ #
    # subscribers (WebSocket viewers)
    # ------------------------------------------------------------------ #
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1024)
        self._subscribers.add(q)
        # Non-web clients have no focus control frame. Treat a freshly
        # attached terminal as focused until its client says otherwise.
        self.set_viewer_focused(q, True)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)
        self.set_viewer_focused(q, False)

    def is_focused(self) -> bool:
        """Whether at least one terminal viewer is actively using this session."""
        return bool(self._focused_subscribers)

    def notify(
        self, text: str, *, ttl: Optional[float] = None, level: str = "info"
    ) -> int:
        """Show ``text`` to everyone looking at this session, for ``ttl``
        seconds, without touching the PTY.

        The daemon's door for a message meant for the *person* at a session
        rather than the program in it: it goes out as a ``notice`` control
        frame to every viewer (the web terminal draws it as an element, an
        attach draws it over row 1 -- see ``daemon/notice.py``) and is never
        typed, logged or seen by the child. Returns how many viewers were
        subscribed to receive it -- 0 means nobody was looking, and the
        message is gone; a caller that needs it read later delivers instead.
        """
        from .notice import Notice

        notice = Notice.make(text, ttl=ttl, level=level)
        self._broadcast(("notice", notice))
        return len(self._subscribers)

    def set_viewer_focused(self, viewer: object, focused: bool) -> None:
        """Apply one viewer's focus state to rendering and child scheduling."""
        before = self.is_focused()
        if focused:
            self._focused_subscribers.add(viewer)
        else:
            self._focused_subscribers.discard(viewer)
        if self.is_focused() != before:
            # Full scrollback while someone is looking; the short one when
            # nobody is (see BACKGROUND_HISTORY). Order matters: raise the
            # limit before the pump renders the parked tail into it.
            screen = getattr(self, "screen", None)  # test doubles skip __init__
            if screen is not None:
                screen.set_history_limit(
                    self._scrollback if self.is_focused()
                    else min(self._scrollback, BACKGROUND_HISTORY)
                )
            self._feeder.wake()
            self._apply_cpu_priority()

    def _apply_cpu_priority(self) -> None:
        """Best-effort priority transition for the direct PTY child.

        Windows children inherit their creator's priority class. The daemon
        remains at normal priority and can therefore continue serving every
        session while background harnesses yield CPU time.
        """
        if not self._focused_session_scheduling or self.pid is None:
            return
        background = not self.is_focused()
        if background == self._cpu_background:
            return
        changed = (
            process_priority.set_background(self.pid)
            if background
            else process_priority.set_foreground(self.pid)
        )
        if changed:
            self._cpu_background = background

    def note_visit(self) -> None:
        """Record that a person is (or just was) looking at this session.

        Called by the viewer socket on both edges — when it opens and when it
        closes — because either edge alone tells the wrong story. Stamping
        only the open leaves a tab that has been watched all afternoon
        claiming a visit from this morning; stamping only the close means a
        session being watched right now has never been visited at all. Both,
        plus :meth:`viewers` for the "right now" case, and the reading is
        complete without the daemon having to tick anything.
        """
        self.last_visited_at = _utcnow()

    def viewers(self) -> int:
        """How many viewer sockets are attached to this session right now.

        The subscriber set *is* the viewer set: ``subscribe`` has exactly one
        caller, the terminal WebSocket. So this counts eyes on the terminal —
        a browser tab or a ``claunch attach`` — and nothing else.
        """
        return len(self._subscribers)

    def last_activity_at(self) -> Optional[str]:
        """When the screen last changed in a way that was not an animation.

        Derived here rather than stamped in the sampling loop, and that is the
        whole performance story: :meth:`_sample_loop` already asks the tracker
        this question every SAMPLE_INTERVAL, so keeping a wall-clock copy up
        to date would mean formatting a timestamp several times a second for
        every session on the machine, forever, for a line nobody is reading
        most of the time. Instead the tracker's monotonic answer is converted
        the moment somebody actually asks — once per session per poll —
        against the offset between the two clocks.

        ``last_output_at`` cannot stand in for this. It moves on every byte,
        and claude's TUI animates a spinner and an elapsed-time counter while
        it waits for you, so raw output never goes quiet and that stamp reads
        "just now" on a session that has done nothing for an hour. The
        tracker exists precisely to tell those apart.

        ``None`` before the first sample, and after a daemon restart: this is
        read off *this* incarnation's screen history, which a restarted
        session does not have. Empty is the honest answer there.
        """
        mono = self.tracker.last_meaningful_change()
        if mono is None:
            return None
        ago = max(0.0, time.monotonic() - mono)
        return datetime.fromtimestamp(time.time() - ago, timezone.utc).isoformat(
            timespec="seconds"
        )

    def _broadcast(self, item: Tuple[str, object]) -> None:
        dead = []
        for q in self._subscribers:
            try:
                q.put_nowait(item)
            except asyncio.QueueFull:
                dead.append(q)  # slow consumer: drop it, it can reattach
        for q in dead:
            self._subscribers.discard(q)
            self.set_viewer_focused(q, False)

    # ------------------------------------------------------------------ #
    # views
    # ------------------------------------------------------------------ #
    def _delivery_queue(self) -> list:
        """The waiting-delivery list, created on first use (see queue_delivery)."""
        queue = getattr(self, "_pending_deliveries", None)
        if queue is None:
            queue = []
            self._pending_deliveries = queue
        return queue

    def pending_deliveries(self) -> list:
        """Automated messages accepted for this session and not yet typed.

        Oldest first, which is the order they will be written in. Each is a
        preview, not the message: the rail shows that something is waiting
        and roughly what, and the message itself arrives in the terminal.
        """
        return [dict(record) for record in self._delivery_queue()]

    def info(self) -> dict:
        return {
            **self.sdef.to_dict(),
            "status": self.status(),
            "pid": self.pid,
            "exit_code": self.exit_code,
            "created_at": self.created_at,
            "last_output_at": self.last_output_at,
            # The three "has anyone been here" readings the rail draws: when
            # a person last looked, when a person last typed, and when the
            # session itself last did something visible. Separate facts —
            # a session can be working hard with nobody watching, or watched
            # all day while doing nothing — and the row shows all three
            # rather than collapsing them into one "active" word.
            "last_visited_at": self.last_visited_at,
            "last_input_at": self.last_input_at,
            "last_activity_at": self.last_activity_at(),
            # How much the screen moved in the last minute (rows, see
            # IdleTracker.moved_rows): the rail grades a busy dot by it, so
            # a turn streaming a reply and one parked on a tool call stop
            # reading as the same yellow. Absent on a DeadSession, which has
            # no screen to have moved.
            "moved_rows": self.tracker.moved_rows(time.monotonic()),
            "viewers": self.viewers(),
            "exited_at": self.exited_at,
            "archived_at": self.archived_at,
            "paused_at": self.paused_at,
            # A person's standing "type nothing in here" (:meth:`delivery_held`).
            # On the list poll rather than only on the per-session queued
            # endpoint, because the rail draws one row per session and the
            # question it answers — which of these did I pin shut — is asked
            # of the whole fleet at once.
            "delivery_hold": self.delivery_held(),
            # What has been accepted for this session and is still waiting to
            # be typed into it. On the rail poll for the same reason the hold
            # is: the question is asked of the fleet, not of one session.
            "pending_deliveries": self.pending_deliveries(),
            # Whether the harness is (or just finished) compacting this
            # session's context — see :mod:`compacting`. Live sessions only:
            # a DeadSession has no stream for the notice to ride.
            "compacting": self._compacting.compacting,
        }


class SessionGone(Exception):
    """Raised when acting on a session whose child already exited."""


class KeyboardHeld(Exception):
    """Raised when a write is refused because a human is typing there.

    The raw passthrough's answer to a hold it cannot wait out
    (:meth:`Session.send_keys`, and the ``/keys`` endpoint behind
    ``claunch send-keys``). Distinct from :class:`SessionGone`: the session is
    perfectly alive and the keys are perfectly valid — they were simply aimed
    at a line somebody else is in the middle of writing.
    """


class DeadSession:
    """The record of a session that is no longer running.

    Sessions die with the daemon, but their *definitions* outlive it, and so
    does the right to revive them: on restart everything the previous daemon
    did not relaunch — already exited, created ``--no-restore``, or a relaunch
    that failed — comes back as one of these instead of being forgotten, so
    ``claunch respawn`` (and the web UI's resume) still reach it. Archive keeps
    this record; explicit DELETE and ``clear-sessions`` drop it.

    It answers the read-only surface a viewer needs (list, capture, attach to
    read the final screen — replayed from the raw log) and raises
    :class:`SessionGone` for anything that needs a live child.
    """

    exited = True

    def __init__(
        self,
        sdef: SessionDef,
        *,
        exit_code: Optional[int] = None,
        pid: Optional[int] = None,
        created_at: Optional[str] = None,
        last_output_at: Optional[str] = None,
        last_visited_at: Optional[str] = None,
        last_input_at: Optional[str] = None,
        exited_at: Optional[str] = None,
        archived_at: Optional[str] = None,
        paused_at: Optional[str] = None,
        swept_at: Optional[str] = None,
        scrollback: int = 5000,
        idle_threshold: float = 2.0,
    ) -> None:
        self.sdef = sdef
        self.exit_code = exit_code
        self.pid = pid
        self.created_at = created_at or _utcnow()
        self.last_output_at = last_output_at
        # Carried across the restart with the rest of the record: "when did I
        # last look in on this one" is a question about a session that is
        # mostly worth asking once it has stopped answering for itself.
        self.last_visited_at = last_visited_at
        self.last_input_at = last_input_at
        self.exited_at = exited_at
        self.archived_at = archived_at
        self.paused_at = paused_at
        self.swept_at = swept_at
        #: A retired record was not ended by the OS in this daemon's lifetime,
        #: whatever its exit code says; see Session.ended_by_os.
        self.ended_by_os = False
        self.idle_threshold = idle_threshold
        self._scrollback = scrollback
        self._screen: Optional[ScreenState] = None

    @property
    def screen(self) -> ScreenState:
        """The session's last screen, rebuilt on first use.

        Replaying a log through pyte is not free, and most records are never
        looked at — so this stays unbuilt until someone captures or attaches.
        """
        if self._screen is None:
            screen = ScreenState(
                self.sdef.cols, self.sdef.rows, history=self._scrollback
            )
            self._replay_into(screen)
            self._screen = screen
        return self._screen

    def _replay_into(self, screen: ScreenState) -> None:
        path = paths.session_log(self.sdef.name)
        try:
            size = path.stat().st_size
            with open(path, "rb") as fh:
                if size > REPLAY_TAIL_BYTES:
                    fh.seek(size - REPLAY_TAIL_BYTES)
                screen.feed(fh.read())
        except OSError:
            pass  # no log (or unreadable): an empty screen is honest enough

    # ------------------------------------------------------------------ #
    # the live surface: nothing to drive any more
    # ------------------------------------------------------------------ #
    def _gone(self) -> SessionGone:
        return SessionGone(
            f"session {self.sdef.name!r} has exited "
            f"(respawn it to get a live terminal back)"
        )

    async def send_keys(self, args: List[str], *, literal: bool = False) -> bytes:
        raise self._gone()

    async def paste(self, text: str, *, enter: bool = False) -> bytes:
        raise self._gone()

    async def deliver(
        self, text: str, *, force: bool = False, wait_for_draft: bool = False
    ) -> bool:
        return False  # nothing is running to read it, forced or not

    def queue_delivery(self, text: str) -> bool:
        return False

    async def write_bytes(self, data: bytes, writer: object = None) -> None:
        raise self._gone()

    def forget_writer(self, writer: object) -> None:
        return None  # there is no PTY left holding anything for it

    def resize(self, cols: int, rows: int) -> None:
        raise self._gone()

    def kill(self, *, force: bool = False) -> None:
        return None

    async def shutdown(self, grace: float = 5.0) -> None:
        return None

    # ------------------------------------------------------------------ #
    # the passive surface: same answers a live session would give
    # ------------------------------------------------------------------ #
    def status(self, threshold: Optional[float] = None) -> str:
        return STATUS_EXITED

    def idle_since(self) -> Optional[float]:
        return None

    def note_human_input(
        self,
        *,
        at_terminal: bool = False,
        data: Optional[bytes] = None,
        composing: bool = False,
    ) -> None:
        return None  # nobody is typing at a terminal that no longer exists

    def draft_open(self) -> bool:
        return False  # and nothing of theirs is left half-written in it

    def keyboard_busy(
        self, guard: Optional[float] = None, *, terminal_only: bool = False
    ) -> bool:
        return False

    def delivery_held(self) -> bool:
        return False  # nothing to hold back from a terminal that is gone

    def set_delivery_hold(self, held: bool) -> bool:
        return False  # and no way to hold it: the answer is always "open"

    def reminders_paused(self) -> bool:
        return False  # no terminal remains for a reminder to reach

    def set_reminder_pause(self, paused: bool) -> bool:
        return False

    def capture(self, *, history: bool = False) -> List[str]:
        return self.screen.render_history() if history else self.screen.render_screen()

    async def wait_for(self, state: str, *, timeout: float, threshold: float) -> str:
        return STATUS_EXITED  # exited satisfies both 'idle' and 'exited'

    def subscribe(self) -> asyncio.Queue:
        return asyncio.Queue(maxsize=1)  # nothing will ever be published to it

    def notify(self, text: str, *, ttl=None, level: str = "info") -> int:
        return 0  # no live viewers to show it to

    def unsubscribe(self, q: asyncio.Queue) -> None:
        return None

    def set_viewer_focused(self, viewer: object, focused: bool) -> None:
        return None

    def note_visit(self) -> None:
        # Reading the last screen of a session that is over still counts as
        # looking in on it — that is most of what these records are opened
        # for — so the visit is recorded here exactly as on a live one.
        self.last_visited_at = _utcnow()

    def viewers(self) -> int:
        return 0  # subscribe() hands out a queue nothing publishes to

    def last_activity_at(self) -> Optional[str]:
        return None  # no tracker, no screen history: nothing honest to say

    def info(self) -> dict:
        return {
            **self.sdef.to_dict(),
            "status": STATUS_EXITED,
            "pid": self.pid,
            "exit_code": self.exit_code,
            "created_at": self.created_at,
            "last_output_at": self.last_output_at,
            "last_visited_at": self.last_visited_at,
            "last_input_at": self.last_input_at,
            "last_activity_at": self.last_activity_at(),
            "viewers": self.viewers(),
            "exited_at": self.exited_at,
            "archived_at": self.archived_at,
            "paused_at": self.paused_at,
            "delivery_hold": self.delivery_held(),  # always False; see above
            "pending_deliveries": [],   # nothing is queued for a session that ended
        }
