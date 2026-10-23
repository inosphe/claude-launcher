"""MCP access to a daemon-owned report timer.

An agent has no clock of its own: it ends its turn, and nothing starts the
next one. ``wait`` (this package's other time tool) sleeps inside the MCP
process and returns when the agent is already awake to hear it. This one
asks the *daemon* to speak on the agent's behalf later: every ``every``
seconds, up to ``times`` times, ``message`` is typed into the session, and a
session that was idle, held or paused is woken for it.

The timer lives in the daemon, not in this process (``daemon/agent_timers.py``),
so it survives the agent's turn ending, a compaction and a daemon restart.
Time the daemon spends stopped does not count toward it, a session's kill
ends it, and while it runs it is drawn on the session's timer line.
"""

from __future__ import annotations

import os

from . import daemon_client, mcp_rpc


class TimerMcpError(Exception):
    pass


TOOLS = [
    {
        "name": "timer",
        "description": (
            "Create a daemon-owned report timer for this session: every "
            "`every` seconds, up to `times` times, the daemon types "
            "`message` into your terminal and wakes the session if it is "
            "held or paused. Use it when the user asks for a periodic "
            "report or reminder you cannot keep yourself: the timer runs "
            "outside your turn, survives a compaction and a daemon restart "
            "(time the daemon is down does not count), and ends after its "
            "last fire or when the session is killed. Each call adds one "
            "timer; it is not a replacement for work you can finish now. "
            "`every` is 10 to 2592000 seconds, `times` 1 to 1000."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "every": {
                    "type": "number",
                    "description": "seconds between fires (10 to 2592000)",
                    "minimum": 10,
                    "maximum": 2592000,
                },
                "times": {
                    "type": "integer",
                    "description": "how many times to fire (1 to 1000)",
                    "minimum": 1,
                    "maximum": 1000,
                },
                "message": {
                    "type": "string",
                    "maxLength": 4000,
                    "description": (
                        "what to type into the session when it fires -- write "
                        "it as the instruction the agent should act on"
                    ),
                },
            },
            "required": ["every", "times", "message"],
            "additionalProperties": False,
        },
    },
]


def _client():
    client, why = daemon_client.connect_with_diagnosis()
    if client is None:
        raise TimerMcpError(
            f"cannot create the timer: {daemon_client.unreachable_reason(why)}"
        )
    return client


def _session() -> str:
    session = os.environ.get("CLAUNCH_SESSION")
    if not session:
        raise TimerMcpError(
            "no $CLAUNCH_SESSION — a timer belongs to one managed session"
        )
    return session


def call_tool(name: str, args: dict) -> dict:
    if name != "timer":
        raise TimerMcpError(f"unknown tool {name!r}")
    session = _session()
    payload = {k: args.get(k) for k in ("every", "times", "message")}
    if payload["message"] is None or str(payload["message"]).strip() == "":
        raise TimerMcpError("'message' is required")
    return _client().post(f"/api/sessions/{session}/timers", payload)


SERVER = mcp_rpc.Server(
    name="claunch-timer",
    tools=tuple(TOOLS),
    dispatch=call_tool,
    errors=(TimerMcpError, daemon_client.DaemonClientError),
)
