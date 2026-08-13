"""One install step for everything an agent needs from claunch.

``claunch install`` registers a single MCP server — ``claunch mcp``, serving
the cflow and mesh tools together (see :mod:`claude_launcher.mcp_server`) —
writes the three skills that teach their protocols, and seeds the shared
workflow layer with the workflows that ship in the package. That last part is
what makes ``~/.claude-launcher/workflows/`` a real layer rather than a
documented empty directory: before it, a workflow was only ever findable from
the one project directory somebody happened to write it in.

Separate skills, one server, on purpose. A skill's body is loaded whole when
it triggers, so folding the workflow protocol and the mesh protocol into one
file would make every session that runs a workflow carry the messaging rules
it will never use, and vice versa; keeping them apart also keeps each
``description`` narrow enough to trigger on the right thing. Authoring a
workflow splits from running one along the same seam: the rules for choosing
a control point are dead weight while executing a step, and the execution
protocol is dead weight while writing YAML. The *server* has no such cost —
its tool schemas are in context either way — so there was nothing to buy by
splitting it, and a real price: installing one feature and not the other used
to leave an agent holding half a toolkit, most visibly when ``spawn`` (which
rides with mesh) was missing from a cflow-only install.

Installs written before the merge registered ``cflow`` and ``mesh`` as
separate servers. Both are superseded here rather than left running, since two
live servers would offer the agent every tool twice.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import List

from . import mesh_install, settings
from .cflow import authoring as cflow_authoring, install as cflow_install
from .profile import Profile

#: The server's key in ``.claude.json`` / ``.mcp.json`` — the name the agent
#: sees its tools namespaced under (``mcp__claunch__spawn``, and so on).
MCP_NAME = "claunch"

#: Server names earlier versions registered, replaced by :data:`MCP_NAME`.
LEGACY_MCP_NAMES = ("cflow", "mesh")


def mcp_server_def() -> dict:
    """The stdio server entry for the merged MCP bridge.

    On Windows ``claunch`` on PATH is typically a ``.bat``/``.cmd`` shim,
    which Claude Code's spawn (no shell) cannot exec directly — wrap in
    ``cmd /c``.
    """
    if sys.platform == "win32":
        return {"command": "cmd", "args": ["/c", "claunch", "mcp"]}
    return {"command": "claunch", "args": ["mcp"]}


def _workflow_lines() -> List[str]:
    """Report the shared-layer seeding, in the same voice as the rest.

    Machine-local, not per-profile and not per-project: the shared workflow
    layer is one directory under the launcher home, so every install writes
    the same place. Saying so on every install is the point — that directory
    is where a workflow goes to be available from every project, and nothing
    else advertises it.
    """
    lines = []
    for _, dest, outcome in cflow_install.seed_global_workflows():
        if outcome == cflow_install.KEPT:
            lines.append(f"workflow -> {dest} (kept; yours differs from the packaged one)")
        elif outcome == cflow_install.SEEDED:
            lines.append(f"workflow -> {dest}")
    return lines


def install_into_profile(profile: Profile) -> List[str]:
    """Register the MCP server + every skill inside a profile's config dir."""
    settings.merge_mcp_servers(
        profile, {MCP_NAME: mcp_server_def()}, remove=LEGACY_MCP_NAMES
    )
    skills = profile.config_dir / "skills"
    return [
        f"mcp server {MCP_NAME!r} -> {profile.config_dir / settings.CLAUDE_JSON}",
        f"skill -> {cflow_install.write_skill(skills)}",
        f"skill -> {cflow_authoring.write_skill(skills)}",
        f"skill -> {mesh_install.write_skill(skills)}",
    ] + _workflow_lines()


def install_into_project(project_dir: Path) -> List[str]:
    """Register the MCP server (.mcp.json) + every skill (.claude/skills)."""
    project_dir.mkdir(parents=True, exist_ok=True)
    mcp_path = project_dir / ".mcp.json"
    try:
        doc = json.loads(mcp_path.read_text(encoding="utf-8")) if mcp_path.is_file() else {}
    except ValueError:
        doc = {}
    if not isinstance(doc, dict):
        doc = {}
    servers = doc.setdefault("mcpServers", {})
    if isinstance(servers, dict):
        for name in LEGACY_MCP_NAMES:
            servers.pop(name, None)
        servers[MCP_NAME] = mcp_server_def()
    mcp_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    skills = project_dir / ".claude" / "skills"
    return [
        f"mcp server {MCP_NAME!r} -> {mcp_path}",
        f"skill -> {cflow_install.write_skill(skills)}",
        f"skill -> {cflow_authoring.write_skill(skills)}",
        f"skill -> {mesh_install.write_skill(skills)}",
    ] + _workflow_lines()
