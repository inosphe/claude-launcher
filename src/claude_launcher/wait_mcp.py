"""MCP access to a bounded, unconditional wait.

This is deliberately local to the MCP process: it is for an agent that has
nothing to do for a known interval, rather than a daemon-owned condition or
scheduled task.
"""

from __future__ import annotations

import math
import time
from numbers import Real

from . import mcp_rpc


MIN_WAIT_SECONDS = 30
MAX_WAIT_SECONDS = 600


TOOLS = [
    {
        "name": "wait",
        "description": (
            "Wait for an unconditional interval before returning. "
            "`seconds` must be between 30 and 600 inclusive."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "seconds": {
                    "type": "number",
                    "minimum": MIN_WAIT_SECONDS,
                    "maximum": MAX_WAIT_SECONDS,
                    "description": "How long to wait, in seconds.",
                }
            },
            "required": ["seconds"],
            "additionalProperties": False,
        },
    },
]


class WaitMcpError(Exception):
    pass


def _seconds(args: dict) -> float:
    seconds = args.get("seconds")
    if isinstance(seconds, bool) or not isinstance(seconds, Real) or not math.isfinite(seconds):
        raise WaitMcpError(
            f"seconds must be a finite number from {MIN_WAIT_SECONDS} to {MAX_WAIT_SECONDS}"
        )
    seconds = float(seconds)
    if not MIN_WAIT_SECONDS <= seconds <= MAX_WAIT_SECONDS:
        raise WaitMcpError(
            f"seconds must be from {MIN_WAIT_SECONDS} to {MAX_WAIT_SECONDS} inclusive"
        )
    return seconds


def call_tool(name: str, args: dict) -> dict:
    if name != "wait":
        raise WaitMcpError(f"unknown tool {name!r}")
    seconds = _seconds(args)
    started = time.monotonic()
    time.sleep(seconds)
    return {
        "requested_seconds": seconds,
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


SERVER = mcp_rpc.Server(
    name="claunch-wait",
    tools=tuple(TOOLS),
    dispatch=call_tool,
    errors=(WaitMcpError,),
)
