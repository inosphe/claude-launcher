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
import time
from typing import Optional

from . import daemon_client

EXIT_OK, EXIT_REFUSED, EXIT_NO_DAEMON = 0, 1, 2
SCOPES = ("view", "control", "exec", "files")


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
        if vm.get("template"):
            print(f"{'':<16} PRESET TEMPLATE: the tart copy of preset {vm['template']!r}; starting it changes later clones")
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
    body = {"confirm_template": True} if getattr(args, "yes_template", False) else {}
    return _call(args, "POST", path, body,
                 show=lambda out: print(f"{out['id']}: {out['state']}"))


def _net_show(args, out) -> None:
    print("expose: " + (", ".join(out.get("expose") or []) or "-"))
    print("allow:  " + (", ".join(out.get("allow") or []) or "-"))
    if out.get("net_pending"):
        print("restart the VM for the change to apply")


def _net(args, what: str, body_for_add, body_for_rm) -> int:
    if args.action == "list":
        return _call(args, "GET", f"/api/vm/{args.vm}", show=lambda out: _net_show(args, out))
    if not args.value:
        print(f"claunch vm {what} {args.action}: a value is required", file=sys.stderr)
        return EXIT_REFUSED
    body = body_for_add(args.value) if args.action == "add" else body_for_rm(args.value)
    if body is None:
        print(f"claunch vm {what}: expected EXTERNAL:GUEST", file=sys.stderr)
        return EXIT_REFUSED
    return _call(args, "POST", f"/api/vm/{args.vm}/{what}", {"action": args.action, **body},
                 show=lambda out: _net_show(args, out))


def _cmd_expose(args) -> int:
    def add(value):
        ext, sep, inner = value.partition(":")
        return {"external": ext, "internal": inner} if sep else None
    return _net(args, "expose", add, lambda value: {"external": value})


def _cmd_allow(args) -> int:
    return _net(args, "allow", lambda v: {"cidr": v}, lambda v: {"cidr": v})

def _share_show(out: dict) -> None:
    rows = out.get("share_rows")
    if rows is None:
        rows = out.get("shared_dirs") or []
    for sh in rows:
        if not sh.get("host_path"):
            continue
        origin = (sh.get("origin") or {}).get("kind", "vm")
        by = (sh.get("origin") or {}).get("by_session")
        state = sh.get("state", "")
        print(f"{sh['name']:<14} {sh['mode']}  {sh['host_path']}  ->  {sh['guest_path']}  "
              f"[{origin}{' ' + by if by else ''}{', ' + state if state else ''}]")
        if state == "left_out":
            print(f"  left out: {sh.get('why')}")
    if not rows:
        print("no shared folders")
    for name, why in (out.get("shares_skipped") or {}).items():
        if all(r.get("name") != name for r in rows):
            print(f"  left out at the last start: {name}: {why}")
    if out.get("shares_pending"):
        print("restart the VM for the change to apply")


def _catalog_show(out: dict) -> None:
    rows = out.get("share_catalog") or []
    for e in rows:
        where = "all VMs" if e["scope"] == "all" else "VMs: " + ", ".join(e["vms"])
        print(f"{e['name']:<14} {e['mode']}  {e['host_path']}  ({where})")
    if not rows:
        print("the share catalog is empty")
    print("applied to a VM at its next start")


def _roots_show(out: dict) -> None:
    for r in out.get("roots") or []:
        print(f"{'ok ' if r['ok'] else 'OFF'}  {r['path']}" + ("" if r["ok"] else f"  -- {r['reason']}"))
    if not out.get("roots"):
        print("no share roots: sharing host folders is off")
    elif not out.get("enabled"):
        print("none of the roots is in effect: sharing host folders is off")


def _cmd_share_roots(args) -> int:
    """``claunch vm share-roots ls | add PATH | rm PATH``: the folders shares may
    come from (``daemon.vm_share_roots``); the user's, read afresh by the daemon."""
    if args.action == "ls":
        return _call(args, "GET", "/api/vm-share-roots", show=_roots_show)
    if not args.path:
        print("claunch vm share-roots add PATH | rm PATH | ls", file=sys.stderr)
        return EXIT_REFUSED
    return _call(args, "POST", "/api/vm-share-roots",
                 {"action": args.action, "path": os.path.abspath(os.path.expanduser(args.path))}, show=_roots_show)


