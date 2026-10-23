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

import os

from . import (
    category_mcp, glossary_mcp, observer_mcp, qa_mcp, sim_mcp, operator_v2_mcp, mcp_rpc, mesh_mcp, reposync_mcp, search_mcp, status_checks_mcp, wait_mcp,
    window_mcp, artifact_cleanup_mcp, threads_mcp, prompt_packs_mcp, webdebug_mcp, session_env,
    cflow_watch_mcp, shell_mcp, timer_mcp, session_fault_mcp,
)
from .cflow import mcp as cflow_mcp
from .cflow2 import mcp as cflow2_mcp


def _operator_session() -> bool:
    """Whether this process serves an operator v2's session.

    The session carries which operator it belongs to (``operators_v2.start``
    writes the variable into its spec, ``session_env.OPERATOR_ENV``), so the
    answer needs no daemon call at a moment the binding may not be written.
    """
    return bool(os.environ.get(session_env.OPERATOR_ENV))


def _hidden_tools() -> tuple:
    """The tool families this process does not offer its session.

    An operator v2's session has no session QA: the user it talks to is in
    the Operator view, where its questions are ``opv2_ask``. Hiding the tools
    here keeps that agent from filing a question into a pane nobody opens for
    it; the daemon refuses the same writes as well (``daemon/qa_api.py``),
    which is the enforcement — this is only the surface. Every other session
    offers everything.
    """
    return qa_mcp.TOOL_NAMES if _operator_session() else ()


SERVERS = (
    cflow_mcp.SERVER, mesh_mcp.SERVER, status_checks_mcp.SERVER,
    window_mcp.SERVER, wait_mcp.SERVER, observer_mcp.SERVER, operator_v2_mcp.SERVER, search_mcp.SERVER,
    glossary_mcp.SERVER,
    category_mcp.SERVER,
    sim_mcp.SERVER,
    cflow2_mcp.SERVER,
    cflow_watch_mcp.SERVER,
    reposync_mcp.SERVER,
    artifact_cleanup_mcp.SERVER,
    qa_mcp.SERVER,
    threads_mcp.SERVER,
    shell_mcp.SERVER,
    prompt_packs_mcp.SERVER,
    webdebug_mcp.SERVER,
    timer_mcp.SERVER,
    session_fault_mcp.SERVER,
)


def _instructions() -> str:
    """The guidance the host shows, without the hidden family's paragraph.

    A tool list that drops qa_* must not keep telling the agent to use them,
    or the operator reads a hint for a door that is not there. Read once at
    import: the environment is this process's own identity and does not
    change while it runs.
    """
    parts = [s.instructions for s in SERVERS if s.instructions]
    if _operator_session():
        parts = [p for p in parts if p != qa_mcp.INSTRUCTIONS]
    return "\n\n".join(parts)


SERVER = mcp_rpc.merge("claunch", SERVERS, hide=_hidden_tools, instructions=_instructions())

#: The merged tool list, for callers that want to report what is offered
#: without speaking JSON-RPC (the installer's summary line, tests).
TOOLS = SERVER.tools


def serve() -> int:
    """Blocking stdio loop; returns when stdin closes."""
    return SERVER.serve()
