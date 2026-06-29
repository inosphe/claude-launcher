"""Remove local profile directories not declared in the store.

``~/.claunch.yaml`` is the source of truth and the launcher *creates* a directory
for every profile it lists (see :func:`bootstrap.reconcile`). The inverse —
deleting a directory that the store no longer lists — is destructive, so it is
never automatic; this is the explicit ``claunch prune`` operation.

An *orphan* is a profile directory whose name has no entry in the store. That
happens when an entry is removed from ``~/.claunch.yaml`` by hand, or a directory
is created outside the launcher.
"""

from __future__ import annotations

from typing import List

from . import profile, store
from .profile import Profile


def orphans() -> List[Profile]:
    """Profile directories present on disk but absent from the store."""
    declared = set(store.profiles())
    return [p for p in profile.list_all() if p.name not in declared]
