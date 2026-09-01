"""MCP access to the calling session's configurable Y/N status checks."""

from __future__ import annotations

import os

from . import daemon_client, mcp_rpc


TOOLS = [
    {
        "name": "status_checks",
        "description": (
            "Read the enabled Y/N status checks configured by the user and this "
            "session's latest reports. Call this before reporting so changed "
            "presets are reflected immediately."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "report_status_checks",
        "description": (
            "Report this managed session's current Y/N answers for configured "
            "status checks. Read status_checks first; report only enabled IDs."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "answers": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "description": "status check ID"},
                            "answer": {"type": "string", "enum": ["yes", "no"]},
                        },
                        "required": ["id", "answer"],
                    },
                },
            },
            "required": ["answers"],
        },
    },
]


class StatusChecksMcpError(Exception):
    pass


def _client():
    client, why = daemon_client.connect_with_diagnosis()
    if client is None:
        raise StatusChecksMcpError(
            f"cannot read status checks: {daemon_client.unreachable_reason(why)}"
        )
    return client


def _session() -> str:
    session = os.environ.get("CLAUNCH_SESSION")
    if not session:
        raise StatusChecksMcpError(
            "no $CLAUNCH_SESSION — status checks only work inside a managed claunch session"
        )
    return session


def call_tool(name: str, args: dict) -> dict:
    session = _session()
    if name == "status_checks":
        return _client().get(f"/api/sessions/{session}/status-checks")
    if name == "report_status_checks":
        answers = args.get("answers")
        if not isinstance(answers, list):
            raise StatusChecksMcpError("'answers' must be an array")
        return _client().post(
            f"/api/sessions/{session}/status-checks/reports", {"answers": answers}
        )
    raise StatusChecksMcpError(f"unknown tool {name!r}")


SERVER = mcp_rpc.Server(
    name="claunch-status-checks",
    tools=tuple(TOOLS),
    dispatch=call_tool,
    errors=(StatusChecksMcpError, daemon_client.DaemonClientError),
)
