"""The clipboard of the machine this daemon runs on.

Two directions, both about the same shared resource.

*Reading* keeps a bounded in-memory history of the host's copied text, which
the web session composer offers back. It never modifies the clipboard. On
Windows it is read in-process; under WSL it is the Windows clipboard, asked of
one long-lived PowerShell; on other Linux it is wl-paste or xclip/xsel.

*Writing* is how an image reaches a harness. A program reading a PTY cannot
be handed an attachment, but a harness does take an image off the clipboard --
so the web session line's image paste stores the file, fills this machine's
clipboard with it, and sends the keystroke that harness reads an image with
(the key comes from the harness declaration, ``image_paste_keys``).

Writing overwrites whatever the person at this keyboard had copied, and every
session on this machine shares the one clipboard. That cost does not go away;
what the caller can do is not interleave two of them, which is why the API
handler holds one lock across the write and the keystroke.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import ctypes
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Mapping, Optional

from aiohttp import web

MAX_TEXT = 100_000
MAX_ITEMS = 30

TOO_LONG = "Clipboard text exceeds the 100,000 character limit"


def read_text(platform: str = sys.platform) -> str:
    """Read the host clipboard's text without modifying it."""
    if platform == "win32":
        return _read_windows()
    if not platform.startswith("linux"):
        raise OSError(f"Host clipboard is not supported on {platform}")
    bridge = _wsl_bridge()
    if bridge is not None:
        return bridge.read()
    return read_command(native_text_command())


def _read_windows():
    """Read Unicode text without modifying the Windows clipboard."""
    from ctypes import wintypes

    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    user.OpenClipboard.argtypes = [wintypes.HWND]
    user.OpenClipboard.restype = wintypes.BOOL
    user.GetClipboardData.argtypes = [wintypes.UINT]
    user.GetClipboardData.restype = wintypes.HANDLE
    kernel.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel.GlobalLock.restype = ctypes.c_void_p
    kernel.GlobalSize.argtypes = [wintypes.HGLOBAL]
    kernel.GlobalSize.restype = ctypes.c_size_t
    kernel.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    if not user.OpenClipboard(None):
        raise OSError("Host clipboard is busy; try Refresh")
    try:
        handle = user.GetClipboardData(13)  # CF_UNICODETEXT
        if not handle:
            return ""
        size = kernel.GlobalSize(handle)
        if size > (MAX_TEXT + 1) * 2:
            raise OSError(TOO_LONG)
        pointer = kernel.GlobalLock(handle)
        if not pointer:
            raise OSError("Host clipboard could not be read")
        try:
            return ctypes.string_at(pointer, size).decode("utf-16-le").split("\0", 1)[0]
        finally:
            kernel.GlobalUnlock(handle)
    finally:
        user.CloseClipboard()


# ---- reading text on Linux ------------------------------------------------
#
# Under WSL the clipboard the person copies into is the Windows one: the
# browser, the terminal and the editor all run on the Windows side. It is
# reached through powershell.exe, which WSL's interop starts as a Windows
# process. Starting one costs about 0.3 s and the history samples every
# second, so one PowerShell is kept running and asked a line per read.
#
# Elsewhere on Linux the clipboard belongs to the display server, and the
# tools that read it are the same ones the image route writes with.


#: Where a Windows install keeps Windows PowerShell, relative to its drive.
_POWERSHELL = "Windows/System32/WindowsPowerShell/v1.0/powershell.exe"


def _drvfs_roots(mounts: str) -> List[str]:
    """Mount points of the Windows drives WSL has mounted (``/mnt/c``...)."""
    try:
        lines = Path(mounts).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    roots = []
    for line in lines:
        fields = line.split()
        if len(fields) >= 4 and (fields[2] == "drvfs" or "aname=drvfs" in fields[3]):
            # /proc/mounts escapes a space in a path as \040.
            roots.append(fields[1].replace("\\040", " "))
    return roots


