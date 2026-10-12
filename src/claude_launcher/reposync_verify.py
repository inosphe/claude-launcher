"""Read-only cflow evidence checks for isolated repository sync candidates."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

from . import reposync


def candidate(cwd: str, assignment: dict) -> dict:
    repo = assignment["repo"]
    common = lambda path: os.path.realpath(reposync.git(
        path, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    if common(cwd) != common(repo):
        raise reposync.SyncError("candidate belongs to another repository")
    if os.path.realpath(cwd) == os.path.realpath(repo):
        raise reposync.SyncError("candidate must use an isolated worktree")
    if reposync.git(cwd, "status", "--porcelain"):
        raise reposync.SyncError("candidate worktree is dirty")
    for state in ("rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD"):
        path = reposync.git(cwd, "rev-parse", "--git-path", state)
        if (Path(cwd) / path).exists():
            raise reposync.SyncError("unfinished git operation: " + state)
    head = reposync.git(cwd, "rev-parse", "HEAD")
    branch = assignment.get("source") or assignment["branch"]
    current = reposync.git(repo, "rev-parse", "--verify", "refs/heads/" + branch)
    if current != assignment["local"]:
        raise reposync.SyncError("original source changed; replan before integration")
    return {"head": head, "tree": reposync.git(cwd, "rev-parse", "HEAD^{tree}"),
            "original": current}


def main() -> int:
    try:
        inputs = json.loads(os.environ["CFLOW_DATA_INPUTS"])
        assignment = json.loads(inputs["assignment"])
        if sys.argv[1:] != ["candidate"]:
            raise ValueError("expected candidate")
        result = candidate(os.getcwd(), assignment)
        prepared = json.loads(os.environ.get("CFLOW_DATA_PREPARED", "{}"))
        if prepared:
            value = prepared
            if (value.get("candidate"), value.get("tree")) != (result["head"], result["tree"]):
                raise reposync.SyncError("prepared candidate changed; replan and review")
        print(json.dumps(result))
        return 0
    except (KeyError, ValueError, reposync.SyncError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
