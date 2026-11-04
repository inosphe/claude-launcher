"""MCP tools for the VMs the daemon runs (vm-setup-8s6, step 5).

Thin clients of ``/api/vm`` (daemon/vm_api.py). The user decides which
sessions may look at a VM and which may drive it (a grant per session and
scope: view, control, exec, files); only one session drives a VM at a time. Every
call carries ``$CLAUNCH_SESSION`` and the daemon refuses what the grants and
the control epoch do not allow.

The sessions never hold the connection to the guest: the daemon does, and
reconnects it when it drops. A call made while it is down answers
``vm_unavailable`` with ``retry_after`` instead of the tools going away.
"""

from __future__ import annotations

import os
import urllib.parse

from . import daemon_client, mcp_rpc

_VM = {"vm": {"type": "string", "description": "VM id (vm_list shows the ones you may use)"}}
_EPOCH = {"epoch": {"type": "integer",
                    "description": "from vm_acquire; omitted: the last one this session got"}}

INSTRUCTIONS = """\
VMs (vm_* tools): ask for access with vm_request; the user approves. Guidance:
prompt pack 'mcp-vm' (load it on demand, see Prompt packs).
"""

TOOLS = [
    {"name": "vm_list",
     "description": "The VMs you have a grant on: state, whether the guest channel is up, who "
                    "drives it, and your scopes (view, control, exec, files). With a files grant a VM "
                    "row also carries files.host_dir and files.guest_dir, the two folders vm_put "
                    "and vm_get use; shared_dirs lists host folders mounted in the guest (host_path, "
                    "guest_path, mode) with share_guidance.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "vm_acquire",
     "description": "Take control of a VM (needs a control grant from the user, and the user "
                    "not driving). One session controls a VM at a time: if another holds it you "
                    "get ok:false and held_by. Returns the epoch your control calls carry. Look "
                    "at the screen (vm_screenshot) before the first action.",
     "inputSchema": {"type": "object", "properties": _VM, "required": ["vm"]}},
    {"name": "vm_release",
     "description": "Give control of a VM back.",
     "inputSchema": {"type": "object", "properties": _VM, "required": ["vm"]}},
    {"name": "vm_screenshot",
     "description": "The guest's desktop state as cua reports it (screenshot and, as the driver "
                    "returns it, the accessibility tree). Needs a view grant. Images are saved "
                    "as files: the result gives their paths on this machine -- open one to look.",
     "inputSchema": {"type": "object", "properties": {**_VM, "arguments": {
         "type": "object", "description": "passed to cua's get_desktop_state"}},
         "required": ["vm"]}},
    {"name": "vm_cua",
     "description": "Call any cua-driver tool in the guest (vm_tools lists them with the scope "
                    "each needs). Tools that only look need a view grant, tools that install "
                    "or reconfigure need exec, any other needs control and an epoch from "
                    "vm_acquire. While the user drives the VM the call is refused with vm_busy: "
                    "wait, then look at the screen again.",
     "inputSchema": {"type": "object", "properties": {
         **_VM, **_EPOCH,
         "tool": {"type": "string"},
         "arguments": {"type": "object"},
         "timeout": {"type": "number", "description": "seconds, default 30"}},
         "required": ["vm", "tool"]}},
    {"name": "vm_tools",
     "description": "The cua-driver tools the guest offers and the scope each needs (view, control, exec).",
     "inputSchema": {"type": "object", "properties": _VM, "required": ["vm"]}},
    {"name": "vm_exec",
     "description": "Run a shell command in the guest as its admin user. Needs an exec grant "
                    "(off by default; the user gives it per session) and control. stdout and "
                    "stderr are cut at 64 KB each.",
     "inputSchema": {"type": "object", "properties": {
         **_VM, **_EPOCH, "command": {"type": "string"},
         "timeout": {"type": "number", "description": "seconds, default 60, at most 600"}},
         "required": ["vm", "command"]}},
    {"name": "vm_request",
     "description": "Ask the user for access to a VM in one step: scopes (view, control, exec, "
                    "files) and shared host folders (shares: name, host_path, mode ro|rw; ro "
                    "unless you need rw). Grants nothing by itself: the request waits in the "
                    "user's VM panel, and the user approves or denies it. A share must lie "
                    "under a root the user allowed (daemon.vm_share_roots) and never touches "
                    "~/.ssh, ~/Library or the daemon's state. A shared folder is mounted in the "
                    "guest at /Volumes/My Shared Files/<name> when the VM next starts. Asking "
                    "again replaces your earlier request.",
     "inputSchema": {"type": "object", "properties": {
         **_VM,
         "scopes": {"type": "array", "items": {"type": "string", "enum": ["view", "control", "exec", "files"]}},
         "shares": {"type": "array", "maxItems": 4, "items": {"type": "object", "properties": {
             "name": {"type": "string"}, "host_path": {"type": "string"},
             "mode": {"type": "string", "enum": ["ro", "rw"]}}, "required": ["name", "host_path"]}},
         "reason": {"type": "string", "description": "shown to the user next to the request"}},
         "required": ["vm"]}},
    {"name": "vm_put",
     "description": "Copy a file or folder from the VM's host exchange folder (files.host_dir in "
                    "vm_list; put the file there first) into the guest's work folder "
                    "(files.guest_dir). Needs a files grant (off by default; the user gives it). "
                    "Paths are relative to those folders: no absolute path, '..' or symbolic link. "
                    "Refuses to replace an existing item unless overwrite is true. Up to 512 MiB / "
                    "10000 items / 10 minutes per call. Nothing is executed.",
     "inputSchema": {"type": "object", "properties": {
         **_VM, "path": {"type": "string", "description": "relative to the host exchange folder"},
         "dest": {"type": "string", "description": "relative to the guest work folder; default: the name of path"},
         "overwrite": {"type": "boolean"}},
         "required": ["vm", "path"]}},
    {"name": "vm_get",
     "description": "Copy a file or folder from the guest's work folder into the VM's host "
                    "exchange folder; the result gives its host_path. Needs a files grant. Regular "
                    "files and folders only (a symbolic link or special file refuses the call); "
                    "setuid bits are dropped and the host never runs what it receives. Same "
                    "limits as vm_put.",
     "inputSchema": {"type": "object", "properties": {
         **_VM, "path": {"type": "string", "description": "relative to the guest work folder"},
         "dest": {"type": "string", "description": "relative to the host exchange folder; default: the name of path"},
         "overwrite": {"type": "boolean"}},
         "required": ["vm", "path"]}},
    {"name": "vm_ls",
     "description": "List a folder of the guest's work folder: name, size (files) and kind "
                    "(file, dir, symlink, other). Needs a files grant.",
     "inputSchema": {"type": "object", "properties": {
         **_VM, "path": {"type": "string", "description": "relative to the guest work folder; default: the folder itself"}},
         "required": ["vm"]}},
    {"name": "vm_rm",
     "description": "Delete a file or folder inside the guest's work folder (never the folder "
                    "itself). Needs a files grant, and confirm must repeat the path exactly.",
     "inputSchema": {"type": "object", "properties": {
         **_VM, "path": {"type": "string", "description": "relative to the guest work folder"},
         "confirm": {"type": "string", "description": "the same path again"}},
         "required": ["vm", "path", "confirm"]}},
]


