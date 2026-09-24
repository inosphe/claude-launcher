"""What each install command would change, and running it from the daemon.

The commands that put claunch into place have multiplied: ``claunch install``
in four scopes (project, ``--global``, ``--profile``, ``--all``; the
``cflow install`` / ``mesh install`` aliases are the same body) and
``claunch cflow update`` with and without ``--force``. Each writes a different
set of files, and from a terminal the only way to learn which was to run it.

This module names every such command as a :class:`Target`, previews it with
:func:`plan` — the real install code run under :func:`fsplan.dry_run`, so the
preview is what the command does and not a description of it — and runs it
for real with :func:`apply`. The daemon's Settings page (``/api/install``) is
the one caller; the CLI keeps its own commands.

Each file a plan touches is sorted into a :data:`CATEGORIES` entry, which
carries the *effect* of that write — what changes for a session after it
lands — because a path alone does not say why anybody should want it.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List

from . import config, fsplan, install, profile as profile_mod, workspaces
from .cflow import install as cflow_install
from .cflow import state as cflow_state


class TargetError(Exception):
    """An unknown or unusable target id."""


@dataclass(frozen=True)
class Target:
    """One install command, addressable by ``id``."""

    id: str
    kind: str  #: "install" or "cflow-update"
    scope: str  #: project / global / profile / all-profiles / global-layer
    label: str
    command: str  #: the CLI line that does the same thing
    run: Callable[[], List[str]]

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "scope": self.scope,
            "label": self.label,
            "command": self.command,
        }


def _cflow_update_lines(force: bool) -> List[str]:
    # ``can_ask`` stays False: the daemon has no terminal to prompt at, so an
    # edited copy is replaced only when the caller asked for --force.
    lines = []
    for name, outcome, applied, detail in cflow_install.update_global_workflows(
        [], force=force, can_ask=False
    ):
        verb = "updated" if applied else "kept"
        lines.append(f"{name}: {outcome} -> {verb}: {detail}")
    return lines


def targets() -> List[Target]:
    """Every install command this machine can run, in a stable order."""
    out: List[Target] = []
    for ws in workspaces.list_all():
        path = Path(ws.path)
        out.append(Target(
            id=f"install:project:{ws.name}",
            kind="install",
            scope="project",
            label=f"Project: {ws.name}",
            command=f'claunch install --project "{ws.path}"',
            run=lambda path=path: install.install_into_project(path),
        ))
    out.append(Target(
        id="install:global",
        kind="install",
        scope="global",
        label="Global (your own Claude Code setup)",
        command="claunch install --global",
        run=install.install_into_user,
    ))
    for p in profile_mod.list_all():
        out.append(Target(
            id=f"install:profile:{p.name}",
            kind="install",
            scope="profile",
            label=f"Profile: {p.name}",
            command=f"claunch install --profile {p.name}",
            run=lambda name=p.name: install.install_into_profile(
                profile_mod.require_selector(name)
            ),
        ))
    out.append(Target(
        id="install:all-profiles",
        kind="install",
        scope="all-profiles",
        label="Every profile",
        command="claunch install --all  (= claunch cflow install --all)",
        run=install.install_into_all_profiles,
    ))
    out.append(Target(
        id="cflow-update",
        kind="cflow-update",
        scope="global-layer",
        label="Workflows: refresh stale copies",
        command="claunch cflow update",
        run=lambda: _cflow_update_lines(False),
    ))
    out.append(Target(
        id="cflow-update:force",
        kind="cflow-update",
        scope="global-layer",
        label="Workflows: replace edited copies too",
        command="claunch cflow update --force",
        run=lambda: _cflow_update_lines(True),
    ))
    return out


def find(target_id: str) -> Target:
    for t in targets():
        if t.id == target_id:
            return t
    raise TargetError(f"unknown install target {target_id!r}")


#: Category -> the effect a write in it has. Matched in order by
#: :func:`categorize`; the first hit wins.
CATEGORIES: Dict[str, str] = {
    "mcp": "Registers the 'claunch' MCP server: sessions started after this "
    "get the mcp__claunch__* tools (cflow, mesh, spawn). Running sessions "
    "keep what they started with.",
    "skill": "Writes a skill's instructions: the next session that triggers "
    "it (e.g. /cflow, /mesh) loads the new text.",
    "guard": "Merges permission rules into settings.json: the agent cannot "
    "run the human cflow gate commands or edit .beads/issues.jsonl, and the "
    "claunch MCP server is allowed. Your own rules are kept.",
    "workflow": "Replaces a workflow in the global layer "
    "(~/.claude-launcher/workflows): runs started after this in a project "
    "without its own copy follow the new steps. Running runs keep theirs.",
    "seed-record": "Records the sha256 of what was seeded, so a later "
    "'cflow update' can tell a stale copy from one you edited.",
    "backup": "Keeps your edited workflow as a single .bak before it is "
    "replaced.",
    "other": "A file the install writes.",
}


def categorize(path: Path) -> str:
    name = path.name
    parts = [p.lower() for p in path.parts]
    if name.endswith(".bak"):
        return "backup"
    if name == cflow_install.SEED_RECORD_NAME:
        return "seed-record"
    if name == "SKILL.md" and "skills" in parts:
        return "skill"
    if name in (".mcp.json", ".claude.json", "mcp.json", "mcp_config.json", "config.toml"):
        return "mcp"
    if name == "settings.json":
        return "guard"
    if _under(path, cflow_state.global_workflows_dir()):
        return "workflow"
    return "other"


def _under(path: Path, root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (ValueError, OSError):
        return False


def _summary(changes: List[fsplan.Change]) -> Dict[str, int]:
    counts = {fsplan.CREATE: 0, fsplan.UPDATE: 0, fsplan.UNCHANGED: 0, fsplan.DELETE: 0}
    for c in changes:
        counts[c.kind] += 1
    return counts


def _report(target: Target, lines: List[str], changes: List[fsplan.Change],
            with_text: bool) -> dict:
    rows = []
    for c in changes:
        row = c.to_dict(with_text=with_text and c.kind != fsplan.UNCHANGED)
        row["category"] = categorize(c.path)
        rows.append(row)
    return {
        "target": target.to_dict(),
        "summary": _summary(changes),
        "changes": rows,
        "lines": lines,
        "effects": {
            cat: CATEGORIES[cat]
            for cat in dict.fromkeys(r["category"] for r in rows
                                     if r["kind"] != fsplan.UNCHANGED)
        },
    }


def plan(target_id: str, with_text: bool = True, skip_defender: bool = False) -> dict:
    """Run ``target_id`` under a dry run and report what it would change.

    Nothing is written. ``lines`` are the lines the CLI would print, from the
    same code; ``changes`` has one row per file with its kind (create /
    update / unchanged) and, when ``with_text``, both texts for a diff.
    """
    target = find(target_id)
    with fsplan.dry_run(skip_defender=skip_defender) as recorded:
        lines = target.run()
    out = _report(target, lines, recorded.changes, with_text)
    out["dry_run"] = True
    return out


def overview() -> dict:
    """Every target's plan, counts only — the Settings page's first view.

    Defender is left out (``skip_defender``): reading its list spawns
    PowerShell, and doing so once per target would make the page wait
    seconds for a line that is the same in every row. A single target's
    :func:`plan` includes it.
    """
    rows = []
    for t in targets():
        try:
            p = plan(t.id, with_text=False, skip_defender=True)
            rows.append({
                **t.to_dict(),
                "summary": p["summary"],
                "pending": [
                    {"path": c["path"], "kind": c["kind"], "category": c["category"]}
                    for c in p["changes"] if c["kind"] != fsplan.UNCHANGED
                ],
            })
        except Exception as exc:  # one broken target must not blank the page
            rows.append({**t.to_dict(), "error": f"{type(exc).__name__}: {exc}"})
    return {
        "targets": rows,
        "global_workflows_dir": str(cflow_state.global_workflows_dir()),
        "user_config_dir": str(config.default_config_dir()),
        "categories": CATEGORIES,
    }


#: One real install at a time: two overlapping runs would each read the
#: other's half-written settings file as their starting point.
_APPLY_LOCK = threading.Lock()


def apply(target_id: str) -> dict:
    """Run ``target_id`` for real and report what it changed.

    The report is the dry run taken just before the real run (so ``changes``
    says what the run was about to do) plus the lines the real run printed.
    """
    target = find(target_id)
    with _APPLY_LOCK:
        before = plan(target_id, with_text=False, skip_defender=True)
        lines = target.run()
    return {
        "target": target.to_dict(),
        "dry_run": False,
        "summary": before["summary"],
        "changes": before["changes"],
        "effects": before["effects"],
        "lines": lines,
        "note": "Restart active agent sessions for the MCP server and skills "
        "to be picked up.",
    }

