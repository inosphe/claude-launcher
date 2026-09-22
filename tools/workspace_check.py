"""Is this session standing in the repository its round's work belongs to?

A worker round decides its goal in ``intake`` and cuts its branch in
``branch-setup``. Nothing between the two asks *where* the branch is about to
be cut, and the answer is not always the right one: this machine has six
registered workspaces and dozens of linked worktrees under one of them, a
session's working directory is fixed by whoever spawned it, and a round whose
issue lives in one repository can be driven from another without anything
saying so. What it costs is not the branch -- that is cheap to throw away --
but the round: the work lands in a tree nobody was reviewing, and the mismatch
is usually noticed at the integration request, several steps too late.

So this is the measurement behind ``improv-worker``'s ``workspace-check``
gate. The exit code is the verdict and the gate is placed before the branch is
cut, so a wrong location is answered by a person rather than by a merge.

    uv run --no-sync python tools/workspace_check.py
    uv run --no-sync python tools/workspace_check.py --issue claunch-abcde
    uv run --no-sync python tools/workspace_check.py --session s704 --cwd .

Exit codes, in the shape the other gate tools use:

    0  every axis that could be measured agrees -- this is the right place
    1  at least one measurable axis disagrees -- the location is wrong
    2  nothing could be measured (no daemon, no issue, not a repository)

One and two are kept apart for the reason ``keepalive_check.py`` keeps them
apart: "I looked and it is false" and "I could not look" are fixed by
different actions. Both stop the gate, and the workflow sends both to the same
human approval, but the person reading the output needs to know which one they
are answering.

Three axes, measured independently and reported separately. A round is not
required to have all three -- an issueless round has no board record to
compare against, and a round with no daemon has no session record -- so each
axis reports "not measurable" rather than a verdict, and the exit code is 2
only when *none* of them produced one.

1. **The board's repository.** The issue names a workspace in its description
   frontmatter and carries the path of the board that minted it
   (``source_repo_path``). Either one is the repository the round belongs to,
   and the directory this tool runs in has to be inside it.

2. **The session's registered directory.** The daemon records the directory a
   session was spawned in. The work may legitimately move *within* that
   repository -- ``branch-setup`` creates a worktree and moves there -- so the
   comparison is between repositories, not between directories. A session that
   has wandered into another checkout fails here even when it has no issue at
   all, which is why this axis is worth having beside the first.

3. **Occupancy of a linked worktree.** Two live sessions in one linked worktree
   share a working tree and a checked-out branch: the second one's
   ``git switch -c`` moves the first one's files under it. The main checkout is
   deliberately excluded -- sessions share it all the time, and
   ``branch-setup`` resolves that case itself by cutting a worktree.

Everything here reaches the daemon through :mod:`claude_launcher.daemon_client`
and git through ``subprocess``. Nothing else is imported, and that is a
constraint rather than an accident: a gate runs under ``uv run --no-sync``,
which never populates a worktree's ``.venv``, so ``yaml`` is not importable and
neither is anything that imports it. That rules out the three helpers this
file would otherwise have used -- ``cli_beads.repo_root``,
``beads_meta.workspace_of`` and ``workspaces.get`` all reach ``store``, which
imports ``yaml`` at module level. The daemon has already done that resolution
on its side: ``GET /api/beads/<id>`` answers with the issue row, the workspace
name it declares, whether that name is registered, and the registered
workspaces themselves. Asking the daemon is therefore not a workaround, it is
the shorter path -- see ``tests/test_gates_run_this_checkout.py`` for the rule
this obeys and the three shipped gates that broke before it existed.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

# Same as the other gate tools: run from a checkout, against that checkout.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from claude_launcher import daemon_client  # noqa: E402

#: Windows long-path prefix. ``br`` records ``source_repo_path`` through it
#: (measured: ``\\?\F:\works\claude-launcher``), and the same directory read
#: through git or the daemon has no prefix, so the two never compare equal
#: until it is taken off.
LONG_PATH_PREFIX = "\\\\?\\"


def _norm(path: str) -> str:
    """A path in the one spelling two sources can be compared in.

    ``normcase`` is what makes this correct on Windows and a no-op elsewhere;
    ``realpath`` collapses the junction and drive-substitution that a worktree
    path can carry. A path that cannot be resolved is returned normalised
    anyway rather than raising -- a comparison against a directory that no
    longer exists is a real answer, and an exception here would be reported as
    "could not measure".
    """
    if not path:
        return ""
    if path.startswith(LONG_PATH_PREFIX):
        path = path[len(LONG_PATH_PREFIX):]
    try:
        return os.path.normcase(os.path.realpath(path))
    except OSError:
        return os.path.normcase(path)


def _oneline(exc: Exception, limit: int = 160) -> str:
    """An exception as one short line.

    The daemon relays ``br``'s own failure text, which is a JSON object over
    eight lines. An axis is a line in a gate's output and a line in the
    approval a person reads; eight lines of a nested error body inside one of
    them buries the other two axes.
    """
    text = " ".join(str(exc).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _git(cwd: str, *args: str) -> str:
    """One git answer, or ``""`` when git cannot answer it."""
    try:
        proc = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True, text=True, check=False, encoding="utf-8",
            errors="replace",
        )
    except OSError:
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def repo_of(path: str) -> str:
    """The repository that owns ``path``, normalised, or ``""``.

    ``--git-common-dir`` is the one git answer that is the same from the main
    checkout and from every worktree cut from it, which is exactly the property
    this comparison needs -- ``cli_beads.repo_root`` picks it for the same
    reason, about the same question.
    """
    common = _git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if not common:
        return ""
    return _norm(str(Path(common).parent))


def is_linked_worktree(path: str) -> bool:
    """Whether ``path`` sits in a linked worktree rather than the main checkout."""
    git_dir = _git(path, "rev-parse", "--path-format=absolute", "--git-dir")
    common = _git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if not git_dir or not common:
        return False
    return _norm(git_dir) != _norm(common)


class Axis:
    """One comparison, its verdict, and the line a person reads.

    ``ok`` is True, False, or None for "not measurable" -- the same three
    values a cflow checklist item carries, and for the same reason: a check
    that could not run has not said "no", and collapsing the two loses the
    only information that tells a reader what to do next.
    """

    def __init__(self, name: str, ok, detail: str) -> None:
        self.name = name
        self.ok = ok
        self.detail = detail

    def line(self) -> str:
        mark = {True: "ok  ", False: "FAIL", None: "?   "}[self.ok]
        return f"  [{mark}] {self.name}: {self.detail}"


def issue_axis(client, issue_id: str, here_repo: str):
    """Axis 1 -- the repository the issue's board and workspace name point at.

    Two sources, and they are asked for together because ``GET
    /api/beads/<id>`` answers with both: the workspace the description
    declares (resolved against the registered workspaces on the daemon's side)
    and the path of the board that minted the issue. They normally agree. When
    they do not, this axis does not pick a winner: an issue whose own record
    contradicts itself is a thing for a person to look at, so it reports "not
    measurable" with both values printed.
    """
    if not issue_id:
        return Axis("issue", None, "no issue id -- this round has no board record to compare against")
    try:
        payload = client.get(f"/api/beads/{issue_id}")
    except daemon_client.DaemonClientError as exc:
        return Axis("issue", None, f"cannot read {issue_id}: {_oneline(exc)}")
    if not isinstance(payload, dict):
        return Axis("issue", None, f"cannot read {issue_id}: the daemon answered {type(payload).__name__}")

    wanted = {}
    name = str(payload.get("workspace") or "")
    if name and payload.get("workspace_known"):
        for row in payload.get("workspaces") or []:
            if isinstance(row, dict) and row.get("name") == name and row.get("path"):
                wanted[f"workspace {name!r}"] = _norm(str(row["path"]))
    issue = payload.get("issue") if isinstance(payload.get("issue"), dict) else {}
    board = str((issue or {}).get("source_repo_path") or "")
    if board:
        wanted["the board that minted it"] = _norm(board)

    if not wanted:
        return Axis(
            "issue",
            None,
            f"{issue_id} names no workspace this machine has registered and no "
            f"source repository -- nothing to compare against",
        )
    if len(set(wanted.values())) > 1:
        pairs = "; ".join(f"{k} -> {v}" for k, v in wanted.items())
        return Axis("issue", None, f"{issue_id} contradicts itself ({pairs}) -- a person has to read it")

    want = next(iter(wanted.values()))
    where = ", ".join(wanted)
    if not here_repo:
        return Axis("issue", None, f"{issue_id} belongs to {want} ({where}), but this directory is in no repository")
    if here_repo == want:
        return Axis("issue", True, f"{issue_id} belongs to {want} ({where}), and that is this repository")
    return Axis(
        "issue",
        False,
        f"{issue_id} belongs to {want} ({where}), but this directory is in {here_repo}",
    )


def session_axis(client, session: str, here_repo: str):
    """Axis 2 -- the repository the daemon spawned this session into.

    Repositories, not directories: ``branch-setup`` moves a session into a
    worktree it cuts itself, and on a second round the session is standing
    there rather than where it was spawned. That move is the workflow working
    as written, so comparing directories would fail every round after the
    first. Leaving the repository is the thing this axis is for.
    """
    if not session:
        return Axis("session", None, "no session name -- pass --session or run this inside the session")
    try:
        info = client.get(f"/api/sessions/{session}")
    except daemon_client.DaemonClientError as exc:
        return Axis("session", None, f"cannot read the record for {session!r}: {_oneline(exc)}")
    registered = str((info or {}).get("cwd") or "") if isinstance(info, dict) else ""
    if not registered:
        return Axis("session", None, f"the record for {session!r} carries no working directory")
    want = repo_of(registered)
    if not want:
        return Axis("session", None, f"{session} is registered in {_norm(registered)}, which is in no repository")
    if not here_repo:
        return Axis("session", None, f"{session} was spawned into {want}, but this directory is in no repository")
    if here_repo == want:
        return Axis("session", True, f"{session} was spawned into {want}, and that is this repository")
    return Axis("session", False, f"{session} was spawned into {want}, but this directory is in {here_repo}")


def occupancy_axis(client, session: str, here: str):
    """Axis 3 -- is another live session standing in this linked worktree?

    Only linked worktrees. Sessions share the main checkout routinely and
    ``branch-setup`` is written for exactly that case (it cuts a worktree when
    the checkout is shared), so flagging it here would stop every round that
    the workflow already handles. A linked worktree is different: it was cut
    for one session, it holds one checked-out branch, and a second session's
    ``git switch -c`` moves the first one's working tree under it.
    """
    if not is_linked_worktree(here):
        return Axis("occupancy", None, "this is the main checkout, not a linked worktree -- branch-setup handles sharing")
    try:
        payload = client.get("/api/sessions")
    except daemon_client.DaemonClientError as exc:
        return Axis("occupancy", None, f"cannot list sessions: {_oneline(exc)}")
    rows = payload.get("sessions") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return Axis("occupancy", None, "the daemon's session list is not a list")
    mine = _norm(here)
    others = [
        str(r.get("name"))
        for r in rows
        if isinstance(r, dict)
        and r.get("name") != session
        and r.get("status") != "exited"
        and _norm(str(r.get("cwd") or "")) == mine
    ]
    if others:
        return Axis(
            "occupancy",
            False,
            f"{len(others)} other live session(s) stand in this worktree ({', '.join(sorted(others))}) "
            f"-- one working tree, one checked-out branch, two writers",
        )
    return Axis("occupancy", True, "no other live session stands in this linked worktree")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--session", default=os.environ.get("CLAUNCH_SESSION", ""),
                    help="the session to check (default: $CLAUNCH_SESSION)")
    ap.add_argument("--issue", default="",
                    help="the round's issue id (default: the one on the session's record)")
    ap.add_argument("--cwd", default="",
                    help="the directory to judge (default: the working directory)")
    args = ap.parse_args(argv[1:])

    here = os.path.abspath(args.cwd or os.getcwd())
    here_repo = repo_of(here)

    # connect(), not ensure_running(): a check must not start a daemon.
    client = daemon_client.connect()
    if client is None:
        print(f"standing in: {_norm(here)}")
        print("  [?   ] all axes: the daemon is not reachable, so neither the issue nor "
              "the session record can be read")
        print("cannot tell: nothing could be measured. Start the daemon and run this again.")
        return 2

    issue_id = args.issue
    if not issue_id and args.session:
        try:
            info = client.get(f"/api/sessions/{args.session}")
            if isinstance(info, dict):
                issue_id = str(info.get("issue") or "")
        except daemon_client.DaemonClientError:
            issue_id = ""

    axes = [
        issue_axis(client, issue_id, here_repo),
        session_axis(client, args.session, here_repo),
        occupancy_axis(client, args.session, here),
    ]

    print(f"standing in: {_norm(here)}")
    print(f"repository:  {here_repo or '(none -- git does not claim this directory)'}")
    for axis in axes:
        print(axis.line())

    failed = [a for a in axes if a.ok is False]
    if failed:
        print(
            "MISMATCH: this is not the place this round's work belongs to "
            f"({', '.join(a.name for a in failed)}). Cutting a branch here puts the "
            "work in a tree nobody is reviewing."
        )
        return 1
    if not any(a.ok is True for a in axes):
        print(
            "cannot tell: no axis produced a verdict. The location is unjudged, "
            "which is not the same as judged correct."
        )
        return 2
    print("MATCH: every axis that could be measured says this is the right place.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
