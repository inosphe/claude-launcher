"""The daemon's side of the PTY host: :class:`HostClient` and :class:`RemotePty`.

:class:`RemotePty` implements :class:`pty_backend.PtyHandle`, so a
:class:`~claude_launcher.daemon.session.Session` runs on it unchanged: its
reader thread blocks in :meth:`RemotePty.read`, its writes go out through the
executor as before, and ``reap``/``isalive`` answer from the host's ``exit``
event. :func:`pty_backend.spawn` hands one out once the daemon has installed a
client with :func:`pty_backend.use_host`.

The client is synchronous on purpose: every caller of a PTY handle is already
a thread (the session reader, the executor running ``write``, the spawn in
``start_async``), and one receive thread routes the host's events to the
handle they belong to.
"""

from __future__ import annotations

import itertools
import logging
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, Optional, Sequence

from ..pty_backend import PtyError, PtyHandle, REAP_TIMEOUT
from . import OWNER_ENV, PROTO_VERSION, log_path, pid_path, socket_path, token_path
from .protocol import ProtocolError, encode, read_frame

log = logging.getLogger(__name__)

#: Seconds a request waits for its reply. Spawning a child is the slow one.
REQUEST_TIMEOUT = 30.0

#: Seconds :func:`ensure_host` waits for a host it started to listen.
START_TIMEOUT = 10.0


class HostUnavailable(PtyError):
    """No PTY host could be reached (or the connection to it ended)."""


def _writer_key(writer: object) -> str:
    """A writer identity that can cross the socket.

    The Windows backend keeps one partial character per writer (see
    ``pty_backend._WinPty.write``); the writers are objects in this process,
    so they travel as a stable string instead.
    """
    if writer is None:
        return ""
    if isinstance(writer, str):
        return writer
    return f"{type(writer).__name__}:{id(writer):x}"


class RemotePty(PtyHandle):
    """One session's PTY, held by the host."""

    def __init__(self, client: "HostClient", sid: str, name: str) -> None:
        self._client = client
        self.sid = sid
        self.name = name
        self.pid: Optional[int] = None
        #: The host's count of this session's output up to the last byte
        #: :meth:`read` returned. Whoever reads also logs (the session's
        #: reader thread), so after :meth:`detach` it is where the log ends
        #: and what the next daemon passes to ``attach`` as ``from_seq``.
        self.seq = 0
        #: Set by :meth:`detach`: the handle let go of a session that is
        #: still running in the host. :meth:`read` has returned EOF, and
        #: nobody may treat that as the child's exit.
        self.detached = False
        self._out: "queue.Queue[bytes]" = queue.Queue()
        self._exited = threading.Event()
        self._code: Optional[int] = None
        self._closed = False

    # -- events from the receive thread ----------------------------------
    def _on_output(self, data: bytes) -> None:
        if data and not self._exited.is_set():
            self._out.put(data)

    def _on_exit(self, code: Optional[int]) -> None:
        if self._exited.is_set():
            return
        self._code = code
        self._exited.set()
        self._out.put(b"")

    # -- PtyHandle ---------------------------------------------------------
    def read(self) -> bytes:
        chunk = self._out.get()
        self.seq += len(chunk)
        return chunk

    def detach(self) -> None:
        """Let go of the session and leave it running in the host.

        No more events are routed here; the chunks already queued are still
        read, and then :meth:`read` returns EOF. The host keeps what follows
        in its ring for the next ``attach``.
        """
        if self.detached or self._exited.is_set():
            return
        self.detached = True
        self._client._forget(self.sid)
        self._out.put(b"")

    def write(self, data: bytes, writer: object = None) -> None:
        if self._exited.is_set():
            raise OSError("session has exited")
        self._client.send({"op": "write", "sid": self.sid, "writer": _writer_key(writer)}, data)

    def forget_writer(self, writer: object) -> None:
        if not self._exited.is_set():
            self._client.send({"op": "forget_writer", "sid": self.sid,
                               "writer": _writer_key(writer)}, quiet=True)

    def resize(self, cols: int, rows: int) -> None:
        if not self._exited.is_set():
            self._client.send({"op": "resize", "sid": self.sid, "cols": int(cols), "rows": int(rows)})

    def isalive(self) -> bool:
        return not self._exited.is_set() and not self.detached

    def exit_code(self) -> Optional[int]:
        return self._code if self._exited.is_set() else None

    def reap(self, timeout: float = REAP_TIMEOUT) -> Optional[int]:
        self._exited.wait(timeout)
        return self.exit_code()

    def terminate(self, force: bool = False) -> None:
        if not self._exited.is_set():
            self._client.send({"op": "terminate", "sid": self.sid, "force": bool(force)}, quiet=True)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if not self.detached:
            # A detached session is not this handle's to end.
            self._client.send({"op": "close", "sid": self.sid}, quiet=True)
        self._client._forget(self.sid)


