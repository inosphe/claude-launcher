"""Attach the current terminal to a managed session (tmux-style).

``claunch attach <session>`` puts the local terminal into raw mode and bridges
it to the daemon's terminal WebSocket — the same endpoint the web dashboard
uses — mirroring the session 1:1: keystrokes go to the PTY, PTY output paints
locally, and the session is resized to the attaching terminal (and follows it
while attached). Attaching takes the session's size from whichever viewer
held it; if another viewer takes it later, this terminal stops sizing the
session until it is detached and attached again. ``Ctrl+]`` detaches; the session keeps running in the daemon,
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
from typing import List, Optional, Tuple

from . import herdr, worktree

#: Ctrl+] — telnet's escape key; no common TUI binds it, so it is safe to
#: reserve as the detach key (anything typed after it in the same chunk is
#: dropped, like tmux's prefix).
DETACH_BYTE = b"\x1d"
DETACH_LABEL = "Ctrl+]"

#: Local terminal size poll cadence — there is no SIGWINCH on Windows, so the
#: size is polled on every platform for one concurrency model.
RESIZE_POLL = 0.5

_STDIN_CHUNK = 4096


#: Focus reporting (DECSET 1004): the local terminal is asked to emit these on
#: focus changes so the bridge can re-assert its size and request a repaint
#: after another viewer (web dashboard) resized the session — event-driven, no
#: polling of session state.
FOCUS_ON = "\x1b[?1004h"
FOCUS_OFF = "\x1b[?1004l"
_FOCUS_IN = b"\x1b[I"
_FOCUS_OUT = b"\x1b[O"


def _herdr_agent_state(status: Optional[str]) -> str:
    """The daemon's session status in Herdr's agent-state vocabulary.

    ``busy`` is what the daemon calls working, ``starting`` has no Herdr
    counterpart and reports unknown rather than pretending, and an exited or
    absent status should never be attached anyway.
    """
    return {"busy": "working", "idle": "idle"}.get(status or "", "unknown")


def split_detach(data: bytes) -> Tuple[bytes, bool]:
    """Payload up to the first detach byte, and whether it was pressed."""
    idx = data.find(DETACH_BYTE)
    if idx < 0:
        return data, False
    return data[:idx], True


def split_focus_events(data: bytes) -> Tuple[bytes, bool, bool]:
    """Remove focus in/out reports from ``data``; says which were seen.

    The session's programs never enabled focus reporting themselves (the
    bridge did, locally), so the reports must not reach the PTY. Only complete
    sequences are matched — terminals emit them atomically, and holding back
    a trailing ``ESC`` would delay a real Escape keypress.
    """
    focus_in = _FOCUS_IN in data
    focus_out = _FOCUS_OUT in data
    if focus_in:
        data = data.replace(_FOCUS_IN, b"")
    if _FOCUS_OUT in data:
        data = data.replace(_FOCUS_OUT, b"")
    return data, focus_in, focus_out


def strip_focus_events(data: bytes) -> Tuple[bytes, bool]:
    """:func:`split_focus_events` without the focus-out flag."""
    data, focus_in, _focus_out = split_focus_events(data)
    return data, focus_in


def focus_control_frames(
    focus_in: bool, focus_out: bool, size
) -> List[str]:
    """The control frames a focus change sends, in order.

    ``focus`` is the frame the web terminal sends for a visible tab; without
    it an attached terminal is a *background* viewer to the daemon — its
    child runs below normal priority and its screen is rendered on the
    background budget — even with a person typing into it (claunch-wpd0).
    Focus-in also re-asserts the size (another viewer may have resized the
    session meanwhile) and asks for a repaint.
    """
    frames: List[str] = []
    if focus_in:
        frames.append(
            json.dumps({"type": "resize", "cols": size.columns, "rows": size.lines})
        )
        frames.append(json.dumps({"type": "focus", "focused": True}))
        frames.append(json.dumps({"type": "repaint"}))
    elif focus_out:
        frames.append(json.dumps({"type": "focus", "focused": False}))
    return frames


def ws_url(
    base_url: str, name: str, *, overlay: bool = False, steal: bool = False
) -> str:
    """The session's terminal socket; ``overlay`` asks the daemon to compose
    notices into the byte stream (this is a real terminal, with nowhere else
    to draw them -- see ``daemon/notice.py``), ``steal`` takes the session's
    size as the socket opens (see "Who sizes the session" in ``daemon/ws.py``)."""
    base = base_url.rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://"):]
    url = f"{base}/api/sessions/{name}/ws"
    query = [q for q, on in (("overlay=1", overlay), ("steal=1", steal)) if on]
    return url + ("?" + "&".join(query) if query else "")


#: Shown over the top row when another viewer takes the size away: the grid
#: now follows their window, and this terminal only mirrors it.
SIZE_LOST_NOTICE = (
    f"another viewer took this session's size -- detach ({DETACH_LABEL}) "
    "and attach again to take it back"
)


def size_owner_update(ctrl: dict, owner: bool) -> Tuple[bool, bool]:
    """Apply a ``size_owner`` frame: (owner now, whether it was just lost).

    ``held: false`` means the holder left and nobody sizes the session; this
    terminal claims it back with its next resize, so it counts as the owner
    again. Losing it to someone else is the one change the person is told.
    """
    now = bool(ctrl.get("owner")) or not ctrl.get("held", True)
    return now, owner and not now


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
    # ReadFile (not ReadConsoleW) so rapid IME commits are not split/dropped
    # by the console's UTF-16 conversion, and VT sequences (arrows, function
    # keys under ENABLE_VIRTUAL_TERMINAL_INPUT) arrive in the same stream.
    # ReadFile encodes the input in the console *input code page*
    # (GetConsoleCP) — 949 on a Korean Windows, 65001 only when the user set
    # it — so the bytes are transcoded to UTF-8 before they reach the PTY,
    # which speaks UTF-8. VT sequences are ASCII and survive both encodings.
    import ctypes

    k32 = ctypes.windll.kernel32
    handle = k32.GetStdHandle(-10)  # STD_INPUT_HANDLE
    buf = ctypes.create_string_buffer(_STDIN_CHUNK)
    n = ctypes.c_uint32()
    while True:
        ok = k32.ReadFile(handle, buf, _STDIN_CHUNK, ctypes.byref(n), None)
        if not ok or n.value == 0:
            return b""  # the one b"" this function means: stdin is closed
        out = _console_input_to_utf8(buf.raw[: n.value], k32.GetConsoleCP())
        if out:
            return out
        # The chunk ended mid-character, so the transcode is holding its
        # leading bytes for the rest. That is not end of input, and the
        # callers of this function read b"" as exactly that: pump_stdin puts
        # None on the queue and returns, send_pump closes the socket and
        # calls it a detach. So read again rather than hand back an empty
        # result -- the keyboard is still there, the character is half in it.


_CP_UTF8 = 65001
#: (code page, incremental decoder) — kept across reads so a multi-byte
#: character split over two ReadFile chunks still decodes as one.
_console_decoder: Optional[Tuple[int, codecs.IncrementalDecoder]] = None


def _console_input_to_utf8(data: bytes, codepage: int) -> bytes:
    """Transcode console input bytes from ``codepage`` to UTF-8.

    Every code page goes through an incremental decoder, UTF-8 included.
    A UTF-8 console used to pass its bytes through untouched, and that left
    ``ReadFile``'s chunk boundary wherever it fell -- which is not a
    character boundary. The daemon then held the leading bytes of a split
    syllable while it waited for the rest, and a delivery landing in that
    gap had its own first bytes read as the end of that character: the
    syllable was lost. So the boundary is repaired here, where the code page
    is known, rather than being carried across the wire
    (``claunch-pty-shared-decoder-across-writers-o3cy4``).

    An unknown code page is still passed through -- better a wrong byte than
    a dropped key.
    """
    global _console_decoder
    if _console_decoder is None or _console_decoder[0] != codepage:
        name = "utf-8" if codepage == _CP_UTF8 else f"cp{codepage}"
        try:
            decoder = codecs.getincrementaldecoder(name)("replace")
        except LookupError:
            return data
        _console_decoder = (codepage, decoder)
    return _console_decoder[1].decode(data).encode("utf-8")


#: Windows 10 1903 — the first conhost where ReadFile under code page 65001
#: returns non-ASCII input instead of zero bytes (which the bridge would read
#: as EOF and turn into a detach). Older consoles keep their code page and
#: rely on the transcode in ``_console_input_to_utf8`` alone.
_UTF8_CONSOLE_MIN_BUILD = 18362


def _utf8_console_input_supported() -> bool:
    try:
        return sys.getwindowsversion().build >= _UTF8_CONSOLE_MIN_BUILD
    except AttributeError:
        return False


def codepage_note(codepage: Optional[int], switched: bool) -> Optional[str]:
    """One line for after detach when the console input code page was not
    UTF-8 — what the bridge did about it, and how to make it permanent."""
    if not codepage or codepage == _CP_UTF8:
        return None
    action = (
        "switched it to UTF-8 (65001) for the attach and restored it"
        if switched
        else "transcoded keystrokes to UTF-8 for the attach; characters "
        "outside that code page arrive as '?'"
    )
    return (
        f"[claunch] console input code page was {codepage}, not UTF-8: "
        f"{action}. To make it permanent: chcp 65001, or Windows' "
        "\"Beta: Use Unicode UTF-8 for worldwide language support\"."
    )


class _RawTerminal:
    """Raw local terminal for the duration of an attach; restores on exit.

    ``original_codepage`` is the console input code page found on entry
    (None outside Windows); ``codepage_switched`` says whether it was moved
    to UTF-8 for the attach so characters outside the native code page
    (emoji, for one) survive ReadFile instead of arriving as ``?``.
    """

    original_codepage: Optional[int] = None
    codepage_switched: bool = False

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
        self.original_codepage = self._k32.GetConsoleCP() or None
        if (
            self.original_codepage
            and self.original_codepage != _CP_UTF8
            and _utf8_console_input_supported()
        ):
            self.codepage_switched = bool(self._k32.SetConsoleCP(_CP_UTF8))
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
        if self.codepage_switched:
            self._k32.SetConsoleCP(self.original_codepage)
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
async def _attach_async(
    base_url: str, token: str, name: str, notice: Optional[str] = None
) -> dict:
    """Bridge stdin/stdout to the session's terminal WebSocket.

    Returns an outcome dict: ``{"reason": "detach" | "exit" | "closed",
    "code": ...}``. The caller owns terminal modes; this only moves bytes.

    ``notice`` is a line to show over the session's top row once attached
    (the daemon draws it for this viewer alone and takes it down again):
    what this client learned about the local terminal, which nothing printed
    before raw mode survives -- the session repaints over it at once.
    """
    import aiohttp

    loop = asyncio.get_running_loop()
    stdin_q: asyncio.Queue = asyncio.Queue()
    stop = threading.Event()
    outcome = {"reason": "closed"}
    # Whether this terminal holds the session's size. It takes it as it
    # attaches (``?steal=1``); once another viewer takes it, this terminal
    # stops sending sizes, and taking it back is a detach and a re-attach.
    sizing = {"owner": True, "last": None}

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
            payload, focus_in, focus_out = split_focus_events(payload)
            for frame in focus_control_frames(
                focus_in, focus_out, shutil.get_terminal_size()
            ):
                if not sizing["owner"] and '"resize"' in frame:
                    continue  # the size is another viewer's now
                await ws.send_str(frame)
            if payload:
                await ws.send_bytes(payload)
            if detach:
                outcome["reason"] = "detach"
                await ws.close()
                return

    async def resize_pump(ws) -> None:
        while True:
            size = shutil.get_terminal_size()
            cur = (size.columns, size.lines)
            if sizing["owner"] and cur != sizing["last"]:
                sizing["last"] = cur
                await ws.send_str(
                    json.dumps({"type": "resize", "cols": cur[0], "rows": cur[1]})
                )
            await asyncio.sleep(RESIZE_POLL)

    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    async with aiohttp.ClientSession() as http:
        async with http.ws_connect(
            ws_url(base_url, name, overlay=True, steal=True),
            headers={"Authorization": f"Bearer {token}"},
            heartbeat=30,
            max_msg_size=0,
        ) as ws:
            # A person is at this terminal from the first byte: say so, or the
            # daemon treats the attach as a background viewer until the
            # terminal's first focus-in report (which a terminal without
            # DECSET 1004 never sends).
            await ws.send_str(json.dumps({"type": "focus", "focused": True}))
            if notice:
                await ws.send_str(
                    json.dumps(
                        {"type": "notice", "text": notice, "ttl": 10, "level": "warn"}
                    )
                )
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
                        if not isinstance(ctrl, dict):
                            continue
                        if ctrl.get("type") == "size_owner":
                            now, lost = size_owner_update(ctrl, sizing["owner"])
                            if now and not sizing["owner"]:
                                sizing["last"] = None  # free again: resend
                            sizing["owner"] = now
                            if lost:
                                await ws.send_str(
                                    json.dumps(
                                        {
                                            "type": "notice",
                                            "text": SIZE_LOST_NOTICE,
                                            "ttl": 10,
                                            "level": "warn",
                                        }
                                    )
                                )
                            continue
                        if ctrl.get("type") == "exit":
                            outcome["reason"] = "exit"
                            outcome["code"] = ctrl.get("code")
                            break
                        if ctrl.get("type") == "shutdown":
                            # The daemon itself is stopping/restarting; the
                            # session goes down with it, not by its own doing.
                            outcome["reason"] = "shutdown"
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
            + (f" (exit code {code})" if code is not None else "")
            + f" — revive it with: claunch respawn {name}",
            file=sys.stderr,
        )
        return 1
    print(f"[claunch] attached to {name!r} — detach: {DETACH_LABEL}", file=sys.stderr)

    # For as long as this attach lasts, the pane IS that session's terminal --
    # so if it is a Herdr pane, it says which session and which worktree. This
    # is the only place a pane and a session genuinely coincide: a created but
    # unattached session runs in the daemon, and a label for one of those
    # would outlive it and still read as true. Cleared on the way out for the
    # same reason.
    labelled = herdr.rename_pane(
        worktree.pane_label(name, info.get("cwd") or "", info.get("role") or "")
    )
    # The pane does not run claude, it mirrors it — so Herdr's own agent
    # detection never sees the agent (it sees this attach). Report it the
    # official way so the pane reads as the session it is, and release it on
    # the way out, on the same occupancy contract as the label.
    agent_reported = (
        herdr.report_agent(
            herdr.MIRROR_AGENT_LABEL,
            state=_herdr_agent_state(info.get("status")),
            message=name,
        )
        if labelled
        else False
    )

    outcome = {"reason": "closed"}
    term = _RawTerminal()
    with term:
        _write_text(FOCUS_ON)
        # What the raw-mode entry found out about this console is only known
        # now, and only this client knows it: hand it to the daemon to draw
        # over the session for this viewer (nothing printed here would stay
        # on screen past the session's next frame).
        notice = codepage_note(
            getattr(term, "original_codepage", None),
            getattr(term, "codepage_switched", False),
        )
        try:
            outcome = asyncio.run(
                _attach_async(client.base_url, client.token, name, notice=notice)
            )
        except KeyboardInterrupt:
            outcome = {"reason": "detach"}
        except Exception as exc:  # restore the terminal before reporting
            outcome = {"reason": "error", "detail": str(exc)}
        finally:
            _write_text(FOCUS_OFF)
    if labelled:
        herdr.clear_pane_label()
    if agent_reported:
        herdr.release_agent(herdr.MIRROR_AGENT_LABEL)

    # Only now is the local terminal cooked again and a line of ours stays
    # readable — everything printed before raw mode is repainted over by the
    # session within a frame, so this is where a console-encoding note lands.
    note = codepage_note(
        getattr(term, "original_codepage", None),
        getattr(term, "codepage_switched", False),
    )
    if note:
        print(file=sys.stderr)
        print(note, file=sys.stderr)

    reason = outcome.get("reason")
    if reason == "exit":
        code = outcome.get("code")
        suffix = f" (exit code {code})" if code is not None else ""
        print(
            f"\n[claunch] session {name!r} exited{suffix} — the program inside "
            "ended (keys like Ctrl+C go to it, not to the attach)"
        )
        print(
            f"[claunch] revive it with its conversation: claunch respawn {name}"
        )
        return 0
    if reason == "detach":
        print(
            f"\n[claunch] detached from {name!r} — it keeps running "
            f"(reattach: claunch attach {name})"
        )
        return 0
    if reason == "shutdown":
        print(
            f"\n[claunch] the daemon is stopping — session {name!r} "
            "goes down with it (not the program's own exit)"
        )
        print(
            f"[claunch] once the daemon is back up, reattach with: "
            f"claunch attach {name}"
        )
        return 0
    if reason == "error":
        print(f"\nerror: attach failed: {outcome.get('detail')}", file=sys.stderr)
        return 1
    print(
        f"\n[claunch] connection closed by the daemon — "
        f"reattach with: claunch attach {name}"
    )
    return 0
