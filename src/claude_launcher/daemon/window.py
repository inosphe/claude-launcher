"""The measurement window: one machine, one arbiter, two classes.

What this replaces, and why it had to be replaced, is on the board
(``claunch-8y5j``, 2026-08-29): the sweep-window protocol stood on process
scans and mesh chat, and four holes were measured -- a scan blind spot of one
to three seconds at startup (``claunch-rwq``, round 12: an entrant 46s after
the holder's start, 33s of overlap), a stale holder blocking the whole queue
until a human noticed (s286's slot 6b), a protocol that never reaches sessions
outside the mesh (``claunch-fnhu``, round 13), and a queue that lived in the
leader's messages. The leader's ruling on ``claunch-rwq`` fixed the direction
-- an occupied/released *state*, not declarations -- and this module is the
state.

The arbiter is the daemon because of what the daemon already is: a single
process (so two acquisitions racing are serialised by construction, and the
scan blind spot stops existing), always up while any session is (every session
is daemon-managed), and the one component that *knows* when a session ends --
:meth:`WindowManager.session_exited` rides the same ``manager.exit_hooks``
funnel as the board's sweep (``daemon/beads.py``), so a holder that dies
releases itself instead of waiting to be noticed.

Two classes, because the resource has two shapes (operator-set values,
2026-08-29, board ``claunch-8y5j``; the measured revision belongs to
``claunch-bl0e``):

* ``sweep`` -- a full-suite run. Capacity 1 and **exclusive against
  everything**: while a sweep holds, no targeted run is granted, and once a
  sweep is *queued* no new targeted grant is made (writer preference -- under
  rotating targeted runs an exclusive requester would starve). Exclusivity is
  the measured requirement, not caution: a targeted run overlapping a full
  sweep flipped red to green in 78 seconds on an identical tree (the
  ``claunch-1dre`` table, 2026-08-27), so a grant that admitted the pair would
  be minting measurements nobody may cite.
* ``targeted`` -- a nodeid-selected run. Capacity 5, shared. The cap is
  enforced by *not granting*, which is what ``claunch-95fa``'s third fix asked
  for: the per-session scan becomes the arbiter's knowledge, and eight
  concurrent targeted runs (where s155/s148 met xdist node-down) cannot
  assemble.

The caps are deliberately NOT derived from the CPU count: this suite is not
CPU-bound (32 cores at 15% under 22 pytest processes -- s159's measurement in
``claunch-95fa``); its cost axes are process spawn and PTY/daemon waits. What
the CPU count does govern is the width of *one* run, so each grant carries
``advisory_n`` -- cores divided by active runs, clamped -- which is the fair
scheduling the arbiter is the only component positioned to do, because the
arbiter is the one that knows how many runs are active.

What is deliberately NOT here:

* a wall-clock TTL. The repository has already paid for that confusion once
  (s143's 19 minutes, cut by a tool ceiling and lost silently): a clock cannot
  tell a stuck holder from a slow legitimate run, and the honest reapers --
  the session exit hook and pid liveness -- already cover death. A holder
  whose session lives and whose pid lives is holding.
* strict fairness policy beyond FIFO + writer preference. No preemption, no
  priorities.
* enforcement against a process that never asks. That half lives at the point
  of consumption: ``tools/sweep.py run`` acquires before it runs, and
  ``tests/conftest.py`` holds the window for any full-suite pytest however
  launched. This module is the state those two consult.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .. import atomic, store
from . import paths

log = logging.getLogger(__name__)

#: The two resource classes. See the module docstring for why sweep is
#: exclusive and targeted is not, and why the caps are not CPU-derived.
SWEEP = "sweep"
TARGETED = "targeted"
CLASSES = (SWEEP, TARGETED)

#: ``advisory_n`` bounds: a run narrower than 2 pays xdist's fixed costs for
#: nothing; wider than 8 buys nothing this suite can spend (the bottleneck is
#: spawn and PTY/daemon waits, not cores).
ADVISORY_MIN = 2
ADVISORY_MAX = 8

#: Server-side ceiling on an acquire's wait, so a client typo cannot park a
#: connection forever. One hour covers the slowest observed full suite by an
#: order of magnitude.
MAX_WAIT = 3600.0

_STATE_VERSION = 1


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def pid_alive(pid: int) -> bool:
    """Is there a live process with this pid? (Best effort, no dependencies.)

    Windows has no ``kill(pid, 0)`` -- ``os.kill`` there *terminates* -- so
    the check is OpenProcess + GetExitCodeProcess. POSIX takes the signal-0
    path. A pid that cannot be queried at all (access denied) counts as
    alive: reaping on an unreadable pid would reap strangers' processes on a
    multi-user machine.
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True  # handle opened; unreadable exit code is not death
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


