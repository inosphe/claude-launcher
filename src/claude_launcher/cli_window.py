"""``claunch window`` -- the measurement window's command-line surface.

The window itself is the daemon's (``daemon/window.py``); this module is a
thin client over it. The point of the command is that the answer is *state
read from the arbiter*, not a process scan and not a chat message: any
session -- in a mesh or out of one (board ``claunch-fnhu``) -- can ask who
holds the machine's test window, take a place in its queue, and hand a grant
back.

Exit codes: ``0`` granted / read fine, ``1`` not granted (or nothing to
release), ``2`` the daemon could not be asked. A caller that cannot tell
apart "the daemon said no" from "the daemon could not be asked" would plan
its retry wrong, so the two do not share a code -- the same discipline
``tools/merge_ready.py`` gives its verdicts.

``prioritize``, ``force``, ``acquire --force`` and ``limits --set/--clear``
(claunch-k7x63) are the operator's queue overrides (claunch-8kald). They are refused inside a managed session
(``$CLAUNCH_SESSION`` set), the rule ``claunch daemon restart`` already
applies: an agent that could reorder or force the machine's test queue
could always put itself first, and the queue would stop meaning anything.
"""

from __future__ import annotations

import argparse
import os
import sys

from . import daemon_client


def _client():
    client, report = daemon_client.connect_with_diagnosis()
    if client is None:
        print(
            f"cannot ask the window: {daemon_client.unreachable_reason(report)}",
            file=sys.stderr,
        )
        return None
    return client


def _operator_only(what: str) -> bool:
    """True (and a refusal printed) when this shell is a managed session."""
    session = os.environ.get("CLAUNCH_SESSION")
    if not session:
        return False
    print(
        f"refused: `claunch window {what}` is an operator command and this shell "
        f"is managed session {session!r}. Ask the operator to run it, or to use "
        "the Window page of the web UI.",
        file=sys.stderr,
    )
    return True


def _who(entry: dict) -> str:
    who = entry.get("session") or f"pid {entry.get('pid')}"
    if entry.get("session") and entry.get("instance"):
        # Another daemon instance on this machine shares the window
        # (claunch-afmzw); its session names are its own.
        who = f"{who}@{entry['instance']}"
    if not entry.get("session") and entry.get("requested_by"):
        # A sessionless run (a delayed sweep) names who it works for.
        who = f"{who} for {entry['requested_by']}"
    return who


def _fmt_holder(entry: dict) -> str:
    who = _who(entry)
    since = entry.get("acquired_at") or entry.get("enqueued_at") or "?"
    label = entry.get("label") or ""
    extra = []
    if entry.get("workers"):
        extra.append(f"-n {entry['workers']}")
    if entry.get("forced"):
        extra.append("forced")
    if entry.get("priority"):
        extra.append(f"priority {entry['priority']}")
    tags = f" [{', '.join(extra)}]" if extra else ""
    return (
        f"{entry.get('grant_id', '?')} {entry.get('cls', '?')}:{who} since {since}"
        + tags
        + (f" -- {label}" if label else "")
    )


def _cmd_status(args) -> int:
    client = _client()
    if client is None:
        return 2
    status = client.get("/api/window")
    holders = status.get("holders") or []
    queue = status.get("queue") or []
    caps = status.get("caps") or {}
    print(
        f"window: {len(holders)} holder(s), {len(queue)} waiting "
        f"(caps: sweep {caps.get('sweep', '?')}, targeted {caps.get('targeted', '?')}; "
        f"advisory -n {status.get('advisory_n_now', '?')}; "
        f"max wait {status.get('max_wait', '?')}s)"
    )
    limits = status.get("limits") or {}
    if limits:
        budget = status.get("worker_budget", limits.get("worker_budget", "?"))
        print(
            f"workers: {status.get('workers_in_use', '?')} of budget "
            f"{budget if budget else 'off'}; targeted width {limits.get('targeted_width', '?')}, "
            f"sweep width {limits.get('sweep_width', '?')}; "
            f"targeted per session {limits.get('targeted_per_session', '?') or 'unlimited'}"
        )
    adaptive = status.get("adaptive") or {}
    if adaptive.get("lowered"):
        print(
            f"budget lowered: {adaptive.get('budget')} of {adaptive.get('configured')} "
            f"(floor {adaptive.get('floor')}) since {adaptive.get('decreased_at') or '?'}"
            f" -- {adaptive.get('reason') or 'timeouts'}; each passed run adds one back"
        )
    for entry in holders:
        print(f"  held: {_fmt_holder(entry)}")
    for i, entry in enumerate(queue, 1):
        print(f"  {i}. {_fmt_holder(entry)}")
    if not holders and not queue:
        print("  free")
    return 0


