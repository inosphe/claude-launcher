"""The PTY host process: ``python -m claude_launcher.daemon.ptyhost --dir D``.

Owns, per session, the PTY handle (:func:`pty_backend.spawn_local`), the
child (the host is its parent, so only the host can collect its exit code),
a reader thread and a writer thread, and a bounded ring of the most recent
output with its cumulative byte count (``seq``). Sessions are keyed by an
``sid`` the client chooses per spawn, so a respawn under the same session
name can never receive an event of the one it replaced.

One client at a time; a second connection is refused. A session is
*attached* to the client that spawned it, or that sent ``attach`` for it
later: the host forwards its output chunks and its exit as events to that
client only. When the client goes away (a daemon restart, or a daemon that was
killed) every session is detached and keeps running; its output collects in
the ring, and an exit is kept until a client acknowledges it (``ack_exit``).
The next daemon re-attaches with ``attach{sid, from_seq}`` and gets the bytes
it missed (stage 2, claunch-xnnct.1). Sessions end only when asked:
``terminate``, ``end``, ``end_all`` or ``shutdown``. With no sessions and no client
for ``idle_exit`` seconds -- or once its directory is gone -- the host exits
by itself, so a test home that was removed never leaves one behind.
"""

from __future__ import annotations

import argparse
import hmac
import logging
import os
import queue
import secrets
import signal
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Dict, Optional

from .. import pty_backend
from . import PROTO_VERSION, log_path, pid_path, socket_path, token_path
from .protocol import ProtocolError, encode, read_frame

log = logging.getLogger("claunch.ptyhost")

#: Output kept per session for a client that (re)attaches: stage 2 replays it.
RING_MAX = 1 << 20

#: Seconds between liveness polls. ConPTY can deliver EOF late (session.py
#: keeps the same safety net), so an exit is also taken from ``isalive``.
POLL_INTERVAL = 0.5

#: A child that is no longer alive but whose reader has not seen EOF this
#: long after is treated as exited.
LATE_EOF_GRACE = 2.0


class HostSession:
    def __init__(self, host: "Host", sid: str, name: str, pty) -> None:
        self.host = host
        self.sid = sid
        self.name = name
        self.pty = pty
        self.pid = pty.pid
        self.ring = bytearray()
        self.seq = 0
        #: Whether the connected client gets this session's events. Read and
        #: written under ``_lock``, so an ``attach`` reply and the output
        #: events around it are ordered by ``seq`` (see :meth:`Host.attach`).
        self.attached = True
        self.exited = threading.Event()
        self.exit_code: Optional[int] = None
        self.exited_at: Optional[float] = None
        self.dead_since: Optional[float] = None
        self._lock = threading.Lock()
        self._inbox: "queue.Queue[Optional[tuple]]" = queue.Queue()
        self._reader = threading.Thread(
            target=self._read_loop, name=f"ptyhost-read-{name}", daemon=True
        )
        self._writer = threading.Thread(
            target=self._write_loop, name=f"ptyhost-write-{name}", daemon=True
        )

    def start(self) -> None:
        self._reader.start()
        self._writer.start()

    # -- output ----------------------------------------------------------
    def _read_loop(self) -> None:
        while True:
            try:
                chunk = self.pty.read()
            except Exception:  # a backend error is an end of output
                log.exception("read failed for %s", self.name)
                chunk = b""
            if not chunk:
                break
            with self._lock:
                self.ring += chunk
                if len(self.ring) > RING_MAX:
                    del self.ring[: len(self.ring) - RING_MAX]
                self.seq += len(chunk)
                # Sent under the lock: an attach (which takes it too) then
                # sees every chunk either in its replay or after its reply,
                # never both and never neither.
                if self.attached:
                    self.host.emit({"op": "output", "sid": self.sid, "seq": self.seq}, chunk)
        self.finish()

    @property
    def ring_start(self) -> int:
        """The ``seq`` of the first byte still in the ring."""
        return self.seq - len(self.ring)

    def finish(self) -> None:
        """Record the exit once and tell the client."""
        with self._lock:
            if self.exited.is_set():
                return
            try:
                self.exit_code = self.pty.reap()
            except Exception:
                self.exit_code = None
            self.exited_at = time.time()
            self.exited.set()
            attached = self.attached
            if attached:
                self.host.emit({"op": "exit", "sid": self.sid, "code": self.exit_code})
        self._inbox.put(None)
        if not attached:
            log.info("session %s (%s) exited with %s while detached",
                     self.name, self.sid, self.exit_code)

    # -- input -----------------------------------------------------------
    def submit(self, item: tuple) -> None:
        if not self.exited.is_set():
            self._inbox.put(item)

    def _write_loop(self) -> None:
        # Its own thread: a child that is not reading its terminal blocks the
        # write, and that must stall this session's input only.
        while True:
            item = self._inbox.get()
            if item is None:
                return
            kind, data, writer = item
            try:
                if kind == "write":
                    self.pty.write(data, writer)
                elif kind == "forget":
                    self.pty.forget_writer(writer)
            except OSError:
                pass  # the child is going; its exit is reported by the reader

    # -- control ---------------------------------------------------------
    def resize(self, cols: int, rows: int) -> None:
        if not self.exited.is_set():
            try:
                self.pty.resize(cols, rows)
            except OSError:
                pass

    def terminate(self, force: bool) -> None:
        try:
            self.pty.terminate(force=force)
        except Exception:
            log.debug("terminate failed for %s", self.name, exc_info=True)

    def close(self) -> None:
        self._inbox.put(None)
        try:
            self.pty.close()
        except Exception:
            log.debug("close failed for %s", self.name, exc_info=True)

    def describe(self) -> dict:
        with self._lock:
            return {
                "sid": self.sid, "name": self.name, "pid": self.pid,
                "seq": self.seq, "ring_start": self.ring_start,
                "attached": self.attached, "exited": self.exited.is_set(),
                "code": self.exit_code, "exited_at": self.exited_at,
            }


