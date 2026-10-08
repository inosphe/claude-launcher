"""Codex's two runtime switches — YOLO and Sandbox — as argv.

Codex has no single "mode" flag: approvals and the sandbox are two separate
options, and the packaged declaration's default (``--dangerously-bypass-
approvals-and-sandbox``) answers both at once. The harness declaration
names the four argv groups involved (``harnesses.yaml``), and these
functions turn the two switches into those groups and back. The new-session
wizard, the web forms (``codexRuntimeArgs`` in ``app.js``) and the daemon's
restart of a running session (:meth:`SessionManager.runtime_mode`) read and
write a session's args by this one rule, so the checkbox a form shows, the
command the daemon builds and the state the detail panel reports agree.

``capabilities`` is a harness declaration as a dict (``HarnessEntry.
to_dict()``, or the web's ``harness_details``). A key it does not declare
falls back to Codex's own spelling.
"""

from __future__ import annotations

from typing import Any, List, Tuple

BYPASS_ARGS = ["--dangerously-bypass-approvals-and-sandbox"]
SKIP_ARGS = ["-a", "never"]
SANDBOX_ON_ARGS = ["--sandbox", "workspace-write"]
SANDBOX_OFF_ARGS = ["--sandbox", "danger-full-access"]
#: What ``skip_permissions_args`` used to declare. Codex 0.160 refuses it
#: ("unexpected argument '--approval-mode'"), so a session that still
#: carries it never starts. It is removed whenever the switches are written
#: again, which is how such a session is repaired — and only removed: the
#: state is read without it, by the same rule the daemon builds the command
#: by (``harness.build_command`` does not count it as a mode group), so the
#: panel and the no-op check see the mode the session is launched with.
LEGACY_SKIP_ARGS = ["--approval-mode", "full-auto"]


def argv_has_group(argv: List[str], group: Any) -> bool:
    """Whether the contiguous argv ``group`` occurs in ``argv``."""
    wanted = [str(value) for value in (group or [])]
    if not wanted:
        return False
    return any(
        argv[i:i + len(wanted)] == wanted
        for i in range(len(argv) - len(wanted) + 1)
    )


def _declared(capabilities: dict, key: str, fallback: List[str]) -> List[str]:
    return [str(value) for value in (capabilities.get(key) or fallback)]


def mode_groups(capabilities: dict, *, legacy: bool = True) -> List[List[str]]:
    """The argv groups the two switches control — with the legacy one for
    removal, without it for reading the state."""
    groups: List[List[str]] = []
    for group in (
        _declared(capabilities, "mode_conflict_args", BYPASS_ARGS),
        _declared(capabilities, "skip_permissions_args", SKIP_ARGS),
        _declared(capabilities, "full_access_args", SANDBOX_OFF_ARGS),
        _declared(capabilities, "full_access_off_args", SANDBOX_ON_ARGS),
        *([LEGACY_SKIP_ARGS] if legacy else []),
    ):
        if group and group not in groups:
            groups.append(group)
    return groups


def without_groups(argv: Any, groups: List[List[str]]) -> List[str]:
    """Remove every complete managed group while preserving other argv."""
    remaining = [str(value) for value in (argv or []) if value != "--"]
    i = 0
    while i < len(remaining):
        found = next(
            (group for group in groups if remaining[i:i + len(group)] == group),
            None,
        )
        if found:
            del remaining[i:i + len(found)]
        else:
            i += 1
    return remaining


def runtime_state(argv: Any, capabilities: dict) -> Tuple[bool, bool]:
    """Return ``(yolo, sandbox)`` for one Codex session's own argv.

    An empty session argv still runs with the harness declaration's default
    YOLO argument.  Once any managed mode group is present, daemon command
    construction removes that default and the session groups are the complete
    answer.  Reading the state by the same boundary keeps a Spawn wizard's
    inherited checkboxes aligned with the command the parent actually runs.
    """
    own = [str(value) for value in (argv or []) if value != "--"]
    managed = any(argv_has_group(own, group)
                  for group in mode_groups(capabilities, legacy=False))
    bypass = _declared(capabilities, "mode_conflict_args", BYPASS_ARGS)
    declared = capabilities.get("args") or bypass
    effective = own if managed else [*[str(value) for value in declared], *own]
    skip = _declared(capabilities, "skip_permissions_args", SKIP_ARGS)
    sandbox_on = _declared(capabilities, "full_access_off_args", SANDBOX_ON_ARGS)
    sandbox_off = _declared(capabilities, "full_access_args", SANDBOX_OFF_ARGS)
    skipping = argv_has_group(effective, skip)
    yolo = argv_has_group(effective, bypass) or skipping
    sandbox = argv_has_group(effective, sandbox_on) or (
        skipping and not argv_has_group(effective, sandbox_off)
    )
    return yolo, sandbox


def runtime_args(
    argv: Any,
    capabilities: dict,
    *,
    yolo: bool,
    sandbox: bool,
) -> List[str]:
    """Apply the two switches to arbitrary extra argv.

    The four states are explicit and mutually consistent:

    * YOLO on, sandbox off: bypass approvals and the sandbox;
    * YOLO on, sandbox on: never ask, with workspace-write;
    * YOLO off, sandbox off: approval prompts with danger-full-access;
    * YOLO off, sandbox on: approval prompts with workspace-write.

    Existing managed groups are removed first, so a value typed into Args
    cannot leave two contradictory Codex modes in the command.
    """
    clean = without_groups(argv, mode_groups(capabilities))
    if yolo and not sandbox:
        return [*_declared(capabilities, "mode_conflict_args", BYPASS_ARGS), *clean]
    mode: List[str] = []
    if yolo:
        mode.extend(_declared(capabilities, "skip_permissions_args", SKIP_ARGS))
    mode.extend(
        _declared(capabilities, "full_access_off_args", SANDBOX_ON_ARGS)
        if sandbox
        else _declared(capabilities, "full_access_args", SANDBOX_OFF_ARGS)
    )
    return [*mode, *clean]


def declares_runtime(capabilities: dict) -> bool:
    """Whether a harness declares the sandbox pair the switches write."""
    return bool(capabilities.get("full_access_args")
                and capabilities.get("full_access_off_args"))
