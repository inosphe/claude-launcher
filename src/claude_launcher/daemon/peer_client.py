"""Build/parse the one-shot HTTP requests daemons exchange over relay bridges.

A peer call is a single raw HTTP/1.1 request-response on a bridged stream
(``RelayUplink.peer_http``): the request carries ``Connection: close`` and a
``Content-Length`` so the peer's aiohttp server treats the stream like any
relay ingress connection, and the response is read to EOF.
"""

from __future__ import annotations

import json
from typing import Tuple


def build_request(path: str, body: dict, *, host: str = "peer") -> bytes:
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
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
    pass


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
            f"peer returned non-JSON body (status {status})"
        ) from None
    return status, doc if isinstance(doc, dict) else {}


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