class WindowManager:
    """Holds and grants the measurement window; persists it across restarts.

    ``caps`` returns ``(sweep_cap, targeted_cap)`` and is read on every
    operation, so an operator's edit lands without a restart -- the pattern
    the cflow reminder keys already use. ``manager`` may be None in tests;
    session-liveness reaping then has nothing to consult.

    Holder changes are pull-based observability through :meth:`status` and
    the window API. They are expected high-frequency state transitions, so
    they do not enter the mesh delivery path, where every delivery is an
    agent-facing user message.
    """

    def __init__(
        self,
        manager=None,
        *,
        caps: Optional[Callable[[], Tuple[int, int]]] = None,
        state_path: Optional[Path] = None,
        cores: Optional[int] = None,
    ) -> None:
        self._manager = manager
        self._caps = caps or self._caps_from_config
        self._state_path = state_path or (paths.daemon_dir() / "window.json")
        self._cores = cores or (os.cpu_count() or 4)
        self._holders: List[dict] = []
        self._queue: List[dict] = []
        self._granted_events: Dict[str, asyncio.Event] = {}
        self._load()

    # ---- configuration --------------------------------------------------- #

    @staticmethod
    def _caps_from_config() -> Tuple[int, int]:
        cfg = store.daemon_config()
        return (
            int(cfg.get("window_sweep_cap", 1)),
            int(cfg.get("window_targeted_cap", 5)),
        )

    def _cap(self, cls: str) -> int:
        sweep_cap, targeted_cap = self._caps()
        return max(1, sweep_cap if cls == SWEEP else targeted_cap)

    # ---- persistence ------------------------------------------------------ #

    def _load(self) -> None:
        """Read the persisted window, if any. Reaping is deliberately NOT done
        here: on boot the sessions that held grants may still be restoring, so
        death is judged lazily at the next acquire/status instead."""
        try:
            doc = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if doc.get("version") != _STATE_VERSION:
            return
        self._holders = list(doc.get("holders") or [])
        self._queue = list(doc.get("queue") or [])

    def _save(self) -> None:
        doc = {
            "version": _STATE_VERSION,
            "saved_at": _utcnow(),
            "holders": self._holders,
            "queue": self._queue,
        }
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            with atomic.scratch(self._state_path) as tmp:
                tmp.write_text(
                    json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8"
                )
                atomic.replace(tmp, self._state_path)
        except OSError as exc:
            # The in-memory window is the arbiter; the file is its survivor.
            # A failed save must not fail the grant it was recording.
            log.warning("window: state not saved: %s", exc)

    # ---- reaping ------------------------------------------------------------ #

    def _session_gone(self, name: str) -> bool:
        if self._manager is None:
            return False
        try:
            session = self._manager.get(name)
        except Exception:
            return True  # the daemon never heard of it: gone
        return bool(getattr(session, "exited", False))

    def _dead(self, entry: dict) -> bool:
        """A holder/waiter nobody is behind: dead session, or a sessionless
        (manual) entry whose pid is gone."""
        session = entry.get("session")
        if session:
            return self._session_gone(session)
        pid = entry.get("pid") or 0
        return not pid_alive(pid)

    def _reap(self) -> bool:
        """Drop dead holders and waiters. Returns True if anything changed."""
        before_h = len(self._holders)
        before_q = len(self._queue)
        self._holders = [h for h in self._holders if not self._dead(h)]
        self._queue = [q for q in self._queue if not self._dead(q)]
        dropped = before_h - len(self._holders) + before_q - len(self._queue)
        if dropped:
            log.info("window: reaped %d dead holder/waiter entr(ies)", dropped)
        return dropped > 0

    # ---- granting ----------------------------------------------------------- #

    def _grantable(self, cls: str) -> bool:
        if cls == SWEEP:
            return not self._holders
        return (
            sum(1 for h in self._holders if h["cls"] == TARGETED) < self._cap(TARGETED)
            and not any(h["cls"] == SWEEP for h in self._holders)
            and not any(q["cls"] == SWEEP for q in self._queue)
        )

    def _grant(self, entry: dict) -> dict:
        holder = dict(entry)
        holder["acquired_at"] = _utcnow()
        holder.pop("enqueued_at", None)
        self._holders.append(holder)
        event = self._granted_events.pop(entry["grant_id"], None)
        if event is not None:
            event.set()
        return holder

    def _process_queue(self) -> List[dict]:
        """Grant everything the head of the queue allows, in order.

        FIFO with writer preference falls out of the predicates: a queued
        sweep makes every later targeted entry ungrantable, and a sweep grants
        only into an empty window.
        """
        granted = []
        for entry in list(self._queue):
            if not self._grantable(entry["cls"]):
                continue
            self._queue.remove(entry)
            granted.append(self._grant(entry))
        return granted

    def advisory_n(self, extra: int = 0) -> int:
        """The width one run should take, given how many are running.

        The one number the arbiter knows that no scanning session can: how
        many runs are actually active. ``extra`` counts a grant about to be
        made, so the answer a requester gets already includes itself.
        """
        active = max(1, len(self._holders) + extra)
        return max(ADVISORY_MIN, min(ADVISORY_MAX, self._cores // active))

    # ---- the public surface ------------------------------------------------- #

    async def acquire(
        self,
        cls: str,
        *,
        session: Optional[str],
        pid: int = 0,
        label: str = "",
        wait: float = 0.0,
    ) -> dict:
        """Grant the window, or queue for it up to ``wait`` seconds.

        A ``wait`` of 0 asks without queueing and jumps nobody: with a
        non-empty queue the answer is ``granted: False`` even into a grantable
        slot, because the slot belongs to the queue's head.
        """
        if cls not in CLASSES:
            return {"granted": False, "error": f"unknown window class {cls!r}"}
        changed = self._reap()
        if changed:
            self._process_queue()
            self._save()
        entry = {
            "grant_id": secrets.token_hex(6),
            "cls": cls,
            "session": session,
            "pid": pid,
            "label": label,
            "enqueued_at": _utcnow(),
        }
        if not self._queue and self._grantable(cls):
            holder = self._grant(entry)
            self._save()
            return {
                "granted": True,
                "grant_id": holder["grant_id"],
                "advisory_n": self.advisory_n(),
            }
        if wait <= 0:
            if changed:
                self._save()
            return {
                "granted": False,
                "position": len(self._queue) + 1,
                "window": self.status(),
            }
        event = asyncio.Event()
        self._granted_events[entry["grant_id"]] = event
        self._queue.append(entry)
        self._save()
        try:
            await asyncio.wait_for(event.wait(), timeout=min(wait, MAX_WAIT))
        except asyncio.TimeoutError:
            if entry in self._queue:
                self._queue.remove(entry)
                self._save()
            return {
                "granted": False,
                "timeout": True,
                "window": self.status(),
            }
        except asyncio.CancelledError:
            # A disconnected long-poll must not leave an orphan that can be
            # granted later without a client to receive the grant id.
            if entry in self._queue:
                self._queue.remove(entry)
                self._save()
            raise
        finally:
            self._granted_events.pop(entry["grant_id"], None)
        return {
            "granted": True,
            "grant_id": entry["grant_id"],
            "advisory_n": self.advisory_n(),
        }

    def release(self, grant_id: str) -> bool:
        """Hand the window back. False = no such grant (already reaped?)."""
        holder = next((h for h in self._holders if h["grant_id"] == grant_id), None)
        if holder is None:
            return False
        self._holders.remove(holder)
        self._process_queue()
        self._save()
        return True

    def release_session(self, session: str) -> int:
        """Everything this session holds, back. Returns how many."""
        ids = [h["grant_id"] for h in self._holders if h.get("session") == session]
        return sum(1 for gid in ids if self.release(gid))

    def cancel(self, grant_id: str) -> bool:
        """Remove one waiting request.  A holder cannot be cancelled here."""
        entry = next((q for q in self._queue if q["grant_id"] == grant_id), None)
        if entry is None:
            return False
        self._queue.remove(entry)
        self._granted_events.pop(grant_id, None)
        self._save()
        return True

    def cancel_session(self, session: str) -> int:
        """Drop every waiting request from this session (a waiter giving up)."""
        entries = [q for q in self._queue if q.get("session") == session]
        for entry in entries:
            self._queue.remove(entry)
            self._granted_events.pop(entry["grant_id"], None)
        if entries:
            self._save()
        return len(entries)

    def status(self) -> dict:
        """The window as state anyone can read -- the whole point.

        Deliberately does not reap: status is a read, and a read with side
        effects is how a quiet ``status`` call ends up releasing somebody's
        grant. Reaping happens where grants are decided (``acquire``).
        """
        sweep_cap, targeted_cap = self._caps()
        return {
            "holders": list(self._holders),
            "queue": list(self._queue),
            "caps": {"sweep": sweep_cap, "targeted": targeted_cap},
            "cores": self._cores,
            "advisory_n_now": self.advisory_n(extra=1),
        }

    # ---- the exit hook -------------------------------------------------------- #

    def session_exited(self, session) -> None:
        """The manager's exit funnel: a session that ends gives the window back.

        Same funnel, same contract as ``daemon/beads.py``'s hook: called
        synchronously in the loop, never awaited. A session whose pytest is
        mid-run when its terminal dies is exactly the stale-holder case this
        hook exists for -- the child pytest dying with its session is what
        makes releasing right, and if it somehow outlives the session the pid
        reaper at the next acquire finishes the job.
        """
        name = session.sdef.name
        held = [h for h in self._holders if h.get("session") == name]
        queued = [q for q in self._queue if q.get("session") == name]
        if not held and not queued:
            return
        self._holders = [h for h in self._holders if h.get("session") != name]
        self._queue = [q for q in self._queue if q.get("session") != name]
        self._process_queue()
        self._save()