def wsl_powershell(
    *,
    osrelease: str = "/proc/sys/kernel/osrelease",
    binfmt: str = "/proc/sys/fs/binfmt_misc",
    mounts: str = "/proc/mounts",
    which: Callable[[str], Optional[str]] = shutil.which,
) -> Optional[str]:
    """powershell.exe as this WSL instance can start it, or None.

    None when this is not WSL, when WSL's interop (the thing that starts a
    Windows program from Linux) is switched off, or when no Windows drive
    holds PowerShell. The daemon is often started without the Windows
    directories on its PATH, so the drives are searched as well.
    """
    try:
        release = Path(osrelease).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if "microsoft" not in release.lower():
        return None
    interop = sorted(Path(binfmt).glob("WSLInterop*")) if Path(binfmt).is_dir() else []
    enabled = False
    for entry in interop:
        with contextlib.suppress(OSError):
            if entry.read_text(encoding="utf-8", errors="replace").startswith("enabled"):
                enabled = True
    if not enabled:
        return None
    found = which("powershell.exe")
    if found:
        return found
    for root in _drvfs_roots(mounts):
        candidate = Path(root) / _POWERSHELL
        if candidate.is_file():
            return str(candidate)
    return None


#: The Windows side of the WSL bridge. One line in, one line out, for as
#: long as stdin stays open. The reply names what it carries, so a line
#: PowerShell prints on its own is never taken for an answer. ``same`` spares
#: the pipe a copy of text the reader already holds; its twin is
#: :attr:`PowerShellBridge._last`, and both start empty with the process.
_BRIDGE_SCRIPT = """\
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$utf8 = New-Object System.Text.UTF8Encoding $false
function Reply($kind, $text) {
  if ($null -ne $text) { $kind += ':' + [Convert]::ToBase64String($utf8.GetBytes($text)) }
  [Console]::Out.WriteLine('clip:' + $kind)
  [Console]::Out.Flush()
}
try {
  Add-Type -AssemblyName System.Windows.Forms
} catch {
  Reply 'error' $_.Exception.Message
  exit 1
}
$last = $null
while ($null -ne [Console]::In.ReadLine()) {
  try {
    $text = [System.Windows.Forms.Clipboard]::GetText([System.Windows.Forms.TextDataFormat]::UnicodeText)
    if ($text.Length -gt %(limit)d) { Reply 'long' $null }
    elseif ($text -ceq $last) { Reply 'same' $null }
    else { $last = $text; Reply 'text' $text }
  } catch {
    Reply 'error' $_.Exception.Message
  }
}
"""


def bridge_command(powershell: str) -> List[str]:
    """The argv that starts the Windows side of the bridge."""
    script = _BRIDGE_SCRIPT % {"limit": MAX_TEXT}
    # -EncodedCommand, because WSL rebuilds a Windows command line from the
    # argv and a script full of quotes and $ does not survive that intact.
    # -STA because the clipboard API is single-threaded-apartment only.
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return [powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-STA",
            "-EncodedCommand", encoded]


