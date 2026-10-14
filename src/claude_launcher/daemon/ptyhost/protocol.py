"""Framing for the PTY host socket.

One frame is ``>II`` (header length, payload length), a UTF-8 JSON object
header, and raw payload bytes. Terminal output and input ride as payload, so
they cross the socket as they are (no base64, no JSON escaping); the header
carries the operation and its fields.

Requests that want an answer carry an integer ``id``; the answer is a
``reply`` frame with the same ``id`` and ``ok`` (plus ``error`` when false).
Everything else is one-way: input, resize and terminate from the client,
``output`` and ``exit`` events from the host.
"""

from __future__ import annotations

import json
import socket
import struct
from typing import Optional, Tuple

_PREFIX = struct.Struct(">II")

#: Upper bounds a reader enforces before allocating, so a corrupt or hostile
#: length cannot make it reserve gigabytes.
HEADER_MAX = 1 << 20
PAYLOAD_MAX = 16 << 20


class ProtocolError(Exception):
    """The peer sent something that is not a frame."""


def encode(header: dict, payload: bytes = b"") -> bytes:
    head = json.dumps(header, separators=(",", ":")).encode("utf-8")
    if len(head) > HEADER_MAX or len(payload) > PAYLOAD_MAX:
        raise ProtocolError("frame too large")
    return _PREFIX.pack(len(head), len(payload)) + head + payload


def _recv_exact(sock: socket.socket, n: int) -> Optional[bytes]:
    """``n`` bytes, or None if the peer closed before the first byte."""
    if n == 0:
        return b""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            if buf:
                raise ProtocolError("connection closed inside a frame")
            return None
        buf += chunk
    return bytes(buf)


def read_frame(sock: socket.socket) -> Optional[Tuple[dict, bytes]]:
    """The next frame from ``sock``, or None at a clean end of stream."""
    prefix = _recv_exact(sock, _PREFIX.size)
    if prefix is None:
        return None
    hlen, plen = _PREFIX.unpack(prefix)
    if hlen > HEADER_MAX or plen > PAYLOAD_MAX:
        raise ProtocolError(f"frame lengths out of range ({hlen}, {plen})")
    head = _recv_exact(sock, hlen) if hlen else b""
    if head is None:
        raise ProtocolError("connection closed inside a frame")
    payload = _recv_exact(sock, plen) if plen else b""
    if payload is None:
        raise ProtocolError("connection closed inside a frame")
    try:
        header = json.loads(head.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProtocolError(f"bad frame header: {exc}") from exc
    if not isinstance(header, dict) or not isinstance(header.get("op"), str):
        raise ProtocolError("frame header has no op")
    return header, payload
