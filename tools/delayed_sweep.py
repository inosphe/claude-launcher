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


def merge_sha_of(repo, item):
    """Full SHA of a reported merge that is in master and has the requested tip as second parent."""
    sha = item["merge_sha"]
    check_commit(repo, sha)
    parents = git(repo, "show", "-s", "--format=%P", sha).split()
    if len(parents) != 2 or parents[1] != item["tip"]:
        raise ValueError("merge second parent does not match the requested tip")
    return sha


def find_job(queue, repo, sha):
    matches = [j for j in queue.status(repo)["jobs"] if j["sha"] == sha]
    if not matches or matches[0]["status"] == "pending":
        raise ValueError(f"merge {sha} has no queued checkpoint")
    return matches[0]


def load_record(args):
    """The reported record from --env NAME, --json TEXT or --file PATH."""
    if args.json is not None:
        return json.loads(args.json)
    if args.file is not None:
        return json.loads(args.file.read_text())
    return json.loads(os.environ[args.env])


def record_items(value):
    repo = Path(value["repo"])
    items = value["merges"]
    if not repo.is_absolute() or not items:
        raise ValueError("batch requires an absolute repo and nonempty items")
    return repo, items


def classify(queue, value):
    """Sort a reported merges record into ok / missing / mismatch / conflict / invalid.

    ok        every row names its merge's one job (the gate would pass)
    missing   a merge has no queued job or its fixed ref is gone: enqueue the same SHA again
    mismatch  only the report is wrong: several rows share one merge, or a row names a
              different issue than the merge's job while the job's own issue is in the report;
              ``suggested`` regroups the rows into one row per merge, keeping job.issue
    conflict  a row names an issue that is not the job's and the job's issue is not reported
    invalid   not a merge of its tip into master, or the fixed ref moved: nothing to repair here
    Never writes: no job, ref or label is created or changed."""
    repo, items = record_items(value)
    rows, groups = [], {}
    for item in items:
        row = {"issue": item.get("issue"), "merge_sha": item.get("merge_sha"), "tip": item.get("tip")}
        rows.append(row)
        try:
            sha = merge_sha_of(repo, item)
        except (ValueError, KeyError, subprocess.CalledProcessError) as exc:
            row.update(kind="invalid", problem=str(exc))
            continue
        try:
            job = find_job(queue, repo, sha)
        except ValueError as exc:
            row.update(kind="missing", problem=str(exc))
            continue
        row["job_issue"] = job["issue"]
        try:
            ref_sha = git(repo, "rev-parse", job["ref"])
        except subprocess.CalledProcessError:
            row.update(kind="missing", problem=f"fixed ref {job['ref']} is gone")
            continue
        if ref_sha != sha:
            row.update(kind="invalid", problem=f"fixed ref {job['ref']} moved")
            continue
        groups.setdefault(sha, []).append(row)
        row["kind"] = "ok" if job["issue"] == item["issue"] else "mismatch"
    for sha, group in groups.items():
        if any(r["kind"] == "ok" for r in group):
            for r in group:
                if r["kind"] == "mismatch":
                    r["problem"] = f"merge {sha} is already reported as {group[0]['job_issue']}"
        else:
            for r in group:
                r["kind"] = "conflict"
                r["problem"] = f"merge {sha} belongs to {r['job_issue']}, which this report does not name"
    kinds = {r["kind"] for r in rows}
    for kind in ("invalid", "missing", "conflict", "mismatch"):
        if kind in kinds:
            break
    else:
        kind = "ok"
    result = {"class": kind, "rows": rows}
    if kind == "mismatch":
        merged = []
        for sha, group in groups.items():
            first = next(r for r in group if r["kind"] == "ok")
            src = [i for i in items if i["merge_sha"] == sha]
            also = [a for i in src for a in i.get("also", [])]
            also += [r["issue"] for r in group if r["kind"] != "ok"]
            also = [a for n, a in enumerate(also) if a != first["issue"] and a not in also[:n]]
            row = {"issue": first["issue"], "tip": first["tip"], "merge_sha": sha}
            if also:
                row["also"] = also
            merged.append(row)
        result["suggested"] = {**{k: v for k, v in value.items() if k != "merges"}, "merges": merged}
    return result


def check_merges(queue, value):
    """Advisory pre-report shape check of a merges record; reads only."""
    repo, items = record_items(value)
    if "master" in value:
        check_commit(repo, value["master"])
    seen = {}
    for item in items:
        issue, sha = item["issue"], merge_sha_of(repo, item)
        if sha in seen:
            raise ValueError(f"merge {sha} is reported twice ({seen[sha]}, {issue}): one row per "
                             "merge, other issues of the same branch and tip go in also")
        seen[sha] = issue
        also = item.get("also", [])
        if not isinstance(also, list) or not all(isinstance(a, str) for a in also) or issue in also:
            raise ValueError("also must list other issue ids of the same merge")
        try:
            job = find_job(queue, repo, sha)
        except ValueError:
            continue  # not enqueued yet: the gate owns that
        if job["issue"] != issue:
            raise ValueError(f"merge {sha} already belongs to {job['issue']}: report that issue")
    return {"checked": len(items)}


VERDICTS = ("passed", "failed", "stuck", "cancelled")


def job_state(queue, ident):
    """A job's own verdict state; a covered merge reports its batch tip's."""
    row = queue.get(ident)
    if row["status"] == "covered":
        tip = queue.get(row["covered_by"])
        return {"job": ident, "status": tip["status"], "tip": tip["id"], "done": tip["status"] in VERDICTS}
    return {"job": ident, "status": row["status"], "tip": ident, "done": row["status"] in VERDICTS}


