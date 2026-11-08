"""``claunch delayed-sweep <action>``: the delayed sweep queue, from any repository.

A packaged workflow is the same file in every repository, so the command a
verify or a probe names has to exist in every repository. The queue tool used
to be spelled ``python tools/delayed_sweep.py``, which exists only in a
claude-launcher checkout; elsewhere a repository had to carry a copy or a
delegating shim (gds6-hkpv). This command is the installed spelling
(claunch-sr6ex), and it runs the package's copy of the tool
(:mod:`claude_launcher.delayed_sweep_cli`).

The checkout's ``tools/delayed_sweep.py`` still exists and delegates here, so
the older spelling keeps working; nothing routes through it any more.

Every argument after the command name passes through unchanged, and so does the
exit code (``job-state`` answers 0/1, ``check-enqueued --classify`` 2).
"""

from __future__ import annotations

import argparse
from typing import List


def _cmd_delayed_sweep(args: argparse.Namespace) -> int:
    from .delayed_sweep_cli import main

    rest: List[str] = list(args.delayed_sweep_args or [])
    if rest[:1] == ["--"]:
        rest = rest[1:]
    return int(main(rest) or 0)


def register(sub) -> None:
    p = sub.add_parser(
        "delayed-sweep",
        help="record, inspect and run post-merge verification jobs (the delayed sweep queue)",
        description=(
            "Run the delayed sweep queue tool with the arguments that follow: "
            "enqueue, admit, repair, release, status, cancel, requeue, "
            "retry-watcher, remeasure, unbatch, job-state, suspects, nudge, "
            "run-one, check-batch, check-enqueued, check-merges. The exit code "
            "is the tool's own (job-state: 0 done, 1 still waiting; "
            "check-enqueued --classify: 2 unless the report is ok). Put the "
            "action first: everything after the command name is passed through, "
            "and that starts at the first positional, so --database and the "
            "other options follow the action."
        ),
    )
    p.add_argument("delayed_sweep_args", nargs=argparse.REMAINDER,
                   help="the queue tool's arguments, passed through unchanged")
    p.set_defaults(func=_cmd_delayed_sweep)
