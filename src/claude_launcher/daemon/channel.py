"""A terminal attachment that rides someone else's socket.

A browser counts a WebSocket against its per-server connection ceiling --
six in Firefox -- and the dashboard opened one per session it was looking
at. Visiting three sessions in a tab meant three, and the ceiling is shared
across every tab of the browser, so the count grew with use and the next
upgrade waited in the browser's own connection queue with nothing on this
side to see (claunch-gh4f).

:class:`ChannelSocket` makes that count a constant. It presents the small
part of :class:`aiohttp.web.WebSocketResponse` that a terminal attachment
actually uses -- ``send_str``, ``send_bytes``, ``close``, ``closed``,
``close_code``, ``exception`` and iteration over incoming messages -- and
writes each of them onto one shared socket, tagged with a channel id. The
attachment code does not know the difference, which is the point: the
terminal protocol has one implementation (``daemon/ws.py``) and this adds a
second carrier for it, not a second copy of it.

Framing, on the shared socket:

- Binary, both directions: a two-byte big-endian channel id, then the
  payload verbatim. PTY output is raw bytes and keystrokes are raw bytes;
  base64 would make both 4/3 the size and cost an encode on every chunk, so
  the id goes in front of the bytes instead of around them.
- Text: JSON objects carrying ``"ch": N``. Frames without ``ch`` belong to
  the shared socket itself (its reads, its pings) and never reach a channel.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

from aiohttp import WSMsgType

log = logging.getLogger(__name__)

#: Width of the binary channel header. Two bytes because a tab that visits
#: more than 255 sessions without a reload is ordinary and one that visits
#: 65535 is not.
HEADER = 2

#: How many channels one shared socket will carry. A tab holds one live
#: terminal and keeps a few parked, so this is generous; it exists so a
#: client cannot make the daemon hold an unbounded number of attachments on
#: a single connection.
MAX_CHANNELS = 16


def pack(ch: int, payload: bytes) -> bytes:
    """One binary frame for the shared socket."""
    return ch.to_bytes(HEADER, "big") + payload


def unpack(data: bytes) -> tuple[Optional[int], bytes]:
    """Channel id and payload, or ``(None, b"")`` for a frame too short to
    carry a header. A truncated frame is dropped rather than guessed at --
    the alternative is writing someone else's keystrokes into a PTY."""
    if len(data) < HEADER:
        return None, b""
    return int.from_bytes(data[:HEADER], "big"), bytes(data[HEADER:])


class _Msg:
    """What ``async for msg in sock`` yields, shaped like aiohttp's."""

    __slots__ = ("type", "data")

    def __init__(self, type_: WSMsgType, data: Any) -> None:
        self.type = type_
        self.data = data


class ChannelSocket:
    """One channel of a shared socket, in the shape of a WebSocketResponse.

    Only the surface ``daemon/ws.py`` uses is implemented. Anything else is
    absent on purpose: a missing attribute is a loud failure at the moment
    the attachment reaches for something this carrier cannot give, which is
    cheaper to find than a silently wrong answer.
    """

    def __init__(self, parent, ch: int) -> None:
        self._parent = parent
        self._ch = ch
        self._inbox: asyncio.Queue = asyncio.Queue()
        self._closed = False
        self._close_code: Optional[int] = None
        self._exception: Optional[BaseException] = None

    # -- what the attachment writes ---------------------------------------

    async def send_str(self, data: str) -> None:
        if self._closed:
            return
        try:
            frame = json.loads(data)
        except (ValueError, TypeError):
            # The attachment only ever sends JSON objects; anything else is
            # a bug there, and forwarding it untagged would deliver it to
            # the page as a control-socket frame.
            return
        if not isinstance(frame, dict):
            return
        frame["ch"] = self._ch
        await self._parent.send_str(json.dumps(frame))

    async def send_bytes(self, data: bytes) -> None:
        if self._closed:
            return
        await self._parent.send_bytes(pack(self._ch, data))

    async def close(self, *, code: int = 1000, message: bytes = b"") -> None:
        """End this channel without touching the socket under it.

        The shared socket outlives every channel on it -- that is the whole
        arrangement -- so a channel's close is a frame to the client and an
        end to this side's loops, never a close of the carrier.
        """
        if self._closed:
            return
        self._closed = True
        self._close_code = code
        await self._parent.channel_ended(self._ch, code)
        # Wake a receive loop that is waiting on an empty inbox.
        self._inbox.put_nowait(_Msg(WSMsgType.CLOSE, None))

    # -- what the attachment reads ----------------------------------------

    @property
    def ch(self) -> int:
        return self._ch

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def close_code(self) -> Optional[int]:
        return self._close_code

    def exception(self) -> Optional[BaseException]:
        return self._exception

    def __aiter__(self) -> "ChannelSocket":
        return self

    async def __anext__(self) -> _Msg:
        msg = await self._inbox.get()
        if msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
            raise StopAsyncIteration
        return msg

    # -- what the carrier delivers ----------------------------------------

    def deliver_text(self, frame: dict) -> None:
        self._inbox.put_nowait(_Msg(WSMsgType.TEXT, json.dumps(frame)))

    def deliver_bytes(self, payload: bytes) -> None:
        self._inbox.put_nowait(_Msg(WSMsgType.BINARY, payload))

    def carrier_gone(self, exc: Optional[BaseException] = None) -> None:
        """The shared socket ended, so every channel on it ended with it."""
        self._closed = True
        self._close_code = 1006
        self._exception = exc
        self._inbox.put_nowait(_Msg(WSMsgType.CLOSE, None))


