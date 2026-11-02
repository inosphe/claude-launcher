---
title: MCP — VMs you may use (vm_* tools)
description: How to get access to a macOS VM, shared host folders, file transfer, and what runs where.
---
## VMs (vm_* tools)

The daemon runs macOS VMs (Tart). The user decides which sessions may use which VM,
per scope: `view` (screenshots), `control` (drive the screen), `exec` (a guest
shell), `files` (copy files between a host folder and the guest work folder).
`vm_list` shows the VMs you have a grant on, your scopes, and for each shared
folder its host path, guest path and mode.

### Asking for access
`vm_request vm=<id> scopes=[...] shares=[{name, host_path, mode}] reason=...` asks
for scopes and shared folders **in one step**. It grants nothing; the request waits
in the user's VM panel and the user approves or denies it. Ask for what you need
and no more: `ro` unless you must write, and no `exec` if `files` or a shared
folder is enough. A share must lie under a root the user allowed
(`daemon.vm_share_roots`) and may never be the home folder, `~/.ssh`, `~/Library`,
`~/.claude*` or the daemon's state; the request is refused at once if it does.
A shared folder is mounted when the VM **next starts** (the panel says "restart
needed" while it is not).

### Shared folders: edit on the host, build in the guest
A shared folder appears in the guest at `/Volumes/My Shared Files/<name>`. Edit
the files at their **host path** with your normal tools. Run builds and `git` in
the guest on a **local copy** with `vm_exec`:

    rsync -a --delete '/Volumes/My Shared Files/<name>/' ~/work/<name>/
    cd ~/work/<name> && <build>

Do not build or use `git` inside the shared folder. Measured on this setup, many
small files are about 10 times slower there (3000 files: 1.9 s against 0.2 s;
`git worktree add` 12 times), and `swiftc` fails with "input file ... was modified
during the build" on files written a moment before. A `ro` share cannot be written
from the guest; bring results back with a `rw` share or `vm_get`. A symbolic link in
a shared folder shows its host target as text in the guest but is not followed.

### What a shared folder exposes
A shared folder is open to the guest, and the guest runs whatever is installed in it.
With `rw`, a program in the guest can plant a `.git/hooks/*` file or a script that
your host session then runs the next time it uses `git` or builds there; so `rw` is
asked for only when results must come back, and `ro` is the default. Even `ro` lets
the guest read everything in the folder, `.env` files and keys included. Ask to
share a working tree that holds no secrets, and read host-side files that came
back through an `rw` share before running them.

### Moving files without a shared folder
`vm_put` / `vm_get` / `vm_ls` / `vm_rm` (files grant) copy between the VM's host
exchange folder (`files.host_dir` in `vm_list`) and the guest work folder. Paths are
relative to those folders; nothing received is executed.

### Driving the screen
`vm_acquire` (control grant) gives you the VM and an epoch; look first with
`vm_screenshot`, then act with `vm_cua`. While the user is driving, calls are
refused with `vm_busy`; a locked VM refuses everything (`vm_locked`).

### Order of use
1. `vm_list`: which VMs you hold, with scopes, shared folders, `files` and lock state.
   Missing scope or folder: `vm_request` (the user approves), then wait.
2. Edit files at the host path (shared folder) or `vm_put` them.
3. `vm_acquire` (control/exec), then `vm_exec` for builds, installs and runs inside the
   guest; copy the shared folder to a guest-local directory first (see above).
4. `vm_get` or an `rw` share for results; `vm_release` when done.

### Limits
- The guest is on softnet with the private ranges blocked: it reaches the internet but
  not the host's LAN. Host ports are published only by the user (`expose`).
- A locked VM refuses every call (`vm_locked`); only the user unlocks it.
- The host's own `cua-driver` is not used for VM work; the screen is driven only
  through `vm_screenshot` and `vm_cua`.
- Do not run builds, package installs or the app on the host when a VM was given for
  that; use `vm_exec`.
