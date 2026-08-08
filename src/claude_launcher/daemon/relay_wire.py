"""Python side of the relay tunnel wire protocol.

A faithful port of the Rust ``wire`` crate's frame format and the tunnel
profile messages (see psmux-relay ``docs/tunnel-protocol.md``). Only the
subset an uplink backend needs is implemented: REGISTER/REGISTER_OK,
STREAM_OPEN/DATA/EOF/CLOSE, and PING/PONG for keepalive.

Frame: ``[ room_id(16B) ][ flags(1B) ][ len(u16 BE) ][ payload ]``
payload: ``[ type(1B) ][ body ]``
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import List, Optional, Tuple

ROOM_ID_LEN = 16
HEADER_LEN = ROOM_ID_LEN + 1 + 2
MAX_PAYLOAD = 0xFFFF
MAX_STREAM_DATA = MAX_PAYLOAD - 5  # payload cap minus type(1) + stream_id(4)

# message type bytes (must match the Rust ``msg_type`` module)
PING = 0x05
PONG = 0x06
REGISTER = 0x10
REGISTER_OK = 0x11
STREAM_OPEN = 0x12
STREAM_DATA = 0x13
STREAM_EOF = 0x14
STREAM_CLOSE = 0x15
PEER_OPEN = 0x16
PEER_OPEN_OK = 0x17
PEER_OPEN_ERR = 0x18
PEER_LIST = 0x19
PEER_LIST_OK = 0x1A

#: REGISTER_OK capability bits. Old relays send an empty body (caps 0); the
#: bit MUST be checked before sending PEER_OPEN — an old relay silently drops
#: unknown message types, so an unchecked request hangs without any error.
CAP_PEERING = 0x01
#: PEER_LIST support (a relay that only knows CAP_PEERING drops PEER_LIST).
CAP_PEER_LIST = 0x02

#: PEER_OPEN_ERR codes.
PEER_ERR_UNKNOWN_BACKEND = 1
PEER_ERR_DISABLED = 2
PEER_ERR_UNREACHABLE = 3

# channel (flags & 0b11): Control=0, Tunnel=3
_CH_CONTROL = 0
_CH_TUNNEL = 3


def _frame(room_id: bytes, channel: int, payload: bytes) -> bytes:
    assert len(room_id) == ROOM_ID_LEN
    assert len(payload) <= MAX_PAYLOAD
    return room_id + bytes([channel]) + struct.pack(">H", len(payload)) + payload


def register(room_id: bytes, name: str, token: str) -> bytes:
    name_b = name.encode("utf-8")
    if len(name_b) > 255:
        raise ValueError("backend name too long (max 255 bytes)")
    payload = bytes([REGISTER, len(name_b)]) + name_b + token.encode("utf-8")
    return _frame(room_id, _CH_TUNNEL, payload)


def stream_open(room_id: bytes, sid: int) -> bytes:
    """Relay→backend only; provided here for symmetry and tests."""
    return _frame(room_id, _CH_TUNNEL, bytes([STREAM_OPEN]) + struct.pack(">I", sid))


def stream_data(room_id: bytes, sid: int, data: bytes) -> bytes:
    payload = bytes([STREAM_DATA]) + struct.pack(">I", sid) + data
    return _frame(room_id, _CH_TUNNEL, payload)


def stream_eof(room_id: bytes, sid: int) -> bytes:
    return _frame(room_id, _CH_TUNNEL, bytes([STREAM_EOF]) + struct.pack(">I", sid))


def stream_close(room_id: bytes, sid: int) -> bytes:
    return _frame(room_id, _CH_TUNNEL, bytes([STREAM_CLOSE]) + struct.pack(">I", sid))


def peer_open(room_id: bytes, req_id: int, name: str) -> bytes:
    """Ask the relay to bridge a stream to another registered backend."""
    name_b = name.encode("utf-8")
    if len(name_b) > 255:
        raise ValueError("backend name too long (max 255 bytes)")
    payload = (
        bytes([PEER_OPEN]) + struct.pack(">I", req_id) + bytes([len(name_b)]) + name_b
    )
    return _frame(room_id, _CH_TUNNEL, payload)


def peer_list(room_id: bytes, req_id: int) -> bytes:
    """Ask the relay for the names of the other registered backends."""
    return _frame(
        room_id, _CH_TUNNEL, bytes([PEER_LIST]) + struct.pack(">I", req_id)
    )


def pong(room_id: bytes, token: int) -> bytes:
    return _frame(room_id, _CH_CONTROL, bytes([PONG]) + struct.pack(">Q", token))


def ping(room_id: bytes, token: int) -> bytes:
    return _frame(room_id, _CH_CONTROL, bytes([PING]) + struct.pack(">Q", token))


def iter_stream_data(room_id: bytes, sid: int, data: bytes) -> List[bytes]:
    """Split ``data`` into STREAM_DATA frames respecting MAX_STREAM_DATA."""
    if not data:
        return [stream_data(room_id, sid, b"")]
    return [
        stream_data(room_id, sid, data[i : i + MAX_STREAM_DATA])
        for i in range(0, len(data), MAX_STREAM_DATA)
    ]


@dataclass
class Msg:
    """A decoded tunnel message. ``kind`` is the type byte."""

    kind: int
    sid: int = 0
    data: bytes = b""
    token: int = 0
    caps: int = 0  # REGISTER_OK capability bits
    req: int = 0  # PEER_OPEN_OK/ERR + PEER_LIST_OK request correlation id
    code: int = 0  # PEER_OPEN_ERR code
    names: tuple = ()  # PEER_LIST_OK backend names


def decode_payload(payload: bytes) -> Optional[Msg]:
    """Decode a frame payload into a Msg, or None for types we ignore."""
    if not payload:
        return None
    ty = payload[0]
    body = payload[1:]
    if ty == REGISTER_OK:
        # Optional [caps u8] body; old relays send none (caps 0), trailing
        # bytes beyond the first are future extensions and are ignored.
        return Msg(REGISTER_OK, caps=body[0] if body else 0)
    if ty in (STREAM_OPEN, STREAM_EOF, STREAM_CLOSE):
        if len(body) < 4:
            return None
        return Msg(ty, sid=struct.unpack(">I", body[:4])[0])
    if ty == STREAM_DATA:
        if len(body) < 4:
            return None
        return Msg(STREAM_DATA, sid=struct.unpack(">I", body[:4])[0], data=body[4:])
    if ty == PEER_OPEN_OK:
        if len(body) < 8:
            return None
        req, sid = struct.unpack(">II", body[:8])
        return Msg(PEER_OPEN_OK, req=req, sid=sid)
    if ty == PEER_OPEN_ERR:
        if len(body) < 5:
            return None
        return Msg(PEER_OPEN_ERR, req=struct.unpack(">I", body[:4])[0], code=body[4])
    if ty == PEER_LIST:
        # Backend→relay only; decoded here for symmetry and tests.
        if len(body) < 4:
            return None
        return Msg(PEER_LIST, req=struct.unpack(">I", body[:4])[0])
    if ty == PEER_LIST_OK:
        # [req_id u32][count u8]([len u8][name])*
        if len(body) < 5:
            return None
        req = struct.unpack(">I", body[:4])[0]
        count, rest = body[4], body[5:]
        names = []
        for _ in range(count):
            if not rest or len(rest) < 1 + rest[0]:
                return None  # truncated — drop the whole message
            names.append(rest[1 : 1 + rest[0]].decode("utf-8", "replace"))
            rest = rest[1 + rest[0]:]
        return Msg(PEER_LIST_OK, req=req, names=tuple(names))
    if ty in (PING, PONG):
        if len(body) < 8:
            return None
        return Msg(ty, token=struct.unpack(">Q", body[:8])[0])
    return None  # unknown / terminal-profile type — ignore on the uplink


class FrameDecoder:
    """Reassembles frames from a byte stream (length-prefix based).

    WebSocket message boundaries need not align with frame boundaries, so
    incoming bytes are buffered and only complete frames are yielded.
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> List[Tuple[bytes, bytes]]:
        """Append bytes; return list of (room_id, payload) for complete frames."""
        self._buf.extend(data)
        out: List[Tuple[bytes, bytes]] = []
        while True:
            if len(self._buf) < HEADER_LEN:
                break
            plen = struct.unpack(">H", self._buf[ROOM_ID_LEN + 1 : HEADER_LEN])[0]
            total = HEADER_LEN + plen
            if len(self._buf) < total:
                break
            room_id = bytes(self._buf[:ROOM_ID_LEN])
            payload = bytes(self._buf[HEADER_LEN:total])
            out.append((room_id, payload))
            del self._buf[:total]
        return out
