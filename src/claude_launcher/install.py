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
import os
import sys
from pathlib import Path
from typing import List

from . import (
    commit_stamp,
    config,
    defender,
    fsplan,
    harnesses,
    lineage,
    mesh_install,
    mesh_topology,
    settings,
)
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
#: ``set`` is the same split for a run's writable state: a path declared
#: ``by: [user]`` is written through that CLI, the agent's door is the
#: ``set_state`` MCP tool (which writes as ``agent``), and without this rule
#: an agent could type the CLI and write as the person.
#:
#: Both spellings per command (exact and ``:*`` prefix), for both shell
#: tools this harness may expose; a rule naming a tool a setup lacks is
#: inert.
GATE_DENY_RULES = tuple(
    f"{tool}(claunch cflow {cmd}{suffix})"
    for cmd in ("approve", "select", "goto", "abort", "set")
    for tool in ("Bash", "PowerShell")
    for suffix in ("", ":*")
)

#: The board's export file, denied to the agent's file tools.
#:
#: The issue board is the SQLite database ``.beads/beads.db``; ``issues.jsonl``
#: beside it is an export of that database, and the only supported way in is
#: ``claunch beads``. A line written into the export by hand never reaches the
#: database, and the next ``br sync --flush-only`` any session runs rewrites
#: the file from the database — the record is gone with no error and no
#: warning. The reverse mistake is worse: an issue present in the export and
#: missing from the database trips ``br``'s stale-export guard, which stops
#: every flush in that repository for every session until someone reconciles
#: the two by hand.
#:
#: Measured on this machine (``claunch-beads-guidance-missing-from-profile-g77zs``):
#: no layer an agent reads said any of this. The instruction now lives in the
#: mesh skill, the improv workflows and this repository's CLAUDE.md; this rule
#: is the half of it that does not depend on the agent having read anything.
#:
#: ``Edit`` alone, and anchored at the filesystem root: Claude Code consults
#: file paths only on ``Edit`` and ``Read`` rules — a path rule written for
#: ``Write``, ``NotebookEdit`` or ``MultiEdit`` is accepted, never consulted,
#: and warned about at startup — so one ``Edit`` rule covers all four writing
#: tools. ``//**/`` matches the path in any repository on any drive, which a
#: rule in user settings otherwise would not: an unanchored path there anchors
#: under ``~/.claude``.
#:
#: What it does not cover: a shell that writes the same file (``sed -i``, a
#: redirection). Command rules match command text, so a rule that tried would
#: be both leaky and noisy. That half is the written instruction's job.
BOARD_DENY_RULES = ("Edit(//**/.beads/issues.jsonl)",)

#: The claunch MCP server, allowed to the agent as one server-scoped rule.
#:
#: The other half of the deny rules above, planted in the same file for the
#: same reason: the harness permission layer is the one place that knows *who*
#: issued a call.
#:
#: Denying the agent the human CLI is what makes the MCP gate commands the
#: agent's only channel. But a session in Claude Code's ``auto`` mode reads
#: the deny list, then sees the MCP tool producing the same effect, and a
#: classifier handed only that list calls it "tool-switching circumvention of
#: an explicit deny rule". The session is then left with no way to advance its
#: own run -- and the effect it was denied is one the MCP tool does not
#: actually have: ``select`` on a ``user`` chooser records a proposal, it does
#: not answer the gate. Allow rules are consulted *before* that classifier, so
#: naming the server here is what keeps the agent's own channel open.
#:
#: Server-scoped (``mcp__claunch``) rather than one rule per tool, because the
#: split this guard draws is between channels, not between tools. Same form
#: the project's own ``.claude/settings.local.json`` already uses for another
#: MCP server; measured in ``claunch-1o3o``.
GATE_ALLOW_RULES = (f"mcp__{MCP_NAME}",)


