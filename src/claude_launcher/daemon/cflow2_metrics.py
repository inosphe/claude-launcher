"""Bounded saved-run observations for the v2 page's throughput chart.

No workflow names are special. Merge observations require the task's IR
contract (git.merge effect and a merges:list return) and structured reports.
Their time is the report time, not the git commit time. Retention before the
first saved interval and gaps between intervals remain unknown.
"""
from __future__ import annotations

import os
import re
import threading
import time
from collections import OrderedDict
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


def _facts(snap: dict) -> dict:
    """What ``observations`` needs from one saved run, none of it relative
    to the observation time: the identity, the times as saved, which tasks
    carry a merge contract and the reports that could fill one."""
    state, chart = snap["state"], snap["chart"]
    rid = state.get("run_id")
    if not isinstance(rid, str) or not rid:
        raise ValueError("missing run identity")
    contracts = {}
    for node in chart.tasks():
        if "git.merge" not in (node.get("effects") or []):
            continue
        outcomes = {name for name, fields in (node.get("returns") or {}).items()
                    if isinstance(fields, dict) and fields.get("merges") == "list"}
        if outcomes:
            contracts[node.id] = outcomes
    reports = [event for event in snap["events"]
               if event.get("event") == "reported" and event.get("node") in contracts
               and event.get("outcome") in contracts[event["node"]]]
    return {"run": rid, "state": {k: state.get(k) for k in
                                  ("workflow", "status", "started", "finished")},
            "contracts": bool(contracts), "complete": snap["complete"], "reports": reports}


#: Facts of saved runs by slot directory, valid while its files' stats hold.
#: The run page asks for the same runs on every poll, and an archive is not
#: written again: re-reading up to RUN_LIMIT checkpoints (each a checksum
#: pass over up to EVENT_LIMIT events) took seconds per poll on a busy daemon
#: (claunch-02w8s).
FACTS_CACHE_MAX = 512
#: A file changed this recently is not cached: a second write inside the
#: same mtime tick would leave its stat unchanged (git's "racily clean").
RACY_NS = 2_000_000_000
_facts_lock = threading.Lock()
_facts_cache: "OrderedDict[str, tuple]" = OrderedDict()


def _stamp(source: store.RunStore) -> tuple | None:
    """The slot files' stats, or None when one changed too recently to trust."""
    out = []
    now = time.time_ns()
    for path in (source.checkpoint_path, source.state_path, source.chart_path,
                 source.journal_path):
        try:
            st = os.stat(path)
        except OSError:
            out.append(None)
            continue
        if now - st.st_mtime_ns < RACY_NS:
            return None
        out.append((st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns))
    return tuple(out)


def _read_facts(source: store.RunStore) -> dict:
    return _facts(source.metrics_snapshot(EVENT_LIMIT, BYTE_LIMIT))


def _cached_facts(source: store.RunStore):
    """``_facts`` of a saved run through the cache, or None when it cannot be
    read. Both are kept: an unreadable run stays unreadable until its files
    change. The error itself is not kept: re-raising one cached exception on
    every poll would grow its traceback by a frame each time."""
    key = str(source.dir)
    stamp = _stamp(source)
    if stamp is not None:
        with _facts_lock:
            hit = _facts_cache.get(key)
            if hit is not None and hit[0] == stamp:
                _facts_cache.move_to_end(key)
                return hit[1]
    try:
        value = _read_facts(source)
    except _UNAVAILABLE:
        value = None
    if stamp is not None and stamp == _stamp(source):
        with _facts_lock:
            _facts_cache[key] = (stamp, value)
            _facts_cache.move_to_end(key)
            while len(_facts_cache) > FACTS_CACHE_MAX:
                _facts_cache.popitem(last=False)
    return value


_UNAVAILABLE = (store.StoreError, OSError, ValueError, KeyError)


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
            facts = _cached_facts(source)
            if facts is None:
                unavailable += 1
                continue
            rid, state = facts["run"], facts["state"]
            if rid in seen:
                continue
            seen.add(rid)
            start = timestamp(state.get("started"))
            finish = timestamp(state.get("finished"))
            live = state.get("status") in ("running", "waiting_approval")
            end = until if live and finish is None else finish
            valid = start is not None and end is not None and start <= end <= until
            complete = facts["complete"]
            merges = []
            for event in facts["reports"]:
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
            if facts["contracts"] and not complete:
                incomplete += 1
            runs.append({"run": rid, "workflow": state.get("workflow"),
                         "started": state.get("started") if valid else None,
                         "ended": now if live and valid else state.get("finished") if valid else None,
                         "completed_at": state.get("finished") if valid and state.get("status") == "done" else None,
                         "duration_s": finish - start if valid and finish is not None else None,
                         "merge_source": facts["contracts"], "merge_complete": complete,
                         "merges": merges})
        except _UNAVAILABLE:
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
