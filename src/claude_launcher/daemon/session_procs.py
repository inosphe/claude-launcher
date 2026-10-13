"""Processes the daemon started on a session's behalf, and what they left running.

The daemon runs commands for a session that the session's own terminal never
sees: a workflow step's ``restart:`` script (``RestartClock._execute``, shared
by cflow v1 and v2). Such a script often starts a long-lived service and exits
-- another project's ``tools/restart_live.py`` launches ``npm run dev`` with
``start_new_session=True`` (claunch-gds6-build-arm-windows-zt651.1) -- so the
service keeps running with no terminal, no session and no way to stop it short
of hunting the pid by hand (claunch-ihwel).

This module keeps that list. A command is *adopted* when the daemon starts it
(:meth:`ProcTracker.adopt`); every :meth:`ProcTracker.refresh` then takes one
process-table snapshot and grows each session's member set with

* every process whose parent is a live member (the process tree, measured
  repeatedly so that a grandchild is caught before its parent exits and the
  parent link is lost), and
* on Linux, every process whose environment carries :data:`OWNER_ENV` naming
  the session -- the adopter puts it in the command's environment, so a
  service that detached itself (``setsid``, a new session, a reparent to init)
  is still found.

A member is identified by pid *and* an identity string (Linux: the start time
in ``/proc/<pid>/stat``; elsewhere the command line), so a recycled pid is not
mistaken for the process that used to hold it. Dead members are dropped; the
set is persisted under the daemon directory so a daemon restart does not lose
a dev server the previous daemon started.

The daemon's own pid never becomes a member: when the adopted command is this
repository's restart script, ``claunch daemon restart`` spawns the next
daemon under it, and that daemon (with every session it hosts) is not a
process the session left behind. :func:`..daemon_client.spawn_daemon` removes
:data:`OWNER_ENV` for the same reason.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

from . import paths

log = logging.getLogger("claunch.daemon.session_procs")

#: Set in the environment of every command the daemon adopts for a session;
#: its value is the session name.
OWNER_ENV = "CLAUNCH_PROC_OWNER"

#: Seconds a SIGTERM'd process gets before SIGKILL (POSIX).
KILL_GRACE = 3.0


@dataclass
class Proc:
    pid: int
    ppid: int
    ident: str
    cmd: str
    owner: Optional[str] = None


Snapshot = Dict[int, Proc]


# ---------------------------------------------------------------------------
# process table


def _linux_snapshot(proc_root: str = "/proc") -> Snapshot:
    out: Snapshot = {}
    marker = (OWNER_ENV + "=").encode()
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return out
    for entry in entries:
        if not entry.isdigit():
            continue
        base = os.path.join(proc_root, entry)
        try:
            with open(os.path.join(base, "stat"), "rb") as fh:
                stat = fh.read().decode("utf-8", "replace")
        except OSError:
            continue
        # comm may contain spaces and parens: split after the last ')'.
        close = stat.rfind(")")
        fields = stat[close + 2:].split()
        if len(fields) < 20:
            continue
        comm = stat[stat.find("(") + 1:close]
        try:
            with open(os.path.join(base, "cmdline"), "rb") as fh:
                argv = [a.decode("utf-8", "replace") for a in fh.read().split(b"\0") if a]
        except OSError:
            argv = []
        owner = None
        try:
            with open(os.path.join(base, "environ"), "rb") as fh:
                for var in fh.read().split(b"\0"):
                    if var.startswith(marker):
                        owner = var[len(marker):].decode("utf-8", "replace")
                        break
        except OSError:
            pass
        pid = int(entry)
        out[pid] = Proc(pid=pid, ppid=int(fields[1]), ident=f"start:{fields[19]}",
                        cmd=" ".join(argv) or f"[{comm}]", owner=owner)
    return out


def _ps_snapshot() -> Snapshot:  # pragma: no cover - macOS / BSD
    out: Snapshot = {}
    try:
        text = subprocess.run(["ps", "-axo", "pid=,ppid=,command="], capture_output=True,
                              text=True, timeout=10, stdin=subprocess.DEVNULL).stdout
    except (OSError, subprocess.SubprocessError):
        return out
    for line in text.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 2 or not parts[0].isdigit():
            continue
        cmd = parts[2] if len(parts) > 2 else ""
        out[int(parts[0])] = Proc(pid=int(parts[0]), ppid=int(parts[1]), ident=f"cmd:{cmd}", cmd=cmd)
    return out


def _windows_snapshot() -> Snapshot:  # pragma: no cover - windows
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_void_p),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    k32.CloseHandle.argtypes = [wintypes.HANDLE]

    def created(pid: int) -> str:
        handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ""
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            if not k32.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
                return ""
            return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
        finally:
            k32.CloseHandle(handle)

    out: Snapshot = {}
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snap or snap == wintypes.HANDLE(-1).value:
        return out
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            pid = int(entry.th32ProcessID)
            exe = entry.szExeFile
            out[pid] = Proc(pid=pid, ppid=int(entry.th32ParentProcessID),
                            ident=f"{exe}:{created(pid)}", cmd=exe)
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return out


def snapshot() -> Snapshot:
    """The machine's process table as this platform can read it; empty when
    it cannot be read at all."""
    try:
        if sys.platform == "win32":
            return _windows_snapshot()
        if os.path.isdir("/proc/self"):
            return _linux_snapshot()
        return _ps_snapshot()
    except Exception:
        log.exception("process snapshot failed")
        return {}


# ---------------------------------------------------------------------------
# tracker


def _default_path() -> Path:
    return paths.daemon_dir() / "session_procs.json"


def _default_kill(pids: List[int]) -> None:
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    if sys.platform == "win32":  # os.kill above is TerminateProcess already
        return
    deadline = time.monotonic() + KILL_GRACE
    left = list(pids)
    while left and time.monotonic() < deadline:
        time.sleep(0.1)
        left = [p for p in left if _alive(p)]
    for pid in left:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    # A zombie answers signal 0; it is not running anything.
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            stat = fh.read()
        return stat[stat.rfind(b")") + 2:][:1] != b"Z"
    except OSError:
        return True


class NotAMember(LookupError):
    """The pid is not a live process this session's commands started."""


