"""Best-effort CPU scheduling for session child processes.

On Windows a harness process without an active terminal viewer runs below
normal priority. Its ordinary children inherit the priority class. POSIX
niceness cannot be restored without privileges, so it is deliberately left
unchanged there.
"""

from __future__ import annotations

import ctypes
import os

_PROCESS_SET_INFORMATION = 0x0200
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_NORMAL_PRIORITY_CLASS = 0x0020
_BELOW_NORMAL_PRIORITY_CLASS = 0x4000


def set_background(pid: int) -> bool:
    """Move one Windows session process to below-normal CPU priority."""
    return _set_windows_priority(pid, _BELOW_NORMAL_PRIORITY_CLASS)


def set_foreground(pid: int) -> bool:
    """Restore one Windows session process to normal CPU priority."""
    return _set_windows_priority(pid, _NORMAL_PRIORITY_CLASS)


def _set_windows_priority(pid: int, priority_class: int) -> bool:
    if os.name != "nt" or pid <= 0:
        return False
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.SetPriorityClass.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        handle = kernel32.OpenProcess(
            _PROCESS_SET_INFORMATION | _PROCESS_QUERY_LIMITED_INFORMATION,
            False,
            pid,
        )
        if not handle:
            return False
        try:
            return bool(kernel32.SetPriorityClass(handle, priority_class))
        finally:
            kernel32.CloseHandle(handle)
    except (AttributeError, OSError):
        return False