def _cmd_guard(args) -> int:
    """The Claude Code PreToolUse hook (``vm_guard``): the tool call on stdin, a
    deny decision on stdout or nothing. Any failure allows the call."""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        from .daemon import vm_guard
        session = os.environ.get("CLAUNCH_SESSION", "")
        reply = vm_guard.hook_response(payload, session) if session and isinstance(payload, dict) else None
        if reply is not None:
            print(json.dumps(reply, ensure_ascii=False))
    except Exception:  # noqa: BLE001 - fail-open
        pass
    return 0


def _only_show(out: dict) -> None:
    for row in out.get("sessions") or []:
        state = "on " if row["on"] else "off"
        extra = "" if row["can_enable"] else f"  (cannot be turned on: {row['why']})"
        print(f"{row['session']:<20} {state}{extra}")
        for b in row.get("blocks", [])[:3]:
            print(f"    blocked {b['kind']} ({b['rule']}): {b['head']}")
    if not out.get("sessions"):
        print("no session holds a VM grant")


def _cmd_only(args) -> int:
    if args.action == "ls":
        return _call(args, "GET", "/api/vm-only", show=_only_show)
    if not args.session:
        print("claunch vm only on|off SESSION | ls", file=sys.stderr)
        return EXIT_REFUSED
    return _call(args, "POST", "/api/vm-only", {"session": args.session, "on": args.action == "on"}, show=_only_show)


def _cmd_catalog(args) -> int:
    """``claunch vm share catalog ls | add NAME PATH [--vm ID ...] [--rw [--yes-rw]] | rm NAME``
    (parsed with ``catalog`` in the VM position)."""
    if args.action == "ls":
        return _call(args, "GET", "/api/vm-share-catalog", show=_catalog_show)
    if not args.name or (args.action == "add" and not args.path):
        print("claunch vm share catalog add NAME PATH [--vm ID ...] [--rw [--yes-rw]] | rm NAME | ls",
              file=sys.stderr)
        return EXIT_REFUSED
    if args.action == "rm":
        return _call(args, "POST", "/api/vm-share-catalog", {"action": "rm", "name": args.name}, show=_catalog_show)
    body = {"action": "put", "name": args.name, "host_path": os.path.abspath(os.path.expanduser(args.path)),
            "mode": "rw" if args.rw else "ro", "scope": "vms" if args.for_vm else "all",
            "vms": list(args.for_vm or []), "confirm_rw": bool(args.yes_rw), "replace": bool(args.replace)}
    return _call(args, "POST", "/api/vm-share-catalog", body, show=_catalog_show)


def _cmd_share(args) -> int:
    if args.vm == "catalog":
        return _cmd_catalog(args)
    if args.action == "ls":
        return _call(args, "GET", f"/api/vm/{args.vm}", show=_share_show)
    if not args.name or (args.action == "add" and not args.path):
        print("claunch vm share add NAME PATH [--rw] | rm NAME | ls", file=sys.stderr)
        return EXIT_REFUSED
    body = {"action": args.action, "name": args.name}
    if args.action == "add":
        body.update({"host_path": os.path.abspath(os.path.expanduser(args.path)),
                     "mode": "rw" if args.rw else "ro"})
    return _call(args, "POST", f"/api/vm/{args.vm}/share", body, show=_share_show)


def _requests_show(out: dict) -> None:
    rows = [(vm["id"], r) for vm in out.get("vms") or [] for r in vm.get("requests") or []]
    for vm_id, r in rows:
        shares = ", ".join(f"{s['name']}({s['mode']}) {s['host_path']}" for s in r.get("shares") or [])
        print(f"{vm_id:<14} {r['session']:<16} id: {r['id']}  scopes: {'+'.join(r.get('scopes') or []) or '-'}  "
              f"shares: {shares or '-'}  {r.get('reason') or ''}")
    if not rows:
        print("no pending requests")


def _cmd_requests(args) -> int:
    return _call(args, "GET", "/api/vm", show=_requests_show)


def _cmd_approve(args) -> int:
    path = f"/api/vm/{args.vm}/{args.vm_command}"
    return _call(args, "POST", path, {"session": args.session, "id": args.id},
                 show=lambda out: print(f"{args.vm}: " + (", ".join(
                     f"{s}:{'+'.join(v)}" for s, v in sorted((out.get('grants') or {}).items())) or "-")))


def _cmd_lock(args) -> int:
    def show(out):
        print(f"{out['id']}: " + ("locked" if out.get("locked") else "unlocked"))
    return _call(args, "POST", f"/api/vm/{args.vm}/{args.vm_command}", {}, show=show)