def _cmd_acquire(args) -> int:
    force = bool(getattr(args, "force", False))
    if force and _operator_only("acquire --force"):
        return 2
    if force and args.session:
        print(
            "refused: a forced grant is the operator's own, pid-tracked hold; "
            "drop --session",
            file=sys.stderr,
        )
        return 2
    client = _client()
    if client is None:
        return 2
    body = {
        "class": args.cls,
        "session": args.session or os.environ.get("CLAUNCH_SESSION"),
        "pid": os.getpid(),
        "label": args.label or "",
        "wait": args.wait,
    }
    if getattr(args, "workers", None):
        body["workers"] = args.workers
    if getattr(args, "priority", 0):
        body["priority"] = args.priority
    if force:
        body["force"] = True
    result = client.post(
        "/api/window/acquire", body, timeout=max(5.0, float(args.wait) + 5.0)
    )
    if result.get("granted"):
        print(
            f"granted: {result['grant_id']} (advisory -n {result.get('advisory_n', '?')})"
            + (" -- forced" if result.get("forced") else "")
        )
        return 0
    if result.get("error"):
        print(f"refused: {result['error']}", file=sys.stderr)
        return 2
    window = result.get("window") or {}
    holders = [_fmt_holder(h) for h in window.get("holders") or []]
    if result.get("timeout"):
        print(
            f"not granted: still waiting after --wait {args.wait}s; "
            f"held by {', '.join(holders) or '(unknown)'}",
            file=sys.stderr,
        )
    else:
        reason = result.get("reason")
        print(
            f"not granted: position {result.get('position', '?')}"
            + (f" ({reason})" if reason else "")
            + f"; held by {', '.join(holders) or '(unknown)'}",
            file=sys.stderr,
        )
    return 1


def _cmd_release(args) -> int:
    client = _client()
    if client is None:
        return 2
    body = {}
    if args.grant_id:
        body["grant_id"] = args.grant_id
    else:
        body["session"] = args.session or os.environ.get("CLAUNCH_SESSION")
    if not body.get("session") and not body.get("grant_id"):
        print(
            "release wants --grant-id, or a session ($CLAUNCH_SESSION is unset "
            "and --session was not given)",
            file=sys.stderr,
        )
        return 2
    result = client.post("/api/window/release", body)
    released = result.get("released", 0)
    if released:
        print(f"released: {released}")
        return 0
    print("nothing to release (no such grant)", file=sys.stderr)
    return 1


def _cmd_cancel(args) -> int:
    client = _client()
    if client is None:
        return 2
    body = {}
    if args.grant_id:
        body["grant_id"] = args.grant_id
    else:
        body["session"] = args.session or os.environ.get("CLAUNCH_SESSION")
    if not body.get("session") and not body.get("grant_id"):
        print(
            "cancel wants --grant-id, or a session ($CLAUNCH_SESSION is unset "
            "and --session was not given)",
            file=sys.stderr,
        )
        return 2
    result = client.post("/api/window/cancel", body)
    cancelled = result.get("cancelled", 0)
    if cancelled:
        print(f"cancelled: {cancelled}")
        return 0
    print("nothing to cancel (no such waiting request)", file=sys.stderr)
    return 1


def _cmd_prioritize(args) -> int:
    if _operator_only("prioritize"):
        return 2
    client = _client()
    if client is None:
        return 2
    body = {"grant_id": args.grant_id}
    if args.priority is not None:
        body["priority"] = args.priority
    try:
        result = client.post("/api/window/prioritize", body)
    except daemon_client.DaemonClientError as exc:
        print(f"not prioritized: {exc}", file=sys.stderr)
        return 1
    if result.get("error"):
        print(f"not prioritized: {result['error']}", file=sys.stderr)
        return 1
    where = "granted now" if result.get("granted") else f"position {result.get('position')}"
    print(f"priority {result.get('priority')}: {args.grant_id} ({where})")
    return 0


