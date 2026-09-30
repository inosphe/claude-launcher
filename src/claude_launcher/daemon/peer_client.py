"""Build/parse the one-shot HTTP requests daemons exchange over relay bridges.

A peer call is a single raw HTTP/1.1 request-response on a bridged stream
(``RelayUplink.peer_http``): the request carries ``Connection: close`` and a
``Content-Length`` so the peer's aiohttp server treats the stream like any
relay ingress connection, and the response is read to EOF.
"""

from __future__ import annotations

import json
from typing import Optional, Tuple
from urllib.parse import quote


def build_request(path: str, body: dict, *, host: str = "peer") -> bytes:
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    # The head must be ASCII, but ``host`` is a relay machine name and those
    # are free text; the peer routes on the bridge, never on this header.
    host = quote(host, safe="-._~:")
    head = (
        f"POST {path} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(payload)}\r\n"
        "Connection: close\r\n"
        "\r\n"
    )
    return head.encode("ascii") + payload


class PeerHttpError(Exception):
    """A peer response that could not be read. ``status`` is set when the
    status line was, and the body was what failed."""

    def __init__(self, message: str, *, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


def parse_response(raw: bytes) -> Tuple[int, dict]:
    """Return (status, parsed-JSON-body) from raw response bytes."""
    sep = raw.find(b"\r\n\r\n")
    if sep < 0:
        raise PeerHttpError("truncated peer response (no header terminator)")
    head = raw[:sep].decode("latin-1")
    body = raw[sep + 4 :]
    lines = head.split("\r\n")
    parts = lines[0].split(" ", 2)
    if len(parts) < 2 or not parts[1].isdigit():
        raise PeerHttpError(f"bad peer status line: {lines[0]!r}")
    status = int(parts[1])
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    if headers.get("transfer-encoding", "").lower() == "chunked":
        body = _unchunk(body)
    else:
        length = headers.get("content-length")
        if length and length.isdigit():
            body = body[: int(length)]
    try:
        doc = json.loads(body.decode("utf-8")) if body else {}
    except (ValueError, UnicodeDecodeError):
        raise PeerHttpError(
            f"peer returned non-JSON body (status {status})", status=status
        ) from None
    return status, doc if isinstance(doc, dict) else {}


class ResponseStream:
    """Incremental reader for a peer response that is not read to EOF.

    ``parse_response`` needs the whole answer; a streamed one (the shadow
    terminal, ``/peer/shadow/stream``) never ends while the viewer watches.
    Feed it the bridge's chunks as they come: ``status``/``headers`` are set
    once the head is complete, and :meth:`feed` returns the body bytes each
    chunk completed, chunked transfer-encoding removed.
    """

    def __init__(self) -> None:
        self.status: int = 0
        self.headers: dict = {}
        self._buf = bytearray()
        self._head_done = False
        self._chunked = False
        self._chunk_left = 0  # bytes of the current chunk still to come
        self._chunk_crlf = 0  # bytes of the CRLF after a chunk still to skip
        self.finished = False  # the terminating zero-size chunk was read

    @property
    def head_done(self) -> bool:
        return self._head_done

    def feed(self, data: bytes) -> bytes:
        self._buf += data
        if not self._head_done:
            sep = self._buf.find(b"\r\n\r\n")
            if sep < 0:
                if len(self._buf) > 64 * 1024:
                    raise PeerHttpError("peer response head too large")
                return b""
            head = bytes(self._buf[:sep]).decode("latin-1")
            del self._buf[: sep + 4]
            lines = head.split("\r\n")
            parts = lines[0].split(" ", 2)
            if len(parts) < 2 or not parts[1].isdigit():
                raise PeerHttpError(f"bad peer status line: {lines[0]!r}")
            self.status = int(parts[1])
            for line in lines[1:]:
                if ":" in line:
                    k, v = line.split(":", 1)
                    self.headers[k.strip().lower()] = v.strip()
            self._chunked = (
                self.headers.get("transfer-encoding", "").lower() == "chunked"
            )
            self._head_done = True
        if not self._chunked:
            out = bytes(self._buf)
            self._buf.clear()
            return out
        return self._dechunk()

    def _dechunk(self) -> bytes:
        out = bytearray()
        while self._buf and not self.finished:
            if self._chunk_crlf:
                drop = min(self._chunk_crlf, len(self._buf))
                del self._buf[:drop]
                self._chunk_crlf -= drop
                continue
            if self._chunk_left:
                take = min(self._chunk_left, len(self._buf))
                out += self._buf[:take]
                del self._buf[:take]
                self._chunk_left -= take
                if not self._chunk_left:
                    self._chunk_crlf = 2
                continue
            eol = self._buf.find(b"\r\n")
            if eol < 0:
                break
            try:
                size = int(bytes(self._buf[:eol]).split(b";")[0], 16)
            except ValueError:
                raise PeerHttpError("bad chunk size in peer response") from None
            del self._buf[: eol + 2]
            if size == 0:
                self.finished = True
                break
            self._chunk_left = size
        return bytes(out)


def _unchunk(body: bytes) -> bytes:
    out = bytearray()
    pos = 0
    while True:
        eol = body.find(b"\r\n", pos)
        if eol < 0:
            raise PeerHttpError("truncated chunked peer response")
        try:
            size = int(body[pos:eol].split(b";")[0], 16)
        except ValueError:
            raise PeerHttpError("bad chunk size in peer response") from None
        if size == 0:
            return bytes(out)
        start = eol + 2
        if len(body) < start + size + 2:
            raise PeerHttpError("truncated chunked peer response")
        out += body[start : start + size]
        pos = start + size + 2