def mcp_server_def() -> dict:
    """The stdio server entry for the merged MCP bridge.

    On Windows ``claunch`` on PATH is typically a ``.bat``/``.cmd`` shim,
    which Claude Code's spawn (no shell) cannot exec directly — wrap in
    ``cmd /c``.
    """
    if sys.platform == "win32":
        return {"command": "cmd", "args": ["/c", "claunch", "mcp"]}
    return {"command": "claunch", "args": ["mcp"]}


def _guard_lines(settings_path: Path) -> List[str]:
    """Merge every permission guard into one settings file; report each one.

    Two guards, one file: the cflow gate split (whose deny half needs the MCP
    allow half beside it, see :data:`GATE_ALLOW_RULES`) and the board's export
    file (:data:`BOARD_DENY_RULES`). Both merge as unions, so a reinstall
    leaves the person's own rules untouched.
    """
    denied = settings.merge_permission_deny(
        settings_path, (*GATE_DENY_RULES, *BOARD_DENY_RULES)
    )
    allowed = settings.merge_permission_allow(settings_path, GATE_ALLOW_RULES)
    note = "" if (denied or allowed) else " (already present)"
    return [
        f"gate guard (cflow human commands, {MCP_NAME} MCP allowed) "
        f"-> {settings_path}{note}",
        f"board guard (.beads/issues.jsonl not editable by file tools) "
        f"-> {settings_path}{note}",
    ]


def _skill_lines(skills_dir: Path) -> List[str]:
    """Write every skill into ``skills_dir``; report each in the shared voice."""
    return [
        f"skill -> {cflow_install.write_skill(skills_dir)}",
        f"skill -> {cflow_authoring.write_skill(skills_dir)}",
        f"skill -> {mesh_install.write_skill(skills_dir)}",
        *(f"skill -> {p}" for p in mesh_topology.write_skills(skills_dir)),
        f"skill -> {commit_stamp.write_skill(skills_dir)}",
    ]


def _codex_mcp_lines(home: Path) -> List[str]:
    """Register the merged server in a Codex harness home.

    Codex reads MCP servers from ``$CODEX_HOME/config.toml``.  Keep the rest
    of that user-owned file byte-for-byte and replace only tables owned by
    this installer (including the two superseded server names).
    """
    import re

    path = home / "config.toml"
    try:
        text = fsplan.read_text(path) or ""
    except (OSError, ValueError):
        text = ""
    for name in (*LEGACY_MCP_NAMES, MCP_NAME):
        table = re.escape(name)
        text = re.sub(
            rf"(?ms)^\[mcp_servers\.{table}(?:\.[^\]]+)?\]\s*.*?"
            rf"(?=^\[(?!mcp_servers\.{table}(?:\.|\]))|\Z)",
            "",
            text,
        )
    server = mcp_server_def()
    block = (
        f"[mcp_servers.{MCP_NAME}]\n"
        f"command = {json.dumps(server['command'])}\n"
        f"args = {json.dumps(server.get('args', []))}\n"
        # Codex passes explicitly allow-listed parent variables to stdio MCP
        # processes.  The session id keeps CLI and MCP calls on the same
        # cflow/mesh scope.
        f"env_vars = {json.dumps([cflow_state.SESSION_ENV])}\n"
    )
    text = text.rstrip()
    fsplan.write_text(path, f"{text}\n\n{block}" if text else block)
    return [f"mcp server {MCP_NAME!r} -> {path}"]


def _json_mcp_lines(home: Path) -> List[str]:
    """Register the merged server in a harness-owned JSON home.

    Kimi Code and Cursor Agent both use the Claude-compatible ``mcpServers``
    document -- Kimi under its profile home, Cursor under the machine-wide
    :func:`cursor_home`.  Their native permission files are different, so
    this helper deliberately touches only the MCP file.
    """
    path = home / "mcp.json"
    settings.merge_mcp_servers_into(
        path, {MCP_NAME: mcp_server_def()}, remove=LEGACY_MCP_NAMES
    )
    return [f"mcp server {MCP_NAME!r} -> {path}"]


