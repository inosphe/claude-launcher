"""What a restart owes the session that asked for it.

A restart kills the turn that asked for it. The daemon goes down with every
terminal attached to it, so the agent that typed ``claunch daemon restart``
never reaches the line after it — ``print(f"daemon restarted at ...")`` is
written to a stdout nobody will ever read again. The session comes back under
``--resume`` with its scrollback intact and *no record anywhere of what it was
in the middle of doing*. So it asks again. That loop has been observed on this
machine three times in ninety seconds, and it only ended by luck.

The fix is not to keep the turn alive — nothing can. It is to make the restart
**write itself down before it stops anything**, so the successor daemon can
hand the answer back to a session that has no other way to learn it:

    record → flush → stop the old daemon → start the new one → deliver

Two files, and they have one writer each, because a shared file here would be
written by a process that is about to die and by the process replacing it:

``restart-requests.jsonl``
    Append-only, written by *whoever asks* — the CLI in the caller's shell,
    or the API handler for the web UI's button. The daemon only reads it, at
    boot, and empties it. Nothing in the shutdown path touches it, which is
    the whole reason it is not the mesh queue: mesh state belongs to the live
    daemon and is rewritten during shutdown (``MeshManager.shutdown`` and the
    ``_persist_*`` calls behind it), so a record the CLI wrote there on its
    way past would be overwritten by the daemon it was about to stop.

``restart-notice.json``
    The daemon's own: the boot ledger, and the debts not yet delivered.

**The absence of a record is itself the signal.** Every door that ends a
daemon on purpose leaves a line here first, so a boot that finds none, with a
boot before it in the ledger, was not asked for by anything on this machine —
a crash, a kill, or a session starting its own daemon behind the command's
back. Saying so is this module's job; *preventing* it is not (claunch-3k9).

**These notices are debts, not nudges.** The resume nudge next door
(:mod:`claude_launcher.daemon.resume`) is allowed to give up — it says "carry
on", and a session already working needs no such thing. This one says "here is
what happened to you while you could not see", and a session that is busy,
that has an unsent line in its composer, or that is parked behind a gate needs
it exactly as much as an idle one — more, since being busy is precisely how
the loop reproduces. So nothing here drops on ``busy``, nothing drops on a
held delivery, and a window that expires leaves the debt on disk for the next
boot to carry. The only debt ever retired undelivered is one whose session has
no terminal left to read it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional

from ..harnesses import CLAUDE_HARNESS
from . import paths
from .session import INPUT_SETTLE, STATUS_IDLE, STATUS_STARTING

log = logging.getLogger("claunch.daemon.restart")

#: How often an undelivered debt is re-offered. Same reason as the resume
#: nudge's: what it waits for is a TUI finishing its startup, seconds not
#: minutes.
POLL = 1.0

#: How long one boot keeps offering. Longer than the nudge's window because
#: expiry costs nothing here — the debt goes back to disk and the next boot
#: picks it up — while a short window would just move the work.
WINDOW = 900.0

#: How long a session may hold the keyboard without ever reading idle before
#: the notice is offered anyway. The quiet test below is the good path, but it
#: is a test a permanently busy session never passes — and busy is precisely
#: where this notice is needed, so it cannot be a precondition. Long enough
#: that the startup burst (seconds) is over for certain.
BUSY_GRACE = 30.0

#: How many boots the ledger keeps. Only the most recent one is ever read;
#: the rest are there for a human reading the file after a bad morning.
BOOT_HISTORY = 20

#: Reasons a door writes a record. ``restart`` and ``stop`` both mean "a
#: human or an agent ended this daemon on purpose"; only ``restart`` is owed
#: an answer, because a stop leaves nothing coming back to answer with.
KIND_RESTART = "restart"
KIND_STOP = "stop"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def requests_file():
    return paths.daemon_dir() / "restart-requests.jsonl"


def ledger_file():
    return paths.daemon_dir() / "restart-notice.json"


# --------------------------------------------------------------------------- #
# the requester's half: write it down before stopping anything
# --------------------------------------------------------------------------- #
def record_request(
    *,
    kind: str = KIND_RESTART,
    via: str = "cli",
    session: Optional[str] = None,
    cwd: str = "",
    daemon: Optional[dict] = None,
) -> Optional[dict]:
    """Write one request line and make sure it is on disk. Returns the record.

    Called *before* the daemon is asked to stop — that ordering is the entire
    point, and it is why this takes ``daemon`` (the identity of the process
    about to end) rather than reading it later: after the stop there is
    nothing left to read it from, ``daemon.json`` being removed early in
    shutdown.

    ``session`` is the requester when a managed session asked (the debt is
    owed to it); ``None`` when a human's shell or the web UI did, and then the
    line exists only so the successor can tell an asked-for restart from one
    nobody asked for. Failure to write is not fatal to the restart: a missing
    line costs a notice, a raised exception would cost the restart.

    Nothing is written when no daemon is announced, and that silence is
    deliberate. The line's second job is to be an alibi — "this gap in the
    daemon's life was asked for" — and there is no gap to excuse when the
    daemon was already gone before the command ran. Writing one anyway would
    spend the alibi on a boot that deserved it and hide the next restart that
    really did happen behind nobody's back.
    """
    if daemon is None:
        from . import runtime_state

        daemon = runtime_state.read_daemon_json() or {}
    if not daemon.get("pid"):
        log.debug("no daemon announced; %s request left unrecorded", kind)
        return None
    record = {
        "kind": kind,
        "via": via,
        "requested_by": session or None,
        "requested_at": _utcnow(),
        "cwd": cwd or "",
        "daemon_pid": daemon.get("pid"),
        "daemon_started_at": daemon.get("started_at"),
    }
    path = requests_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    except OSError as exc:
        log.warning("could not record the %s request: %s", kind, exc)
        return None
    return record


def record_request_from_env(*, kind: str = KIND_RESTART, **kw) -> Optional[dict]:
    """:func:`record_request` with the caller's shell read for identity.

    ``CLAUNCH_SESSION`` is set inside every managed session and unset in a
    human's shell, which is exactly the distinction the debt turns on — so
    the CLI never has to decide who it is.
    """
    return record_request(
        kind=kind,
        session=os.environ.get("CLAUNCH_SESSION") or None,
        cwd=os.getcwd() if not kw.get("cwd") else kw.pop("cwd"),
        **kw,
    )


def read_requests() -> List[dict]:
    """Every request line waiting for a boot to answer it (oldest first)."""
    path = requests_file()
    if not path.is_file():
        return []
    out: List[dict] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            doc = json.loads(line)
        except ValueError:
            continue  # a torn line loses one notice, not the whole boot
        if isinstance(doc, dict):
            out.append(doc)
    return out


def clear_requests() -> None:
    with contextlib.suppress(OSError):
        requests_file().unlink()


# --------------------------------------------------------------------------- #
# the daemon's half: the ledger
# --------------------------------------------------------------------------- #
def read_ledger() -> dict:
    path = ledger_file()
    doc = None
    if path.is_file():
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            doc = None
    if not isinstance(doc, dict):
        doc = {}
    boots = doc.get("boots")
    debts = doc.get("debts")
    return {
        "boots": boots if isinstance(boots, list) else [],
        "debts": debts if isinstance(debts, list) else [],
    }


def write_ledger(doc: dict) -> None:
    path = ledger_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    except OSError as exc:
        log.warning("could not write the restart ledger: %s", exc)


def previous_boot() -> Optional[dict]:
    """The boot before this one, or ``None`` on a machine that never ran one.

    ``daemon.json`` cannot answer this: it is removed early in shutdown
    (``runtime_state.remove_daemon_json``), so at boot there is nothing of the
    predecessor left in it, and it is rewritten in place anyway. The ledger is
    append-only for exactly this reason — an identity to compare against has
    to outlive the process that owned it.
    """
    boots = read_ledger()["boots"]
    return boots[-1] if boots else None


def note_boot(
    *,
    pid: int,
    started_at: str,
    version: str = "",
    port: Optional[int] = None,
    restored: Iterable[str] = (),
) -> List[dict]:
    """Record this boot, consume the pending requests, and return new debts.

    The pending requests are read *and cleared* here — one boot answers them,
    and leaving them would make the next boot answer them again. What comes
    back is the list of debts this boot created; they are already on disk.
    """
    ledger = read_ledger()
    previous = ledger["boots"][-1] if ledger["boots"] else None
    requests = read_requests()
    clear_requests()

    boot = {
        "pid": pid,
        "started_at": started_at,
        "version": version,
        "port": port,
        "requested": bool(requests),
        "requested_by": [r.get("requested_by") for r in requests if r.get("requested_by")],
    }
    ledger["boots"] = (ledger["boots"] + [boot])[-BOOT_HISTORY:]

    new: List[dict] = []
    for req in requests:
        who = req.get("requested_by")
        if not who or req.get("kind") != KIND_RESTART:
            # A stop, or a restart asked for from a human's shell or the web
            # UI: the line did its other job (this boot is not unexplained),
            # and there is no session sitting on a dead turn to answer.
            continue
        new.append({
            "session": who,
            "kind": "requested",
            "created_at": _utcnow(),
            "text": requested_block(who, req, boot, previous),
        })
    if not requests and previous is not None:
        # Nothing on this machine asked, and a daemon ran before this one:
        # somebody's restart went around the command, or the predecessor died.
        for name in restored:
            new.append({
                "session": name,
                "kind": "unsolicited",
                "created_at": _utcnow(),
                "text": unsolicited_block(name, boot, previous),
            })

    ledger["debts"] = ledger["debts"] + new
    write_ledger(ledger)
    return new


def owed(session: Optional[str] = None) -> List[dict]:
    """Debts still on disk — all of them, or one session's."""
    debts = read_ledger()["debts"]
    if session is None:
        return debts
    return [d for d in debts if d.get("session") == session]


