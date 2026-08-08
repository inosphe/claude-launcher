"""Multi-endpoint mesh e2e: two REAL daemon processes, one per named instance.

Where ``test_federation_integration`` wires two in-process apps together, this
test exercises the whole production shape: ``python -m claude_launcher.daemon
--name a`` and ``--name b`` run as separate OS processes under one launcher
home (tmux-server style), each with its own state dir, singleton lock, token
and ephemeral port, linked through a real psmux-relay backend bridge. All
driving happens over each daemon's HTTP API, exactly like the CLI would.

Covers: instance isolation on disk, per-instance relay identity via env,
invite/link across processes, member fan-out, and *bidirectional* message
delivery into the recipient terminals.

Skipped unless a peering-capable psmux-relay binary is available (build
mux-relay master, or point ``PSMUX_RELAY_EXE`` at one).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

import claude_launcher
from claude_launcher import store
from claude_launcher.daemon_client import DaemonClient

PASSWORD = "md-pw"
BACKEND_TOKEN = "md-backend-token"

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


def _wait(cond, what: str, timeout: float = 30.0, poll: float = 0.2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = cond()
        if result:
            return result
        time.sleep(poll)
    raise AssertionError(f"timed out waiting for {what}")


def _spawn_daemon(instance: str, relay_port: int, relay_name: str, log_path: Path):
    """Start one named daemon instance as a real detacheable subprocess."""
    env = os.environ.copy()
    env["CLAUNCH_DAEMON"] = instance
    env["CLAUNCH_RELAY_URL"] = f"ws://127.0.0.1:{relay_port}/"
    env["CLAUNCH_RELAY_TOKEN"] = BACKEND_TOKEN
    env["CLAUNCH_RELAY_NAME"] = relay_name
    # the subprocess must import the same source tree the test runs against
    src = str(Path(claude_launcher.__file__).resolve().parents[1])
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    log = open(log_path, "wb")
    try:
        return subprocess.Popen(
            [sys.executable, "-m", "claude_launcher.daemon", "--name", instance],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
        )
    finally:
        log.close()


def _client_for(home: Path, instance: str) -> DaemonClient:
    """Build an API client from the instance's own discovery + token files."""
    inst_dir = home / "daemons" / instance
    doc = json.loads((inst_dir / "daemon.json").read_text(encoding="utf-8"))
    assert doc["instance"] == instance
    token = (inst_dir / "token").read_text(encoding="utf-8").strip()
    return DaemonClient(f"http://127.0.0.1:{doc['port']}", token)


def _daemon_up(home: Path, instance: str) -> bool:
    path = home / "daemons" / instance / "daemon.json"
    if not path.is_file():
        return False
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        req = urllib.request.Request(f"http://127.0.0.1:{doc['port']}/api/health")
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            return resp.status == 200
    except Exception:
        return False


def test_mesh_between_two_daemon_processes(home, tmp_path):
    # shared config file: harness for the echo child + a fast idle threshold
    # so mesh delivery does not sit out the default 2s settle per message
    store.update(
        lambda doc: doc.update(
            {
                "harnesses": {"py": {"command": [sys.executable, "-u", "-c", CHILD]}},
                "daemon": {"idle_threshold": 0.5},
            }
        )
    )

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
    proc_a = proc_b = None
    try:
        proc_a = _spawn_daemon("a", relay_port, "pca", tmp_path / "daemon-a.log")
        proc_b = _spawn_daemon("b", relay_port, "pcb", tmp_path / "daemon-b.log")
        _wait(lambda: _daemon_up(home, "a"), "daemon instance 'a' to serve")
        _wait(lambda: _daemon_up(home, "b"), "daemon instance 'b' to serve")

        ca = _client_for(home, "a")
        cb = _client_for(home, "b")

        # two processes, two ports, two disjoint state roots under one home
        assert proc_a.poll() is None and proc_b.poll() is None
        assert ca.base_url != cb.base_url
        assert (home / "daemons" / "a" / "daemon.lock").is_file()
        assert (home / "daemons" / "b" / "daemon.lock").is_file()

        # both uplinks must register with the relay under their own names
        _wait(
            lambda: ca.get("/api/daemon")["relay"]["connected"]
            and cb.get("/api/daemon")["relay"]["connected"],
            "both relay uplinks to connect",
        )
        assert ca.get("/api/daemon")["relay"]["name"] == "pca"
        assert cb.get("/api/daemon")["relay"]["name"] == "pcb"

        # daemon A hosts the mesh and a member session
        ca.post("/api/sessions", {"name": "sa", "harness": "py", "cwd": str(tmp_path)})
        ca.post("/api/mesh", {"name": "fedmesh"})
        ca.post("/api/mesh/fedmesh/members", {"session": "sa", "handle": "alice"})
        code = ca.post("/api/mesh/fedmesh/invite")["code"]

        # daemon B redeems the invite over the relay bridge and joins bob
        cb.post("/api/sessions", {"name": "sb", "harness": "py", "cwd": str(tmp_path)})
        linked = cb.post("/api/mesh/link", {"code": code})
        assert linked["peer"] == "pca"
        cb.post("/api/mesh/fedmesh/members", {"session": "sb", "handle": "bob"})

        def _member(client, handle):
            info = client.get("/api/mesh/fedmesh")
            return next((m for m in info["members"] if m["handle"] == handle), None)

        bob_on_a = _wait(
            lambda: _member(ca, "bob"), "bob's membership to reach daemon A"
        )
        assert bob_on_a["machine"] == "pcb"
        assert _member(cb, "alice")["machine"] == "pca"

        def _capture(client, name: str) -> str:
            return client.get(f"/api/sessions/{name}/capture", raw=True).decode(
                "utf-8", "replace"
            )

        # B -> A: bob's message crosses the bridge and lands in alice's PTY
        sent = cb.post(
            "/api/mesh/fedmesh/messages",
            {"from": "bob", "to": "alice", "body": "ping from instance b"},
        )
        assert sent["queued_remote"] == []  # relay was connected -> forwarded live
        _wait(
            lambda: "ping from instance b" in _capture(ca, "sa"),
            "cross-daemon delivery into alice's terminal",
            timeout=40.0,
        )

        # A -> B: and the reverse direction through the same link
        ca.post(
            "/api/mesh/fedmesh/messages",
            {"from": "alice", "to": "bob", "body": "pong from instance a"},
        )
        _wait(
            lambda: "pong from instance a" in _capture(cb, "sb"),
            "cross-daemon delivery into bob's terminal",
            timeout=40.0,
        )

        # clean shutdown via the API, like `claunch -L a daemon stop`
        ca.post("/api/daemon/shutdown")
        cb.post("/api/daemon/shutdown")
        assert proc_a.wait(timeout=20) == 0
        assert proc_b.wait(timeout=20) == 0
        proc_a = proc_b = None
    finally:
        for proc in (proc_a, proc_b):
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        relay.terminate()
        try:
            relay.wait(timeout=5)
        except subprocess.TimeoutExpired:
            relay.kill()
