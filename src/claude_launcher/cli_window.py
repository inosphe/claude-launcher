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
"""

from __future__ import annotations

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


def _fmt_holder(entry: dict) -> str:
    who = entry.get("session") or f"pid {entry.get('pid')}"
    since = entry.get("acquired_at") or entry.get("enqueued_at") or "?"
    label = entry.get("label") or ""
    return f"{entry.get('cls', '?')}:{who} since {since}" + (
        f" -- {label}" if label else ""
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
        f"advisory -n {status.get('advisory_n_now', '?')})"
    )
    for entry in holders:
        print(f"  held: {_fmt_holder(entry)}")
    for i, entry in enumerate(queue, 1):
        print(f"  {i}. {_fmt_holder(entry)}")
    if not holders and not queue:
        print("  free")
    return 0


def _cmd_acquire(args) -> int:
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
    result = client.post(
        "/api/window/acquire", body, timeout=max(5.0, float(args.wait) + 5.0)
    )
    if result.get("granted"):
        print(
            f"granted: {result['grant_id']} (advisory -n {result.get('advisory_n', '?')})"
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
        print(
            f"not granted: position {result.get('position', '?')}; "
            f"held by {', '.join(holders) or '(unknown)'}",
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
        const=3600.0,
        default=0.0,
        metavar="SECONDS",
        help="queue and wait (default with no value: up to one hour; omitted: ask once)",
    )
    p.add_argument("--label", help="what the window is being used for")
    p.add_argument(
        "--session",
        help="holder name (default: $CLAUNCH_SESSION; empty = a manual, "
        "pid-tracked hold)",
    )
    p.set_defaults(func=_cmd_acquire)

    p = wsub.add_parser("release", help="hand a grant back")
    p.add_argument("--grant-id", help="the grant to release (default: all of "
                                      "this session's)")
    p.add_argument("--session", help="whose grants (default: $CLAUNCH_SESSION)")
    p.set_defaults(func=_cmd_release)