def settle(debt: dict) -> None:
    """Remove one debt from disk. Called only after a delivery landed."""
    ledger = read_ledger()
    before = len(ledger["debts"])
    ledger["debts"] = [
        d for d in ledger["debts"]
        if not (
            d.get("session") == debt.get("session")
            and d.get("created_at") == debt.get("created_at")
            and d.get("kind") == debt.get("kind")
        )
    ]
    if len(ledger["debts"]) != before:
        write_ledger(ledger)


# --------------------------------------------------------------------------- #
# what the session reads
# --------------------------------------------------------------------------- #
def _pid_line(boot: dict, old: Optional[dict]) -> str:
    was = old.get("pid") if old else None
    when = (old or {}).get("started_at") or "an unrecorded time"
    if was:
        return (
            f"pid {was} (up since {when}) is gone; pid {boot.get('pid')} is "
            f"answering now, up since {boot.get('started_at')}"
        )
    return (
        f"pid {boot.get('pid')} is answering now, up since "
        f"{boot.get('started_at')} (the pid it replaced was not recorded)"
    )


def requested_block(name: str, req: dict, boot: dict, previous: Optional[dict]) -> str:
    """The answer to "did my restart work?", written where the asker can read it.

    It leads with the pids because that is the one check the asker can repeat
    for itself afterwards, and it says outright not to ask again — an agent
    that cannot find the outcome of an action assumes the action failed, and
    that assumption is what turned one restart into three.
    """
    old = {
        "pid": req.get("daemon_pid"),
        "started_at": req.get("daemon_started_at"),
    }
    if not old["pid"]:
        old = previous or {}
    return "\n".join([
        "---",
        "# claunch: daemon restart -- machine-generated, not typed by the user",
        f"session: {name}",
        f"you asked for this at: {req.get('requested_at')} (via {req.get('via')})",
        f"outcome: it worked. {_pid_line(boot, old)}.",
        "what it cost: the turn you asked from died with the daemon you "
        "stopped -- this terminal was relaunched with --resume, so the "
        "conversation is intact, but whatever that turn had not written down "
        "is gone.",
        "protocol: do NOT restart again to find out whether the restart "
        "worked. This block is written after the new daemon is listening, so "
        "its existence is the proof, and the pid above is the same fact "
        "'claunch daemon status' will tell you. Pick your work back up from "
        "the messages above; if you are driving a cflow run, call its "
        "'status' tool first -- it is the current truth.",
        "---",
    ])