def _cmd_files(args) -> int:
    def show(out):
        print(f"host folder   {out['host_dir']}")
        print(f"guest folder  {out['guest_dir']}" + ("  (default)" if out['guest_dir'] == out['guest_dir_default'] else ""))
        lim = out["limits"]
        print(f"limits        {lim['max_bytes'] // (1024 * 1024)} MiB, {lim['max_items']} items, {lim['timeout']:g}s per request")
        for row in out.get("recent") or []:
            when = time.strftime("%m-%d %H:%M:%S", time.localtime(row["at"]))
            print(f"{when}  {row['session']:<14} {row['op']:<4} {row['path']}  "
                  + (f"{row.get('bytes', 0)} bytes" if row.get("ok") else f"refused: {row.get('code')}"))
        if not out.get("recent"):
            print("no transfers since the daemon started")
    if args.guest_dir is not None:
        return _call(args, "POST", f"/api/vm/{args.vm}/files_dir", {"dir": args.guest_dir}, show=show)
    return _call(args, "GET", f"/api/vm/{args.vm}/files", show=show)


def _preset_list(args) -> int:
    def show(out):
        rows = out.get("presets") or []
        for p in rows:
            gone = "" if p.get("present") is not False else "  (tart copy missing)"
            mark = "  (default)" if p.get("default") else ""
            print(f"{p['name']}\t{p.get('source') or '-'}\t{p.get('description') or ''}{gone}{mark}")
        if not rows:
            print("no presets")
        if out.get("default_lost"):
            print(out["default_lost"])
    return _call(args, "GET", "/api/vm-presets", show=show)


def _preset_default(args) -> int:
    def show(out):
        print(f"default preset: {out.get('default') or '(none)'}")
        if out.get("default_lost"):
            print(out["default_lost"])
    if args.name and args.clear:
        print("give a preset name or --clear, not both", file=sys.stderr)
        return EXIT_REFUSED
    if not args.name and not args.clear:
        return _call(args, "GET", "/api/vm-presets", show=show)
    return _call(args, "POST", "/api/vm-presets-default", {"name": "" if args.clear else args.name}, show=show)


def _preset_save(args) -> int:
    return _call(args, "POST", "/api/vm-presets",
                 {"vm": args.vm, "name": args.name, "description": args.description},
                 show=lambda out: print(f"preset {out['name']} saved from {out['source']}"))


def _preset_describe(args) -> int:
    return _call(args, "POST", f"/api/vm-presets/{args.name}/describe",
                 {"description": args.description})


def _preset_rm(args) -> int:
    if not args.yes:
        print(f"this deletes the preset {args.name!r} and its tart copy preset-{args.name}; "
              "run again with --yes to confirm", file=sys.stderr)
        return EXIT_REFUSED
    return _call(args, "POST", f"/api/vm-presets/{args.name}/delete", {"confirm": args.name})


def _preset_new(args) -> int:
    def show(out):
        vm = out["vm"]
        print(f"{vm['id']}: {vm['state']}" + ("" if out.get("started") else f" (not started: {out.get('why')})"))
    return _call(args, "POST", f"/api/vm-presets/{args.name}/create", {"id": args.id}, show=show)


def _cmd_rm(args) -> int:
    return _call(args, "DELETE", f"/api/vm/{args.vm}",
                 show=lambda out: print(f"unregistered {out.get('unregistered', args.vm)}: the tart VM "
                                        f"{out.get('tart_kept', args.vm)!r} and its disk stay on this host "
                                        f"('claunch vm destroy {args.vm} --confirm {args.vm}' deletes them for good)"))


def _destroy_show(out: dict) -> None:
    size = f"{out['size_gb']} GB" if out.get("size_gb") else "size unknown"
    print(f"VM {out['id']} (tart {out['tart_name']!r}, {out['state']}, {size})")
    for blocker in out.get("blockers") or []:
        print(f"  REFUSED  {blocker['code']}: {blocker['reason']}")
    for warning in out.get("warnings") or []:
        print(f"  WARNING  {warning}")


