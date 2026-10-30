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
"""

from __future__ import annotations

import json
import os
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

PROTOCOL_VERSION = "2024-11-05"

#: Worker threads for ``tools/call``. A call past this many in flight waits
#: for a free worker.
MAX_CALL_WORKERS = 32

_current = threading.local()


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
                # process, as it did when calls ran on the main thread.
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
                response = self.handle(msg)
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