def unsolicited_block(name: str, boot: dict, previous: Optional[dict]) -> str:
    """A restart nobody on this machine asked for, said as an observation.

    Every door that ends a daemon on purpose writes itself down first, so no
    record means no door was used — but that is evidence, not a verdict: a
    crash and an out-of-band kill leave the same silence, and this module
    cannot tell them apart from out here. It reports what it can prove (there
    is a boot before this one, and no request between them) and stops.
    """
    return "\n".join([
        "---",
        "# claunch: daemon restart -- machine-generated, not typed by the user",
        f"session: {name}",
        f"what happened: the daemon restarted and nothing asked it to. "
        f"{_pid_line(boot, previous)}.",
        "how that is known: every restart and stop that goes through claunch "
        "writes a record before stopping the old daemon. There is no record "
        "between the previous boot and this one -- so this was a crash, a "
        "kill from outside, or a session that started its own daemon without "
        "using the command.",
        "what it cost you: your turn died with that daemon. This terminal was "
        "relaunched with --resume, so the conversation above is intact, but "
        "nothing has been driving this session since.",
        "protocol: this is a report, not a task, and restarting again will "
        "not diagnose it. If you were the one who restarted the daemon, you "
        "did it in a way that leaves no record -- use 'claunch daemon "
        "restart' so the next one can be traced. Otherwise carry on with the "
        "work above.",
        "---",
    ])


