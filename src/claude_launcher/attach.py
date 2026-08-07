"""Attach the current terminal to a managed session (tmux-style).

``claunch attach <session>`` puts the local terminal into raw mode and bridges
it to the daemon's terminal WebSocket — the same endpoint the web dashboard
uses — mirroring the session 1:1: keystrokes go to the PTY, PTY output paints
locally, and the session is resized to the attaching terminal (and follows it
while attached). ``Ctrl+]`` detaches; the session keeps running in the daemon,
exactly like detaching from tmux.

Kept import-light for the CLI: aiohttp is only imported once an attach starts.
"""

from __future__ import annotations

import asyncio
import codecs
import json
import shutil
import sys
import threading
from typing import Optional, Tuple

#: Ctrl+] — telnet's escape key; no common TUI binds it, so it is safe to
#: reserve as the detach key (anything typed after it in the same chunk is
#: dropped, like tmux's prefix).
DETACH_BYTE = b"\x1d"
DETACH_LABEL = "Ctrl+]"

#: Local terminal size poll cadence — there is no SIGWINCH on Windows, so the
#: size is polled on every platform for one concurrency model.
RESIZE_POLL = 0.5

_STDIN_CHUNK = 4096


def split_detach(data: bytes) -> Tuple[bytes, bool]:
    """Payload up to the first detach byte, and whether it was pressed."""
    idx = data.find(DETACH_BYTE)
    if idx < 0:
        return data, False
    return data[:idx], True


def ws_url(base_url: str, name: str) -> str:
    base = base_url.rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://"):]
    return f"{base}/api/sessions/{name}/ws"


# --------------------------------------------------------------------------- #
# local terminal I/O (monkeypatchable seams for tests)
# --------------------------------------------------------------------------- #
def _write_text(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


def _read_stdin() -> bytes:
    """One blocking read of raw keyboard input; b"" on EOF/error."""
    if sys.platform == "win32":
        return _read_stdin_windows()
    import os

    try:
        return os.read(sys.stdin.fileno(), _STDIN_CHUNK)
    except OSError:
        return b""


def _read_stdin_windows() -> bytes:
    # ReadConsoleW (not os.read/ReadFile) so non-ASCII input survives: it
    # returns UTF-16 characters, and with ENABLE_VIRTUAL_TERMINAL_INPUT set
    # arrows/function keys arrive as VT escape sequences in the same stream.
    import ctypes

    k32 = ctypes.windll.kernel32
    handle = k32.GetStdHandle(-10)  # STD_INPUT_HANDLE
    buf = ctypes.create_unicode_buffer(_STDIN_CHUNK)
    n = ctypes.c_uint32()
    ok = k32.ReadConsoleW(handle, buf, _STDIN_CHUNK, ctypes.byref(n), None)
    if not ok or n.value == 0:
        return b""
    return buf[: n.value].encode("utf-8", errors="replace")


class _RawTerminal:
    """Raw local terminal for the duration of an attach; restores on exit."""

    def __enter__(self) -> "_RawTerminal":
        if sys.platform == "win32":
            self._enter_windows()
        else:
            self._enter_unix()
        return self

    def __exit__(self, *exc) -> None:
        if sys.platform == "win32":
            self._exit_windows()
        else:
            self._exit_unix()

    # -- Windows: console modes via ctypes ------------------------------- #
    def _enter_windows(self) -> None:
        import ctypes

        self._k32 = ctypes.windll.kernel32
        self._hin = self._k32.GetStdHandle(-10)
        self._hout = self._k32.GetStdHandle(-11)
        self._old_in = self._console_mode(self._hin)
        self._old_out = self._console_mode(self._hout)
        if self._old_in is not None:
            PROCESSED, LINE, ECHO, MOUSE, QUICK_EDIT = 0x1, 0x2, 0x4, 0x10, 0x40
            EXTENDED_FLAGS, VT_INPUT = 0x80, 0x200
            mode = self._old_in & ~(PROCESSED | LINE | ECHO | MOUSE | QUICK_EDIT)
            # EXTENDED_FLAGS makes the QUICK_EDIT clear stick (mouse selection
            # would otherwise freeze output mid-attach).
            mode |= EXTENDED_FLAGS | VT_INPUT
            self._k32.SetConsoleMode(self._hin, mode)
        if self._old_out is not None:
            PROCESSED_OUT, VT_OUT, NO_AUTO_RETURN = 0x1, 0x4, 0x8
            # The PTY stream carries its own \r\n (ConPTY render / ONLCR), so
            # newline auto-return would double-space it.
            self._k32.SetConsoleMode(
                self._hout, self._old_out | PROCESSED_OUT | VT_OUT | NO_AUTO_RETURN
            )

    def _console_mode(self, handle) -> Optional[int]:
        import ctypes

        mode = ctypes.c_uint32()
        if not self._k32.GetConsoleMode(handle, ctypes.byref(mode)):
            return None
        return mode.value

    def _exit_windows(self) -> None:
        if self._old_in is not None:
            self._k32.SetConsoleMode(self._hin, self._old_in)
        if self._old_out is not None:
            self._k32.SetConsoleMode(self._hout, self._old_out)

    # -- Unix: termios --------------------------------------------------- #
    def _enter_unix(self) -> None:
        import termios
        import tty

        self._fd = sys.stdin.fileno()
        self._old = termios.tcgetattr(self._fd)
        tty.setraw(self._fd)

    def _exit_unix(self) -> None:
        import termios

        termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old)


