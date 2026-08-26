"""One install step for everything an agent needs from claunch.

``claunch install`` registers a single MCP server — ``claunch mcp``, serving
the cflow and mesh tools together (see :mod:`claude_launcher.mcp_server`) —
and writes the skills that teach their protocols (plus ``commit-stamp``,
which teaches an agent to sign its commits). It does so into one
of three scopes, and the rule is the same everywhere: an install writes only
inside its own scope.

- **project** (the default): ``.mcp.json`` and ``.claude/skills/`` in one
  project directory.
- **global** (``--global``): the user's own Claude Code setup —
  ``~/.claude/skills/`` and the user-scope ``.claude.json`` (a *sibling* of
  ``~/.claude`` in the default layout; see :func:`config.user_claude_json`).
- **profile** (``--profile``): a claunch-managed isolated config dir.

``--all-profile`` (alias ``--all``) is not a fourth scope but the profile
scope fanned out: a profile install into every profile that exists, and
nothing else — in particular not the global install. It exists because a
profile is *isolated* — nothing global ever reaches it — so covering every
profile is otherwise one command per profile, forgotten as soon as the next
profile is created.

The global and profile installs additionally seed the machine-wide workflow
layer (``~/.claude-launcher/workflows/``) with the workflows that ship in the
package. That seeding is what makes the layer a real thing rather than a
documented empty directory — but it is machine state, so it belongs to the
machine-scoped installs; a project install never writes outside its project.

They also ask Windows Defender to stop scanning the trees claunch works in
(:mod:`claude_launcher.defender`), for the same scoping reason: an antivirus
exclusion is machine state. It needs an elevated shell, so on an ordinary one
it fails — and that failure is *printed with the command to re-run*, rather
than aborting an install whose real work has already succeeded.

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

from . import commit_stamp, config, defender, mesh_install, mesh_topology, settings
from .cflow import authoring as cflow_authoring, install as cflow_install
from .cflow import state as cflow_state
from .profile import Profile

#: The server's key in ``.claude.json`` / ``.mcp.json`` — the name the agent
#: sees its tools namespaced under (``mcp__claunch__spawn``, and so on).
MCP_NAME = "claunch"

#: Server names earlier versions registered, replaced by :data:`MCP_NAME`.
LEGACY_MCP_NAMES = ("cflow", "mesh")

#: Harness permission rules that keep the AGENT's hands off the human gate
#: commands (``claunch cflow approve|select|goto|abort``).
#:
#: The cflow split is channels: the MCP tools are the agent's and carry no
#: approve, the CLI is the human's — but an agent with a shell tool holds
#: both, and could clear its own (or another run's) user gate with one Bash
#: call. The CLI itself cannot tell them apart: a human approving from a chat
#: session's ``!`` shell — a flow the gate messages themselves recommend —
#: runs with the very same environment. The harness permission layer is the
#: one place that knows WHO issued a command (deny rules bind the model's
#: tool calls and never the user's typed ``!`` input), so that is where the
#: guard lives. Same spirit as ``new-session``'s in-session refusal: a
#: drift-arresting bump that leaves every human door open, not a security
#: boundary.
#:
#: Both spellings per command (exact and ``:*`` prefix), for both shell
#: tools this harness may expose; a rule naming a tool a setup lacks is
#: inert.
GATE_DENY_RULES = tuple(
    f"{tool}(claunch cflow {cmd}{suffix})"
    for cmd in ("approve", "select", "goto", "abort")
    for tool in ("Bash", "PowerShell")
    for suffix in ("", ":*")
)


def mcp_server_def() -> dict:
    """The stdio server entry for the merged MCP bridge.

    On Windows ``claunch`` on PATH is typically a ``.bat``/``.cmd`` shim,
    which Claude Code's spawn (no shell) cannot exec directly — wrap in
    ``cmd /c``.
    """
    if sys.platform == "win32":
        return {"command": "cmd", "args": ["/c", "claunch", "mcp"]}
    return {"command": "claunch", "args": ["mcp"]}


def _gate_guard_lines(settings_path: Path) -> List[str]:
    """Merge :data:`GATE_DENY_RULES` into one settings file; report it."""
    changed = settings.merge_permission_deny(settings_path, GATE_DENY_RULES)
    note = "" if changed else " (already present)"
    return [f"gate guard (cflow human commands) -> {settings_path}{note}"]


def _skill_lines(skills_dir: Path) -> List[str]:
    """Write every skill into ``skills_dir``; report each in the shared voice."""
    return [
        f"skill -> {cflow_install.write_skill(skills_dir)}",
        f"skill -> {cflow_authoring.write_skill(skills_dir)}",
        f"skill -> {mesh_install.write_skill(skills_dir)}",
        *(f"skill -> {p}" for p in mesh_topology.write_skills(skills_dir)),
        f"skill -> {commit_stamp.write_skill(skills_dir)}",
    ]


def _workflow_lines() -> List[str]:
    """Report the global-layer seeding, in the same voice as the rest.

    Machine-local: the global workflow layer is one directory under the
    launcher home, which is why only the machine-scoped installs (global,
    profile) call this. Saying so on every such install is the point — that
    directory is where a workflow goes to be available from every project,
    and nothing else advertises it.

    An unchanged file gets no line of its own, but total silence reads as an
    omission — so when nothing was seeded or kept, one summary line says the
    layer is already up to date.
    """
    lines = []
    unchanged = 0
    for _, dest, outcome in cflow_install.seed_global_workflows():
        if outcome == cflow_install.SEEDED:
            lines.append(f"workflow -> {dest}")
        elif outcome == cflow_install.KEPT:
            # KEPT is what seeding did; say why, and how to act on it. Only a
            # stale copy — still exactly what we seeded — offers a next step,
            # and the next step is printed so it can be pasted, not guessed.
            why = cflow_install.worktree_state(
                _bundled_src_for(dest), dest, cflow_install.seed_record(dest.parent)
            )
            if why == cflow_install.STALE:
                lines.append(
                    f"workflow -> {dest} (stale; refresh with: "
                    f"claunch cflow update {dest.stem})"
                )
            elif why == cflow_install.EDITED:
                lines.append(f"workflow -> {dest} (kept; yours differs from the packaged one)")
            else:
                lines.append(
                    f"workflow -> {dest} (kept; not ours to tell whether edited — "
                    f"see 'claunch cflow update --help')"
                )
        else:
            unchanged += 1
    if not lines and unchanged:
        lines.append(f"workflow layer -> up to date ({unchanged} workflows)")
    return lines


def _bundled_src_for(dest: Path) -> Path:
    """The packaged file a global-layer copy of ``dest`` came from.

    Seeding copies both ``*.y*ml`` and their ``*_assets```, so the packaged
    counterpart is the same filename under the bundled directory — which is
    the only non-stable mapping (a name that exists in the layer but not in
    the bundle is not a seed at all, and the comparison falls back to the
    copy itself so it reads "unchanged" rather than crashing).
    """
    cand = cflow_state.bundled_workflows_dir() / dest.name
    if cand.exists():
        return cand
    return dest


def install_into_user() -> List[str]:
    """Register the MCP server + every skill for the user, globally.

    The user-scope targets of the default (profile-less) Claude Code setup:
    skills under ``~/.claude/skills``, MCP servers in ``~/.claude.json``.
    Both honour ``CLAUDE_CONFIG_DIR`` when it is set.
    """
    path = config.user_claude_json()
    settings.merge_mcp_servers_into(
        path, {MCP_NAME: mcp_server_def()}, remove=LEGACY_MCP_NAMES
    )
    skills = config.default_config_dir() / "skills"
    return (
        [f"mcp server {MCP_NAME!r} -> {path}"]
        + _skill_lines(skills)
        + _gate_guard_lines(config.default_config_dir() / settings.SETTINGS_FILENAME)
        + _workflow_lines()
        + defender.lines()
    )


def _profile_lines(profile: Profile) -> List[str]:
    """The profile-scoped writes: MCP registration + skills, nothing global."""
    settings.merge_mcp_servers(
        profile, {MCP_NAME: mcp_server_def()}, remove=LEGACY_MCP_NAMES
    )
    return (
        [f"mcp server {MCP_NAME!r} -> {profile.config_dir / settings.CLAUDE_JSON}"]
        + _skill_lines(profile.config_dir / "skills")
        + _gate_guard_lines(profile.config_dir / settings.SETTINGS_FILENAME)
    )


def install_into_profile(profile: Profile) -> List[str]:
    """Register the MCP server + every skill inside a profile's config dir."""
    return _profile_lines(profile) + _workflow_lines() + defender.lines()


def install_into_all_profiles() -> List[str]:
    """A profile install into every existing profile, in one pass.

    A profile is an isolated ``CLAUDE_CONFIG_DIR``, so a global install never
    reaches it — covering every profile means writing each in turn. The user's
    own global setup is deliberately not touched; that stays ``--global``'s
    job. The workflow layer is machine-wide and is seeded once rather than
    re-reported per profile — and not at all when there is no profile to
    install into.
    """
    from . import profile as profile_mod

    lines: List[str] = []
    for p in profile_mod.list_all():
        lines += _profile_lines(p)
    if not lines:
        return []
    return lines + _workflow_lines() + defender.lines()


def install_into_project(project_dir: Path) -> List[str]:
    """Register the MCP server (.mcp.json) + every skill (.claude/skills).

    Writes nothing outside ``project_dir`` — in particular it does not seed
    the global workflow layer; that is the global/profile installs' job
    (``claunch install`` prints a hint when the layer is empty).
    """
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
    return (
        [f"mcp server {MCP_NAME!r} -> {mcp_path}"]
        + _skill_lines(project_dir / ".claude" / "skills")
        + _gate_guard_lines(
            project_dir / ".claude" / settings.SETTINGS_FILENAME
        )
    )
