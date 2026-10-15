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

The decision is ``tools/deploy_check.py``'s, imported rather than restated,
and it is a one-sided one: the restart is skipped only on the answer that
proves it pointless, and taken on every other::

    0  serving the branch's code      -> nothing to do
    1  serving older code / nothing   -> restart
    2  cannot tell                    -> restart (the old, unconditional
                                         behaviour; a guess must not skip)
    3  the checkout is dirty          -> restart, and say the checklist
                                         will stay red until it is clean

3 is taken, not skipped, because of a deadlock measured on 2026-09-11 18:03:
``deploy_check`` folds the dirt the daemon *booted* with into its answer, so a
daemon that loaded edited files keeps answering 3 after those files are
committed -- and the only thing that clears boot-time dirt is a restart. A
first version of this file skipped on 3, and the round could not be closed by
committing, nor by forcing ``reflect`` again. Dirt that is still there *now*
is a different matter: the restart goes out (it may carry new commits), but
``deployed`` cannot turn green until somebody commits or sets that dirt aside,
which ``deploy_check`` says in its own words.

The two platform wrappers (``restart_live.ps1``, ``restart_live.sh``) call
this file and pass its exit status through, so the RestartClock's result block
carries the verdict to the run's driver. Exit 0 both when the daemon was
restarted and when it did not need to be: either way the step's checklist
(``deploy_check`` again, polled by the daemon) is what settles ``deployed``.

The restart goes out with ``--reason``: the verdict above and the commits of
``<branch>`` the live daemon does not serve yet (claunch-mm8zv). Run as the
driving session, the restart waits on the web UI's approval gate, and the
person deciding reads that reason on the card -- "these merges are not
live" is what makes approving it an informed click.

The restart keeps the sessions that run in the PTY host (claunch-xnnct.1):
the new daemon adopts them, still running. It adds ``--with-pty-host`` --
ending them and the host, and relaunching them on a new one -- only when the
PTY host's own code (:data:`HOST_CODE`) changed between what the live daemon
serves and ``<branch>``: a kept host would go on running the old code. The
reason says which of the two it is and why.

This file is the one place in the repository that runs ``claunch daemon
restart`` on a workflow's behalf. It runs as the daemon's child: the v1 and
v2 RestartClocks set ``CLAUNCH_SESSION`` to the driving run's session
(``cflow_clock.RestartClock._execute``, ``cflow2_clock.perform_restart``),
so the restart goes through the web UI's approval gate like any session's.
An agent session must not run it, or the command inside it, by hand (see
README, "Who may restart or stop the daemon").
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

#: What to tell the driver on each answer that still ends in a restart.
#: Keyed by exit code so a new verdict in ``deploy_check`` is a KeyError here
#: (a test pins the set) rather than a silent guess.
_RESTARTING_BECAUSE = {
    deploy_check.NOT_RESTARTED: "restarting: the daemon serves older code than the branch tip",
    deploy_check.CANNOT_TELL: (
        "restarting: deploy_check cannot tell what the daemon serves, and a "
        "restart is the safe side of not knowing"
    ),
    deploy_check.DIRTY: (
        "restarting: the served content is no commit's (dirt at boot, or "
        "dirt now). A restart clears dirt the daemon booted with; dirt that "
        "is still in the checkout keeps the 'deployed' check red until it is "
        "committed or set aside"
    ),
}


#: The PTY host's code: a change here is what a restart that keeps the host
#: would leave unserved, so it restarts with ``--with-pty-host`` instead.
HOST_CODE = ("src/claude_launcher/daemon/ptyhost/", "src/claude_launcher/daemon/pty_backend.py")


def host_code_changed(repo: Path, branch: str, daemon_json: Optional[Path]) -> Optional[list]:
    """The :data:`HOST_CODE` files that differ between what the live daemon
    serves and ``<branch>`` -- or None when that cannot be told (no recorded
    HEAD, git refusing). An empty list means the host's code is the same."""
    try:
        _path, doc = deploy_check._daemon_doc(daemon_json)
        code = doc.get("code")
        head = code.get("head") if isinstance(code, dict) else None
        if not head:
            return None
        out = deploy_check._git(repo, "diff", "--name-only", head, branch, "--", *HOST_CODE)
    except Exception:  # noqa: BLE001 - best effort, as _unserved
        return None
    return [line for line in out.splitlines() if line.strip()]