# --------------------------------------------------------------------------- #
# the bridge
# --------------------------------------------------------------------------- #
async def _attach_async(base_url: str, token: str, name: str) -> dict:
    """Bridge stdin/stdout to the session's terminal WebSocket.

    Returns an outcome dict: ``{"reason": "detach" | "exit" | "closed",
    "code": ...}``. The caller owns terminal modes; this only moves bytes.
    """
    import aiohttp

    loop = asyncio.get_running_loop()
    stdin_q: asyncio.Queue = asyncio.Queue()
    stop = threading.Event()
    outcome = {"reason": "closed"}

    def pump_stdin() -> None:  # runs on a daemon thread; blocking reads
        while not stop.is_set():
            data = _read_stdin()
            try:
                loop.call_soon_threadsafe(stdin_q.put_nowait, data or None)
            except RuntimeError:
                return  # loop already gone
            if not data:
                return

    async def send_pump(ws) -> None:
        while True:
            data = await stdin_q.get()
            if data is None:  # stdin EOF counts as a detach
                outcome["reason"] = "detach"
                await ws.close()
                return
            payload, detach = split_detach(data)
            if payload:
                await ws.send_bytes(payload)
            if detach:
                outcome["reason"] = "detach"
                await ws.close()
                return

    async def resize_pump(ws) -> None:
        last = None
        while True:
            size = shutil.get_terminal_size()
            cur = (size.columns, size.lines)
            if cur != last:
                last = cur
                await ws.send_str(
                    json.dumps({"type": "resize", "cols": cur[0], "rows": cur[1]})
                )
            await asyncio.sleep(RESIZE_POLL)

    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    async with aiohttp.ClientSession() as http:
        async with http.ws_connect(
            ws_url(base_url, name),
            headers={"Authorization": f"Bearer {token}"},
            heartbeat=30,
            max_msg_size=0,
        ) as ws:
            reader = threading.Thread(
                target=pump_stdin, name=f"attach-stdin-{name}", daemon=True
            )
            reader.start()
            tasks = [
                asyncio.ensure_future(send_pump(ws)),
                asyncio.ensure_future(resize_pump(ws)),
            ]
            try:
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.BINARY:
                        _write_text(decoder.decode(msg.data))
                    elif msg.type == aiohttp.WSMsgType.TEXT:
                        try:
                            ctrl = json.loads(msg.data)
                        except ValueError:
                            continue
                        if isinstance(ctrl, dict) and ctrl.get("type") == "exit":
                            outcome["reason"] = "exit"
                            outcome["code"] = ctrl.get("code")
                            break
                    elif msg.type in (
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.CLOSING,
                        aiohttp.WSMsgType.ERROR,
                    ):
                        break
            finally:
                stop.set()
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    return outcome


def attach(client, name: str) -> int:
    """Attach the calling terminal to session ``name``; 0 on detach/exit."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print(
            "error: attach needs an interactive terminal "
            "(use capture-pane/send-keys from scripts)",
            file=sys.stderr,
        )
        return 1
    info = client.get(f"/api/sessions/{name}")
    if info.get("status") == "exited":
        code = info.get("exit_code")
        print(
            f"error: session {name!r} has exited"
            + (f" (exit code {code})" if code is not None else ""),
            file=sys.stderr,
        )
        return 1
    print(f"[claunch] attached to {name!r} — detach: {DETACH_LABEL}", file=sys.stderr)

    outcome = {"reason": "closed"}
    with _RawTerminal():
        try:
            outcome = asyncio.run(_attach_async(client.base_url, client.token, name))
        except KeyboardInterrupt:
            outcome = {"reason": "detach"}
        except Exception as exc:  # restore the terminal before reporting
            outcome = {"reason": "error", "detail": str(exc)}

    reason = outcome.get("reason")
    if reason == "exit":
        code = outcome.get("code")
        suffix = f" (exit code {code})" if code is not None else ""
        print(f"\n[claunch] session {name!r} exited{suffix}")
        return 0
    if reason == "detach":
        print(
            f"\n[claunch] detached from {name!r} — it keeps running "
            f"(reattach: claunch attach {name})"
        )
        return 0
    if reason == "error":
        print(f"\nerror: attach failed: {outcome.get('detail')}", file=sys.stderr)
        return 1
    print("\n[claunch] connection closed by the daemon")
    return 0
