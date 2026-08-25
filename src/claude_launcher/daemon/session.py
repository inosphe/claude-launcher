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
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Set, Tuple

from . import keys as keys_mod
from . import paths, pty_backend
from .harness import CLAUDE_HARNESS, SessionDef
from .idle import IdleTracker
from .screen import ScreenFeeder, ScreenState

#: Screen sampling cadence for idle detection (seconds).
SAMPLE_INTERVAL = 0.4

#: Rotate the raw output log beyond this size (a single .1 backup is kept).
LOG_MAX_BYTES = 10 * 1024 * 1024

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

log = logging.getLogger(__name__)


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

#: Seconds a row carrying the in-turn marker (or the animated spinner/counter
#: rows around it) may stay unchanged before the marker is distrusted. A live
#: turn repaints that region constantly — the spinner spins, the elapsed time
#: ticks — so a marker row frozen past this is a crashed or dead TUI's fossil,
#: not a turn. Generous, since a genuine turn's animation is far more frequent;
#: the cost of being too small is a slow turn flickering to idle, the cost of
#: too large is a stale dot persisting. Made an env var so it can be tuned
#: against a real frozen claude without a rebuild.
TURN_MARKER_FRESH_FOR = float(
    os.environ.get("CLAUNCH_TURN_MARKER_FRESH_FOR") or 15.0
)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
    ) -> None:
        self.sdef = sdef
        self.argv: List[str] = []
        self.pty = None
        self.pid: Optional[int] = None
        self.idle_threshold = idle_threshold
        self.screen = ScreenState(sdef.cols, sdef.rows, history=scrollback)
        self._feeder = ScreenFeeder(self.screen)
        self.tracker = IdleTracker()
        #: When this session was *first* made, not when this object was.
        #: A relaunch that keeps the name — a daemon restart's restore, a
        #: respawn, a redefine — is the same session continuing, and the
        #: manager hands the old value back in so listings stay in the order
        #: the sessions were actually created (see SessionManager.list).
        self.created_at = created_at or _utcnow()
        self.last_output_at: Optional[str] = None
        self.exit_code: Optional[int] = None
        self.exited_at: Optional[str] = None
        self.exited = False
        self._started_mono = time.monotonic()
        self._subscribers: Set[asyncio.Queue] = set()
        self._loop = asyncio.get_running_loop()
        self._status = STATUS_STARTING
        self._saw_output = False
        #: Latched once the harness has been seen ready to take a message; see
        #: :meth:`_await_readable`.
        self._input_ready = False
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
        #: A person's standing "don't type anything in here" — set from the
        #: dashboard, not derived from the keyboard. Every hold above is the
        #: daemon *guessing* from timing that now is a bad moment; this one
        #: is somebody saying so, and it does not expire, because a guess
        #: that lapses after five seconds is right and a decision that
        #: lapses after five seconds is broken. Read by the delivery gate
        #: (:meth:`MeshManager._deliver_to`) and reported by the queued view;
        #: cleared by :meth:`set_delivery_hold` and by nothing else.
        #:
        #: In memory only, and deliberately: it says "I am at this keyboard
        #: right now", which a daemon restart has already ended.
        self._delivery_hold = False
        #: Called once, with this session, when the child is gone for good —
        #: whatever ended it (see :meth:`_finish`). Set by the manager, which
        #: fans it out to whoever asked (the board sweep in
        #: :mod:`claude_launcher.daemon.beads`). Synchronous: a hook that has
        #: work to do schedules it.
        self.on_exit: Optional[Callable[["Session"], None]] = None

        session_dir = paths.session_dir(sdef.name)
        session_dir.mkdir(parents=True, exist_ok=True)
        self._log_path = paths.session_log(sdef.name)
        self._log = open(self._log_path, "ab")

    def start(self, argv: List[str], env: Dict[str, str], cwd: str) -> None:
        """Spawn the child and begin reading it. Called once, by the manager."""
        self.argv = argv
        self._started_mono = time.monotonic()
        self.pty = pty_backend.spawn(
            argv, env=env, cwd=cwd, cols=self.sdef.cols, rows=self.sdef.rows
        )
        self.pid = self.pty.pid

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
                self._loop.call_soon_threadsafe(self._on_output, chunk)
            self._loop.call_soon_threadsafe(self._on_eof)
        except RuntimeError:
            pass  # event loop already closed (daemon teardown)

    def _on_output(self, chunk: bytes) -> None:
        if self.exited:
            return
        self._saw_output = True
        self.last_output_at = _utcnow()
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
        self._append_log(chunk)
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
        await self._feeder.drained()

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

    def _on_eof(self) -> None:
        if self.exited:
            return
        self._finish()

    def _finish(self) -> None:
        self.exited = True
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
            paths.session_dir(self.sdef.name).joinpath("meta.json").write_text(
                json.dumps(meta, indent=2), encoding="utf-8"
            )
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
        if heur == STATUS_IDLE and self._claude_turn_in_flight():
            return STATUS_BUSY
        return heur

    # ------------------------------------------------------------------ #
    # commands
    # ------------------------------------------------------------------ #
    def status(self, threshold: Optional[float] = None) -> str:
        return self._compute_status(self.idle_threshold if threshold is None else threshold)

    def idle_since(self) -> Optional[float]:
        """Seconds the session has been idle (None when not idle).

        Agrees with :meth:`status`: a mid-turn session (footer marker present)
        is not idle even though the transient heuristic has been quiet for a
        while, so it reports None — one answer for both callers.
        """
        if self.exited or self._claude_turn_in_flight():
            return None
        idle_for = self.tracker.idle_for(time.monotonic())
        if idle_for is None or idle_for < self.idle_threshold:
            return None
        return idle_for

    async def deliver(self, text: str) -> bool:
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
        hold its position and retry on the next tick. Nothing is stored here:
        a ``False`` means nothing was typed and the message is still wholly
        the caller's, exactly as it was before the call.

        A human writing a prompt in this terminal is one of the reasons for
        that ``False`` (see :meth:`await_keyboard_quiet`). The message waits
        for their Enter — seconds away, since they are typing — and the next
        attempt goes in behind it.

        Every message is stamped with the wall-clock time it actually lands
        (after the readiness/keyboard holds, in the machine's local zone), so
        the receiving agent — and anyone reading its transcript — can tell
        *when* an automated delivery arrived, not just that it did.
        """
        try:
            await self._await_readable()
            if not await self.await_keyboard_quiet() and self.draft_open():
                # Not a failure to report to anyone: somebody is mid-sentence
                # at this keyboard. Said out loud all the same, because from a
                # sender's side "held behind a human's prompt" and "the TUI
                # never came up" look identical — both are just an undelivered
                # message — and only one of them resolves on its own.
                log.info(
                    "deliver to %r held: an unsent line is in that "
                    "terminal's composer; nothing was typed, and the message "
                    "stays with its sender until the human sends or clears it",
                    self.sdef.name,
                )
                return False
            await self.paste(f"{delivery_stamp()}\n{text}", enter=True)
        except Exception as exc:  # noqa: BLE001 — SessionGone, PTY write, ...
            log.debug("deliver to %r failed: %s", self.sdef.name, exc)
            return False
        return True

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
        if self.sdef.harness != CLAUDE_HARNESS:
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
        Both are terminal-only: ``send-keys`` types a line and its Enter
        together, and holding *itself* behind its own text would deadlock.
        """
        now = time.monotonic()
        self._last_human_input = now
        if not at_terminal:
            return
        self._last_terminal_input = now
        if composing:
            self._draft_open = True
        if data:
            state = draft_state_from_bytes(data)
            if state is not None:
                self._draft_open = state

    def draft_open(self) -> bool:
        """Whether a human has an unsent line in this terminal's composer.

        Capped by DRAFT_GUARD since the last keystroke — not because the
        draft stops existing, but because the *person* may have stopped: one
        character typed before walking away would otherwise hold this
        session's mail forever, and nobody is around for the interleaving to
        harm. Everything short of that is released by the keyboard instead —
        Enter, ``C-c`` — so an actual composer is protected for exactly as
        long as it is being written.
        """
        if not self._draft_open:
            return False
        if time.monotonic() - self._last_terminal_input >= DRAFT_GUARD:
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

    async def await_keyboard_quiet(self, *, terminal_only: bool = False) -> bool:
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
        """
        deadline = time.monotonic() + TYPING_HOLD_TIMEOUT
        while not self.exited and time.monotonic() < deadline:
            if not self.keyboard_busy(terminal_only=terminal_only):
                return True
            await asyncio.sleep(0.2)
        return not self.keyboard_busy(terminal_only=terminal_only)

    async def send_keys(self, args: List[str], *, literal: bool = False) -> bytes:
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
        data = keys_mod.encode_keys(
            args, literal=literal, app_cursor=self.screen.app_cursor_keys
        )
        if keys_mod.has_text(args, literal=literal):
            quiet = await self.await_keyboard_quiet(terminal_only=True)
            if not quiet and self.draft_open():
                raise KeyboardHeld(
                    f"session {self.sdef.name!r}: someone is typing there "
                    f"right now — nothing was sent. Retry in a moment, or "
                    f"use 'claunch mesh send' / the deliver API, which keeps "
                    f"the message and types it in when the line is free."
                )
            if self.exited:
                raise SessionGone(f"session {self.sdef.name!r} has exited")
        self.note_human_input()
        head, submit = keys_mod.split_submit(data)
        if submit and self.screen.bracketed_paste:
            # Text and its submitting CR in one write is the same trap
            # :meth:`paste` documents: a bracketed-paste TUI reads the chunk
            # as one paste and folds the CR into the text, so the line is
            # typed but never sent. Split it here too — this path is reached
            # by hand ('claunch send-keys ... Enter') where the caller has no
            # way to know the difference.
            await self.write_bytes(head)
            await asyncio.sleep(PASTE_ENTER_DELAY)
            await self.write_bytes(submit)
            return data
        await self.write_bytes(data)
        return data

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
        await self.write_bytes(data)
        if not enter:
            return data
        await asyncio.sleep(PASTE_ENTER_DELAY)
        await self.write_bytes(b"\r")
        return data + b"\r"

    async def write_bytes(self, data: bytes) -> None:
        if self.exited or self.pty is None:
            raise SessionGone(f"session {self.sdef.name!r} has exited")
        # PTY writes can block briefly (ConPTY pipe backpressure); keep the
        # event loop responsive by writing from the thread pool.
        await self._loop.run_in_executor(None, self.pty.write, data)

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
        self.pty.terminate(force=force)

    async def shutdown(self, grace: float = 5.0) -> None:
        """Terminate the child and wait briefly; force-kill stragglers."""
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
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def _broadcast(self, item: Tuple[str, object]) -> None:
        dead = []
        for q in self._subscribers:
            try:
                q.put_nowait(item)
            except asyncio.QueueFull:
                dead.append(q)  # slow consumer: drop it, it can reattach
        for q in dead:
            self._subscribers.discard(q)

    # ------------------------------------------------------------------ #
    # views
    # ------------------------------------------------------------------ #
    def info(self) -> dict:
        return {
            **self.sdef.to_dict(),
            "status": self.status(),
            "pid": self.pid,
            "exit_code": self.exit_code,
            "created_at": self.created_at,
            "last_output_at": self.last_output_at,
            "exited_at": self.exited_at,
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
    ``claunch respawn`` (and the web UI's resume) still reach it. Only the user
    drops such a record, via ``kill-session`` or ``clear-sessions``.

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
        exited_at: Optional[str] = None,
        scrollback: int = 5000,
        idle_threshold: float = 2.0,
    ) -> None:
        self.sdef = sdef
        self.exit_code = exit_code
        self.pid = pid
        self.created_at = created_at or _utcnow()
        self.last_output_at = last_output_at
        self.exited_at = exited_at
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

    async def deliver(self, text: str) -> bool:
        return False  # nothing is running to read it

    async def write_bytes(self, data: bytes) -> None:
        raise self._gone()

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

    def capture(self, *, history: bool = False) -> List[str]:
        return self.screen.render_history() if history else self.screen.render_screen()

    async def wait_for(self, state: str, *, timeout: float, threshold: float) -> str:
        return STATUS_EXITED  # exited satisfies both 'idle' and 'exited'

    def subscribe(self) -> asyncio.Queue:
        return asyncio.Queue(maxsize=1)  # nothing will ever be published to it

    def unsubscribe(self, q: asyncio.Queue) -> None:
        return None

    def info(self) -> dict:
        return {
            **self.sdef.to_dict(),
            "status": STATUS_EXITED,
            "pid": self.pid,
            "exit_code": self.exit_code,
            "created_at": self.created_at,
            "last_output_at": self.last_output_at,
            "exited_at": self.exited_at,
        }
