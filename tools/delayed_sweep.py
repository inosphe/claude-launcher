"""Record, inspect and execute immutable post-merge verification jobs."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from claude_launcher.delayed_sweep import Queue, git, run_one


def check_commit(repo, sha):
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("checkpoint requires a full commit SHA")
    if git(repo, "rev-parse", f"{sha}^{{commit}}") != sha:
        raise ValueError("checkpoint SHA does not name a commit")
    if not Queue.contains(repo, sha, "refs/heads/master"):
        raise ValueError("checkpoint commit is not contained in master")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path)
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("enqueue", "admit", "repair", "status"):
        p = sub.add_parser(action)
        p.add_argument("--repo", type=Path, required=True)
        if action in ("enqueue", "admit", "repair"):
            p.add_argument("--issue", required=True)
        if action == "enqueue":
            p.add_argument("--merge", required=True)
            p.add_argument("--owner", default=os.environ.get("CLAUNCH_SESSION"))
            p.add_argument("--command")
        if action == "repair":
            p.add_argument("--job", required=True)
    for action in ("check-batch", "check-enqueued"):
        p = sub.add_parser(action)
        p.add_argument("--env", required=True)
    p = sub.add_parser("run-one")
    p.add_argument("--job", required=True)
    p.add_argument("--scratch", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        queue = Queue(args.database)
        if args.action == "enqueue":
            result = queue.enqueue(args.repo, args.merge, args.issue, args.owner or "",
                                   Path(__file__).with_name("sweep.py"), args.command)
        elif args.action == "admit":
            queue.admit(args.repo, args.issue)
            result = {"admitted": args.issue}
        elif args.action == "repair":
            queue.repair(args.repo, args.job, args.issue)
            result = {"repair_issue": args.issue, "job": args.job}
        elif args.action == "status":
            result = queue.status(args.repo)
        elif args.action in ("check-batch", "check-enqueued"):
            value = json.loads(os.environ[args.env])
            repo = Path(value["repo"])
            items = value["batch"] if args.action == "check-batch" else value["merges"]
            if not repo.is_absolute() or not items:
                raise ValueError("batch requires an absolute repo and nonempty items")
            if args.action == "check-enqueued" and "master" in value:
                check_commit(repo, value["master"])
            for item in items:
                if args.action == "check-batch":
                    queue.admit(repo, item["issue"])
                else:
                    sha = item["merge_sha"]
                    check_commit(repo, sha)
                    parents = git(repo, "show", "-s", "--format=%P", sha).split()
                    if len(parents) != 2 or parents[1] != item["tip"]:
                        raise ValueError("merge second parent does not match the requested tip")
                    matches = [j for j in queue.status(repo)["jobs"] if j["sha"] == sha]
                    if not matches or matches[0]["status"] == "pending":
                        raise ValueError(f"merge {sha} has no queued checkpoint")
                    job = matches[0]
                    if job["issue"] != item["issue"] or git(repo, "rev-parse", job["ref"]) != sha:
                        raise ValueError("verification checkpoint does not match the merge")
            result = {"checked": len(items)}
        else:
            run_one(queue, args.job, args.scratch)
            result = queue.get(args.job)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as exc:
        print(f"delayed sweep: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