def devin_home() -> Path:
    """Devin's machine-wide config home.

    Devin is the one declared harness with no per-profile home, and that is a
    property of the harness rather than a gap in this installer: no
    environment variable relocates it (see the ``devin`` entry in
    ``harnesses.yaml`` for the measurement). So a profile install has two
    honest options -- write where devin actually reads, or write into a
    profile child devin never opens. This picks the first, and every line it
    returns says ``machine-wide`` so the caller is not misled into thinking a
    profile install isolated anything.

    The layout differs by platform, so both are resolved rather than
    assumed: ``APPDATA\\devin`` on Windows, ``~/.config/devin`` elsewhere
    (which is where ``XDG_CONFIG_HOME`` would put it on a platform that
    honours it).

    ``CLAUNCH_DEVIN_HOME`` overrides the whole thing. It exists because this
    is the only install target that resolves to a *real machine directory
    outside* the throwaway home the test fixture builds -- without it, any
    test that installs a devin profile would write into the developer's own
    devin config and still pass, which is the exact failure the ``home``
    fixture exists to prevent. The fixture sets it for every test.
    """
    override = os.environ.get("CLAUNCH_DEVIN_HOME")
    if override:
        return Path(override)
    if sys.platform == "win32":
        roaming = os.environ.get("APPDATA")
        if roaming:
            return Path(roaming) / "devin"
    return Path.home() / ".config" / "devin"


def cursor_home() -> Path:
    """Where Cursor Agent reads user MCP servers and skills: ``~/.cursor``.

    The ``agent`` harness does declare a ``home_env`` (``CURSOR_CONFIG_DIR``),
    but that variable moves only ``cli-config.json``. The CLI resolves the
    user ``mcp.json`` and the user skill directories from ``os.homedir()``
    directly (measured -- see the ``agent`` entry in ``harnesses.yaml``), so
    an MCP registration written into the profile home is one it never opens.
    A profile install therefore writes here, and says ``machine-wide``, for
    the same reason :func:`devin_home` does.

    ``CLAUNCH_CURSOR_HOME`` overrides it so the test fixture keeps installs
    out of the developer's real ``~/.cursor``.
    """
    override = os.environ.get("CLAUNCH_CURSOR_HOME")
    if override:
        return Path(override)
    return Path.home() / ".cursor"


def _devin_mcp_lines() -> List[str]:
    """Register the merged server where the devin CLI reads MCP servers.

    ``devin mcp add -s user`` writes a ``mcpServers`` document -- the same
    shape Kimi and Cursor Agent use, so the same merge helper applies -- plus
    a ``transport`` key that devin requires on each stdio entry. Written
    directly rather than by shelling out to ``devin mcp add``, so the install
    stays a pure file write and needs no working binary.
    """
    path = devin_home() / "mcp_config.json"
    server = {**mcp_server_def(), "transport": "stdio"}
    settings.merge_mcp_servers_into(
        path, {MCP_NAME: server}, remove=LEGACY_MCP_NAMES
    )
    return [f"mcp server {MCP_NAME!r} -> {path} (machine-wide)"]


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
        + _guard_lines(config.default_config_dir() / settings.SETTINGS_FILENAME)
        + _workflow_lines()
        + defender.lines()
    )


