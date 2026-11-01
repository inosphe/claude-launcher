# VM hub (daemon/vmhub.py and vm_*.py)

The daemon runs Tart VMs, keeps one control channel to each, lets the user watch
and drive them from `/vm`, and lets chosen sessions look at or drive a VM through
the `vm_*` tools. Design and decisions: issue `vm-setup-8s6`.

## Network: softnet with the private ranges blocked

`tart run` gets `--net-softnet --net-softnet-block=10.0.0.0/8,172.16.0.0/12,192.168.0.0/16`
(setting `daemon.vm_softnet`, default on; the block list is `daemon.vm_softnet_block`).
With softnet the guest's gateway is the host, so without the block every port the
host listens on is open to the guest (d-005). With the block the guest reaches the
public internet only. Softnet picks a new random private /29 on each start, so the
block is by range, never by address.

Consequences:

- The host cannot ssh into the guest. The daemon does not use ssh: every guest
  action is `tart exec` (the Tart Guest Agent), and the cua-driver channel is
  `tart exec -i -- <vm> /Users/admin/.local/bin/cua-driver mcp`. `ssh_key`,
  `ssh_user` and `ip` stay in the record (`ssh_user` is the account logged in on
  the console); the daemon uses no key and no address.
- `tart exec` has a minimal PATH. Commands the daemon runs use absolute paths.
- Guest DNS must not be the gateway (it is blocked).

### Guest DNS (applied once per guest, by the user)

On the guest, as its admin account:

    networksetup -setdnsservers Ethernet 1.1.1.1 8.8.8.8

The daemon never changes guest settings. For the main VM `agent-mac-01` this is
done only after the user agrees. Check afterwards from the host:

    tart exec -- agent-mac-01 /usr/sbin/scutil --dns

(the first `nameserver` is a public address, not the gateway).

## Measured behaviour worth knowing (tart 2.41.1, macOS 26.2 host, clone t03)

- `tart exec` returns the command's own rc. Failures of the agent itself print
  `VM "<name>" is not running` (rc 2), `Error: unavailable (14): Transport became inactive`
  (the VM went away during the call) or `the specified VM "<name>" does not exist`;
  the daemon turns those into `vm_unavailable`.
- A foreground `sudo -n /sbin/shutdown -h now` through `tart exec` holds the call
  until the VM is gone (about 34 s) and ends rc 1 with the message above, so the
  daemon starts the shutdown in the background (`vm_tart.SHUTDOWN`) and waits on
  `tart list`.
- When the host side of `tart exec -i` ends (killed, or closed and terminated),
  the guest's `cua-driver mcp` stays. The channel ends the stale ones
  (`pkill -f '[c]ua-driver mcp'`) before it opens a new one.
- `screencapture` refuses to write a dot-file; the frame capture uses
  `/tmp/claunch-frame-$$.jpg`.

## Checkpoints: stop + cold boot, suspend only with an unlocked screen

The default way to keep a VM's state is a clean stop (`sync`, shutdown from
inside) and a cold boot. `suspend` is optional: resuming a suspended VM fails
with `VZErrorDomain Code=12 ... permission denied` while the host console
screen is locked, and succeeds on the same saved file after the unlock
(d-006, runs/d-006/investigation.md). Before a resume the daemon reads
`CGSSessionScreenIsLocked` from `ioreg -n Root -d1`. When it is set, `start`
does not run `tart run`, answers `vm_host_locked` (HTTP 423), and the VM stays
`suspended`; a retry after the unlock is safe. `GET /api/vm` carries
`host_locked`, and the `/vm` panel shows it and disables the resume button
while it holds.

## The dashboard tab

`#/vm` in the dashboard (left menu, "VM") shows the same panel as `/vm`: the tab
puts `/vm?embed=1` in a frame when it is opened and removes the frame when it is
left, which also ends the panel's refresh, screen and takeover-heartbeat loops.
Nothing in the daemon sets `X-Frame-Options` or a frame-blocking CSP on the
dashboard or the panel.

Registering a VM from the panel: "VM 등록" lists the VMs `tart list` reports on
this host (`GET /api/vm-tart`, the user's only; registered names and names the
registry would refuse are shown and not selectable). Registering posts the VM
(`POST /api/vm`) and then a reconcile, the same two steps as
`claunch vm add` and `claunch vm reconcile`. Sessions for a grant are picked from
the session list; when the list cannot be read the name is typed. The limit in
the panel (one token, actor declared and audited) applies to the new route too.

## Presets: new VMs by cloning

A preset is a stopped tart copy named `preset-<name>` plus a record in
`~/.claude-launcher/daemon/vm-presets.json` (0600: description, the VM it was
saved from, when, tart's reported size). Everything here is the user's: the
panel's "프리셋" section and `claunch vm preset save|list|describe|rm|new`; no
session tool and no session route (a session gets `vm_user_only`).

- **Save**: a registered VM that is `stopped` (shut it down first; running and
  suspended VMs are refused) is copied with `tart clone`. The copy is never
  registered as a VM, and the register form lists it as a preset copy.
- **New VM**: the name is checked (`_NAME_RE`), tart must not already have it,
  the disk must have as many GB free as tart reports the source uses (an APFS
  clone shares blocks at first, so this is the room the copy may grow into), then
  `tart clone` registers the VM with no grants and no controller and with softnet
  pinned on (`Vm.softnet = true`, so turning `vm_softnet` off does not open it),
  and it starts at once. If both macOS slots are in use it is left registered and
  stopped, with the reason in the answer. If the clone or the registration fails,
  the copy this call made is deleted and nothing else. If the start fails, the
  copy stays registered (the fault shows) and `claunch vm start` retries.
- **Delete**: the request must repeat the preset's name. Only `preset-<name>` is
  deleted, only when the record names it and no registered VM uses that tart
  name. A missing record file deletes nothing; an unreadable one is left as it
  is and every write is refused until it is fixed.

Limits to know before saving:

- The copy has the source's accounts and its admin password and keys. A preset
  changes nothing inside the guest; changing the password is not built.
- A new VM runs on softnet with the private ranges blocked, so the guest's
  gateway DNS does not answer. Set public DNS in the source guest before saving
  (`networksetup -setdnsservers Ethernet 1.1.1.1 8.8.8.8`); the panel says so and
  does not check it.

A daemon restart is needed for the preset routes and the register form's preset
marks (`/api/vm-presets`, `softnet` in the VM record). The panel's static files
need only a refresh; until the restart the panel says the daemon has no preset
API.
