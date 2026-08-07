"""Cross-repo federation e2e: two in-process daemons linked through a real
psmux-relay backend bridge (PEER_OPEN), exchanging a mesh over the wire.

Covers the full remote path the unit tests fake: invite minted on daemon A,
redeemed on daemon B — the link handshake, member push and message forward all
travel as raw HTTP over relay-bridged streams — and the message physically
lands in A's recipient terminal via paste injection.

Skipped unless a peering-capable psmux-relay binary is available (build the
mux-relay ``backend-peering`` branch, or point ``PSMUX_RELAY_EXE`` at one).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from aiohttp import web

from claude_launcher import store
from claude_launcher.daemon.__main__ import _wire_federation
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.mesh import MeshManager
from claude_launcher.daemon.relay_uplink import RelayUplink

PASSWORD = "fed-pw"
BACKEND_TOKEN = "fed-backend-token"

CHILD = (
    "import sys\n"
    "print('READY')\n"
    "for line in sys.stdin:\n"
    "    print('echo:' + line.strip())\n"
)


def _find_relay_exe() -> Path | None:
    env = os.environ.get("PSMUX_RELAY_EXE")
    if env and Path(env).is_file():
        return Path(env)
    exe = "psmux-relay.exe" if os.name == "nt" else "psmux-relay"
    # the main mux-relay checkout first: peering landed on its backend-peering
    # branch, so older worktree builds may predate PEER_OPEN (we still check
    # CAP_PEERING at runtime and skip if the binary is too old)
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


class _Daemon:
    """One in-process claunch daemon: app + manager + mesh + relay uplink."""

    def __init__(self, name: str, relay_port: int, mesh_root: Path) -> None:
        self.name = name
        self.manager = SessionManager(
            idle_threshold=0.5, scrollback=200, restore_default=False
        )
        self.mesh = MeshManager(self.manager, settle=0.05, root=mesh_root)
        self.app = build_app(
            self.manager, f"token-{name}", started_at=time.monotonic(),
            mesh=self.mesh,
        )
        self.relay_port = relay_port
        self.runner: web.AppRunner | None = None
        self.uplink: RelayUplink | None = None
        self.uplink_task: asyncio.Task | None = None

    async def start(self) -> None:
        self.runner = web.AppRunner(self.app, access_log=None)
        await self.runner.setup()
        port = _free_port()
        site = web.TCPSite(self.runner, "127.0.0.1", port)
        await site.start()
        self.uplink = RelayUplink(
            url=f"ws://127.0.0.1:{self.relay_port}/",
            token=BACKEND_TOKEN,
            name=self.name,
            local_host="127.0.0.1",
            local_port=port,
        )
        self.uplink_task = asyncio.ensure_future(self.uplink.run())
        _wire_federation(self.mesh, self.uplink)
        self.mesh.start()

    async def stop(self) -> None:
        if self.uplink is not None:
            self.uplink.stop()
        if self.uplink_task is not None:
            self.uplink_task.cancel()
            try:
                await self.uplink_task
            except (asyncio.CancelledError, Exception):
                pass
        await self.mesh.shutdown()
        await self.manager.shutdown_all()
        if self.runner is not None:
            await self.runner.cleanup()


async def _wait(cond, what: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


def test_mesh_federation_over_real_relay(home, tmp_path):
    store.update(
        lambda doc: doc.update(
            {"harnesses": {"py": {"command": [sys.executable, "-u", "-c", CHILD]}}}
        )
    )

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
                "--allow-backend-peering",
                "--web-dir", str(cfg_dir / "noweb"),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        a = _Daemon("pca", relay_port, tmp_path / "meshA")
        b = _Daemon("pcb", relay_port, tmp_path / "meshB")
        try:
            await a.start()
            await b.start()
            await _wait(
                lambda: a.uplink.connected and b.uplink.connected,
                "both uplinks to register",
            )
            if not a.uplink.peering:
                pytest.skip("relay binary lacks CAP_PEERING — rebuild mux-relay")

            # daemon A hosts the mesh and a recipient session
            a.manager.create(SessionDef(name="sa", harness="py", cwd=str(tmp_path)))
            a.mesh.create("fedmesh")
            a.mesh.join("fedmesh", "sa", handle="alice")
            code = a.mesh.invite("fedmesh")["code"]

            # daemon B redeems the invite: the handshake crosses the relay
            b.manager.create(SessionDef(name="sb", harness="py", cwd=str(tmp_path)))
            result = await b.mesh.link(code)
            assert result["peer"] == "pca"
            assert b.mesh.get("fedmesh").members["alice"].machine == "pca"

            # B's join fans out over the bridge too
            b.mesh.join("fedmesh", "sb", handle="bob")
            await _wait(
                lambda: "bob" in a.mesh.get("fedmesh").members,
                "member push to reach daemon A",
            )
            assert a.mesh.get("fedmesh").members["bob"].machine == "pcb"

            # a message from bob is forwarded by B's worker and typed into
            # alice's terminal by A's worker
            sent = b.mesh.send("fedmesh", "bob", "alice", "hello over relay")
            assert sent["queued_remote"] == []  # relay is connected
            session_a = a.manager.get("sa")
            await _wait(
                lambda: "hello over relay" in "\n".join(session_a.capture()),
                "cross-machine delivery into the recipient terminal",
                timeout=25.0,
            )
            mesh_b = b.mesh.get("fedmesh")
            assert b.mesh.pending_for_machine(mesh_b, "pca") == []
            assert mesh_b.peer_status["pca"]["ok"] is True
        finally:
            await b.stop()
            await a.stop()
            relay.terminate()
            try:
                relay.wait(timeout=5)
            except subprocess.TimeoutExpired:
                relay.kill()

    asyncio.run(run())
