"""The clipboard of the machine this daemon runs on.

Two directions, both about the same shared resource.

*Reading* keeps a bounded in-memory history of the host's copied text, which
the web session composer offers back. It never modifies the clipboard.

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
import contextlib
import ctypes
import os
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from aiohttp import web

MAX_TEXT = 100_000
MAX_ITEMS = 30


def read_text():
    """Read Unicode text without modifying the Windows clipboard."""
    if sys.platform != "win32":
        raise OSError("Host clipboard is currently supported on Windows only")
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
            raise OSError("Clipboard text exceeds the 100,000 character limit")
        pointer = kernel.GlobalLock(handle)
        if not pointer:
            raise OSError("Host clipboard could not be read")
        try:
            return ctypes.string_at(pointer, size).decode("utf-16-le").split("\0", 1)[0]
        finally:
            kernel.GlobalUnlock(handle)
    finally:
        user.CloseClipboard()


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
                    raise OSError("Clipboard text exceeds the 100,000 character limit")
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
        raise ClipboardError(f"{cmd.argv[0]} did not finish within {timeout:g}s") from exc
    if proc.returncode != 0:
        lines = (err or b"").decode("utf-8", "replace").strip().splitlines()
        # The last line, because that is where a tool puts what went wrong.
        tail = lines[-1] if lines else f"exit {proc.returncode}"
        raise ClipboardError(f"{cmd.argv[0]}: {tail}")
