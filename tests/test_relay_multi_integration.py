"""End-to-end proof that one daemon can hold two real relays open at once.

Two psmux-relay processes are started on separate ports. Backend ``pca``
registers with BOTH through a :class:`RelayPool`; ``pcb`` registers only with
the second relay and ``pcc`` only with the first. Then ``pca`` is asked to
reach each of them, which it can only do by routing each request to the relay
that actually carries that name.

The unit tests in ``test_relay_pool.py`` cover the routing logic against a
stub. This covers it against relay processes and real WebSocket frames, so a
protocol-level mistake in the multi-uplink path cannot pass unseen.

Skipped unless a peering-capable psmux-relay binary is available (build the
mux-relay ``backend-peering`` branch, or point ``PSMUX_RELAY_EXE`` at one).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from pathlib import Path

import pytest
from aiohttp import web

from claude_launcher.daemon.relay_uplink import PeerError, RelayPool, RelayUplink

PASSWORD = "multi-pw"
BACKEND_TOKEN = "multi-backend-token"


def _find_relay_exe() -> Path | None:
    env = os.environ.get("PSMUX_RELAY_EXE")
    if env and Path(env).is_file():
        return Path(env)
    exe = "psmux-relay.exe" if os.name == "nt" else "psmux-relay"
    roots = [
        Path("F:/works/mux-relay/target"),
        Path("F:/works/mux-relay/.claude/worktrees/relay-tunnel/target"),
    ]
    for root in roots:
        for prof in ("debug", "release"):
            p = root / prof / exe
            if p.is_file():
                return p
    return None


RELAY_EXE = _find_relay_exe()
pytestmark = pytest.mark.skipif(
    RELAY_EXE is None, reason="psmux-relay binary not found"
)


def _free_port() -> int:
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _start_relay(port: int, cfg_dir: Path) -> subprocess.Popen:
    cfg_dir.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen(
        [
            str(RELAY_EXE),
            "--ws-plain",
            "--ws-addr", f"127.0.0.1:{port}",
            "--config", str(cfg_dir / "relay.toml"),
            "--password", PASSWORD,
            "--backend-token", BACKEND_TOKEN,
            "--allow-backend-peering",
            "--web-dir", str(cfg_dir / "noweb"),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


async def _serve_whoami(name: str) -> tuple[web.AppRunner, int]:
    """A minimal local HTTP server standing in for the daemon's own port.

    The uplink pipes relay streams straight to a loopback TCP port, so what
    listens there is irrelevant to the routing under test — only that the
    answer identifies which backend replied.
    """
    app = web.Application()

    async def whoami(_request: web.Request) -> web.Response:
        return web.Response(text=name)

    app.router.add_get("/whoami", whoami)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    port = _free_port()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    return runner, port


def _whoami_request() -> bytes:
    # Self-delimiting, per the relay's 1 request = 1 stream convention.
    return (
        b"GET /whoami HTTP/1.1\r\n"
        b"Host: peer\r\n"
        b"Connection: close\r\n"
        b"\r\n"
    )


async def _wait(cond, what: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


def test_one_daemon_spans_two_relays(tmp_path):
    async def run():
        port_a = _free_port()
        port_b = _free_port()
        relays = [
            _start_relay(port_a, tmp_path / "relayA"),
            _start_relay(port_b, tmp_path / "relayB"),
        ]
        runners: list[web.AppRunner] = []
        tasks: list[asyncio.Task] = []
        pools: list[RelayPool] = []

        def uplink(name: str, port: int, local_port: int, ident: str) -> RelayUplink:
            return RelayUplink(
                url=f"ws://127.0.0.1:{port}/",
                token=BACKEND_TOKEN,
                name=name,
                local_host="127.0.0.1",
                local_port=local_port,
                id=ident,
            )

        try:
            # pca: one backend, both relays.
            runner, local = await _serve_whoami("pca")
            runners.append(runner)
            pca = RelayPool([
                uplink("pca", port_a, local, "work"),
                uplink("pca", port_b, local, "home"),
            ])
            # pcb reachable only on relay B, pcc only on relay A.
            runner, local = await _serve_whoami("pcb")
            runners.append(runner)
            pcb = RelayPool([uplink("pcb", port_b, local, "home")])
            runner, local = await _serve_whoami("pcc")
            runners.append(runner)
            pcc = RelayPool([uplink("pcc", port_a, local, "work")])

            pools = [pca, pcb, pcc]
            for pool in pools:
                tasks.append(asyncio.ensure_future(pool.run()))

            await _wait(
                lambda: all(
                    up.connected for pool in pools for up in pool.uplinks
                ),
                "all four uplinks to register",
            )
            if not all(up.peering for up in pca.uplinks):
                pytest.skip("relay binary lacks CAP_PEERING — rebuild mux-relay")

            # Both relays are up, so the aggregate says connected and the
            # per-relay rows say which is which.
            state = pca.state()
            assert state["count"] == 2 and state["connected_count"] == 2
            assert [r["id"] for r in state["relays"]] == ["work", "home"]

            # The directory pca sees is the union of the two relays'.
            peers = await pca.peer_list()
            assert "pcb" in peers, peers  # only relay B knows this one
            assert "pcc" in peers, peers  # only relay A knows this one

            # And each request goes out over the relay that carries the name.
            resp = await pca.peer_http("pcb", _whoami_request())
            assert resp.endswith(b"pcb"), resp
            resp = await pca.peer_http("pcc", _whoami_request())
            assert resp.endswith(b"pcc"), resp

            # A name on no relay fails with both relays' reasons, not one.
            with pytest.raises(PeerError) as err:
                await pca.peer_http("ghost", _whoami_request())
            assert "work:" in str(err.value) and "home:" in str(err.value)

            # Losing one relay does not cost the backends on the other: kill
            # relay A and pcb (which lives on relay B) is still reachable.
            relays[0].terminate()
            relays[0].wait(timeout=10)
            await _wait(
                lambda: not pca.uplinks[0].connected, "the work uplink to drop"
            )
            assert pca.connected is True
            assert pca.state()["connected_count"] == 1
            resp = await pca.peer_http("pcb", _whoami_request())
            assert resp.endswith(b"pcb"), resp
        finally:
            for pool in pools:
                pool.stop()
            for task in tasks:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            for runner in runners:
                await runner.cleanup()
            for proc in relays:
                if proc.poll() is None:  # the test kills one of them itself
                    proc.terminate()
                proc.wait(timeout=10)

    asyncio.run(run())
