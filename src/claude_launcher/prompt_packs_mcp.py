"""MCP tool that reads a prompt pack (claunch-8do04).

The guidance for claunch's tool families lives in the ``mcp-*`` prompt packs,
ticked by default and delivered in a session's opening; the server
instructions only point at them, because clients cut the merged
instructions short (Claude Code keeps the first 4096 characters). A session
whose opening lacks a pack -- unticked at creation, or a saved default list
that predates the pack -- reads it here, the copy its workspace would get.
Read-only and local: no daemon call.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from . import mcp_rpc, prompt_packs

INSTRUCTIONS = """\
Prompt packs: the guidance for claunch's tool families is in the mcp-*
prompt packs, normally in your opening as "# claunch prompt pack: <id>".
When the pack for a family you use is missing there, read it with
prompt_pack first.
"""

TOOLS = [
    {"name": "prompt_pack",
     "description": "Read one prompt pack: the text a session created in this workspace is given for "
                    "it (the workspace's installed copy, else claunch's). Without 'id', lists the packs "
                    "claunch ships (id, title, description).",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string", "description": "pack id, e.g. mcp-threads"}}}},
]


class PromptPackMcpError(Exception):
    pass


def _workspace() -> Optional[Path]:
    from . import workspaces

    try:
        owner = workspaces.owning(os.getcwd())
    except Exception:  # an unreadable registry still leaves claunch's copy
        return None
    return Path(owner.path) if owner is not None else None


def call_tool(name: str, args: dict) -> dict:
    if name != "prompt_pack":
        raise PromptPackMcpError(f"unknown tool {name!r}")
    pack_id = str(args.get("id") or "").strip()
    if not pack_id:
        return {"packs": [p.to_dict() for p in prompt_packs.catalog()]}
    try:
        return prompt_packs.preview(_workspace(), pack_id)
    except prompt_packs.PackError as exc:
        raise PromptPackMcpError(str(exc)) from None


SERVER = mcp_rpc.Server(
    name="claunch-prompt-packs",
    tools=tuple(TOOLS),
    dispatch=call_tool,
    errors=(PromptPackMcpError,),
    instructions=INSTRUCTIONS,
)