def _cmd_force(args) -> int:
    if _operator_only("force"):
        return 2
    client = _client()
    if client is None:
        return 2
    try:
        result = client.post("/api/window/force", {"grant_id": args.grant_id})
    except daemon_client.DaemonClientError as exc:
        print(f"not forced: {exc}", file=sys.stderr)
        return 1
    if result.get("error") or not result.get("forced"):
        print(f"not forced: {result.get('error') or 'no such waiting request'}", file=sys.stderr)
        return 1
    holder = result.get("holder") or {}
    print(f"forced: {_fmt_holder(holder)}")
    return 0


def _parse_limit_args(sets, clears) -> dict:
    changes = {}
    for item in sets or []:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            raise ValueError(f"--set wants name=value, got {item!r}")
        changes[key.strip()] = value.strip()
    for key in clears or []:
        changes[key.strip()] = None
    return changes


def _print_limits(view: dict) -> None:
    values = view.get("values") or {}
    overrides = view.get("overrides") or {}
    bounds = view.get("bounds") or {}
    if view.get("editable"):
        print(f"limits: set on this daemon ({view.get('by') or 'default'})")
    elif view.get("source") == "published":
        print(
            f"limits: published by the main daemon ({view.get('by') or 'default'}); "
            "change them there"
        )
    else:
        print("limits: this daemon's config (no main daemon has published limits yet)")
    for key in bounds or values:
        low, high = (bounds.get(key) or [None, None])[:2]
        mark = "  (override)" if key in overrides else ""
        print(f"  {key:<22} {values.get(key, '-')!s:>5}   range {low}..{high}{mark}")
    if view.get("set_at"):
        print(f"  last changed {view['set_at']} by {view.get('set_by') or '?'}")


def _cmd_limits(args) -> int:
    try:
        changes = _parse_limit_args(args.set, args.clear)
    except ValueError as exc:
        print(f"not changed: {exc}", file=sys.stderr)
        return 1
    if changes and _operator_only("limits --set/--clear"):
        return 2
    client = _client()
    if client is None:
        return 2
    try:
        if changes:
            view = client.put(
                "/api/window/limits", {"limits": changes, "by": "cli"}
            )
        else:
            view = client.get("/api/window/limits")
    except daemon_client.DaemonClientError as exc:
        print(f"not changed: {exc}" if changes else f"cannot read limits: {exc}", file=sys.stderr)
        return 1
    if view.get("error"):
        print(f"not changed: {view['error']}", file=sys.stderr)
        return 1
    if args.json:
        import json

        print(json.dumps(view, indent=2, ensure_ascii=False))
        return 0
    _print_limits(view)
    return 0


def _fmt_seconds(value) -> str:
    if value is None:
        return "-"
    value = float(value)
    if value < 60:
        return f"{value:.0f}s"
    if value < 3600:
        return f"{value / 60:.1f}m"
    return f"{value / 3600:.1f}h"


def _fmt_result(result) -> str:
    if not result:
        return "unreported"
    counts = ", ".join(
        f"{result[k]} {k}"
        for k in ("passed", "failed", "errors", "skipped", "timeouts")
        if result.get(k)
    )
    return f"{result.get('outcome', '?')}" + (f" ({counts})" if counts else "")


