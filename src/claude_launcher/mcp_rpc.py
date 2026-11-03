"""The JSON-RPC 2.0 stdio plumbing every claunch MCP server shares.

A newline-delimited subset of MCP — initialize / tools/list / tools/call /
ping — with no SDK dependency. cflow and mesh each grew their own copy of this
loop, identical down to the error envelope; the copies are now one, so a fix to
the wire format cannot land in one server and miss the other.

A :class:`Server` is *what* is exposed (a name, a tool list, a dispatch
function, the exceptions that mean "the agent asked for something impossible"
rather than "the server broke"). :func:`merge` composes several into one, which
is how a single ``claunch mcp`` process serves both feature sets.

:meth:`Server.serve` runs each ``tools/call`` on a worker thread. The host
sends calls concurrently and moves one that passes 120 s into the background,
so a loop that answered one line at a time made a ``wait 600`` hold every later
call of the session behind it (claunch-368ef). A ``notifications/cancelled``
sets the call's :func:`cancel_event`; a call that has not started is dropped.

Every tool call that ends in an exception is written to the session's
``mcp-server.log`` (:func:`record`, claunch-f7osr): a refusal (one of
``errors``) as one line, and a crash (anything else) with its traceback just
before the process exits. The host keeps none of this -- its MCP log says
only ``Connection closed`` -- and the traceback on stderr went nowhere.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

PROTOCOL_VERSION = "2024-11-05"

#: Worker threads for ``tools/call``. A call past this many in flight waits
#: for a free worker.
MAX_CALL_WORKERS = 32

#: The per-session log of failed tool calls, next to the session's
#: ``output.log`` (``daemon/sessions/<session>/``).
LOG_NAME = "mcp-server.log"
#: When a write would take the log past this size, the log is moved to
#: ``<name>.1`` (replacing the previous one) and a new one is started, so a
#: session holds at most about twice this much.
LOG_MAX_BYTES = 1 << 20
#: The arguments are logged as their JSON text cut to this many characters:
#: enough to tell which call it was, not a copy of a message body.
LOG_ARGS_CHARS = 400
#: A traceback longer than this keeps its last part (where the raise is).
LOG_TRACEBACK_CHARS = 32_000

_current = threading.local()
_log_lock = threading.Lock()


def log_path() -> Optional[Path]:
    """Where this process records failed tool calls, or ``None``.

    ``None`` when the process serves no claunch session (``CLAUNCH_SESSION``
    unset, e.g. an ``mcp`` started by hand). There is no fallback location:
    a fixed name under ``/tmp`` is shared by every session on the machine
    (AGENTS.md).
    """
    session = os.environ.get("CLAUNCH_SESSION", "").strip()
    if not session:
        return None
    from .daemon import paths

    return paths.session_dir(session) / LOG_NAME


def _cut(text: str, limit: int, *, keep_tail: bool = False) -> str:
    if len(text) <= limit:
        return text
    dropped = len(text) - limit
    if keep_tail:
        return f"...({dropped} chars cut)\n" + text[-limit:]
    return text[:limit] + f"...({dropped} chars cut)"


def record(kind: str, *, server: str, method: object, tool: Optional[str] = None,
           args: object = None, message: str = "",
           trace: Optional[str] = None) -> Optional[Path]:
    """Append one failed call to :func:`log_path`; return the file written.

    ``kind`` is ``refused`` (an exception in the server's ``errors``: the
    agent got an error result) or ``crash`` (anything else: the process is
    about to exit). This never raises -- it runs on the way to ``os._exit``,
    and a logging problem must not change how the call ends -- and returns
    ``None`` when nothing was written.
    """
    try:
        path = log_path()
        if path is None:
            return None
        entry = {
            "time": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "session": os.environ.get("CLAUNCH_SESSION", ""),
            "pid": os.getpid(),
            "server": server,
            "kind": kind,
            "method": method,
            "tool": tool,
            "args": _cut(json.dumps(args, ensure_ascii=False, default=str), LOG_ARGS_CHARS),
            "message": message,
        }
        if trace:
            entry["traceback"] = _cut(trace, LOG_TRACEBACK_CHARS, keep_tail=True)
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with _log_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            if size and size + len(line.encode("utf-8")) > LOG_MAX_BYTES:
                os.replace(path, path.with_name(path.name + ".1"))
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line)
        return path
    except Exception:
        return None


def _call_gist(msg: object) -> Tuple[object, Optional[str], object]:
    """``(method, tool name, arguments)`` of a request, for :func:`record`."""
    if not isinstance(msg, dict):
        return None, None, None
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    tool = params.get("name") if msg.get("method") == "tools/call" else None
    return msg.get("method"), (str(tool) if tool is not None else None), params.get("arguments")


def _record_crash(server: str, msg: object) -> None:
    """Log the exception being handled as a crash, and say where on stderr."""
    exc = sys.exc_info()[1]
    method, tool, args = _call_gist(msg)
    path = record("crash", server=server, method=method, tool=tool, args=args,
                  message=f"{type(exc).__name__}: {exc}", trace=traceback.format_exc())
    if path is not None:
        print(f"claunch mcp: crash recorded in {path}", file=sys.stderr)


def cancel_event() -> threading.Event:
    """The event set when the host cancels the call this thread is running.

    Outside :meth:`Server.serve` (a direct :meth:`Server.handle`, a test) there
    is no host to cancel, and the event returned is never set.
    """
    event = getattr(_current, "cancel", None)
    return event if event is not None else threading.Event()


class ToolNameCollision(Exception):
    """Two merged servers offer the same tool name.

    Raised at import time rather than left to whichever server the dispatch
    map happened to see last: a silently shadowed tool is a feature that
    stops working with no error anywhere.
    """


@dataclass(frozen=True)
class Server:
    """One MCP surface: its identity, its tools, and how to run them.

    ``errors`` are the exception types that become a tool result with
    ``isError: true`` — a message written for the agent to read and act on.
    Anything else propagates and kills the process, which is the right
    outcome for a bug: a server quietly answering "error" to every call is
    harder to diagnose than one that is visibly gone.
    """

    name: str
    tools: Tuple[dict, ...]
    dispatch: Callable[[str, dict], dict]
    errors: Tuple[type, ...]
    #: The guidance text ``initialize`` hands the host.
    instructions: str = ""
    #: Tool names this process leaves out of ``tools/list`` and refuses in
    #: ``tools/call``, read on every request rather than fixed at import: one
    #: shipped server serves every session, and a session whose surface
    #: differs (an operator v2 has no session QA) hides that family for
    #: itself by what its environment says it is.
    hide: Optional[Callable[[], Sequence[str]]] = None

    def hidden(self) -> frozenset:
        """The tool names hidden from this process right now."""
        return frozenset(self.hide() or ()) if self.hide is not None else frozenset()

    def handle(self, msg: dict) -> Optional[dict]:
        """Return a response dict, or ``None`` for notifications."""
        method = msg.get("method")
        msg_id = msg.get("id")
        params = msg.get("params") or {}

        if method == "initialize":
            from . import __version__

            return _result(
                msg_id,
                {
                    "protocolVersion": params.get("protocolVersion")
                    or PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": self.name, "version": __version__},
                    **({"instructions": self.instructions} if self.instructions else {}),
                },
            )
        if method == "notifications/initialized":
            # The host finished its handshake: tell the daemon, so a session's
            # opening can wait for its servers (claunch-xixag).
            from .daemon import mcp_ready

            mcp_ready.announce()
            return None
        if method == "notifications/cancelled":
            return None
        if method == "ping":
            return _result(msg_id, {})
        if method == "tools/list":
            hidden = self.hidden()
            tools = self.tools if not hidden else [t for t in self.tools if t.get("name") not in hidden]
            return _result(msg_id, {"tools": list(tools)})
        if method == "tools/call":
            name = str(params.get("name") or "")
            args = params.get("arguments") or {}
            try:
                if name in self.hidden():
                    raise self.errors[0](f"tool {name!r} is not available to this session")
                payload = self.dispatch(name, args if isinstance(args, dict) else {})
                text = json.dumps(payload, ensure_ascii=False, indent=2)
                return _result(
                    msg_id,
                    {"content": [{"type": "text", "text": text}], "isError": False},
                )
            except self.errors as exc:
                record("refused", server=self.name, method=method, tool=name,
                       args=args, message=str(exc))
                return _result(
                    msg_id,
                    {
                        "content": [{"type": "text", "text": f"error: {exc}"}],
                        "isError": True,
                    },
                )
        if msg_id is None:
            return None  # unknown notification: ignore
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32601, "message": f"method not found: {method}"},
        }

    def serve(self) -> int:
        """Blocking stdio loop; returns when stdin closes.

        ``tools/call`` runs on a worker thread and answers when it finishes, in
        any order (the host matches responses by id). Everything else is
        answered inline, in order.
        """
        stdin = sys.stdin.buffer
        stdout = sys.stdout.buffer
        write_lock = threading.Lock()
        pending: Dict[object, threading.Event] = {}
        pending_lock = threading.Lock()

        def write(response: dict) -> None:
            data = json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n"
            with write_lock:
                stdout.write(data)
                stdout.flush()

        def run_call(msg: dict, cancel: threading.Event) -> None:
            msg_id = msg.get("id")
            try:
                if cancel.is_set():
                    return
                _current.cancel = cancel
                response = self.handle(msg)
            except BaseException:
                # An exception outside ``errors`` is a bug and kills the
                # process, as it did when calls ran on the main thread. The
                # log is written first: stderr is not kept by the host.
                _record_crash(self.name, msg)
                traceback.print_exc()
                sys.stderr.flush()
                os._exit(1)
            finally:
                _current.cancel = None
                with pending_lock:
                    if pending.get(msg_id) is cancel:
                        del pending[msg_id]
            # The host has dropped a cancelled request; a late answer to it
            # is not sent.
            if response is not None and not cancel.is_set():
                write(response)

        with ThreadPoolExecutor(max_workers=MAX_CALL_WORKERS,
                                thread_name_prefix="mcp-call") as pool:
            for raw in iter(stdin.readline, b""):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    msg = json.loads(raw.decode("utf-8"))
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                method = msg.get("method")
                msg_id = msg.get("id")
                if method == "tools/call" and isinstance(msg_id, (str, int)):
                    cancel = threading.Event()
                    with pending_lock:
                        pending[msg_id] = cancel
                    pool.submit(run_call, msg, cancel)
                    continue
                if method == "notifications/cancelled":
                    request_id = (msg.get("params") or {}).get("requestId")
                    with pending_lock:
                        cancel = pending.get(request_id) if isinstance(request_id, (str, int)) else None
                    if cancel is not None:
                        cancel.set()
                    continue
                try:
                    response = self.handle(msg)
                except BaseException:
                    # Answered inline (initialize, notifications/initialized,
                    # ping): the exception ends serve and the process.
                    _record_crash(self.name, msg)
                    raise
                if response is not None:
                    write(response)
            # stdin closed: the host is gone, so nothing in flight is wanted.
            with pending_lock:
                for cancel in pending.values():
                    cancel.set()
        return 0


def merge(name: str, servers: Sequence[Server],
          hide: Optional[Callable[[], Sequence[str]]] = None,
          instructions: Optional[str] = None) -> Server:
    """One server offering every tool of ``servers``, dispatched by name.

    Tool names stay as they are — no prefixing. The agent-facing names are
    written into the skills and into every error message the servers produce,
    and a merge that renamed them would make the docs lie for the sake of a
    namespace nothing is currently competing for. :class:`ToolNameCollision`
    is what keeps that assumption honest as tools are added.

    ``hide`` is the merged surface's own :attr:`Server.hide`: a process that
    serves one session with a narrower surface decides it there, since the
    per-feature servers below it are shared by every session. ``instructions``
    likewise overrides the joined guidance, for the same reason.
    """
    owner: Dict[str, Server] = {}
    tools: List[dict] = []
    for server in servers:
        for tool in server.tools:
            tool_name = str(tool.get("name") or "")
            if tool_name in owner:
                raise ToolNameCollision(
                    f"tool {tool_name!r} is offered by both "
                    f"{owner[tool_name].name!r} and {server.name!r} — rename one "
                    f"before merging them into {name!r}"
                )
            owner[tool_name] = server
            tools.append(tool)

    def dispatch(tool_name: str, args: dict) -> dict:
        server = owner.get(tool_name)
        if server is None:
            # Raised as the *first* server's error type so the envelope stays
            # an agent-readable tool error rather than a crash.
            raise servers[0].errors[0](f"unknown tool {tool_name!r}")
        return server.dispatch(tool_name, args)

    errors: Tuple[type, ...] = tuple(
        dict.fromkeys(err for server in servers for err in server.errors)
    )
    return Server(
        name=name, tools=tuple(tools), dispatch=dispatch, errors=errors,
        instructions=(instructions if instructions is not None
                      else "\n\n".join(s.instructions for s in servers if s.instructions)),
        hide=hide,
    )


def _result(msg_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}