#: How many unserved commits the reason lists by name before it counts the rest.
UNSERVED_SHOWN = 10


def _unserved(repo: Path, branch: str, daemon_json: Optional[Path]) -> list:
    """``<branch>``'s first-parent commits the live daemon does not serve yet,
    one ``<short sha> <subject>`` line each, newest first. Best effort: any
    failure to tell (no daemon.json, no recorded HEAD, git refusing) is an
    empty list, because the reason is for a person and must not stop the
    restart."""
    try:
        _path, doc = deploy_check._daemon_doc(daemon_json)
        code = doc.get("code")
        head = code.get("head") if isinstance(code, dict) else None
        if not head:
            return []
        out = deploy_check._git(
            repo, "log", "--first-parent", "--format=%h %s", f"{head}..{branch}"
        )
    except Exception:  # noqa: BLE001 - see the docstring
        return []
    return [line for line in out.splitlines() if line.strip()]


def restart_reason(verdict: int, unserved: list, host_changed: Optional[list] = None) -> str:
    """What the approval card shows: why this restart is needed, and what
    it does with the PTY host (``host_changed``: :func:`host_code_changed`)."""
    lines = [_RESTARTING_BECAUSE[verdict]]
    if host_changed:
        lines.append(
            f"with the PTY host: its code changed ({', '.join(host_changed[:5])}"
            + (", ..." if len(host_changed) > 5 else "")
            + "), so the sessions end and are relaunched on a new host"
        )
    elif host_changed is None:
        lines.append("keeping the PTY host (whether its code changed cannot be told)")
    if unserved:
        lines.append(f"commits the live daemon does not serve yet ({len(unserved)}):")
        lines += [f"  {line}" for line in unserved[:UNSERVED_SHOWN]]
        if len(unserved) > UNSERVED_SHOWN:
            lines.append(f"  ... and {len(unserved) - UNSERVED_SHOWN} more")
    return "\n".join(lines)


def _restart_command(reason: str = "", *, with_pty_host: bool = False) -> list:
    exe = shutil.which("claunch")
    if exe is None:
        raise LookupError("claunch is not on PATH; nothing can restart the daemon")
    command = [exe, "daemon", "restart"]
    if with_pty_host:
        command.append("--with-pty-host")
    if reason:
        command += ["--reason", reason]
    return command


def main(argv: Optional[list] = None, *, run=subprocess.call) -> int:
    ap = argparse.ArgumentParser(
        prog="restart_live",
        description="restart the live daemon if it is serving older code than <branch>",
    )
    ap.add_argument("--repo", type=Path, default=Path("."))
    ap.add_argument("--branch", default="master")
    ap.add_argument("--daemon-json", type=Path, default=None)
    ap.add_argument("--allow-dirty", default=None, metavar="PATHS|sha1:HEX")
    ap.add_argument(
        "--dry-run", action="store_true",
        help="say what would happen and exit 0 without restarting anything",
    )
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
    host_changed = host_code_changed(args.repo, args.branch, args.daemon_json)
    reason = restart_reason(
        verdict, _unserved(args.repo, args.branch, args.daemon_json), host_changed,
    )
    print(reason, file=sys.stderr)
    with_host = bool(host_changed)
    if args.dry_run:
        print("dry run: would run 'claunch daemon restart"
              + (" --with-pty-host" if with_host else "") + "' now; not doing it")
        return 0

    try:
        command = _restart_command(reason, with_pty_host=with_host)
    except LookupError as exc:
        print(f"cannot restart: {exc}", file=sys.stderr)
        return 1
    print(f"running: {' '.join(command[:3])} (reason above)", flush=True)
    return int(run(command))


if __name__ == "__main__":
    raise SystemExit(main())
