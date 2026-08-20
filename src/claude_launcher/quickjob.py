"""Quick job dispatch: the leader's one-form worker, preconfigured in YAML.

A leader session hands work out by spawning children, and in practice it
spawns the same *kind* of child every time: a worker-role session driving the
worker workflow, in a checkout of its own, briefed with one task. The spawn
wizard can build that child, but it asks every question every time; the
dashboard's quick-job form asks only the task and reads everything else from
here.

The defaults live in ``~/.claunch.yaml`` (see :mod:`store`), so they are the
user's to set and survive the daemon::

    quick_job:
      role: worker             # the child's role (stance at every spawn)
      workflow: improv-worker  # cflow run started for the child ('' = none)
      worktree: true           # cut the child a checkout of its own
      name_prefix: job         # child names/worktrees are stamped after this
      task: ''                 # text placed before the typed job description

This is *defaults for a form*, not a policy: what a child may actually be is
still :mod:`spawn`'s decision, and a default that the spawn policy refuses is
refused at spawn time exactly as if it had been typed. Malformed values fall
back field by field rather than raising, the same reading discipline
:class:`~claude_launcher.spawn.SpawnPolicy` keeps — a YAML typo must read as
a wrong default, not a broken dashboard.
"""

from __future__ import annotations

from typing import Optional

from . import store

#: What the form offers before anybody edits the config. The role/workflow
#: pair is the worker convention the meshes here already run on; the prefix
#: keeps a fleet of quick jobs recognisable in the rail.
DEFAULTS = {
    "role": "worker",
    "workflow": "improv-worker",
    "worktree": True,
    "name_prefix": "job",
    "task": "",
}

#: The block's string-valued keys, for load/save symmetry.
_STR_KEYS = ("role", "workflow", "name_prefix", "task")


def load(doc: Optional[dict] = None) -> dict:
    """The ``quick_job`` block over :data:`DEFAULTS`, field by field.

    Unknown keys are dropped and wrongly-typed values fall back alone: the
    block is read on a dashboard poll, and one bad field must not take the
    other four defaults down with it.
    """
    raw = (doc if doc is not None else store.load()).get("quick_job")
    block = dict(DEFAULTS)
    if isinstance(raw, dict):
        for key in _STR_KEYS:
            if isinstance(raw.get(key), str):
                block[key] = raw[key].strip()
        if isinstance(raw.get("worktree"), bool):
            block["worktree"] = raw["worktree"]
    return block


def save(values: dict) -> dict:
    """Merge ``values`` into the stored block and return the result.

    A partial update on purpose — the form saves the fields it shows, and a
    key it does not name keeps its stored value. Values are normalised the
    way :func:`load` reads them, so what is written is exactly what the next
    read reports; a key that is not the block's is refused with a
    ``ValueError`` rather than stored and silently never read again.
    """
    unknown = sorted(set(values) - set(DEFAULTS))
    if unknown:
        raise ValueError(
            f"unknown quick_job key(s): {', '.join(unknown)} — "
            f"known: {', '.join(sorted(DEFAULTS))}"
        )
    clean: dict = {}
    for key in _STR_KEYS:
        if key in values:
            if not isinstance(values[key], str):
                raise ValueError(f"quick_job.{key} must be a string")
            clean[key] = values[key].strip()
    if "worktree" in values:
        if not isinstance(values["worktree"], bool):
            raise ValueError("quick_job.worktree must be true or false")
        clean["worktree"] = values["worktree"]

    def put(doc: dict) -> None:
        block = doc.get("quick_job")
        if not isinstance(block, dict):
            block = {}
        block.update(clean)
        doc["quick_job"] = block

    return load(store.update(put))
