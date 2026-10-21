"""MCP interface for this session's bounded artifact cleanup."""
from __future__ import annotations

import os
from . import daemon_client, mcp_rpc


TOOLS = [{
    "name": "artifact_cleanup",
    "description": (
        "Plan or execute deletion of this session's completed test artifacts. "
        "Omit plan_id for a read-only plan; inspect its exact paths, evidence and "
        "pending reasons before submitting that plan_id to delete ready items. "
        "Plans expire after ten minutes and are revalidated before deletion. "
        "No arbitrary paths, commands, other sessions, force removal or ref "
        "deletion are accepted. Unknown, changed or incomplete artifacts, and "
        "artifacts that are links themselves, are kept as cleanup_pending; a link "
        "inside an artifact is unlinked, never followed. Execution requires an idle measurement "
        "window and writes a durable receipt. When that window is busy the apply is "
        "scheduled instead and the daemon runs it once the window frees up -- report "
        "status 'scheduled' as the outcome and do not re-plan. Any other denial must "
        "be reported, not retried through shell commands or another tool."
    ),
    "inputSchema": {"type": "object", "additionalProperties": False,
                    "properties": {"plan_id": {"type": "string"}}},
}]


class CleanupMcpError(ValueError):
    pass


def call_tool(name, args):
    if name != "artifact_cleanup" or set(args) - {"plan_id"}:
        raise CleanupMcpError("only artifact_cleanup with optional plan_id is accepted")
    session = os.environ.get("CLAUNCH_SESSION")
    if not session:
        raise CleanupMcpError("artifact_cleanup requires $CLAUNCH_SESSION")
    body = {"session": session}
    if "plan_id" in args:
        if not isinstance(args["plan_id"], str) or not args["plan_id"]:
            raise CleanupMcpError("plan_id must be a nonempty string")
        body["plan_id"] = args["plan_id"]
    client, why = daemon_client.connect_with_diagnosis()
    if client is None:
        raise CleanupMcpError(daemon_client.unreachable_reason(why))
    return client.post("/api/artifact-cleanup", body, timeout=120)


SERVER = mcp_rpc.Server(name="claunch-artifact-cleanup", tools=tuple(TOOLS),
                        dispatch=call_tool,
                        errors=(CleanupMcpError, daemon_client.DaemonClientError))