def _cmd_history(args) -> int:
    client = _client()
    if client is None:
        return 2
    query = [f"limit={args.limit}"]
    if args.days:
        query.append(f"days={args.days}")
    if args.cls:
        query.append(f"class={args.cls}")
    if args.session:
        query.append(f"session={args.session}")
    try:
        answer = client.get("/api/window/history?" + "&".join(query))
    except daemon_client.DaemonClientError as exc:
        print(f"cannot read the window history: {exc}", file=sys.stderr)
        return 2
    if args.json:
        import json

        print(json.dumps(answer, indent=1, ensure_ascii=False))
        return 0
    print(
        f"window history: last {answer.get('days')} day(s), {answer.get('total', 0)} "
        f"entr(ies) (the log keeps {answer.get('retention_days', 7)} days, then drops them)"
    )
    for cls, st in ((answer.get("stats") or {}).get("classes") or {}).items():
        wait, held = st.get("wait_seconds") or {}, st.get("held_seconds") or {}
        outcomes = ", ".join(f"{v} {k}" for k, v in sorted((st.get("outcomes") or {}).items()))
        print(
            f"  {cls}: {st.get('runs', 0)} run(s) [{outcomes or 'none'}]; "
            f"wait p50 {_fmt_seconds(wait.get('p50'))} p90 {_fmt_seconds(wait.get('p90'))} "
            f"max {_fmt_seconds(wait.get('max'))}; held p50 {_fmt_seconds(held.get('p50'))} "
            f"p90 {_fmt_seconds(held.get('p90'))} max {_fmt_seconds(held.get('max'))}; "
            f"gave up waiting {st.get('gave_up_waiting', 0)}, forced {st.get('forced', 0)}"
        )
    for entry in answer.get("entries") or []:
        who = _who(entry)
        print(
            f"  {entry.get('ended_at', '?')} {entry.get('cls', '?')}:{who} "
            f"{entry.get('end', '?')} wait {_fmt_seconds(entry.get('wait_seconds'))} "
            f"held {_fmt_seconds(entry.get('held_seconds'))} -n {entry.get('workers') or '-'} "
            f"{_fmt_result(entry.get('result'))}"
            + (" forced" if entry.get("forced") else "")
            + (f" -- {entry['label']}" if entry.get("label") else "")
        )
    return 0


def _reported(grant_id: str) -> bool:
    """True when something already attached a result to this held grant (a
    pytest under it reports its own, with counts the wrapper does not have)."""
    client = daemon_client.connect()
    if client is None:
        return False
    try:
        state = client.get("/api/window", timeout=3.0)
    except daemon_client.DaemonClientError:
        return False
    return any(h.get("grant_id") == grant_id and h.get("result")
               for h in state.get("holders") or [])


def _cmd_run(args) -> int:
    """Hold the window for one command of any kind (claunch-ersj2).

    pytest in this repository takes the window through tests/conftest.py; a
    command that does not -- another repository's npm or cargo test, a script
    -- runs here under a grant instead, so it waits its turn and shows on the
    Window page. A pytest inside it inherits the grant instead of queueing
    behind it. Exit status is the command's; 1 when no grant was given.
    """
    import shlex
    import subprocess
    import time

    from . import test_window

    command = list(args.command or [])
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("window run wants a command: claunch window run [options] -- <command>",
              file=sys.stderr)
        return 2
    try:
        grant = test_window.acquire(
            args.cls,
            label=args.label or shlex.join(command)[:200],
            wait=args.wait,
            workers=args.workers or 0,
        )
    except test_window.WindowUnavailable as exc:
        print(f"window run: {exc}", file=sys.stderr)
        return 1
    started = time.monotonic()
    try:
        try:
            code = subprocess.run(command, env=grant.child_env()).returncode
        except FileNotFoundError:
            print(f"window run: command not found: {command[0]}", file=sys.stderr)
            code = 127
        if test_window.reportable(grant.grant_id) and not _reported(grant.grant_id):
            test_window.report_result(grant.grant_id, {
                "outcome": "passed" if code == 0 else "failed" if code == 1 else "error",
                "exit_code": code,
                "duration": round(time.monotonic() - started, 1),
            })
    finally:
        grant.release()
    return code