class HostClient:
    """One connection to a PTY host."""

    def __init__(self, sock: socket.socket, *, host_pid: Optional[int] = None,
                 directory: Optional[Path] = None) -> None:
        self._sock = sock
        self.host_pid = host_pid
        self.directory = directory
        self._send_lock = threading.Lock()
        self._ids = itertools.count(1)
        self._pending: Dict[int, list] = {}
        self._pending_lock = threading.Lock()
        self._ptys: Dict[str, RemotePty] = {}
        self._ptys_lock = threading.Lock()
        self.closed = threading.Event()
        self._recv = threading.Thread(target=self._recv_loop, name="ptyhost-client-recv", daemon=True)

    # -- connect -----------------------------------------------------------
    @classmethod
    def connect(cls, directory: Path, *, timeout: float = 5.0) -> "HostClient":
        path = socket_path(Path(directory))
        try:
            token = token_path(Path(directory)).read_text(encoding="ascii").strip()
        except OSError as exc:
            raise HostUnavailable(f"no PTY host token in {directory}: {exc}") from exc
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect(str(path))
            sock.sendall(encode({"op": "hello", "id": 0, "token": token,
                                 "proto_min": PROTO_VERSION, "proto_max": PROTO_VERSION,
                                 "client": f"daemon:{os.getpid()}"}))
            frame = read_frame(sock)
        except (OSError, ProtocolError) as exc:
            sock.close()
            raise HostUnavailable(f"could not reach the PTY host at {path}: {exc}") from exc
        if frame is None or frame[0].get("op") != "reply" or not frame[0].get("ok"):
            sock.close()
            why = frame[0].get("error") if frame else "connection closed"
            raise HostUnavailable(f"the PTY host refused the connection: {why}")
        sock.settimeout(None)
        client = cls(sock, host_pid=frame[0].get("pid"), directory=Path(directory))
        client._recv.start()
        return client

    # -- sending -----------------------------------------------------------
    def send(self, header: dict, payload: bytes = b"", *, quiet: bool = False) -> None:
        """Send a one-way frame. ``quiet`` drops it if the host is gone
        (for frames whose only purpose is cleanup)."""
        if self.closed.is_set():
            if quiet:
                return
            raise OSError("the PTY host connection is closed")
        frame = encode(header, payload)
        try:
            with self._send_lock:
                self._sock.sendall(frame)
        except OSError:
            self._shutdown("send failed")
            if not quiet:
                raise

    def request(self, header: dict, payload: bytes = b"", *, timeout: float = REQUEST_TIMEOUT) -> dict:
        req_id = next(self._ids)
        slot = [threading.Event(), None, b""]
        with self._pending_lock:
            self._pending[req_id] = slot
        try:
            try:
                self.send({**header, "id": req_id}, payload)
            except OSError as exc:
                raise HostUnavailable(str(exc)) from exc
            if not slot[0].wait(timeout):
                raise HostUnavailable(f"the PTY host did not answer {header.get('op')!r} in {timeout:.0f}s")
        finally:
            with self._pending_lock:
                self._pending.pop(req_id, None)
        reply = slot[1]
        if reply is None:
            raise HostUnavailable("the PTY host connection ended")
        if not reply.get("ok"):
            raise PtyError(str(reply.get("error") or "the PTY host refused the request"))
        if slot[2]:
            reply = {**reply, "payload": slot[2]}
        return reply

    # -- operations ----------------------------------------------------------
    def spawn(self, argv: Sequence[str], *, env: Dict[str, str], cwd: str,
              cols: int, rows: int, name: str = "") -> RemotePty:
        sid = uuid.uuid4().hex
        label = name or (os.path.basename(str(argv[0])) if argv else sid)
        handle = RemotePty(self, sid, label)
        # Registered before the request: the host starts the reader right
        # after its reply, and output must have somewhere to go.
        with self._ptys_lock:
            self._ptys[sid] = handle
        try:
            reply = self.request({"op": "spawn", "sid": sid, "name": handle.name,
                                  "argv": list(argv), "env": dict(env), "cwd": str(cwd),
                                  "cols": int(cols), "rows": int(rows)})
        except Exception:
            self._forget(sid)
            raise
        handle.pid = reply.get("pid")
        return handle

    def attach(self, sid: str, *, name: str = "", from_seq: int = 0) -> "tuple":
        """Re-attach to a session the host kept: ``(handle, replay, reply)``.

        ``replay`` is the output the host kept from ``from_seq`` on (from the
        ring's start when ``reply["gap"]`` bytes fell out of it); live output
        continues in ``handle`` from ``reply["seq"]``. A session that exited
        while nobody was attached comes back as a handle that has exited,
        with the host's code.
        """
        handle = RemotePty(self, sid, name or sid)
        # Registered before the request, as in spawn: the host's next output
        # event for it follows the reply directly.
        with self._ptys_lock:
            self._ptys[sid] = handle
        try:
            reply = self.request({"op": "attach", "sid": sid, "from_seq": int(from_seq)})
        except Exception:
            self._forget(sid)
            raise
        handle.pid = reply.get("pid")
        handle.seq = int(reply.get("seq") or 0)
        if reply.get("exited"):
            handle._on_exit(reply.get("code"))
        return handle, bytes(reply.get("payload") or b""), reply

    def ack_exit(self, sid: str) -> bool:
        """Drop the host's record of an exit the daemon has now recorded."""
        return bool(self.request({"op": "ack_exit", "sid": sid}, timeout=5.0).get("acked"))

    def end_all(self, *, timeout: float = 15.0) -> None:
        """End every session the host holds (attached or not)."""
        self.request({"op": "end_all"}, timeout=timeout)

    def list(self) -> list:
        return list(self.request({"op": "list"}).get("sessions") or [])

    def shutdown_host(self, *, timeout: float = 10.0) -> None:
        """Ask the host to end its sessions and exit, then close.

        Returns once the host has removed its pid file (the last thing it
        does after ending its sessions) or ``timeout`` passed."""
        try:
            self.request({"op": "shutdown"}, timeout=timeout)
        finally:
            self.close()
        if self.directory is not None:
            deadline = time.monotonic() + timeout
            while pid_path(self.directory).exists() and time.monotonic() < deadline:
                time.sleep(0.05)

    def ping(self) -> dict:
        return self.request({"op": "ping"}, timeout=5.0)

    def close(self) -> None:
        self._shutdown("closed by the daemon")

    # -- receiving -----------------------------------------------------------
    def _forget(self, sid: str) -> None:
        with self._ptys_lock:
            self._ptys.pop(sid, None)

    def _recv_loop(self) -> None:
        why = "the PTY host closed the connection"
        try:
            while True:
                frame = read_frame(self._sock)
                if frame is None:
                    break
                header, payload = frame
                op = header["op"]
                if op == "reply":
                    with self._pending_lock:
                        slot = self._pending.get(header.get("id"))
                    if slot is not None:
                        slot[1] = header
                        slot[2] = payload
                        slot[0].set()
                    continue
                with self._ptys_lock:
                    handle = self._ptys.get(str(header.get("sid", "")))
                if handle is None:
                    continue
                if op == "output":
                    handle._on_output(payload)
                elif op == "exit":
                    handle._on_exit(header.get("code"))
        except (OSError, ProtocolError) as exc:
            why = f"the PTY host connection failed: {exc}"
        self._shutdown(why)

    def _shutdown(self, why: str) -> None:
        if self.closed.is_set():
            return
        self.closed.set()
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass
        with self._pending_lock:
            pending = list(self._pending.values())
        for slot in pending:
            slot[0].set()
        with self._ptys_lock:
            handles = list(self._ptys.values())
            self._ptys.clear()
        if handles:
            log.warning("%s; %d session(s) end with it", why, len(handles))
        for handle in handles:
            # Unknown code: the host was the only one who could collect it.
            handle._on_exit(None)


