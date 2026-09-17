"""Daemon-host text clipboard history, bounded and kept only in memory."""
from __future__ import annotations

import asyncio
import contextlib
import ctypes
import sys
from datetime import datetime, timezone

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
