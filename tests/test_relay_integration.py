"""End-to-end integration across both repos: real relay binary + real daemon
app (in-process) + real uplink, driven through the browser's HTTP flow.

Proves the two-layer auth the design promises:
  outer gate = relay login cookie (relay_session)
  inner gate = the daemon's own token/cookie (claunch_session)

A request that clears only the outer gate still can't reach a protected daemon
endpoint. Only after authenticating to the daemon *through the tunnel* does it
succeed — which is exactly how the SPA behaves.

Skipped unless the psmux-relay binary is available (built in the sibling
worktree, or pointed to by ``PSMUX_RELAY_EXE``).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from pathlib import Path

import pytest
from aiohttp import web

from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.relay_uplink import RelayUplink

PASSWORD = "browser-pw-int"
BACKEND_TOKEN = "backend-token-int-xyz"
DAEMON_TOKEN = "daemon-sekrit-int"
NAME = "intpc"


def _find_relay_exe() -> Path | None:
    env = os.environ.get("PSMUX_RELAY_EXE")
    if env and Path(env).is_file():
        return Path(env)
    exe = "psmux-relay.exe" if os.name == "nt" else "psmux-relay"
    # sibling psmux-relay worktree, both debug and release
    roots = [
        Path("F:/works/psmux-relay/.claude/worktrees/relay-tunnel/target"),
        Path("F:/works/psmux-relay/target"),
    ]
    for root in roots:
        for prof in ("debug", "release"):
            p = root / prof / exe
            if p.is_file():
                return p
    return None


RELAY_EXE = _find_relay_exe()
pytestmark = pytest.mark.skipif(
    RELAY_EXE is None, reason="psmux-relay binary not found (build the relay-tunnel worktree)"
)


def _free_port() -> int:
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def _raw_http(port: int, request: str) -> tuple[int, dict, bytes]:
    """Send one raw HTTP request, read until close, return (status, headers, body)."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(request.encode("utf-8") if isinstance(request, str) else request)
    await writer.drain()
    raw = await asyncio.wait_for(reader.read(-1), timeout=10)
    writer.close()
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    status = int(lines[0].split()[1])
    headers: dict[str, list[str]] = {}
    for line in lines[1:]:
        k, _, v = line.partition(b":")
        headers.setdefault(k.decode().strip().lower(), []).append(v.decode().strip())
    return status, headers, body


def _set_cookie_value(headers: dict) -> str | None:
    for sc in headers.get("set-cookie", []):
        return sc.split(";")[0]  # "name=value"
    return None


async def _wait_registered(port: int, cookie: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status, _h, body = await _raw_http(
            port,
            f"GET /dir HTTP/1.1\r\nHost: r\r\nCookie: {cookie}\r\nConnection: close\r\n\r\n",
        )
        if status == 200 and NAME.encode() in body:
            return
        await asyncio.sleep(0.3)
    raise AssertionError("backend never appeared in /dir")


def test_two_layer_auth_through_tunnel(home, tmp_path):
    async def run():
        relay_port = _free_port()
        cfg_dir = tmp_path / "relay"
        cfg_dir.mkdir()

        relay = subprocess.Popen(
            [
                str(RELAY_EXE),
                "--ws-plain",
                "--ws-addr", f"127.0.0.1:{relay_port}",
                "--config", str(cfg_dir / "relay.toml"),
                "--password", PASSWORD,
                "--backend-token", BACKEND_TOKEN,
                "--web-dir", str(cfg_dir / "noweb"),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=False)
        app = build_app(mgr, DAEMON_TOKEN, started_at=time.monotonic())
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        dport = _free_port()
        site = web.TCPSite(runner, "127.0.0.1", dport)
        await site.start()

        uplink = RelayUplink(
            url=f"ws://127.0.0.1:{relay_port}/",
            token=BACKEND_TOKEN,
            name=NAME,
            local_host="127.0.0.1",
            local_port=dport,
        )
        uplink_task = asyncio.ensure_future(uplink.run())

        try:
            # wait for the relay port to accept
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    r, w_ = await asyncio.open_connection("127.0.0.1", relay_port)
                    w_.close()
                    break
                except OSError:
                    await asyncio.sleep(0.2)

            # --- outer gate: relay login ---
            bad = f'{{"password":"nope"}}'
            status, _h, _b = await _raw_http(
                relay_port,
                f"POST /login HTTP/1.1\r\nHost: r\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(bad)}\r\nConnection: close\r\n\r\n{bad}",
            )
            assert status == 401, "wrong relay password accepted"

            good = f'{{"password":"{PASSWORD}"}}'
            status, headers, _b = await _raw_http(
                relay_port,
                f"POST /login HTTP/1.1\r\nHost: r\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(good)}\r\nConnection: close\r\n\r\n{good}",
            )
            assert status == 200
            relay_cookie = _set_cookie_value(headers)
            assert relay_cookie and relay_cookie.startswith("relay_session=")

            await _wait_registered(relay_port, relay_cookie)

            # tunnel reaches the daemon: open /api/health (daemon-open endpoint)
            status, _h, body = await _raw_http(
                relay_port,
                f"GET /t/{NAME}/api/health HTTP/1.1\r\nHost: r\r\nCookie: {relay_cookie}\r\n"
                f"Connection: close\r\n\r\n",
            )
            assert status == 200 and b'"status": "ok"' in body, body

            # --- inner gate: a protected daemon endpoint still needs daemon auth ---
            status, _h, _b = await _raw_http(
                relay_port,
                f"GET /t/{NAME}/api/daemon HTTP/1.1\r\nHost: r\r\nCookie: {relay_cookie}\r\n"
                f"Connection: close\r\n\r\n",
            )
            assert status == 401, "daemon endpoint reachable without daemon auth!"

            # authenticate to the daemon *through the tunnel* (as the SPA does)
            authbody = f'{{"token":"{DAEMON_TOKEN}"}}'
            status, headers, _b = await _raw_http(
                relay_port,
                f"POST /t/{NAME}/api/auth/session HTTP/1.1\r\nHost: r\r\nCookie: {relay_cookie}\r\n"
                f"Content-Type: application/json\r\nContent-Length: {len(authbody)}\r\n"
                f"Connection: close\r\n\r\n{authbody}",
            )
            assert status == 200, "daemon auth through tunnel failed"
            daemon_cookie = _set_cookie_value(headers)
            assert daemon_cookie and daemon_cookie.startswith("claunch_session=")
            # the daemon's Set-Cookie Path must be rewritten under the tunnel prefix
            assert any(f"Path=/t/{NAME}/" in sc for sc in headers["set-cookie"]), headers["set-cookie"]

            # now both gates cleared → protected endpoint returns 200
            status, _h, body = await _raw_http(
                relay_port,
                f"GET /t/{NAME}/api/daemon HTTP/1.1\r\nHost: r\r\n"
                f"Cookie: {relay_cookie}; {daemon_cookie}\r\nConnection: close\r\n\r\n",
            )
            assert status == 200 and b'"sessions"' in body, body
        finally:
            uplink.stop()
            uplink_task.cancel()
            try:
                await uplink_task
            except (asyncio.CancelledError, Exception):
                pass
            await runner.cleanup()
            relay.terminate()
            try:
                relay.wait(timeout=5)
            except subprocess.TimeoutExpired:
                relay.kill()

    asyncio.run(run())
