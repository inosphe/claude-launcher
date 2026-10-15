"""Hide Windows ``reg.exe`` from new sessions under WSL (``daemon.wsl_hide_reg_exe``).

Only WSL needs this. Claude Code, started under WSL, runs
``/mnt/c/Windows/System32/reg.exe query HKLM|HKCU\\SOFTWARE\\Policies\\ClaudeCode``
twice on every launch to look for a Windows managed-settings policy. Each call
goes through WSL interop: ``wslhost.exe`` on the Windows side starts the
program and relays its I/O. In WSL 2.7.11 that relay occasionally fails to
finish after ``reg.exe`` exits (measured on 2026-10-14: 16 times in 24 hours
of heavy session churn), and the thread-pool worker that served it keeps
re-issuing a read that returns at once -- one host CPU core at 100% each,
until ``wsl --shutdown``. Claude Code has no switch for the probe: it decides
"this is WSL" from ``WSL_DISTRO_NAME`` or ``/proc/version`` and calls
``reg.exe`` by absolute path, so neither the environment nor ``PATH`` can
turn it off.

With the setting on, the daemon starts each new session through
:func:`main` (``python -m claude_launcher.wsl_reg_stub -- <argv>``). It
unshares a user namespace and a mount namespace, bind-mounts a two-line
``exit 1`` script over ``reg.exe`` inside them, and ``exec``\\ s the session's
own command -- so no extra process is left in the tree, and nothing outside the
session sees the change. ``exit 1`` is what ``reg.exe query`` answers for a key
that does not exist, so Claude Code reads "no policy", as it does on a machine
without one, and no Windows process is started.

Costs, which is why the setting is off by default:

* ``sudo`` does not work inside such a session. In the user namespace only the
  session's own uid is mapped; files owned by root (``/etc/sudo.conf``,
  ``/usr/bin/sudo``) appear owned by ``nobody`` and sudo refuses to run.
* A Claude Code managed policy deployed through the Windows registry is not
  read by these sessions.
* Other Windows programs (``cmd.exe``, ``powershell.exe``) still run.

The setting is read at each spawn; sessions already running keep the
namespace they were started with. Any failure in the shim (no user-namespace
support, a missing ``reg.exe``) is reported on the session's terminal and the
command is started without the stub.
"""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path
from typing import List, Mapping, Optional, Sequence

#: ``daemon`` setting that turns the stub on.
SETTING = "wsl_hide_reg_exe"

#: Where WSL exposes Windows' ``reg.exe``. Claude Code calls this exact path.
REG_EXE = "/mnt/c/Windows/System32/reg.exe"

#: The stand-in: the answer ``reg.exe query`` gives for a missing key.
STUB_TEXT = "#!/bin/sh\n# claunch: reg.exe hidden from this session (daemon.wsl_hide_reg_exe)\nexit 1\n"

_CLONE_NEWNS = 0x00020000
_CLONE_NEWUSER = 0x10000000
_MS_BIND = 4096
_MS_REC = 16384
_MS_PRIVATE = 1 << 18


def is_wsl(osrelease: str = "/proc/sys/kernel/osrelease") -> bool:
    """Whether this Linux runs under WSL (its kernel release names Microsoft)."""
    if not sys.platform.startswith("linux"):
        return False
    try:
        return "microsoft" in Path(osrelease).read_text(encoding="utf-8", errors="replace").lower()
    except OSError:
        return False


def enabled(cfg: Mapping) -> bool:
    """``daemon.wsl_hide_reg_exe`` as a bool (off unless set true)."""
    return cfg.get(SETTING) is True


def stub_path() -> Path:
    """The stub script, written beside the daemon's state (rewritten when it differs)."""
    from .daemon import paths

    path = paths.daemon_dir() / "reg-exe-stub.sh"
    try:
        current = path.read_text(encoding="utf-8")
    except OSError:
        current = None
    if current != STUB_TEXT:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
        tmp.write_text(STUB_TEXT, encoding="utf-8")
        tmp.chmod(0o755)
        os.replace(tmp, path)
    return path


def wrap_argv(argv: Sequence[str], cfg: Mapping) -> List[str]:
    """``argv`` as the session should be spawned: through the shim when the
    setting is on and this is WSL with ``reg.exe`` present, unchanged otherwise."""
    argv = list(argv)
    if (not argv or not enabled(cfg) or not hasattr(os, "unshare")  # 3.12+
            or not is_wsl() or not os.path.exists(REG_EXE)):
        return argv
    try:
        stub = stub_path()
    except OSError:
        return argv
    return [sys.executable, "-m", __name__, "--stub", str(stub), "--", *argv]


def spawn_argv(argv: Sequence[str]) -> List[str]:
    """:func:`wrap_argv` against the config file as it is now (the daemon's
    call at each session spawn). Off Linux it reads nothing."""
    if not sys.platform.startswith("linux"):
        return list(argv)
    from . import store

    try:
        cfg = store.daemon_config()
    except store.StoreError:
        return list(argv)
    return wrap_argv(argv, cfg)


def _libc_mount(source: Optional[bytes], target: bytes, flags: int) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.mount.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p,
                           ctypes.c_ulong, ctypes.c_void_p]
    if libc.mount(source, target, None, flags, None) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), target.decode())


def hide(stub: str, reg_exe: str = REG_EXE) -> None:
    """In this process: new user + mount namespace, ``stub`` bound over ``reg_exe``.

    The user namespace maps only the caller's own uid and gid, so the process
    keeps its identity; it is what lets an unprivileged process create the
    mount namespace. ``mount(8)`` refuses a non-root caller before asking the
    kernel, hence the direct system call.
    """
    uid, gid = os.getuid(), os.getgid()
    os.unshare(_CLONE_NEWUSER | _CLONE_NEWNS)
    Path("/proc/self/setgroups").write_text("deny")
    Path("/proc/self/uid_map").write_text(f"{uid} {uid} 1")
    Path("/proc/self/gid_map").write_text(f"{gid} {gid} 1")
    # Keep the bind mount from propagating back to the parent namespace.
    _libc_mount(None, b"/", _MS_REC | _MS_PRIVATE)
    _libc_mount(stub.encode(), reg_exe.encode(), _MS_BIND)


def main(args: Optional[Sequence[str]] = None) -> int:
    """``--stub PATH -- CMD...``: hide ``reg.exe``, then become ``CMD``."""
    args = list(sys.argv[1:] if args is None else args)
    if len(args) < 4 or args[0] != "--stub" or args[2] != "--":
        print("usage: python -m claude_launcher.wsl_reg_stub --stub PATH -- CMD...",
              file=sys.stderr)
        return 2
    stub, command = args[1], args[3:]
    try:
        hide(stub)
    except OSError as exc:
        print(f"claunch: could not hide reg.exe ({exc}); starting without it",
              file=sys.stderr)
    try:
        os.execvp(command[0], command)
    except OSError as exc:
        print(f"claunch: could not start {command[0]!r}: {exc}", file=sys.stderr)
        return 127


if __name__ == "__main__":
    sys.exit(main())