def register(sub) -> None:
    p_window = sub.add_parser(
        "window",
        help="the machine's measurement window: who may run tests right now",
    )
    wsub = p_window.add_subparsers(dest="window_command", required=True)

    p = wsub.add_parser("status", help="who holds the window and who waits")
    p.set_defaults(func=_cmd_status)

    p = wsub.add_parser(
        "acquire",
        help="ask for the window (exit 1 = held by someone else, 2 = cannot ask)",
    )
    p.add_argument(
        "--class",
        dest="cls",
        choices=["sweep", "targeted"],
        default="sweep",
        help="sweep = a full-suite run, exclusive (default); targeted = a "
        "nodeid-selected run, shared up to the cap",
    )
    p.add_argument(
        "--wait",
        type=float,
        nargs="?",
        const=1800.0,
        default=0.0,
        metavar="SECONDS",
        help="queue and wait (default with no value: up to 30 minutes; omitted: ask once)",
    )
    p.add_argument("--label", help="what the window is being used for")
    p.add_argument("--priority", type=int, default=0,
                   help="queue priority; negative values run behind normal tests")
    p.add_argument(
        "--session",
        help="holder name (default: $CLAUNCH_SESSION; empty = a manual, "
        "pid-tracked hold)",
    )
    p.add_argument(
        "--workers",
        type=int,
        metavar="N",
        help="the xdist width this run wants; the grant never exceeds it "
        "(default: the class ceiling)",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="operator only: grant now, past every cap, budget and "
        "exclusivity rule (refused inside a managed session)",
    )
    p.set_defaults(func=_cmd_acquire)

    p = wsub.add_parser(
        "run",
        help="run one command under a grant: wait for the window, run, release "
        "(for tests that do not take the window themselves)",
    )
    p.add_argument("--class", dest="cls", choices=["sweep", "targeted"],
                   default="targeted",
                   help="targeted (default) shares the window up to the cap; "
                   "sweep runs alone")
    p.add_argument("--wait", type=float, default=1800.0, metavar="SECONDS",
                   help="how long to queue for the grant (default 1800)")
    p.add_argument("--label", help="what runs (default: the command line)")
    p.add_argument("--workers", type=int, metavar="N",
                   help="the parallel width the command uses (default: the class "
                   "ceiling); the grant's width is in $CLAUNCH_WINDOW_ADVISORY_N")
    p.add_argument("command", nargs=argparse.REMAINDER,
                   help="-- then the command and its arguments")
    p.set_defaults(func=_cmd_run)

    p = wsub.add_parser("release", help="hand a grant back")
    p.add_argument("--grant-id", help="the grant to release (default: all of "
                                      "this session's)")
    p.add_argument("--session", help="whose grants (default: $CLAUNCH_SESSION)")
    p.set_defaults(func=_cmd_release)

    p = wsub.add_parser("cancel", help="withdraw a waiting request")
    p.add_argument("--grant-id", help="the waiting request to withdraw (default: "
                                      "all of this session's)")
    p.add_argument("--session", help="whose waiting requests (default: $CLAUNCH_SESSION)")
    p.set_defaults(func=_cmd_cancel)

    p = wsub.add_parser(
        "prioritize",
        help="operator only: move a waiting request up the queue (refused "
        "inside a managed session)",
    )
    p.add_argument("grant_id", help="the waiting request (ids: `claunch window status`)")
    p.add_argument(
        "--priority",
        type=int,
        help="explicit priority; higher goes first, 0 is the default, negative "
        "demotes (omitted: to the top)",
    )
    p.set_defaults(func=_cmd_prioritize)

    p = wsub.add_parser(
        "force",
        help="operator only: grant a waiting request now, past every limit "
        "(refused inside a managed session)",
    )
    p.add_argument("grant_id", help="the waiting request (ids: `claunch window status`)")
    p.set_defaults(func=_cmd_force)

    p = wsub.add_parser(
        "limits",
        help="the test window's limits (caps, widths, worker budget); "
        "--set/--clear are operator only and work on the main daemon",
    )
    p.add_argument(
        "--set", action="append", metavar="NAME=VALUE",
        help="override one limit, e.g. --set worker_budget=8 (repeatable)",
    )
    p.add_argument(
        "--clear", action="append", metavar="NAME",
        help="drop an override so the config default applies again (repeatable)",
    )
    p.add_argument("--json", action="store_true", help="the API answer as JSON")
    p.set_defaults(func=_cmd_limits)

    p = wsub.add_parser(
        "history",
        help="test runs the window granted: wait, hold, result, and statistics "
        "(the daemon keeps 7 days of this log and drops older entries)",
    )
    p.add_argument("--days", type=float, help="look back this many days (at most 7)")
    p.add_argument("--class", dest="cls", choices=["sweep", "targeted"])
    p.add_argument("--session", help="only this holder session")
    p.add_argument("--limit", type=int, default=20, help="entries to list (default 20)")
    p.add_argument("--json", action="store_true", help="the API answer as JSON")
    p.set_defaults(func=_cmd_history)
