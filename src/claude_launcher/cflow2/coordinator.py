"""Read-only evidence checks for improv-coordinator-v2.

The board remains authoritative. These probes do not close issues, assign
sessions, or turn an unavailable board into a successful completion.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

from .. import cli_beads


class CheckError(ValueError):
    pass


def board_root(cwd: str) -> str:
    ref = cli_beads.resolve(cwd)
    if ref is None or not ref.db_path.is_file():
        raise CheckError("no existing beads database for this repository")
    return str(ref.root_path.resolve())


def _run(argv: list[str], cwd: str) -> str:
    try:
        result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                                encoding="utf-8", timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CheckError(f"cannot read evidence: {exc}") from exc
    if result.returncode:
        raise CheckError(f"evidence command failed ({result.returncode}): "
                         f"{result.stderr.strip() or result.stdout.strip()}")
    return result.stdout


def read_issue(board: str, issue: str) -> dict[str, Any]:
    path = Path(board)
    if not path.is_absolute() or not path.is_dir() or str(path.resolve()) != board:
        raise CheckError("board must be an existing, canonical absolute directory")
    if board_root(board) != board:
        raise CheckError("board must name the actual board root, not a fallback or worktree")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", issue):
        raise CheckError("invalid issue id")
    try:
        rows = json.loads(_run(["claunch", "beads", "show", issue, "--json"], board))
    except json.JSONDecodeError as exc:
        raise CheckError("board did not return JSON") from exc
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict) \
            or rows[0].get("id") != issue:
        raise CheckError("board did not return exactly the requested issue")
    return rows[0]


def check_issue(board: str, issue: str, *, exists: bool = False,
                key: str | None = None) -> None:
    if key is not None and key != f"{board}#{issue}":
        raise CheckError("key must be <canonical board directory>#<issue id>")
    row = read_issue(board, issue)
    if not exists and row.get("status") != "closed":
        raise CheckError(f"{issue} is {row.get('status')!r}, expected closed")


def check_direct(cwd: str, board: str, *, ready: bool = False) -> None:
    # A coordinator has one checkout. It cannot serialize writes in an
    # unrelated repository using this run's slot.
    if not board:
        raise CheckError("direct work requires the member's board")
    own = _run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd).strip()
    target = _run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], board).strip()
    if Path(own).resolve() != Path(target).resolve():
        raise CheckError("direct work must use the coordinator's repository")
    branch = _run(["git", "symbolic-ref", "--short", "HEAD"], cwd).strip()
    if not branch or (not ready and branch in ("master", "main")):
        raise CheckError("direct work requires a feature branch")
    if _run(["git", "status", "--porcelain"], cwd).strip():
        raise CheckError("direct work has uncommitted changes")
    _run(["git", "rev-parse", "--verify", "HEAD"], cwd)


def check_settled(state: dict[str, Any]) -> None:
    cols = state.get("collections") or {}
    if "jobs" not in cols or "direct" not in cols:
        raise CheckError("not a coordinator run: missing collections")
    if (cols["direct"].get("items") or []):
        raise CheckError("direct work still owns the checkout")
    for row in cols["jobs"].get("items") or []:
        status = row.get("status")
        if status == "done":
            check_issue(row["board"], row["issue"], key=row["key"])
        elif status == "deferred":
            # Deferral is reached through the member's user decision. Keep
            # the issue on the board; never close it as part of settlement.
            check_issue(row["board"], row["issue"], exists=True, key=row["key"])
        else:
            raise CheckError(f"{row.get('key')} is not settled ({status!r})")


def command(args: Any) -> int:
    try:
        if args.parent:
            try:
                inputs = json.loads(os.environ.get("CFLOW_DATA_INPUTS", "{}"))
            except json.JSONDecodeError as exc:
                raise CheckError("invalid run inputs") from exc
            if not isinstance(inputs, dict):
                raise CheckError("invalid run inputs")
            check_issue(board_root(os.getcwd()), inputs.get("issue", ""), exists=True)
        elif args.direct_clean or args.direct_ready:
            check_direct(os.getcwd(), os.environ.get("CFLOW_IN_BOARD", ""), ready=args.direct_ready)
        elif args.settled:
            from .store import RunStore, StoreError

            scope = os.environ.get("CFLOW2_SCOPE")
            run = os.environ.get("CFLOW2_RUN")
            if not scope or not run:
                raise CheckError("settlement check requires a cflow verify environment")
            try:
                state = RunStore(os.getcwd(), scope).load_state()
            except StoreError as exc:
                raise CheckError(str(exc)) from exc
            if state.get("run_id") != run:
                raise CheckError("coordinator run changed")
            check_settled(state)
        else:
            check_issue(args.board or "", args.issue or "", exists=args.exists, key=args.key)
    except (CheckError, KeyError, TypeError) as exc:
        print(f"coordinator check: {exc}", file=sys.stderr)
        return 1
    print("coordinator check: passed")
    return 0
