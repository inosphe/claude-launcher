"""``claunch sim ...`` -- the simulation run session's Node runtime.

Simulations themselves are driven from the dashboard (``#/sims``) and the
``sim_*`` MCP tools; this command only installs what the daemon's run
session loads (:mod:`claude_launcher.sim.runtime`).
"""

from __future__ import annotations

import argparse
import json

from .sim import runtime


def _cmd_status(args: argparse.Namespace) -> int:
    st = runtime.status()
    if args.json:
        print(json.dumps(st, indent=1))
    else:
        state = "stale (run 'claunch sim runtime install')" if st["stale"] else (
            "installed" if st["installed"] else "not installed")
        print(f"{st['dir']}: {state}")
    return 0 if st["installed"] and not st["stale"] else 1


def _cmd_install(args: argparse.Namespace) -> int:
    try:
        res = runtime.install(force=args.force)
    except runtime.InstallError as exc:
        print(f"error: {exc}")
        return 1
    print(f"{res['dir']}: {res['action']}")
    return 0


def register(sub) -> None:
    p = sub.add_parser("sim", help="world-model simulations: the run session's runtime")
    ssub = p.add_subparsers(dest="sim_cmd", required=True)
    rt = ssub.add_parser("runtime", help="the Node runtime the simulation run session loads")
    rsub = rt.add_subparsers(dest="sim_runtime_cmd", required=True)
    s = rsub.add_parser("status", help="say whether the runtime is installed and current; exit 1 if not")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=_cmd_status)
    i = rsub.add_parser("install", help="install or refresh it with npm ci (pinned lock)")
    i.add_argument("--force", action="store_true", help="reinstall even when the lock is current")
    i.set_defaults(func=_cmd_install)


def install_note() -> str:
    """One line for ``claunch install --global``: install the runtime, or
    say why it was not. A missing npm is not an install failure."""
    try:
        res = runtime.install()
    except runtime.InstallError as exc:
        return f"note: simulation runtime not installed: {str(exc).splitlines()[0]}"
    return f"installed: simulation runtime ({res['action']}) at {res['dir']}"
