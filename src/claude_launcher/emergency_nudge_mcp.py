"""MCP ``emergency_nudge`` (claunch-8myyt): ask a person for help from a run
that waits on no one.

``flow_nudge`` (``cflow2/mcp.py``) is refused unless the run's own files say it
waits on a person. A run can still need one: an auto-mode classifier denial
stops the agent inside a step, and the engine state never shows it. This tool
files the same dashboard card without that check, so it works whatever the
run is doing, or when there is none. The daemon half is
``daemon/flow_nudge.py`` (``NudgeBoard.emergency``).
"""

from __future__ import annotations

import os

from . import daemon_client, mcp_rpc

TOOLS = [
    {
        "name": "emergency_nudge",
        "description": (
            "Raise an emergency card on the user's dashboard notification channel "
            "(a corner card, a row in the notification history, the bell's waiting "
            "count) when you are stopped by something only a person can release and "
            "flow_nudge is refused with 'the run waits on no person right now': an "
            "auto-mode classifier denial, a permission your run cannot ask the engine "
            "for. Works whatever state the run is in, or with none. 'message' is the "
            "card's one or two lines: the decision and your recommendation. One card "
            "per run slot: call again about every 20 minutes while the wait lasts and "
            "the same card's message is updated, nothing is stacked. It settles when "
            "the run moves, a person resolves it, your session ends, or you call with "
            "cancel. At most one call per minute per session. Then stop your turn and "
            "wait."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {"type": "string",
                            "description": "what the person is asked to do, in one or two "
                                           "lines: the decision and your recommendation "
                                           "(required unless cancel)"},
                "cancel": {"type": "boolean",
                           "description": "withdraw your emergency card instead"},
            },
        },
    },
]


class EmergencyNudgeError(Exception):
    pass


def call_tool(name: str, args: dict) -> dict:
    from .daemon import flow_nudge

    if name != "emergency_nudge":
        raise EmergencyNudgeError(f"unknown tool {name!r}")
    args = args or {}
    session = os.environ.get("CLAUNCH_SESSION", "")
    if not session:
        raise EmergencyNudgeError("emergency_nudge works inside a managed claunch session "
                                  "($CLAUNCH_SESSION is not set)")
    cancel = bool(args.get("cancel"))
    message = str(args.get("message") or "").strip()
    if not message and not cancel:
        raise EmergencyNudgeError("'message' is required: what the person is asked to do")
    client, why = daemon_client.connect_with_diagnosis()
    if client is None:
        raise EmergencyNudgeError(
            f"cannot reach the claunch daemon: {daemon_client.unreachable_reason(why)}")
    body = {"session": session, "cwd": os.getcwd(), "scope": session, "message": message,
            "emergency": True}
    if cancel:
        body["cancel"] = True
    result = client.post("/api/flow-nudges", body)
    return flow_nudge.reply(result)


SERVER = mcp_rpc.Server(
    name="claunch-emergency-nudge",
    tools=tuple(TOOLS),
    dispatch=call_tool,
    errors=(EmergencyNudgeError, daemon_client.DaemonClientError),
)