#: A transfer may run for the daemon's whole limit (600 s by default).
FILES_WAIT = 660.0


class VmMcpError(Exception):
    pass


#: The epoch this session was last given for each VM.
_epochs = {}


def _client():
    client, why = daemon_client.connect_with_diagnosis()
    if client is None:
        raise VmMcpError(f"cannot reach the claunch daemon: {daemon_client.unreachable_reason(why)}")
    return client


def _session() -> str:
    session = os.environ.get("CLAUNCH_SESSION")
    if not session:
        raise VmMcpError("no $CLAUNCH_SESSION -- VM tools only work inside a managed claunch session")
    return session


def _vm(args: dict) -> str:
    vm = str(args.get("vm") or "")
    if not vm:
        raise VmMcpError("'vm' is required")
    return urllib.parse.quote(vm, safe="")


def _post(path: str, body: dict, timeout: float = 40.0) -> dict:
    try:
        return _client().post(path, body, timeout=timeout)
    except daemon_client.DaemonClientError as exc:
        # The daemon's refusals carry a code the session can act on.
        raise VmMcpError(str(exc)) from None


def _seconds(args: dict, default: float) -> float:
    raw = args.get("timeout")
    if raw in (None, ""):
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        raise VmMcpError("timeout must be a number of seconds") from None


def _epoch(vm: str, args: dict):
    return args.get("epoch") if args.get("epoch") is not None else _epochs.get(vm)


def call_tool(name: str, args: dict) -> dict:
    me = _session()
    args = args or {}
    if name == "vm_list":
        return _client().get("/api/vm?" + urllib.parse.urlencode({"session": me}))
    vm = _vm(args)
    if name == "vm_acquire":
        out = _post(f"/api/vm/{vm}/acquire", {"session": me})
        if out.get("ok"):
            _epochs[vm] = out["epoch"]
        return out
    if name == "vm_release":
        _epochs.pop(vm, None)
        return _post(f"/api/vm/{vm}/release", {"session": me})
    if name == "vm_tools":
        return _client().get(f"/api/vm/{vm}/tools?" + urllib.parse.urlencode({"session": me}))
    if name == "vm_screenshot":
        return _post(f"/api/vm/{vm}/call", {
            "session": me, "tool": "get_desktop_state",
            "arguments": args.get("arguments") or {}})
    if name == "vm_cua":
        timeout = _seconds(args, 30)
        return _post(f"/api/vm/{vm}/call", {
            "session": me, "tool": args.get("tool"), "arguments": args.get("arguments") or {},
            "epoch": _epoch(vm, args), "timeout": timeout}, timeout + 10)
    if name == "vm_exec":
        timeout = _seconds(args, 60)
        return _post(f"/api/vm/{vm}/exec", {
            "session": me, "command": args.get("command"),
            "epoch": _epoch(vm, args), "timeout": timeout}, timeout + 10)
    if name == "vm_request":
        return _post(f"/api/vm/{vm}/request", {
            "session": me, "scopes": args.get("scopes") or [], "shares": args.get("shares") or [],
            "reason": args.get("reason") or ""})
    if name in ("vm_put", "vm_get"):
        return _post(f"/api/vm/{vm}/files/{name[3:]}", {
            "session": me, "path": args.get("path"), "dest": args.get("dest"),
            "overwrite": args.get("overwrite") is True}, FILES_WAIT)
    if name == "vm_ls":
        return _post(f"/api/vm/{vm}/files/ls", {"session": me, "path": args.get("path")}, 90)
    if name == "vm_rm":
        return _post(f"/api/vm/{vm}/files/rm", {
            "session": me, "path": args.get("path"), "confirm": args.get("confirm")}, 90)
    raise VmMcpError(f"unknown tool {name!r}")


SERVER = mcp_rpc.Server(
    name="claunch-vm",
    tools=tuple(TOOLS),
    dispatch=call_tool,
    errors=(VmMcpError, daemon_client.DaemonClientError),
    instructions=INSTRUCTIONS,
)