# --------------------------------------------------------------------------- #
# delivery
# --------------------------------------------------------------------------- #
class RestartNotice:
    """Hands the boot's debts to their sessions, and keeps the ones it cannot.

    Deliberately unlike :class:`~claude_launcher.daemon.resume.ResumeNudge` in
    the three places that matter, all of which are the same place: this owes
    the session something, so nothing about the session's *state* is grounds
    to stop owing it.

    * A session that is working again is not "already driven" — being mid-turn
      is how a session that never learned its restart succeeded ends up asking
      for another one. It is waited for, not skipped.
    * A refused delivery (an unsent line in the composer, a TUI still coming
      up) is retried for as long as the window lasts.
    * When the window ends, whatever is left stays on disk. The next boot
      offers it again.

    The one debt that is retired without being read is one whose session has
    no terminal: an exited record cannot be typed into, and holding a debt for
    it forever would only grow the file.
    """

    def __init__(
        self,
        manager,
        debts: Optional[List[dict]] = None,
        *,
        poll: float = POLL,
        window: float = WINDOW,
    ) -> None:
        self.manager = manager
        self.poll = poll
        self.window = window
        #: name -> monotonic time the TUI was first seen holding the keyboard.
        self._paste_since: Dict[str, float] = {}
        #: Everything owed, this boot's and any boot's before it that never
        #: managed to hand its notice over.
        self.pending: List[dict] = list(owed() if debts is None else debts)
        self._ready_since: Dict[str, float] = {}
        self._task: Optional[asyncio.Task] = None
        #: Session names actually typed into — what the tests read, and what
        #: the closing log line reports.
        self.delivered: List[str] = []
        #: Debts retired without delivery because there was no terminal left.
        self.abandoned: List[str] = []

    def start(self) -> None:
        if self._task is not None or not self.pending:
            return
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def shutdown(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _run(self) -> None:
        log.info(
            "restart notice: %d session(s) owed an account of this boot: %s",
            len(self.pending),
            ", ".join(sorted({d.get("session", "?") for d in self.pending})),
        )
        deadline = time.monotonic() + self.window
        try:
            while self.pending and time.monotonic() < deadline:
                await asyncio.sleep(self.poll)
                for debt in list(self.pending):
                    try:
                        settled = await self._attempt(debt)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        # One unreadable session must not strand the rest --
                        # but the debt goes back to disk, not away.
                        log.exception(
                            "restart notice for %r failed", debt.get("session")
                        )
                        settled = True
                    if settled:
                        self.pending.remove(debt)
        except asyncio.CancelledError:
            raise
        finally:
            if self.pending:
                log.info(
                    "restart notice still owed to %s — kept on disk for the "
                    "next boot",
                    ", ".join(sorted({d.get("session", "?") for d in self.pending})),
                )
            if self.delivered:
                log.info("restart notice delivered to %s", ", ".join(self.delivered))

    async def _attempt(self, debt: dict) -> bool:
        """One pass at one debt. True once it leaves the pending list."""
        name = debt.get("session") or ""
        try:
            session = self.manager.get(name)
        except Exception:
            session = None
        if session is None or getattr(session, "exited", True):
            log.info(
                "restart notice for %r retired undelivered: no terminal to "
                "read it (the session is gone or exited)",
                name,
            )
            settle(debt)
            self.abandoned.append(name)
            return True

        if not self._ready(session, name):
            return False
        if not await session.deliver(debt.get("text") or ""):
            # Held, not dropped: the composer has an unsent line, or the TUI
            # is not taking the keyboard yet. Both pass.
            return False
        settle(debt)
        self.delivered.append(name)
        return True

    def _ready(self, session, name: str) -> bool:
        """Whether the TUI can take a paste — and nothing more than that.

        The resume nudge reads the same two signals and then asks a third
        question, "is somebody else driving this?", because its message is
        redundant if so. This one has no such question to ask: the message is
        about something that happened to the session, and a session that went
        straight back to work is the one most in need of hearing it.

        The signals themselves are the nudge's, for its reason — a paste
        written into the gap between a restored TUI enabling bracketed paste
        and accepting its first submit is typed and never sent, and deliver's
        own wait is bounded from spawn, which a restart exhausts by spawning
        everything at once. But "has been quiet since" is a test a session
        that is working never passes, so it cannot be the only way through:
        after ``BUSY_GRACE`` of holding the keyboard the startup burst is
        over whatever the status says, and the notice is offered. A refusal
        there costs one poll; never offering costs the whole point.
        """
        status = session.status()
        if status == STATUS_STARTING:
            return False
        if getattr(session.sdef, "harness", CLAUDE_HARNESS) != CLAUDE_HARNESS:
            return True
        screen = getattr(session, "screen", None)
        if not (screen and screen.bracketed_paste):
            return False  # the input does not exist yet
        held = self._paste_since.setdefault(name, time.monotonic())
        armed = self._ready_since.get(name)
        if armed is None:
            if status != STATUS_IDLE:
                return time.monotonic() - held >= BUSY_GRACE
            self._ready_since[name] = time.monotonic()
            return False  # armed, not settled: later polls serve the settle
        if time.monotonic() - armed < INPUT_SETTLE:
            if status != STATUS_IDLE:
                # The burst that follows the mode going on. Count again --
                # unless it has been going on so long it is not a burst.
                del self._ready_since[name]
                return time.monotonic() - held >= BUSY_GRACE
            return False
        return True