def _cmd_destroy(args) -> int:
    """``claunch vm destroy ID [--confirm ID]``: show what deleting the tart VM would do
    and, with ``--confirm`` equal to the id, delete it (disk image included, no undo)."""
    path = f"/api/vm/{args.vm}/destroy"
    if args.confirm != args.vm:
        rc = _call(args, "GET", path, show=_destroy_show)
        if rc == 0:
            print(f"nothing deleted. To delete the tart VM and its disk for good: "
                  f"claunch vm destroy {args.vm} --confirm {args.vm}")
        return rc
    return _call(args, "POST", path, {"confirm": args.confirm},
                 show=lambda out: print(f"destroyed {out['destroyed']}: tart VM {out['tart_name']!r} deleted"
                                        + (f" ({out['size_gb']} GB)" if out.get("size_gb") else "")))


def _cmd_reconcile(args) -> int:
    return _call(args, "POST", "/api/vm-reconcile", {},
                 show=lambda out: print("\n".join(out.get("changed") or ["nothing changed"])))


def _cmd_grant(args) -> int:
    return _call(args, "POST", f"/api/vm/{args.vm}/grant",
                 {"session": args.session, "scopes": args.scopes},
                 show=lambda out: (print(f"{args.session}: {', '.join(out['grants'])}"),
                                   out.get("warning") and print(f"warning: {out['warning']}")))


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
    sp.add_argument("--ssh-key", help="unused: the daemon reaches the guest through tart exec; kept in the record")
    sp.add_argument("--ssh-user", default="admin",
                    help="the guest account that is logged in on the console (default admin)")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=_cmd_add)

    sp = v.add_parser("rm", help="UNREGISTER a stopped VM: the daemon forgets it (grants, shares, requests); "
                                 "the tart VM and its disk stay on the host. To delete them use 'destroy'")
    vm_arg(sp)
    sp.set_defaults(func=_cmd_rm)

    sp = v.add_parser("destroy", help="DELETE a VM for good (tart delete: the disk image is gone, no undo). Without "
                                      "--confirm it only shows what would happen; --confirm must repeat the VM id. "
                                      "Only a stopped, unlocked VM no session holds a grant on")
    vm_arg(sp)
    sp.add_argument("--confirm", default="", metavar="ID", help="the VM id, typed again")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=_cmd_destroy)

    for name, text in (("start", "start it, or resume a suspended one (refused while the host screen is locked)"),
                       ("stop", "shut the guest down and wait (never a forced stop)"),
                       ("force-stop", "tart stop: the VM goes at once and unsynced writes with it"),
                       ("suspend", "optional: save its state and free its slot; resuming needs an unlocked host screen (the default way to keep a VM is stop + cold boot)")):
        sp = v.add_parser(name, help=text)
        vm_arg(sp)
        sp.add_argument("--json", action="store_true")
        if name == "start":
            sp.add_argument("--yes-template", action="store_true",
                            help="confirm starting a VM that is the copy of a preset (it changes later clones)")
        sp.set_defaults(func=_lifecycle)

    sp = v.add_parser("expose", help="TCP ports of the guest published on this host's LAN address "
                                     "(next start; anyone on the LAN can connect)")
    vm_arg(sp)
    sp.add_argument("action", choices=("list", "add", "rm"))
    sp.add_argument("value", nargs="?", help="add: EXTERNAL:GUEST (external 1024-65535); rm: EXTERNAL")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=_cmd_expose)

    sp = v.add_parser("allow", help="IPv4 ranges softnet lets through inside the blocked private "
                                    "ranges (next start; a /32 address is the usual choice; "
                                    "0.0.0.0/0, wider than /8, a whole private range and a range "
                                    "holding this host are refused)")
    vm_arg(sp)
    sp.add_argument("action", choices=("list", "add", "rm"))
    sp.add_argument("value", nargs="?", help="the address or CIDR")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=_cmd_allow)
    for name, text in (("lock", "lock a stopped VM: the daemon starts, resumes and calls nothing on it "
                                "until it is unlocked (it does not stop a running VM)"),
                       ("unlock", "unlock a VM for administration")):
        sp = v.add_parser(name, help=text)
        vm_arg(sp)
        sp.add_argument("--json", action="store_true")
        sp.set_defaults(func=_cmd_lock)

    sp = v.add_parser("share", help="host folders mounted in the guest at /Volumes/My Shared Files/<name> "
                                    "(tart run --dir; applied at the next start). Only paths under the "
                                    "daemon.vm_share_roots setting; read-only unless --rw. 'share catalog ls|add|rm' "
                                    "manages folders shared with all VMs or listed ones")
    vm_arg(sp)
    sp.add_argument("action", choices=("ls", "add", "rm"))
    sp.add_argument("name", nargs="?")
    sp.add_argument("path", nargs="?")
    sp.add_argument("--rw", action="store_true", help="read-write (default: read-only)")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--vm", dest="for_vm", action="append", default=None, metavar="ID",
                    help="catalog add: apply to this VM only (repeatable); default: every VM")
    sp.add_argument("--yes-rw", action="store_true",
                    help="catalog add: confirm a read-write entry for every VM")
    sp.add_argument("--replace", action="store_true", help="catalog add: change the existing entry of that name")
    sp.set_defaults(func=_cmd_share)
    sp = v.add_parser("share-roots", help="the host folders shares may come from (daemon.vm_share_roots); empty "
                                          "means sharing is off. The home folder, ~/.ssh, ~/Library and the like "
                                          "are refused. Takes effect at the next share or start, no restart")
    sp.add_argument("action", choices=("ls", "add", "rm"))
    sp.add_argument("path", nargs="?")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=_cmd_share_roots)
    sp = v.add_parser("only", help="VM-only mode of a session: the host refuses builds, installs and program "
                                   "runs (Bash) and says to use vm_exec. A nudge, not a security boundary; "
                                   "takes effect on the session's next Bash call")
    sp.add_argument("action", choices=("on", "off", "ls"))
    sp.add_argument("session", nargs="?")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=_cmd_only)
    sp = v.add_parser("guard", help=argparse.SUPPRESS)      # the PreToolUse hook; not for people
    sp.set_defaults(func=_cmd_guard)
    sp = v.add_parser("requests", help="access requests sessions have made (vm_request) and not had answered")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=_cmd_requests)
    for name, text in (("approve", "grant a session's request: its scopes and shared folders, in one step"),
                       ("deny", "drop a session's request")):
        sp = v.add_parser(name, help=text)
        vm_arg(sp)
        sp.add_argument("session")
        sp.add_argument("--id", required=True, help="the request id 'claunch vm requests' shows; "
                        "refused when the session has asked again since")
        sp.add_argument("--json", action="store_true")
        sp.set_defaults(func=_cmd_approve)

    sp = v.add_parser("files", help="a VM's file exchange: the host folder, the guest work folder "
                                    "(--guest-dir sets it, \"\" restores the default) and recent transfers")
    vm_arg(sp)
    sp.add_argument("--guest-dir", default=None, metavar="PATH",
                    help="under /Users/<account>/<folder>, /private/tmp/<folder> or /tmp/<folder>")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=_cmd_files)

    pr = v.add_parser("preset", help="VM presets: a stopped copy to make new VMs from")
    ps = pr.add_subparsers(dest="preset_command", required=True)
    sp = ps.add_parser("list", help="the presets")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=_preset_list)
    sp = ps.add_parser("default", help="show, set or clear (--clear) the default preset a one-click new VM uses (user only)")
    sp.add_argument("name", nargs="?", default="")
    sp.add_argument("--clear", action="store_true")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=_preset_default)
    sp = ps.add_parser("save", help="copy a stopped VM into a preset (set the guest DNS to public "
                                    "servers first; the copy keeps the source's accounts and passwords)")
    vm_arg(sp)
    sp.add_argument("name")
    sp.add_argument("-d", "--description", default="")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=_preset_save)
    sp = ps.add_parser("describe", help="change a preset's description")
    sp.add_argument("name")
    sp.add_argument("description")
    sp.set_defaults(func=_preset_describe)
    sp = ps.add_parser("rm", help="PRESET delete: remove a preset and its tart copy preset-<name> (nothing else; deleting a VM is 'vm destroy')")
    sp.add_argument("name")
    sp.add_argument("--yes", action="store_true", help="confirm deleting the named preset")
    sp.set_defaults(func=_preset_rm)
    sp = ps.add_parser("new", help="clone a preset into a new VM, register it and start it "
                                   "(softnet on; the new VM has the source's admin password)")
    sp.add_argument("name")
    sp.add_argument("id", help="the new VM's id (also its tart name)")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=_preset_new)

    sp = v.add_parser("reconcile", help="make the records agree with what tart reports")
    sp.set_defaults(func=_cmd_reconcile)

    sp = v.add_parser("grant", help="let a session use a VM")
    vm_arg(sp)
    sp.add_argument("session")
    sp.add_argument("scopes", nargs="+", choices=SCOPES,
                    help="view: look; control: drive (implies view); exec: guest shell and "
                         "installs (off unless named; implies view); files: copy files between the "
                         "VM's host folder and its guest work folder (off unless named; implies "
                         "nothing)")
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