class PowerShellBridge:
    """One PowerShell on the Windows side, asked for the clipboard per read.

    A read that is not answered within ``timeout`` kills the process: its
    late answer would otherwise be taken for the next read's. The next read
    starts a new one, but not before ``retry_after`` has passed since a
    failure, so a PowerShell that cannot start is not restarted every second.
    """

    def __init__(self, argv: List[str], *, timeout: float = 10.0,
                 retry_after: float = 10.0, popen=subprocess.Popen,
                 clock: Callable[[], float] = time.monotonic):
        self.argv = argv
        self.timeout = timeout
        self.retry_after = retry_after
        self._popen = popen
        self._clock = clock
        self._lock = threading.Lock()
        self._proc = None
        self._lines: Optional[queue.Queue] = None
        self._last = ""
        self._failed: Optional[str] = None
        self._retry_at = 0.0

    def read(self) -> str:
        with self._lock:
            proc, lines = self._running()
            try:
                proc.stdin.write(b"read\n")
                proc.stdin.flush()
            except OSError as exc:
                raise self._fail(f"powershell.exe stopped taking requests: {exc}")
            deadline = self._clock() + self.timeout
            while True:
                try:
                    line = lines.get(timeout=max(deadline - self._clock(), 0))
                except queue.Empty:
                    raise self._fail(
                        f"powershell.exe did not answer within {self.timeout:g}s")
                if line is None:
                    raise self._fail(
                        f"powershell.exe exited (code {proc.poll()}) without answering")
                answer = self._answer(line.strip())
                if answer is not None:
                    return answer

    def _answer(self, line: bytes) -> Optional[str]:
        if not line.startswith(b"clip:"):
            return None  # not a reply; the next line may be
        kind, _, payload = line[5:].decode("ascii", "replace").partition(":")
        if kind == "same":
            return self._last
        if kind == "long":
            raise OSError(TOO_LONG)
        try:
            text = base64.b64decode(payload, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeError) as exc:
            raise self._fail(f"powershell.exe sent an unreadable reply: {exc}")
        if kind == "text":
            self._last = text
            return text
        raise OSError(f"Host clipboard could not be read: {text}")

    def _running(self):
        if self._proc is not None:
            code = self._proc.poll()
            if code is None:
                return self._proc, self._lines
            # It ended between reads -- the script exits when it cannot load
            # the clipboard API, after saying so in its last reply.
            raise self._fail(f"powershell.exe exited (code {code})")
        if self._failed is not None and self._clock() < self._retry_at:
            raise OSError(self._failed)
        try:
            proc = self._popen(self.argv, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError as exc:
            raise self._fail(f"powershell.exe could not be started: {exc}")
        lines: queue.Queue = queue.Queue()
        threading.Thread(target=_pump, args=(proc.stdout, lines),
                         name="clipboard-powershell", daemon=True).start()
        self._proc, self._lines, self._last = proc, lines, ""
        self._failed = None
        return proc, lines

    def _fail(self, reason: str) -> OSError:
        self._stop()
        self._failed = reason
        self._retry_at = self._clock() + self.retry_after
        return OSError(reason)

    def _stop(self):
        proc, self._proc, self._lines = self._proc, None, None
        if proc is None:
            return
        # stdin first: an open stdin is what keeps the script's loop going.
        # stdout is left to the pump thread, which is blocked reading it and
        # closes it at its end.
        with contextlib.suppress(Exception):
            proc.stdin.close()
        with contextlib.suppress(OSError):
            proc.kill()
        with contextlib.suppress(Exception):
            proc.wait(timeout=5)

    def close(self):
        with self._lock:
            self._stop()


def _pump(stream, lines: queue.Queue):
    """Hand every line of ``stream`` to ``lines``, then None at its end."""
    with contextlib.suppress(Exception):
        for line in iter(stream.readline, b""):
            lines.put(line)
    lines.put(None)
    with contextlib.suppress(Exception):
        stream.close()


_bridge: Optional[PowerShellBridge] = None
_bridge_known = False
_bridge_guard = threading.Lock()


def _wsl_bridge() -> Optional[PowerShellBridge]:
    """The process-wide bridge, or None when this Linux is not WSL."""
    global _bridge, _bridge_known
    with _bridge_guard:
        if not _bridge_known:
            powershell = wsl_powershell()
            _bridge = PowerShellBridge(bridge_command(powershell)) if powershell else None
            _bridge_known = True
        return _bridge


def close_bridge():
    """End the bridge's PowerShell; the next read starts another."""
    with _bridge_guard:
        bridge = _bridge
    if bridge is not None:
        bridge.close()


#: What a clipboard tool says when there is simply no text to give. Those
#: are an empty clipboard, not a failure.
_NO_TEXT = ("nothing is copied", "no suitable type", "not available")


def native_text_command(
    environ: Optional[Mapping[str, str]] = None,
    which: Callable[[str], Optional[str]] = shutil.which,
) -> List[str]:
    """The command that prints the display server's clipboard text."""
    env = os.environ if environ is None else environ
    if env.get("WAYLAND_DISPLAY") and which("wl-paste"):
        return ["wl-paste", "--no-newline", "--type", "text"]
    if env.get("DISPLAY"):
        if which("xclip"):
            return ["xclip", "-selection", "clipboard", "-out"]
        if which("xsel"):
            return ["xsel", "--clipboard", "--output"]
    if not env.get("WAYLAND_DISPLAY") and not env.get("DISPLAY"):
        raise OSError("Host clipboard: the daemon has no display to read one from "
                      "(neither WAYLAND_DISPLAY nor DISPLAY is set)")
    raise OSError("Host clipboard: install wl-clipboard (Wayland) or xclip/xsel (X11) "
                  "on the daemon's machine")


def read_command(argv: List[str], *, timeout: float = 5.0, run=subprocess.run) -> str:
    """Run a clipboard tool and return the text it printed."""
    try:
        done = run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise OSError(f"{argv[0]} did not finish within {timeout:g}s")
    except OSError as exc:
        raise OSError(f"{argv[0]} could not be started: {exc}")
    if done.returncode != 0:
        err = (done.stderr or b"").decode("utf-8", "replace").strip()
        if any(marker in err.lower() for marker in _NO_TEXT):
            return ""
        lines = err.splitlines()
        raise OSError(f"{argv[0]}: {lines[-1] if lines else f'exit {done.returncode}'}")
    if len(done.stdout) > MAX_TEXT * 4:  # no UTF-8 character is longer
        raise OSError(TOO_LONG)
    return done.stdout.decode("utf-8", "replace")


class History:
    def __init__(self, reader=read_text):
        self.reader = reader
        self.items = []
        self.last = None
        self.error = None
        self.lock = asyncio.Lock()
        self.serial = 0

    async def sample(self):
        async with self.lock:
            try:
                text = await asyncio.to_thread(self.reader)
                if len(text) > MAX_TEXT:
                    raise OSError(TOO_LONG)
                self.error = None
            except (OSError, UnicodeError) as exc:
                self.error = str(exc)
                return
            if text == self.last:
                return
            self.last = text
            if not text:
                return
            self.serial += 1
            self.items = [item for item in self.items if item["text"] != text]
            self.items.insert(0, {"id": str(self.serial), "text": text,
                                  "copied_at": datetime.now(timezone.utc).isoformat()})
            del self.items[MAX_ITEMS:]

    async def watch(self):
        while True:
            await self.sample()
            await asyncio.sleep(1)


async def lifecycle(app):
    task = asyncio.create_task(app["clipboard"].watch())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await asyncio.to_thread(close_bridge)


async def get_history(request):
    history = request.app["clipboard"]
    await history.sample()
    return web.json_response({"items": history.items, "error": history.error},
                             headers={"Cache-Control": "no-store"})


async def delete_history(request):
    history = request.app["clipboard"]
    async with history.lock:
        item_id = request.match_info.get("item_id")
        history.items = [item for item in history.items if item["id"] != item_id] if item_id else []
    return web.json_response({"ok": True}, headers={"Cache-Control": "no-store"})


def install(app):
    app["clipboard"] = History()
    app.cleanup_ctx.append(lifecycle)
    app.router.add_get("/api/clipboard", get_history)
    app.router.add_delete("/api/clipboard", delete_history)
    app.router.add_delete("/api/clipboard/{item_id}", delete_history)


# ---- writing an image onto this machine's clipboard ----------------------
#
# There is no portable way to do it, so each platform gets its own command:
#
#   Windows  Windows PowerShell in STA mode, Clipboard::SetImage
#   macOS    osascript, `set the clipboard to (read ... as <class>)`
#   Linux    wl-copy under Wayland, otherwise xclip
#
# None of them is guaranteed to be installed, and the Windows one converts the
# image to a device-independent bitmap on the way in, so an alpha channel does
# not survive. Failures come back as a reason rather than an exception the
# caller has to translate: by the time this runs the file is already stored,
# and "stored but not delivered, because X" is a different answer from
# "nothing happened".


class ClipboardError(RuntimeError):
    """The image could not be put on this machine's clipboard."""


#: AppleScript class codes for the types the paste route accepts. ``webp`` is
#: absent because the macOS pasteboard has no class for it -- naming one that
#: does not exist would fail inside osascript with a parse error instead of
#: here with a reason the caller can show.
_OSA_CLASS = {
    "image/png": "«class PNGf»",
    "image/jpeg": "«class JPEG»",
    "image/gif": "«class GIFf»",
}


@dataclass(frozen=True)
class ClipboardCommand:
    """What to run, and whether the image goes in through stdin."""

    argv: List[str]
    #: The file to feed the command on stdin, for the ones that read there
    #: (``wl-copy``) rather than taking a path.
    stdin_path: Optional[Path] = None


def _ps_quote(value: str) -> str:
    """Quote for a PowerShell single-quoted string (doubling is the escape)."""
    return "'" + value.replace("'", "''") + "'"


def _windows_image(path: Path) -> ClipboardCommand:
    script = (
        "Add-Type -AssemblyName System.Windows.Forms,System.Drawing; "
        f"$img = [System.Drawing.Image]::FromFile({_ps_quote(str(path))}); "
        "[System.Windows.Forms.Clipboard]::SetImage($img); "
        "$img.Dispose()"
    )
    # Windows PowerShell rather than pwsh: the clipboard APIs are
    # single-threaded-apartment only, powershell.exe takes -STA and is present
    # on every Windows install, and pwsh is neither guaranteed to be there nor
    # consistent about that flag across its versions.
    return ClipboardCommand(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-STA", "-Command", script]
    )


def _macos_image(path: Path, media_type: str) -> ClipboardCommand:
    klass = _OSA_CLASS.get(media_type)
    if klass is None:
        raise ClipboardError(
            f"the macOS pasteboard has no class for {media_type} "
            f"({', '.join(sorted(_OSA_CLASS))} are the ones it takes)"
        )
    posix = str(path).replace("\\", "\\\\").replace('"', '\\"')
    return ClipboardCommand(
        ["osascript", "-e",
         f'set the clipboard to (read (POSIX file "{posix}") as {klass})']
    )


def _linux_image(path: Path, media_type: str) -> ClipboardCommand:
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy"):
        return ClipboardCommand(["wl-copy", "--type", media_type], stdin_path=path)
    if shutil.which("xclip"):
        return ClipboardCommand(
            ["xclip", "-selection", "clipboard", "-t", media_type, "-i", str(path)]
        )
    raise ClipboardError(
        "neither wl-copy nor xclip is installed on the daemon's machine, "
        "so there is no way to reach its clipboard"
    )


def image_command(
    path: Path, media_type: str, *, platform: str = sys.platform
) -> ClipboardCommand:
    """The command that puts ``path`` on this machine's clipboard.

    Raises :class:`ClipboardError` when the platform has no route for this
    image type, or when the tool that would carry it is not installed. Split
    out from :func:`put_image` so the mapping can be tested without running
    anything -- running it would write to the clipboard of whatever machine
    the suite is on, which is shared with the person using it.
    """
    if platform == "win32":
        return _windows_image(path)
    if platform == "darwin":
        return _macos_image(path, media_type)
    if platform.startswith("linux"):
        return _linux_image(path, media_type)
    raise ClipboardError(f"no clipboard route is known for platform {platform!r}")


async def put_image(path: Path, media_type: str, *, timeout: float = 15.0) -> None:
    """Put the image at ``path`` on this machine's clipboard.

    Raises :class:`ClipboardError` with a reason a reader can act on: the
    caller shows it in the web session line, where the person who pressed the
    key is looking.
    """
    cmd = image_command(Path(path), media_type)
    stdin = None
    try:
        if cmd.stdin_path is not None:
            stdin = cmd.stdin_path.open("rb")
        proc = await asyncio.create_subprocess_exec(
            *cmd.argv,
            stdin=stdin if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise ClipboardError(
            f"{cmd.argv[0]} is not installed on the daemon's machine"
        ) from exc
    except OSError as exc:
        raise ClipboardError(f"{cmd.argv[0]} could not be started: {exc}") from exc
    finally:
        # The child holds its own duplicate of the descriptor by now, so this
        # closes the parent's copy and not the child's input.
        if stdin is not None:
            stdin.close()
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        proc.kill()
        # Reaped, not just killed. A kill only asks; until the process is
        # waited for, the child stays around and its transport stays open,
        # and on Windows a loop closing over a live subprocess transport
        # takes the whole process down with it.
        with contextlib.suppress(Exception):
            await proc.communicate()
        raise ClipboardError(f"{cmd.argv[0]} did not finish within {timeout:g}s") from exc
    if proc.returncode != 0:
        lines = (err or b"").decode("utf-8", "replace").strip().splitlines()
        # The last line, because that is where a tool puts what went wrong.
        tail = lines[-1] if lines else f"exit {proc.returncode}"
        raise ClipboardError(f"{cmd.argv[0]}: {tail}")