class ProcTracker:
    def __init__(self, path: Optional[Path] = None, *,
                 snapshot_fn: Callable[[], Snapshot] = snapshot,
                 kill_fn: Callable[[List[int]], None] = _default_kill,
                 self_pid: Optional[int] = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self.snapshot_fn = snapshot_fn
        self.kill_fn = kill_fn
        self.self_pid = os.getpid() if self_pid is None else self_pid
        self.clock = clock
        self._lock = threading.RLock()
        #: session -> pid -> member record (pid, ppid, ident, cmd, label, since)
        self._members: Dict[str, Dict[int, dict]] = {}
        #: session -> label of the command last adopted for it: what a
        #: process found by its owner tag alone is said to be started by.
        self._labels: Dict[str, str] = {}
        self._load()

    # -- persistence -------------------------------------------------------

    def _load(self) -> None:
        if self.path is None:
            return
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for session, members in (doc.get("sessions") or {}).items():
            self._members[session] = {int(m["pid"]): dict(m) for m in members
                                      if isinstance(m, dict) and "pid" in m}
        self._labels.update({str(k): str(v) for k, v in (doc.get("labels") or {}).items()})

    def _save(self) -> None:
        if self.path is None:
            return
        doc = {"sessions": {s: sorted(m.values(), key=lambda r: r["pid"])
                            for s, m in self._members.items() if m},
               "labels": dict(self._labels)}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            log.debug("could not persist %s", self.path, exc_info=True)

    # -- tracking ----------------------------------------------------------

    def adopt(self, session: str, pid: int, *, label: str, command: str = "",
              snap: Optional[Snapshot] = None) -> None:
        """Start tracking ``pid`` (a command the daemon just started for
        ``session``) and everything it goes on to start."""
        if not session or int(pid) == self.self_pid:
            return
        snap = self.snapshot_fn() if snap is None else snap
        proc = snap.get(int(pid))
        with self._lock:
            self._labels[session] = label
            self._members.setdefault(session, {})[int(pid)] = {
                "pid": int(pid),
                "ppid": proc.ppid if proc else 0,
                "ident": proc.ident if proc else "",
                "cmd": (proc.cmd if proc else "") or command,
                "label": label,
                "since": self.clock(),
                "root": True,
            }
            self._save()
        self.refresh(snap)

    def has_members(self) -> bool:
        with self._lock:
            return any(self._members.values())

    def refresh(self, snap: Optional[Snapshot] = None) -> None:
        """One snapshot: drop dead members, add new descendants and tagged
        processes, persist when anything changed."""
        snap = self.snapshot_fn() if snap is None else snap
        changed = False
        with self._lock:
            # A process carrying a session's owner tag belongs to it even when
            # nothing of that session is tracked any more (the command that
            # started it exited before a snapshot saw the two together).
            tagged_owners = {p.owner for p in snap.values() if p.owner}
            for session in list(self._members) + sorted(tagged_owners - set(self._members)):
                members = self._members.setdefault(session, {})
                for pid, rec in list(members.items()):
                    proc = snap.get(pid)
                    if proc is None or (rec.get("ident") and proc.ident != rec["ident"]):
                        del members[pid]
                        changed = True
                    elif not rec.get("ident"):
                        rec["ident"] = proc.ident
                        changed = True
                label_of = {pid: rec.get("label", "") for pid, rec in members.items()}
                grew = True
                while grew:
                    grew = False
                    for proc in snap.values():
                        if proc.pid in members or proc.pid == self.self_pid:
                            continue
                        tagged = proc.owner == session
                        if proc.ppid in members or tagged:
                            parent_label = label_of.get(proc.ppid)
                            members[proc.pid] = {
                                "pid": proc.pid, "ppid": proc.ppid, "ident": proc.ident,
                                "cmd": proc.cmd, "since": self.clock(),
                                "label": parent_label if parent_label is not None else (
                                    self._labels.get(session, "") if tagged else ""),
                            }
                            label_of[proc.pid] = members[proc.pid]["label"]
                            grew = changed = True
                if not members:
                    del self._members[session]
                    if self._labels.pop(session, None) is not None:
                        changed = True
            if changed:
                self._save()

    # -- reading -----------------------------------------------------------

    def listing(self, session: str) -> List[dict]:
        """The session's live processes as the UI shows them: one row per
        process whose parent is not itself a member (the top of each tree
        still running), with the count of members under it."""
        with self._lock:
            members = dict(self._members.get(session) or {})
        rows = []
        for pid, rec in members.items():
            if rec.get("ppid") in members:
                continue
            under = len(self._subtree(members, pid)) - 1
            rows.append({"pid": pid, "cmd": rec.get("cmd", ""), "label": rec.get("label", ""),
                         "since": rec.get("since"), "children": under})
        rows.sort(key=lambda r: (r.get("since") or 0, r["pid"]))
        return rows

    def summary(self) -> Dict[str, List[dict]]:
        with self._lock:
            names = [s for s, m in self._members.items() if m]
        return {s: self.listing(s) for s in names}

    @staticmethod
    def _subtree(members: Dict[int, dict], top: int) -> List[int]:
        out = [top]
        frontier = [top]
        while frontier:
            parent = frontier.pop()
            for pid, rec in members.items():
                if rec.get("ppid") == parent and pid not in out:
                    out.append(pid)
                    frontier.append(pid)
        return out

    # -- killing -----------------------------------------------------------

    def kill(self, session: str, pid: int) -> List[int]:
        """End ``pid`` and every member under it. Refused unless ``pid`` is a
        live member of ``session`` -- this never signals an arbitrary pid."""
        self.refresh()
        with self._lock:
            members = dict(self._members.get(session) or {})
        if int(pid) not in members:
            raise NotAMember(f"pid {pid} is not a running process of session {session!r}")
        targets = self._subtree(members, int(pid))
        # Leaves first, so a parent does not respawn a child we just ended.
        self.kill_fn(list(reversed(targets)))
        self.refresh()
        return targets


_TRACKERS: Dict[str, ProcTracker] = {}
_TRACKERS_LOCK = threading.Lock()


def tracker() -> ProcTracker:
    """The tracker of this daemon instance (keyed by its state file, so a test
    that moves the launcher home gets a fresh one)."""
    path = _default_path()
    with _TRACKERS_LOCK:
        found = _TRACKERS.get(str(path))
        if found is None:
            found = _TRACKERS[str(path)] = ProcTracker(path)
        return found


def owner_env(env: Dict[str, str], session: str) -> Dict[str, str]:
    """``env`` with the owner tag set for ``session`` (removed when empty)."""
    env = dict(env)
    if session:
        env[OWNER_ENV] = session
    else:
        env.pop(OWNER_ENV, None)
    return env


def strip_owner(env: Optional[Dict[str, str]]) -> Dict[str, str]:
    """A copy of ``env`` (default: this process's) without the owner tag."""
    out = dict(os.environ if env is None else env)
    out.pop(OWNER_ENV, None)
    return out


class RefreshClock:
    """Re-read the process table every ``poll`` seconds while anything is
    tracked, so the session list (which only reads the tracker's memory)
    drops a process soon after it exits and picks up what it started."""

    def __init__(self, *, poll: float = 3.0,
                 get: Callable[[], ProcTracker] = tracker) -> None:
        self.poll = poll
        self.get = get
        self._task = None

    def start(self) -> None:
        import asyncio

        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def shutdown(self) -> None:
        import asyncio
        import contextlib

        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _run(self) -> None:
        import asyncio

        while True:
            await asyncio.sleep(self.poll)
            try:
                found = self.get()
                if found.has_members():
                    await asyncio.to_thread(found.refresh)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("session process refresh failed")
