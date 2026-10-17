"""``claunch operators`` -- operator v2 state for the operator's own workflow.

``claunch operators watch`` is the probe of the operator-v2 workflow's watch
gate (``workflows/v2/operator-v2.yaml``): it reads the operator's record from
the daemon directory and answers in the wait-probe exit-code convention --
``0`` watch is on, ``1`` watch is off, ``2`` no record answers. It reads the
file, not the daemon, so a busy or restarting daemon does not turn a watch
that is off into a failed probe.

The operator is ``--id``, else the ``operator`` start input of the run
(``$CFLOW_DATA_INPUTS``, which the engine sets for a probe), else the record
bound to ``$CLAUNCH_SESSION``.
"""

from __future__ import annotations

import json
import os
import sys


def _input_id():
    try:
        inputs = json.loads(os.environ.get("CFLOW_DATA_INPUTS") or "{}")
    except ValueError:
        return ""
    value = inputs.get("operator") if isinstance(inputs, dict) else None
    return value.strip() if isinstance(value, str) else ""


def _cmd_watch(args) -> int:
    from .daemon import operators_v2, paths

    op_id = (args.id or "").strip() or _input_id()
    session = os.environ.get("CLAUNCH_SESSION") or ""
    watch, why = operators_v2.watch_state(paths.daemon_dir(), op_id or None, session or None)
    if watch is None:
        print(f"unknown: {why}", file=sys.stderr)
        return 2
    print(f"{'on' if watch else 'off'}: {why}")
    return 0 if watch else 1


def register(sub) -> None:
    p = sub.add_parser("operators", help="operator v2 state (the operator workflow's probes)")
    osub = p.add_subparsers(dest="operators_cmd", required=True)
    w = osub.add_parser(
        "watch",
        help="exit 0 when the operator's watch is on, 1 when off, 2 when no record answers",
    )
    w.add_argument("--id", default=None,
                   help="operator id (default: the run's 'operator' input, then the "
                        "operator bound to $CLAUNCH_SESSION)")
    w.set_defaults(func=_cmd_watch)