def window_entries(sha, ref):
    """(holders, queue) entries of the daemon's test window that name this job, or None."""
    try:
        from claude_launcher import daemon_client
        client, _ = daemon_client.connect_with_diagnosis()
        if client is None:
            return None
        status = client.get("/api/window")
    except Exception:
        return None
    def mine(entry):
        label = str(entry.get("label") or "")
        return sha in label or ref in label
    holders = [e for e in status.get("holders") or [] if mine(e)]
    waiting = [i for i, e in enumerate(status.get("queue") or [], 1) if mine(e)]
    return holders, waiting, len(status.get("holders") or []), len(status.get("queue") or [])


def nudge_text(queue, ident):
    """The reminder a sweep-watching session reads on every wake-up.

    A session whose sweep holds the test window is told to leave it alone; one
    still waiting for the window is told it has nothing to do but stay. Both are
    told the exit conditions, because an idle session is held (ended) by the
    daemon and only a message or a workflow notice brings it back."""
    state = job_state(queue, ident)
    row = queue.get(state["tip"])
    head = f"sweep job {row['id']} ({row['sha'][:12]}, {1 + len(queue.members(row['id']))} merge(s))"
    if state["done"]:
        return (f"DONE: {head} is {state['status']}. Read tools/delayed_sweep.py status for the "
                "receipt and report it; there is nothing left to wait for.")
    window = window_entries(row["sha"], row["ref"])
    if row["status"] == "running" and window and window[0]:
        return (f"TEST WINDOW HELD: {head} is running and holds the sweep window. Do not kill, "
                "restart or re-run it, and do not start any other test: a second sweep invalidates "
                "both. The result arrives through the queue. Wait for the next wake-up.")
    if window and window[1]:
        return (f"WAITING FOR WINDOW: {head} is number {window[1][0]} of {window[3]} in the test "
                "window queue (sweeps rank below unit tests, so the wait can be long). "
                "Nothing is wrong and nothing needs doing. Stay in the workflow's wait.")
    return (f"QUEUED: {head} is {row['status']} and not yet in the window queue; the daemon starts "
            "it when the window is idle. Nothing needs doing. Stay in the workflow's wait.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path)
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("enqueue", "admit", "repair", "release", "status"):
        p = sub.add_parser(action)
        p.add_argument("--repo", type=Path, required=True)
        if action in ("enqueue", "admit", "repair"):
            p.add_argument("--issue", required=True)
        if action == "enqueue":
            p.add_argument("--merge", required=True)
            p.add_argument("--owner", default=os.environ.get("CLAUNCH_SESSION"))
            p.add_argument("--command")
        if action in ("repair", "release"):
            p.add_argument("--job", required=True)
        if action == "release":
            p.add_argument("--reason", required=True)
    p = sub.add_parser("cancel", help="drop a job that will never reach a verdict")
    p.add_argument("--job", required=True)
    p.add_argument("--reason", required=True)
    p = sub.add_parser("requeue", help="give a stuck or cancelled job fresh attempts")
    p.add_argument("--job", required=True)
    p = sub.add_parser("remeasure", help="measure the active pause job's SHA again (leader action, once)")
    p.add_argument("--repo", type=Path, required=True)
    p.add_argument("--job", required=True)
    p.add_argument("--reason", required=True)
    p = sub.add_parser("check-batch")
    p.add_argument("--env", required=True)
    for action in ("check-enqueued", "check-merges"):
        p = sub.add_parser(action)
        source = p.add_mutually_exclusive_group(required=True)
        source.add_argument("--env")
        source.add_argument("--json")
        source.add_argument("--file", type=Path)
        if action == "check-enqueued":
            p.add_argument("--classify", action="store_true",
                           help="print the class (ok/missing/mismatch/conflict/invalid) and exit 2 unless ok")
    p = sub.add_parser("unbatch", help="release the merges a failed batch tip covered so each is swept alone")
    p.add_argument("--job", required=True)
    p = sub.add_parser("job-state", help="exit 0 once the job (or its batch tip) has a verdict, 1 while it waits")
    p.add_argument("--job", required=True)
    p = sub.add_parser("nudge", help="print the reminder for a session watching this sweep job")
    p.add_argument("--job", required=True)
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
        elif args.action == "release":
            queue.release(args.repo, args.job, args.reason)
            result = {"released": args.job}
        elif args.action == "cancel":
            queue.cancel(args.job, args.reason)
            result = queue.get(args.job)
        elif args.action == "requeue":
            queue.requeue(args.job)
            result = queue.get(args.job)
        elif args.action == "unbatch":
            result = queue.unbatch(args.job)
        elif args.action == "job-state":
            result = job_state(queue, args.job)
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result["done"] else 1
        elif args.action == "nudge":
            print(nudge_text(queue, args.job))
            return 0
        elif args.action == "remeasure":
            queue.remeasure(args.repo, args.job, args.reason)
            result = queue.get(args.job)
        elif args.action == "status":
            result = queue.status(args.repo)
        elif args.action == "check-merges":
            result = check_merges(queue, load_record(args))
        elif args.action == "check-enqueued" and args.classify:
            result = classify(queue, load_record(args))
            if result["class"] != "ok":
                print(json.dumps(result, ensure_ascii=False))
                return 2
        elif args.action in ("check-batch", "check-enqueued"):
            value = json.loads(os.environ[args.env]) if args.action == "check-batch" else load_record(args)
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
                    sha = merge_sha_of(repo, item)
                    job = find_job(queue, repo, sha)
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