class Carrier:
    """The shared socket, and the channels riding it.

    One of these lives for as long as the page's control socket does. It
    owns the real :class:`~aiohttp.web.WebSocketResponse`, hands each
    attachment a :class:`ChannelSocket` in its place, and is the only writer
    to the socket, so two terminals producing output at once cannot
    interleave a frame.
    """

    def __init__(self, ws, app, request) -> None:
        self._ws = ws
        self._app = app
        self._request = request
        self._channels: dict[int, ChannelSocket] = {}
        self._tasks: dict[int, asyncio.Task] = {}
        self._write = asyncio.Lock()
        self._gone = False

    # -- the one writer ----------------------------------------------------

    async def send_str(self, data: str) -> None:
        if self._gone or self._ws.closed:
            return
        async with self._write:
            await self._ws.send_str(data)

    async def send_bytes(self, data: bytes) -> None:
        if self._gone or self._ws.closed:
            return
        async with self._write:
            await self._ws.send_bytes(data)

    # -- incoming ----------------------------------------------------------

    def deliver_binary(self, data: bytes) -> None:
        """A binary frame from the client: keystrokes and resizes, tagged."""
        ch, payload = unpack(data)
        chan = self._channels.get(ch) if ch is not None else None
        if chan is not None:
            chan.deliver_bytes(payload)

    async def deliver_text(self, frame: dict) -> bool:
        """Route one text frame. ``True`` when it belonged to a channel or
        was channel bookkeeping, so the control socket's own handling of
        reads and pings runs only on frames that are its own."""
        kind = frame.get("type")
        if kind == "attach":
            await self.attach(frame)
            return True
        if kind == "detach":
            await self.detach(frame.get("ch"))
            return True
        ch = frame.get("ch")
        if not isinstance(ch, int):
            return False
        chan = self._channels.get(ch)
        if chan is not None:
            chan.deliver_text(frame)
        return True

    # -- channel lifecycle -------------------------------------------------

    async def attach(self, frame: dict) -> None:
        ch = frame.get("ch")
        name = frame.get("session")
        if not isinstance(ch, int) or ch < 0 or ch >= 1 << (HEADER * 8):
            await self._fail(ch, "bad channel id")
            return
        if ch in self._channels:
            await self._fail(ch, "channel in use")
            return
        if len(self._channels) >= MAX_CHANNELS:
            await self._fail(ch, f"at most {MAX_CHANNELS} channels per socket")
            return
        if not isinstance(name, str) or not name:
            await self._fail(ch, "attach needs a session name")
            return
        try:
            session = self._app["manager"].get(name)
        except Exception as exc:  # noqa: BLE001 -- the client named it, so tell it
            await self._fail(ch, str(exc))
            return
        chan = ChannelSocket(self, ch)
        self._channels[ch] = chan
        await self.send_str(json.dumps({"type": "attached", "ch": ch, "session": name}))
        self._tasks[ch] = asyncio.create_task(
            self._serve(chan, session, bool(frame.get("scrollback")), bool(frame.get("overlay"))),
            name=f"channel-{ch}-{name}",
        )

    async def _serve(self, chan: ChannelSocket, session, scrollback: bool, overlay: bool) -> None:
        from . import ws as ws_mod  # local: ws.py knows nothing of this file

        try:
            await ws_mod.attach_terminal(
                chan, session, self._app,
                want_scrollback=scrollback,
                overlay=overlay,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- one channel's fault ends one channel
            log.exception("channel %d (%s) failed", chan.ch, session.sdef.name)
        finally:
            await chan.close(code=1000)

    async def detach(self, ch) -> None:
        chan = self._channels.get(ch) if isinstance(ch, int) else None
        if chan is None:
            return
        await chan.close(code=1000)

    async def channel_ended(self, ch: int, code: int) -> None:
        """Called by a :class:`ChannelSocket` as it closes: drop it and say
        so, leaving the socket under it open for every other channel."""
        self._channels.pop(ch, None)
        task = self._tasks.pop(ch, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        await self.send_str(json.dumps({"type": "detached", "ch": ch, "code": code}))

    async def _fail(self, ch, error: str) -> None:
        await self.send_str(json.dumps({"type": "attach_error", "ch": ch, "error": error}))

    async def shutdown(self, exc: Optional[BaseException] = None) -> None:
        """The shared socket ended. Every attachment on it ends with it."""
        self._gone = True
        for chan in list(self._channels.values()):
            chan.carrier_gone(exc)
        self._channels.clear()
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