class Host:
    def __init__(self, directory: Path, *, idle_exit: float = 30.0) -> None:
        self.dir = Path(directory)
        self.idle_exit = idle_exit
        self.sessions: Dict[str, HostSession] = {}
        self._sessions_lock = threading.Lock()
        self._conn: Optional[socket.socket] = None
        self._send_lock = threading.Lock()
        self._stop = threading.Event()
        self._token = b""
        self._sock_path = socket_path(self.dir)

    def stop(self) -> None:
        self._stop.set()

    # -- outgoing --------------------------------------------------------
    def emit(self, header: dict, payload: bytes = b"") -> None:
        conn = self._conn
        if conn is None:
            return
        frame = encode(header, payload)
        with self._send_lock:
            try:
                conn.sendall(frame)
            except OSError:
                pass  # the client loop sees the broken connection and ends it

    def _reply(self, conn: socket.socket, req_id, ok: bool = True, *,
               payload: bytes = b"", **fields) -> None:
        header = {"op": "reply", "id": req_id, "ok": ok, **fields}
        with self._send_lock:
            try:
                conn.sendall(encode(header, payload))
            except OSError:
                pass

    # -- lifecycle -------------------------------------------------------
    def serve(self) -> int:
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        self._token = secrets.token_hex(32).encode("ascii")
        tok = token_path(self.dir)
        fd = os.open(str(tok), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(self._token)
        try:
            self._sock_path.unlink()
        except FileNotFoundError:
            pass
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old_umask = os.umask(0o177)
        try:
            listener.bind(str(self._sock_path))
        finally:
            os.umask(old_umask)
        listener.listen(4)
        listener.settimeout(POLL_INTERVAL)
        pid_path(self.dir).write_text(str(os.getpid()), encoding="ascii")
        log.info("pty host %d listening on %s", os.getpid(), self._sock_path)
        idle_since = time.monotonic()
        try:
            while not self._stop.is_set():
                try:
                    conn, _ = listener.accept()
                except socket.timeout:
                    conn = None
                if conn is not None:
                    threading.Thread(
                        target=self._client_loop, args=(conn,),
                        name="ptyhost-client", daemon=True,
                    ).start()
                self._poll_liveness()
                if not self.dir.is_dir():
                    log.info("directory %s is gone; exiting", self.dir)
                    break
                with self._sessions_lock:
                    busy = bool(self.sessions) or self._conn is not None
                if busy:
                    idle_since = time.monotonic()
                elif time.monotonic() - idle_since >= self.idle_exit:
                    log.info("no sessions and no client for %.0fs; exiting", self.idle_exit)
                    break
        finally:
            listener.close()
            self._end_all(force=True)
            for path in (self._sock_path, pid_path(self.dir), token_path(self.dir)):
                try:
                    path.unlink()
                except OSError:
                    pass
        return 0

    def _poll_liveness(self) -> None:
        now = time.monotonic()
        with self._sessions_lock:
            live = [s for s in self.sessions.values() if not s.exited.is_set()]
        for s in live:
            try:
                alive = s.pty.isalive()
            except Exception:
                alive = False
            if alive:
                s.dead_since = None
            elif s.dead_since is None:
                s.dead_since = now
            elif now - s.dead_since >= LATE_EOF_GRACE:
                s.finish()

    @staticmethod
    def _end(s: HostSession) -> None:
        if not s.exited.is_set():
            s.terminate(force=False)
            s.exited.wait(timeout=2.0)
            if not s.exited.is_set():
                s.terminate(force=True)
        s.close()

    def _end_all(self, *, force: bool) -> None:
        with self._sessions_lock:
            sessions = list(self.sessions.values())
            self.sessions.clear()
        for s in sessions:
            if not s.exited.is_set():
                s.terminate(force=force)
        for s in sessions:
            s.exited.wait(timeout=2.0)
            if not s.exited.is_set():
                s.terminate(force=True)
            s.close()

    # -- one client ------------------------------------------------------
    def _client_loop(self, conn: socket.socket) -> None:
        try:
            first = read_frame(conn)
        except (OSError, ProtocolError):
            conn.close()
            return
        if first is None or not self._hello_ok(conn, first[0]):
            conn.close()
            return
        with self._sessions_lock:
            if self._conn is not None:
                self._reply(conn, first[0].get("id"), ok=False, error="busy: another client is connected")
                conn.close()
                return
            self._conn = conn
        self._reply(conn, first[0].get("id"), ok=True, proto=PROTO_VERSION, pid=os.getpid())
        try:
            while True:
                frame = read_frame(conn)
                if frame is None:
                    break
                self._dispatch(conn, *frame)
        except (OSError, ProtocolError) as exc:
            log.info("client connection ended: %s", exc)
        finally:
            with self._sessions_lock:
                if self._conn is conn:
                    self._conn = None
                sessions = list(self.sessions.values())
            try:
                conn.close()
            except OSError:
                pass
            # Stage 2: the sessions outlive the client. Whatever happens to
            # the daemon -- a restart, a crash, a kill -- the next one
            # attaches to them again; ending them is an explicit request.
            for s in sessions:
                with s._lock:
                    s.attached = False
            if sessions:
                log.info("client went away; %d session(s) detached and kept", len(sessions))

    def _hello_ok(self, conn: socket.socket, header: dict) -> bool:
        if header.get("op") != "hello":
            return False
        token = str(header.get("token", "")).encode("ascii", "replace")
        if not hmac.compare_digest(token, self._token):
            self._reply(conn, header.get("id"), ok=False, error="bad token")
            return False
        lo, hi = header.get("proto_min", 0), header.get("proto_max", 0)
        if not (isinstance(lo, int) and isinstance(hi, int) and lo <= PROTO_VERSION <= hi):
            self._reply(conn, header.get("id"), ok=False,
                        error=f"protocol {PROTO_VERSION} not in [{lo}, {hi}]")
            return False
        return True

    def _session(self, header: dict) -> Optional[HostSession]:
        with self._sessions_lock:
            return self.sessions.get(str(header.get("sid", "")))

    def _dispatch(self, conn: socket.socket, header: dict, payload: bytes) -> None:
        op = header["op"]
        if op == "write":
            s = self._session(header)
            if s is not None:
                s.submit(("write", payload, header.get("writer") or None))
        elif op == "forget_writer":
            s = self._session(header)
            if s is not None:
                s.submit(("forget", b"", header.get("writer") or None))
        elif op == "resize":
            s = self._session(header)
            if s is not None:
                s.resize(int(header["cols"]), int(header["rows"]))
        elif op == "terminate":
            s = self._session(header)
            if s is not None:
                s.terminate(bool(header.get("force")))
        elif op == "close":
            with self._sessions_lock:
                s = self.sessions.pop(str(header.get("sid", "")), None)
            if s is not None:
                s.close()
        elif op == "spawn":
            # Off the client thread: spawning takes a few hundred ms and the
            # other sessions' input must keep flowing meanwhile.
            threading.Thread(
                target=self._spawn, args=(conn, header), daemon=True,
                name=f"ptyhost-spawn-{header.get('name')}",
            ).start()
        elif op == "attach":
            self._attach(conn, header)
        elif op == "ack_exit":
            # The client has recorded this exit; nothing more is owed for it.
            with self._sessions_lock:
                s = self.sessions.get(str(header.get("sid", "")))
                if s is not None and s.exited.is_set():
                    del self.sessions[s.sid]
                else:
                    s = None
            if s is not None:
                s.close()
            if "id" in header:
                self._reply(conn, header["id"], acked=s is not None)
        elif op == "end":
            # One session, attached or not: terminate, wait, drop.
            with self._sessions_lock:
                s = self.sessions.pop(str(header.get("sid", "")), None)
            if s is not None:
                threading.Thread(target=self._end, args=(s,), daemon=True,
                                 name=f"ptyhost-end-{s.name}").start()
            if "id" in header:
                self._reply(conn, header["id"], ended=s is not None)
        elif op == "end_all":
            # Off the client thread: it waits for the children to go.
            def end_all() -> None:
                self._end_all(force=False)
                if "id" in header:
                    self._reply(conn, header["id"])
            threading.Thread(target=end_all, daemon=True, name="ptyhost-end-all").start()
        elif op == "list":
            with self._sessions_lock:
                rows = [s.describe() for s in self.sessions.values()]
            self._reply(conn, header.get("id"), sessions=rows)
        elif op == "ping":
            self._reply(conn, header.get("id"), pid=os.getpid())
        elif op == "shutdown":
            # End every session and exit (the serve loop's finally does both).
            self._reply(conn, header.get("id"), pid=os.getpid())
            self.stop()
        else:
            if "id" in header:
                self._reply(conn, header["id"], ok=False, error=f"unknown op {op!r}")

    def _attach(self, conn: socket.socket, header: dict) -> None:
        """Re-attach a session to this client, replaying from ``from_seq``.

        The reply's payload is the ring from ``from_seq`` (or from its start,
        when ``from_seq`` fell out of it -- ``gap`` is then the number of
        bytes lost) up to ``seq``; every output event after the reply
        continues from ``seq``. For a session that exited while detached the
        reply says so, with its code, and no exit event follows.
        """
        req_id = header.get("id")
        s = self._session(header)
        if s is None:
            self._reply(conn, req_id, ok=False, error=f"no session {header.get('sid')!r}")
            return
        try:
            from_seq = max(0, int(header.get("from_seq") or 0))
        except (TypeError, ValueError):
            from_seq = 0
        with s._lock:
            start = s.ring_start
            begin = max(from_seq, start)
            data = bytes(s.ring[begin - start:]) if begin < s.seq else b""
            fields = {
                "seq": s.seq, "ring_start": start, "from": begin,
                "gap": max(0, start - from_seq), "pid": s.pid,
                "exited": s.exited.is_set(), "code": s.exit_code,
            }
            # The reply goes out under the lock, before any later chunk.
            self._reply(conn, req_id, ok=True, payload=data, **fields)
            s.attached = not s.exited.is_set()

    def _spawn(self, conn: socket.socket, header: dict) -> None:
        req_id = header.get("id")
        sid = str(header.get("sid", ""))
        name = str(header.get("name", sid))
        with self._sessions_lock:
            taken = not sid or sid in self.sessions
        if taken:
            self._reply(conn, req_id, ok=False, error=f"sid {sid!r} is empty or in use")
            return
        try:
            pty = pty_backend.spawn_local(
                list(header["argv"]), env=dict(header["env"]), cwd=str(header["cwd"]),
                cols=int(header["cols"]), rows=int(header["rows"]),
            )
        except Exception as exc:
            self._reply(conn, req_id, ok=False, error=str(exc))
            return
        s = HostSession(self, sid, name, pty)
        with self._sessions_lock:
            self.sessions[sid] = s
        # The reply goes before the reader starts, so the client knows the pid
        # before the first output event arrives for it.
        self._reply(conn, req_id, ok=True, pid=s.pid)
        s.start()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="claunch-ptyhost")
    parser.add_argument("--dir", required=True)
    parser.add_argument("--idle-exit", type=float, default=30.0)
    args = parser.parse_args(argv)
    if sys.platform == "win32":
        print("the PTY host runs on POSIX only", file=sys.stderr)
        return 2
    directory = Path(args.dir)
    directory.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(log_path(directory), encoding="utf-8")],
    )
    host = Host(directory, idle_exit=args.idle_exit)
    # SIGTERM ends the sessions and exits through the same path as idle:
    # the children are this process's, and they must not outlive it.
    signal.signal(signal.SIGTERM, lambda *_: host.stop())
    return host.serve()
