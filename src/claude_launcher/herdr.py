"""Talking to Herdr, when claunch happens to be running inside it.

Herdr is a terminal multiplexer that arranges agents into workspaces, tabs and
panes. claunch does not depend on it and must not require it -- but when a
launch *is* happening inside a Herdr pane, two things are free that are
otherwise guesswork: the pane has a stable name, and its label is the one
place a human scanning a wall of panes can read what that pane is doing.

Everything here is therefore best-effort and silent on failure. A pane label
is decoration; a launch that dies because a multiplexer was mid-restart would
be a bug. Per Herdr's own skill, ``HERDR_ENV=1`` is the flag that says the
CLI may be trusted to talk to the current session -- outside it we do not
probe, we just report that we are not there.
"""

from __future__ import annotations

import os
import re
import subprocess
from typing import Optional

#: Set to "1" inside a Herdr-managed pane; the gate on everything below.
ENV_FLAG = "HERDR_ENV"

#: The caller's pane, injected by Herdr (e.g. ``w4:p4``).
PANE_ID_ENV = "HERDR_PANE_ID"

#: Seconds to wait on the CLI. It talks to a local socket, so this is a
#: deadlock guard, not a budget.
_TIMEOUT = 5.0


def in_herdr() -> bool:
    """Whether this process is running inside a Herdr-managed pane."""
    return os.environ.get(ENV_FLAG) == "1"


def pane_id() -> Optional[str]:
    """The calling pane's id, or ``None`` outside Herdr."""
    if not in_herdr():
        return None
    return os.environ.get(PANE_ID_ENV) or None


def _run(args: list) -> bool:
    try:
        done = subprocess.run(
            ["herdr", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def rename_pane(label: str, *, pane: Optional[str] = None) -> bool:
    """Relabel a pane; ``True`` if Herdr took it.

    Defaults to the calling pane. Does nothing (and says so by returning
    ``False``) outside Herdr, so callers can treat it as advisory.
    """
    target = pane or pane_id()
    if not target:
        return False
    label = " ".join(label.split())[:60]
    if not label:
        return False
    return _run(["pane", "rename", target, label])


def launch_label(worktree: str, branch: str) -> str:
    """The pane label for a launch: which worktree, on which branch.

    They are usually the same word -- a fresh worktree is cut with a branch of
    its own name -- and printing it twice would waste the only line a human
    reads at a glance. They diverge when an existing checkout is reused, and
    then both matter.
    """
    worktree = worktree.strip()
    branch = branch.strip()
    if not branch or branch == worktree:
        return worktree
    return f"{worktree} [{branch}]"


def sanitize_fragment(text: str, limit: int = 40) -> str:
    """A path- and branch-safe fragment of ``text`` (may be empty).

    Lives here because the only caller that needs it is naming things after a
    pane id, and ``w4:p4`` is exactly the shape that needs the treatment.
    """
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-._")[:limit]
