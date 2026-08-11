"""The one MCP server Claude Code spawns: ``claunch mcp``.

cflow and mesh shipped as two stdio servers, so a session that wanted both
carried two Python processes and two install steps — and, because the
team-building tools (``spawn``/``children``/``connect``/``disconnect``) rode
along with mesh, installing only cflow silently left an agent with no way to
create a helper at all. There was never a boundary worth that price: the two
sets already reach across it, since ``spawn`` can start a cflow run scoped to
the child it creates.

So they are one server. The tool names are untouched — ``start``/``next``/
``report``/``select``/``status`` from cflow, ``send``/``members``/``history``/
``spawn``/``children``/``connect``/``disconnect`` from mesh — and
:func:`claude_launcher.mcp_rpc.merge` refuses to build the server at all if a
future tool collides, rather than letting one half quietly shadow the other.

The per-feature servers stay importable and stay wired to ``claunch cflow
mcp`` / ``claunch mesh mcp``: installs written before the merge point at those
commands, and an upgrade should not break a session that has not been
reinstalled yet.
"""

from __future__ import annotations

from . import mcp_rpc, mesh_mcp
from .cflow import mcp as cflow_mcp

SERVER = mcp_rpc.merge("claunch", [cflow_mcp.SERVER, mesh_mcp.SERVER])

#: The merged tool list, for callers that want to report what is offered
#: without speaking JSON-RPC (the installer's summary line, tests).
TOOLS = SERVER.tools


def serve() -> int:
    """Blocking stdio loop; returns when stdin closes."""
    return SERVER.serve()