def host_alive(directory: Path) -> Optional[int]:
    """The pid in the host's pid file if that process exists, else None."""
    try:
        pid = int(pid_path(Path(directory)).read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        return pid
    return pid


#: Every host this process started, newest last. tests/conftest.py checks
#: that none is still running when a test ends.
_started: list = []


def start_host(directory: Path, *, idle_exit: float = 30.0) -> subprocess.Popen:
    """Start a detached host for ``directory`` (its own session, no terminal)."""
    from ..statsworker import child_env  # the same "run the daemon's code" rule

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    env = child_env()
    stderr = open(log_path(directory), "ab")
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "claude_launcher.daemon.ptyhost",
             "--dir", str(directory), "--idle-exit", str(idle_exit)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=stderr,
            cwd=str(directory), env=env, close_fds=True, start_new_session=True,
        )
    finally:
        stderr.close()
    _started.append(proc)
    return proc


def ensure_host(directory: Path, *, idle_exit: float = 30.0,
                timeout: float = START_TIMEOUT) -> HostClient:
    """Connect to the host for ``directory``, starting one if none answers."""
    directory = Path(directory)
    try:
        return HostClient.connect(directory)
    except HostUnavailable as exc:
        if host_alive(directory) is not None:
            raise  # a host is there and refused us: starting another would not help
        log.info("starting a PTY host for %s (%s)", directory, exc)
    proc = start_host(directory, idle_exit=idle_exit)
    deadline = time.monotonic() + timeout
    last: Optional[Exception] = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise HostUnavailable(
                f"the PTY host exited with {proc.returncode} at start; see {log_path(directory)}"
            )
        try:
            return HostClient.connect(directory)
        except HostUnavailable as exc:
            last = exc
            time.sleep(0.05)
    raise HostUnavailable(f"the PTY host did not start listening in {timeout:.0f}s: {last}")


__all__ = ["HostClient", "HostUnavailable", "RemotePty", "ensure_host", "host_alive",
           "start_host", "OWNER_ENV"]
