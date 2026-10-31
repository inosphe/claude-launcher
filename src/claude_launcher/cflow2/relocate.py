"""Move a session's cflow v2 run slots when the session changes directory.

Runs are keyed ``(cwd, scope)`` — a slot's files live at
``<cwd>/.cflow2/runs/<scope>`` (:class:`claude_launcher.cflow2.store.RunStore`).
:meth:`claude_launcher.daemon.manager.SessionManager.migrate` changes a
session's cwd, and a session's slots must travel with it: left behind, the
session's ``flow_*`` calls resolve the run store under the new cwd and find
nothing, while the run's files sit unreachable under the old one (the
dashboard rail works around the stale run by preferring the cwd that still
holds the live session — a workaround this move makes unnecessary).

Finished runs archived under ``<cwd>/.cflow2/archive/`` stay where they are:
they are history no live run reads, and dragging them along is not needed for
the session to keep working.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import List

from . import store as store_mod

log = logging.getLogger(__name__)


def _slots_of(runs_dir: Path, session: str) -> List[Path]:
    """Slot directories under ``runs_dir`` holding a live run of ``session``."""
    if not runs_dir.is_dir():
        return []
    out = []
    for p in sorted(runs_dir.iterdir()):
        if p.is_symlink() or not p.is_dir():
            continue
        if store_mod.session_of(p.name) != session:
            continue
        if (p / store_mod.STATE_FILE).is_file() or (p / store_mod.checkpoint.FILE).exists():
            out.append(p)
    return out


def move_slots(old_cwd: str, new_cwd: str, session: str) -> List[str]:
    """Move every run slot of ``session`` from ``old_cwd`` to ``new_cwd``.

    Returns the moved slot names (``[]`` when the session had none — the
    common case). Refuses to overwrite a slot that already exists at the
    destination: a collision means the destination already holds a run under
    that scope, and replacing it would orphan whatever drove that one.
    """
    if not old_cwd:
        return []
    src_root = store_mod.resolve_cwd(old_cwd) / store_mod.BASE_DIR / "runs"
    dest_root = store_mod.resolve_cwd(new_cwd) / store_mod.BASE_DIR / "runs"
    moved: List[str] = []
    try:
        for slot in _slots_of(src_root, session):
            target = dest_root / slot.name
            if target.exists():
                raise store_mod.StoreError(
                    f"run slot {slot.name!r} already exists at {target}"
                )
            dest_root.mkdir(parents=True, exist_ok=True)
            shutil.move(str(slot), str(target))
            moved.append(slot.name)
    except BaseException:
        for name in reversed(moved):
            # put back what we took before letting the failure travel on
            back = dest_root / name
            if back.exists() and not (src_root / name).exists():
                src_root.mkdir(parents=True, exist_ok=True)
                shutil.move(str(back), str(src_root / name))
        raise
    if moved:
        log.info("cflow2 slots of %s moved %s -> %s: %s",
                 session, src_root, dest_root, ", ".join(moved))
    return moved
