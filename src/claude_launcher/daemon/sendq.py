"""How much a websocket has written that its socket has not taken yet.

Both writers that share a socket among several flows -- the relay uplink
(every tunnelled stream on one connection) and the page's control socket
(every terminal and read on one connection) -- put their small frames ahead
of their bulk ones (claunch-iss86). That ordering only holds for what is
still in their own queue: a frame handed to the socket goes out behind
everything the transport already buffered, whatever its priority. So they
hold bulk back while the transport's buffer is above a low-water mark, and
this is where they read it.
"""

from __future__ import annotations

import asyncio

#: Bytes a writer lets sit in the transport before it holds the next bulk
#: frame back. Small enough that a frame queued behind it waits a moment on
#: a slow link, large enough to keep a fast one busy between two checks.
LOW_WATER = 64 * 1024

#: How long a writer waits before looking at the buffer again when it was
#: above the mark. The transport has no "fell below" event of its own to wait
#: on (``drain`` waits for the high-water mark, far above this one).
POLL_S = 0.005


def backlog(ws) -> int:
    """Bytes buffered in ``ws``'s transport, or 0 when that cannot be read.

    aiohttp keeps the transport on the websocket's writer, for the client
    and the server side alike; a stand-in without one reads as empty, which
    is what a writer would have assumed before this existed.
    """
    transport = getattr(getattr(ws, "_writer", None), "transport", None)
    size = getattr(transport, "get_write_buffer_size", None)
    if size is None:
        return 0
    try:
        return int(size())
    except Exception:  # noqa: BLE001 -- a closing transport, most likely
        return 0


async def below_low_water(ws, more_urgent=lambda: False) -> bool:
    """Wait until ``ws``'s transport holds no more than :data:`LOW_WATER`.

    Returns ``False`` early when ``more_urgent()`` turns true while it waits,
    so the caller can send that first and come back.
    """
    while backlog(ws) > LOW_WATER:
        if more_urgent():
            return False
        await asyncio.sleep(POLL_S)
    return True
