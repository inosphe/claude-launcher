"""``claunch vm ...`` -- the VMs the daemon runs (vm-setup-8s6).

The user's side of daemon/vm_api.py: register a VM, start and stop it, decide
which sessions may look at it, drive it or run commands in it, and take the
screen or take control back. A managed session's shell is refused here: the
daemon turns away these routes when the request declares a session as its
actor, and this command says so before asking.

Exit codes: 0 done, 1 the daemon refused (the reason is printed), 2 no daemon.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional

from . import daemon_client

EXIT_OK, EXIT_REFUSED, EXIT_NO_DAEMON = 0, 1, 2
SCOPES = ("view", "control", "exec")


def _client():
    client, report = daemon_client.connect_with_diagnosis()
    if client is None:
        print(f"cannot reach the daemon: {daemon_client.unreachable_reason(report)}",
              file=sys.stderr)
    return client


def _user_only(what: str) -> bool:
    session = os.environ.get("CLAUNCH_SESSION")
    if not session:
        return False
    print(f"{what} is the user's to do, from a shell outside a session or the "
          f"dashboard (this shell belongs to {session})", file=sys.stderr)
    return True


def _call(args, method: str, path: str, body: Optional[dict] = None, *, show=None) -> int:
    if _user_only(f"claunch vm {args.vm_command}"):
        return EXIT_REFUSED
    client = _client()
    if client is None:
        return EXIT_NO_DAEMON
    try:
        # start, stop and suspend wait for the guest: give them room.
        out = client.request(method, path, body, timeout=600.0)
    except daemon_client.DaemonClientError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    if getattr(args, "json", False):
        print(json.dumps(out, ensure_ascii=False, indent=2))
    elif show is not None:
        show(out)
    else:
        print("ok")
    return EXIT_OK


def _row(vm: dict) -> str:
    who = vm.get("controller") or "-"
    if vm.get("human") == "driving":
        who += " (user driving)"
    grants = ",".join(f"{s}:{'+'.join(v)}" for s, v in sorted((vm.get("grants") or {}).items())) or "-"
    chan = vm.get("channel") or "-"
    return (f"{vm['id']:<16} {vm['state']:<10} {chan:<10} {vm.get('ip') or '-':<15} "
            f"{who:<20} {grants}")


def _show_list(out: dict) -> None:
    vms = out.get("vms") or []
    if not vms:
        print("no VMs registered")
        return
    print(f"{'VM':<16} {'STATE':<10} {'CHANNEL':<10} {'IP':<15} {'CONTROLLER':<20} GRANTS"
          f"   (slots: {out.get('slot_limit')})")
    for vm in vms:
        print(_row(vm))
        if vm.get("fault_reason"):
            print(f"{'':<16} fault: {vm['fault_reason']}")
        if vm.get("channel_error"):
            print(f"{'':<16} channel: {vm['channel_error']}")


def _cmd_list(args) -> int:
    client = _client()
    if client is None:
        return EXIT_NO_DAEMON
    try:
        out = client.get("/api/vm")
    except daemon_client.DaemonClientError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        _show_list(out)
    return EXIT_OK


def _cmd_add(args) -> int:
    body = {"id": args.vm, "tart_name": args.tart_name or args.vm, "ssh_user": args.ssh_user}
    if args.ssh_key:
        body["ssh_key"] = os.path.abspath(os.path.expanduser(args.ssh_key))
    return _call(args, "POST", "/api/vm", body,
                 show=lambda out: print(f"registered {out['id']} ({out['state']})"))


def _lifecycle(args) -> int:
    path = f"/api/vm/{args.vm}/{args.vm_command.replace('-', '_')}"
    return _call(args, "POST", path, {},
                 show=lambda out: print(f"{out['id']}: {out['state']}"))


def _cmd_rm(args) -> int:
    return _call(args, "DELETE", f"/api/vm/{args.vm}")


def _cmd_reconcile(args) -> int:
    return _call(args, "POST", "/api/vm-reconcile", {},
                 show=lambda out: print("\n".join(out.get("changed") or ["nothing changed"])))


def _cmd_grant(args) -> int:
    return _call(args, "POST", f"/api/vm/{args.vm}/grant",
                 {"session": args.session, "scopes": args.scopes},
                 show=lambda out: print(f"{args.session}: {', '.join(out['grants'])}"))


def _cmd_revoke(args) -> int:
    body = {"session": args.session}
    if args.scopes:
        body["scopes"] = args.scopes
    return _call(args, "POST", f"/api/vm/{args.vm}/revoke", body,
                 show=lambda out: print(f"{args.session}: {', '.join(out['grants']) or 'no grants'}"))


def _cmd_revoke_control(args) -> int:
    return _call(args, "POST", f"/api/vm/{args.vm}/revoke_controller", {})


def _cmd_duplicate(args) -> int:
    return _call(args, "POST", f"/api/vm/{args.vm}/extra_controller",
                 {"session": args.session, "reason": args.reason})


def _cmd_takeover(args) -> int:
    return _call(args, "POST", f"/api/vm/{args.vm}/human", {"action": "driving"},
                 show=lambda out: print("you are driving; sessions' control calls are refused "
                                        f"(calls running: {out.get('inflight', 0)})"))


def _cmd_return(args) -> int:
    return _call(args, "POST", f"/api/vm/{args.vm}/human", {"action": "idle"},
                 show=lambda out: print("handed back; the controller must look at the screen first"))


def register(sub) -> None:
    p = sub.add_parser(
        "vm", help="the VMs the daemon runs: start, stop, who may look at or drive them",
        description="Register, start, stop and suspend VMs, grant sessions view, control or "
                    "exec on one, take the screen yourself, and take control back. "
                    "Run these outside a managed session. Limit: the daemon has one token and "
                    "cannot tell a session's shell from yours, so this refusal is a courtesy "
                    "and not a boundary; every change is audited with the actor it declared.",
    )
    v = p.add_subparsers(dest="vm_command", required=True)

    def vm_arg(sp):
        sp.add_argument("vm", help="VM id")

    sp = v.add_parser("list", aliases=["ls"], help="the registered VMs")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=_cmd_list)

    sp = v.add_parser("add", help="register a VM that tart already has")
    vm_arg(sp)
    sp.add_argument("--tart-name", help="the name tart knows it by (default: the id)")
    sp.add_argument("--ssh-key", help="private key the daemon uses to reach the guest")
    sp.add_argument("--ssh-user", default="admin")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=_cmd_add)

    sp = v.add_parser("rm", help="forget a stopped VM (tart keeps it)")
    vm_arg(sp)
    sp.set_defaults(func=_cmd_rm)

    for name, text in (("start", "start it, or resume a suspended one"),
                       ("stop", "shut the guest down and wait (never a forced stop)"),
                       ("force-stop", "tart stop: the VM goes at once and unsynced writes with it"),
                       ("suspend", "save its state and free its slot")):
        sp = v.add_parser(name, help=text)
        vm_arg(sp)
        sp.add_argument("--json", action="store_true")
        sp.set_defaults(func=_lifecycle)

    sp = v.add_parser("reconcile", help="make the records agree with what tart reports")
    sp.set_defaults(func=_cmd_reconcile)

    sp = v.add_parser("grant", help="let a session use a VM")
    vm_arg(sp)
    sp.add_argument("session")
    sp.add_argument("scopes", nargs="+", choices=SCOPES,
                    help="view: look; control: drive (implies view); exec: guest shell and "
                         "installs (off unless named; implies view)")
    sp.set_defaults(func=_cmd_grant)

    sp = v.add_parser("revoke", help="take scopes (default: all) away from a session")
    vm_arg(sp)
    sp.add_argument("session")
    sp.add_argument("scopes", nargs="*", choices=SCOPES)
    sp.set_defaults(func=_cmd_revoke)

    sp = v.add_parser("revoke-control", help="end the control the current controller holds")
    vm_arg(sp)
    sp.set_defaults(func=_cmd_revoke_control)

    sp = v.add_parser("duplicate", help="let a second session drive alongside the controller")
    vm_arg(sp)
    sp.add_argument("session")
    sp.add_argument("--reason", required=True)
    sp.set_defaults(func=_cmd_duplicate)

    sp = v.add_parser("takeover", help="you take the screen: sessions' control calls are refused")
    vm_arg(sp)
    sp.set_defaults(func=_cmd_takeover)

    sp = v.add_parser("return", help="hand the screen back to the controller")
    vm_arg(sp)
    sp.set_defaults(func=_cmd_return)
