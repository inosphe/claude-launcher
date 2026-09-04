"""Windows Job Objects: end a session's whole process tree, not just its root.

The Unix backend makes the PTY child a session leader and signals the
process group, so everything the harness started goes down with it. Windows
has no process groups worth the name: ``TerminateProcess`` reaches exactly
one pid, and ``taskkill /T`` walks parent links that break as soon as an
intermediate parent has already exited. What Windows has instead is the Job
Object -- a kernel container every descendant inherits at creation, whose
membership survives the death of any intermediate parent.

One job per PTY child, created with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``:

* :meth:`ProcessJob.terminate` ends every member at once (the forced kill);
* :meth:`ProcessJob.close` drops the last handle, which the flag turns into
  the same kill -- so a child that exited on its own, leaving a dev server
  or a ``tail -f`` behind, still takes its leftovers with it when the
  session's PTY is closed;
* the daemon dying (crash, kill) closes the handle too, so no session tree
  outlives the daemon that was driving it.

The child is assigned *after* it is spawned (pywinpty owns the
``CreateProcess`` call), so a grandchild forked in the first milliseconds
would be missed. In practice the harness spends far longer than that
starting up before it launches anything, and the alternative -- reaching
into pywinpty's spawn -- is not worth the coupling.

Assignment can be refused: a daemon that is itself inside a job forbidding
nested jobs (rare since Windows 8) gets ``ERROR_ACCESS_DENIED``. That is
logged once per daemon and the session runs without a job, exactly as before
this module existed -- a session that starts is worth more than one that
does not.
"""

from __future__ import annotations

import logging
import sys
from typing import Optional

log = logging.getLogger("claunch.daemon.win_job")

_warned_unavailable = False


def _kernel32():
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    k32.SetInformationJobObject.restype = wintypes.BOOL
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k32.AssignProcessToJobObject.restype = wintypes.BOOL
    k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k32.TerminateJobObject.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    return k32


def _extended_limit_struct():
    import ctypes
    from ctypes import wintypes

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
            ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    return JOBOBJECT_EXTENDED_LIMIT_INFORMATION


_JobObjectExtendedLimitInformation = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100


class ProcessJob:
    """A kill-on-close job holding one PTY child and everything under it."""

    def __init__(self, handle, k32) -> None:
        self._handle = handle
        self._k32 = k32

    @classmethod
    def for_pid(cls, pid: int) -> Optional["ProcessJob"]:
        """Create a job and put ``pid`` in it; ``None`` when that cannot be
        done here (not Windows, or the OS refused). Never raises."""
        global _warned_unavailable
        if sys.platform != "win32":
            return None
        import ctypes

        try:
            k32 = _kernel32()
            job = k32.CreateJobObjectW(None, None)
            if not job:
                raise OSError(ctypes.get_last_error(), "CreateJobObjectW")
            info = _extended_limit_struct()()
            info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not k32.SetInformationJobObject(
                job,
                _JobObjectExtendedLimitInformation,
                ctypes.byref(info),
                ctypes.sizeof(info),
            ):
                err = ctypes.get_last_error()
                k32.CloseHandle(job)
                raise OSError(err, "SetInformationJobObject")
            proc = k32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
            if not proc:
                err = ctypes.get_last_error()
                k32.CloseHandle(job)
                raise OSError(err, "OpenProcess")
            ok = k32.AssignProcessToJobObject(job, proc)
            err = ctypes.get_last_error()
            k32.CloseHandle(proc)
            if not ok:
                k32.CloseHandle(job)
                raise OSError(err, "AssignProcessToJobObject")
            return cls(job, k32)
        except Exception as exc:
            if not _warned_unavailable:
                _warned_unavailable = True
                log.warning(
                    "job object unavailable for pid %s (%s); session trees "
                    "will not be ended as a unit",
                    pid,
                    exc,
                )
            return None

    def terminate(self, exit_code: int = 1) -> None:
        """End every process in the job right now."""
        if self._handle is None:
            return
        try:
            self._k32.TerminateJobObject(self._handle, exit_code)
        except Exception:
            pass

    def close(self) -> None:
        """Drop the handle -- with kill-on-close, this ends any member still
        running. Safe to call more than once."""
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            self._k32.CloseHandle(handle)
        except Exception:
            pass

    def __del__(self) -> None:  # pragma: no cover - GC safety net
        try:
            self.close()
        except Exception:
            pass