def _profile_lines(profile: Profile) -> List[str]:
    """The profile-scoped writes, routed to the selected harness's home."""
    harness_name = lineage.effective_harness(profile)
    harness = harnesses.get(harness_name)
    assert harness is not None  # effective_harness validates the registry
    home = harness.profile_home(profile.config_dir)
    # Skills normally live inside the harness's profile home. Devin has no
    # per-profile home to put them in, so it overrides this below; keeping
    # the default here means the other four branches are untouched.
    skills_home = home
    if harness_name == "codex":
        mcp_lines = _codex_mcp_lines(home)
        guard_lines: List[str] = []
    elif harness_name == "kimi":
        mcp_lines = _json_mcp_lines(home)
        guard_lines = []
    elif harness_name == "agent":
        # Cursor Agent's profile home holds cli-config.json only; MCP servers
        # and user skills are read from ~/.cursor whatever CURSOR_CONFIG_DIR
        # says, so both go there.
        skills_home = cursor_home()
        mcp_lines = [
            f"{line} (machine-wide)" for line in _json_mcp_lines(skills_home)
        ]
        guard_lines = []
    elif harness_name == "devin":
        # Devin reads neither a profile child nor Claude Code's files, so the
        # profile-scope targets are its own machine-wide home. Its permission
        # format is likewise unmeasured, so no gate guard is claimed.
        mcp_lines = _devin_mcp_lines()
        skills_home = devin_home()
        guard_lines = []
    elif harness_name == "pi":
        # Pi intentionally has no MCP client.  Skills remain useful for the
        # protocol text, but claiming that its MCP tools were installed would
        # make a successful install misleading.
        mcp_lines = ["mcp server skipped (Pi does not support MCP)"]
        guard_lines = []
    elif harness_name == harnesses.CLAUDE_HARNESS:
        # Claude Code is the native installer target: its config dir *is*
        # the profile root, so it is the one harness whose files land there,
        # and the only one claunch knows the permission format of.
        settings.merge_mcp_servers(
            profile, {MCP_NAME: mcp_server_def()}, remove=LEGACY_MCP_NAMES
        )
        mcp_lines = [
            f"mcp server {MCP_NAME!r} -> "
            f"{profile.config_dir / settings.CLAUDE_JSON}"
        ]
        guard_lines = _guard_lines(
            profile.config_dir / settings.SETTINGS_FILENAME
        )
    else:
        # A declared harness with no MCP or permission format of its own.
        #
        # This used to be the Claude Code branch, reached by everything the
        # branches above did not name -- so a harness whose author had not
        # written a branch yet silently received Claude Code's `.claude.json`
        # and a gate guard in its profile home, files it never opens. The
        # install printed those writes as successes, so the mistake was
        # invisible from the outside until a session could not find its
        # tools. That is the form this arrived in: devin sat here until
        # claunch-2qr22 gave it a branch, and the next harness would have
        # landed in exactly the same place.
        #
        # Naming the fall-through is the fix. Claiming to have installed
        # something into a harness nobody has taught claunch to speak to is
        # worse than saying nothing was installed, because the caller (and
        # the human reading the output) can act on the second and not the
        # first. Skills still go to the harness's profile home below: that
        # path is the declared convention for any harness with a home_env,
        # so it is a default rather than a guess.
        mcp_lines = [
            f"mcp server skipped ({harness_name!r} declares no MCP format)"
        ]
        guard_lines = []
    return (
        mcp_lines
        + _skill_lines(skills_home / "skills")
        + guard_lines
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
    fsplan.mkdir(project_dir)
    mcp_path = project_dir / ".mcp.json"
    try:
        text = fsplan.read_text(mcp_path)
        doc = json.loads(text) if text is not None else {}
    except (OSError, ValueError):
        doc = {}
    if not isinstance(doc, dict):
        doc = {}
    servers = doc.setdefault("mcpServers", {})
    if isinstance(servers, dict):
        for name in LEGACY_MCP_NAMES:
            servers.pop(name, None)
        servers[MCP_NAME] = mcp_server_def()
    fsplan.write_text(mcp_path, json.dumps(doc, indent=2) + "\n")
    return (
        [f"mcp server {MCP_NAME!r} -> {mcp_path}"]
        + _skill_lines(project_dir / ".claude" / "skills")
        + _guard_lines(
            project_dir / ".claude" / settings.SETTINGS_FILENAME
        )
    )
