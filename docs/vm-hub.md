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
