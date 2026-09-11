"""Restart the live daemon only when a restart would change what it serves.

``improv-leader``'s ``reflect`` step declares ``tools/restart_live.*`` as its
``restart:`` command, and the daemon's RestartClock runs that command the
moment the step is entered -- once per visit, before the step's checklist has
said anything. Until this file existed the two shell wrappers ran ``claunch
daemon restart`` unconditionally, so every round that reached ``reflect``
restarted the daemon, including rounds that had merged nothing (2026-09-11
17:09: the tip differed from the running daemon's commit only in
``.beads/``). A restart takes every attached session's turn down with it, so
one that serves nothing new is pure cost.

The decision is ``tools/deploy_check.py``'s, imported rather than restated:
it already knows the four answers, and only one of them is fixed by a
restart::

    0  serving the branch's code      -> nothing to do
    1  serving older code / nothing   -> restart
    2  cannot tell                    -> do not restart; say why
    3  the checkout is dirty          -> do not restart; a restart onto a
                                         dirty tree serves no commit either

The two platform wrappers (``restart_live.ps1``, ``restart_live.sh``) call
this file and pass its exit status through, so the RestartClock's result block
carries the verdict to the run's driver. Exit 0 both when the daemon was
restarted and when it did not need to be: either way the step's checklist
(``deploy_check`` again, polled by the daemon) is what settles ``deployed``.

This file is the one place in the repository that runs ``claunch daemon
restart`` on a workflow's behalf. It runs as the daemon's child, in the
operator's environment (no ``CLAUNCH_SESSION``), which is what makes the
restart immediate; an agent session must not run it, or the command inside
it, by hand (see README, "Who may restart or stop the daemon").
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import deploy_check  # noqa: E402  -- tools/deploy_check.py, from the line above

#: The verdicts of ``deploy_check`` that a restart does not fix, and what to
#: tell the driver about each. Keyed by exit code so a new verdict there is
#: an error here rather than a silent restart.
_NOT_FIXED_BY_RESTART = {
    deploy_check.CANNOT_TELL: (
        "not restarting: deploy_check cannot tell what the daemon serves, "
        "and a restart would not answer that -- fix what it reported first"
    ),
    deploy_check.DIRTY: (
        "not restarting: the served checkout is dirty, so a restarted daemon "
        "would serve no commit's code either -- commit or set aside the "
        "listed paths (or declare them with --allow-dirty), then run again"
    ),
}


def _restart_command() -> list:
    exe = shutil.which("claunch")
    if exe is None:
        raise LookupError("claunch is not on PATH; nothing can restart the daemon")
    return [exe, "daemon", "restart"]


def main(argv: Optional[list] = None, *, run=subprocess.call) -> int:
    ap = argparse.ArgumentParser(
        prog="restart_live",
        description="restart the live daemon if it is serving older code than <branch>",
    )
    ap.add_argument("--repo", type=Path, default=Path("."))
    ap.add_argument("--branch", default="master")
    ap.add_argument("--daemon-json", type=Path, default=None)
    ap.add_argument("--allow-dirty", default=None, metavar="PATHS|sha1:HEX")
    args = ap.parse_args(argv)

    check_argv = ["--repo", str(args.repo), "--branch", args.branch]
    if args.daemon_json is not None:
        check_argv += ["--daemon-json", str(args.daemon_json)]
    if args.allow_dirty is not None:
        check_argv += ["--allow-dirty", args.allow_dirty]
    verdict = deploy_check.main(check_argv)

    if verdict == deploy_check.SERVING:
        print("no restart needed: the daemon already serves this code")
        return 0
    if verdict != deploy_check.NOT_RESTARTED:
        message = _NOT_FIXED_BY_RESTART.get(
            verdict, f"not restarting: deploy_check answered {verdict}, which is not a restart's to fix"
        )
        print(message, file=sys.stderr)
        return verdict

    try:
        command = _restart_command()
    except LookupError as exc:
        print(f"cannot restart: {exc}", file=sys.stderr)
        return 1
    print(f"restarting: {' '.join(command)}", flush=True)
    return int(run(command))


if __name__ == "__main__":
    raise SystemExit(main())
