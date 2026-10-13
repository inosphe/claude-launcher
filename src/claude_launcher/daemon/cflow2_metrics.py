"""Bounded saved-run observations for the v2 page's throughput chart.

No workflow names are special. Merge observations require the task's IR
contract (git.merge effect and a merges:list return) and structured reports.
Their time is the report time, not the git commit time. Retention before the
first saved interval and gaps between intervals remain unknown.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from ..cflow2 import store

RUN_LIMIT = 64
EVENT_LIMIT = 2048
BYTE_LIMIT = 256 * 1024
SHA = re.compile(r"[0-9a-fA-F]{40}\Z")


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return date.timestamp() if date.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def observations(st: store.RunStore, *, now: str | None = None) -> dict:
    now = now or datetime.now(timezone.utc).isoformat(timespec="seconds")
    until = timestamp(now)
    paths = st.archived()
    sources = [st]
    for path in reversed(paths[-(RUN_LIMIT - 1):]):
        old = store.RunStore(str(st.cwd), st.scope)
        old.dir = path
        sources.append(old)
    runs, seen = [], set()
    unavailable = incomplete = 0
    for source in sources:
        try:
            snap = source.metrics_snapshot(EVENT_LIMIT, BYTE_LIMIT)
            state, chart = snap["state"], snap["chart"]
            rid = state.get("run_id")
            if not isinstance(rid, str) or not rid:
                raise ValueError("missing run identity")
            if rid in seen:
                continue
            seen.add(rid)
            start = timestamp(state.get("started"))
            finish = timestamp(state.get("finished"))
            live = state.get("status") in ("running", "waiting_approval")
            end = until if live and finish is None else finish
            valid = start is not None and end is not None and start <= end <= until
            contracts = {}
            for node in chart.tasks():
                if "git.merge" not in (node.get("effects") or []):
                    continue
                outcomes = {name for name, fields in (node.get("returns") or {}).items()
                            if isinstance(fields, dict) and fields.get("merges") == "list"}
                if outcomes:
                    contracts[node.id] = outcomes
            complete = snap["complete"]
            merges = []
            for event in snap["events"]:
                if event.get("event") != "reported" or event.get("node") not in contracts:
                    continue
                if event.get("outcome") not in contracts[event["node"]]:
                    continue
                value = event.get("value")
                at = timestamp(event.get("ts"))
                if (not isinstance(value, dict) or not isinstance(value.get("merges"), list)
                        or not isinstance(value.get("repo"), str) or not value["repo"]
                        or not valid or at is None or not start <= at <= end):
                    complete = False
                    continue
                for row in value["merges"]:
                    if (not isinstance(row, dict) or not isinstance(row.get("issue"), str)
                            or not row["issue"] or not isinstance(row.get("merge_sha"), str)
                            or not SHA.fullmatch(row["merge_sha"])
                            or not isinstance(row.get("tip"), str) or not SHA.fullmatch(row["tip"])):
                        complete = False
                        continue
                    merges.append({"at": event["ts"], "sha": row["merge_sha"], "issue": row["issue"],
                                   "identity": value["repo"] + ":" + row["merge_sha"].lower()})
            if not valid:
                unavailable += 1
            if contracts and not complete:
                incomplete += 1
            runs.append({"run": rid, "workflow": state.get("workflow"),
                         "started": state.get("started") if valid else None,
                         "ended": now if live and valid else state.get("finished") if valid else None,
                         "completed_at": state.get("finished") if valid and state.get("status") == "done" else None,
                         "duration_s": finish - start if valid and finish is not None else None,
                         "merge_source": bool(contracts), "merge_complete": complete,
                         "merges": merges})
        except (store.StoreError, OSError, ValueError, KeyError):
            unavailable += 1
    # Re-reports in later runs do not move a merge into a later time bin.
    retained = set()
    ordered = [(timestamp(m["at"]), m) for run in runs for m in run["merges"]]
    for _, row in sorted(ordered, key=lambda item: item[0]):
        if row["identity"] in retained:
            row["duplicate"] = True
        retained.add(row["identity"])
    for run in runs:
        run["merges"] = [m for m in run["merges"] if not m.pop("duplicate", False)]
    return {"observed_at": now, "runs": runs, "limit": RUN_LIMIT,
            "event_limit": EVENT_LIMIT, "byte_limit": BYTE_LIMIT,
            "truncated": len(paths) > RUN_LIMIT - 1, "unavailable": unavailable,
            "journal_incomplete": incomplete}
