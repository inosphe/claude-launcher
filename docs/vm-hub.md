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

Lifecycle buttons (start / resume, suspend, stop, force stop) show a press at once.
The request returns only when the guest has booted or tart reports the state, and the
list is polled every 2 s, so the page keeps its own per-VM record of the press
(`busy` in `vm.js`) from the click until the state the press asked for shows in the
list (`running` after start or resume, `suspended`, `stopped`), the request fails, or
120 s pass. Meanwhile the pressed button is disabled with a spinner and "시작 중…" /
"재개 중…" / "일시 중단 중…" / "종료 중…" / "강제 종료 중…", the other buttons of that VM and
the lock toggle are disabled (a second click sends nothing), and force stop stays
enabled except during a force stop, as the way out of a guest that does not answer. A
failed request restores the buttons and shows the error (for example `vm_busy_state`
from the daemon). After the timeout the buttons come back with "아직 반영되지 않았습니다.
상태를 확인하십시오". A VM the daemon reports in `starting`, `restoring`, `saving`,
`pausing` or `stopping` shows the same progress and locks even when this page did not
send the request (a page opened mid-transition, another tab, the CLI). The daemon is not
changed: its duplicate-request answers (`vm_busy_state`, `vm_bad_state`) are shown as
they are. Static files only; the daemon serves them from disk, no restart is needed.

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

### The default preset

One preset can be marked as the default; the one-click "new VM" in a session's
VM panel uses it. Only the user sets it: the page's preset card ("기본으로 지정"
/ "기본 해제", the chosen card shows a "기본" badge) or
`claunch vm preset default [NAME | --clear]` (no argument prints it). The route
is `POST /api/vm-presets-default {"name": ""}` (user only; a session gets 403
`vm_user_only`; an unknown name is 404 `vm_unknown_preset`; an empty name clears).
The choice is stored in `vm-presets.json` as `default`. When that preset is
deleted, or the file no longer lists it, the default is cleared and
`default_lost` carries the reason (shown under the preset list and by
`preset default` / `preset list`) until the user chooses again. A restart is
needed for the new route; the page and CLI changes are picked up on reload.

### A preset's copy is not a VM

The tart copy `preset-<name>` is the source of later clones; running it as a VM would change
what every new clone starts from.

- **Registration is refused.** `POST /api/vm` (and `claunch vm add`) with a tart name that is
  a recorded preset copy (`vm-presets.json`, `tart_name`), including the default tart name
  equal to the id, fails with `vm_preset_copy` (409) and a Korean reason that names the
  preset and points to "프리셋으로 새 VM 만들기" (`claunch vm preset new <preset> <id>`). The
  register form already listed such copies as not usable.
- **A copy registered before the rule** (e.g. by an earlier `vm add`) is a template: the
  user's `GET /api/vm` and `GET /api/vm/{id}` rows carry `template: "<preset>"` (`""` for an
  ordinary VM; sessions get no such field). The VM page marks it in purple: the list row says
  "프리셋 템플릿" (ordinary rows say "VM"), the detail has a badge and a note, and the
  lineage tree adds "프리셋 템플릿".
- **Starting a template needs confirmation.** `POST /api/vm/{id}/start` without
  `confirm_template: true` (only a JSON `true`) fails with `vm_template_confirm` (409); the
  page asks "템플릿을 시작하면 이후 복제본이 바뀝니다" and sends it, the CLI has
  `claunch vm start ID --yes-template`. Other lifecycle actions are not asked.
- **Granting a template** is allowed (the user decides), with a confirmation on the page and a
  `warning` in the grant response (the CLI prints it).
- **Forgetting it** (`claunch vm rm`, `DELETE /api/vm/{id}`) keeps working; it cannot be
  registered again.
- The daemon needs a restart for the refusal, the `template` field and the start
  confirmation; the page marking needs the page reloaded.

### Unregister and delete

Two different things, with different names everywhere (page, CLI, audit log):

- **Unregister (등록 해제)**: `claunch vm rm ID`, `DELETE /api/vm/{id}`, the page's "등록 해제 (tart
  VM은 남음)" button. The daemon forgets the VM (record, grants, requests, catalog entries). The
  tart VM and its disk image stay on the host and can be registered again. Allowed for
  stopped, suspended and fault VMs. Audit row `unregister` (with `tart_name`); the response says
  `tart_kept`.
- **Delete (물리적 삭제)**: `claunch vm destroy ID --confirm ID`, `POST /api/vm/{id}/destroy`
  `{"confirm": "<id>"}`, the page's red "위험 영역" section (its own box at the bottom of the VM
  detail). It runs `tart delete`: the disk image is gone, nothing can undo it.
  - User only (a session gets 403 `vm_user_only`, also for the preview).
  - `confirm` must repeat the VM id (as the preset delete repeats the preset name); otherwise
    `vm_confirm` (400). The page enables the button only once the id is typed.
  - Refused (`blockers`, nothing is deleted): VM not `stopped` (suspended, running, fault:
    `vm_bad_state`, 409); locked (`vm_locked`, 423: unlock first); a session holds a grant or
    the control (`vm_destroy_granted`, 409: revoke first); another registration uses the same
    tart VM (`vm_held`); tart reports it as not stopped; a delete of it is already running
    (`vm_busy_state`; a start is refused meanwhile).
  - Warned about, not blocked (`warnings`, shown with the typed confirmation): presets saved from
    this VM (their `preset-<name>` copies stay), VMs made from those presets (descendants; their
    disks are independent), the VM being a preset template (deleting it ends that preset's
    ability to clone), and tart no longer listing it (then only the record goes).
  - `GET /api/vm/{id}/destroy` is the daemon's own plan: `size_gb` / `disk_gb` as tart reports
    them, `blockers`, `warnings`, `presets`, `descendants`, `template`. The page and
    `claunch vm destroy ID` (without `--confirm`, which deletes nothing) show it.
  - Audit (`vm audit`, the registry's log, actor and time): `destroy` before the call
    (`tart_name`, `size_gb`, presets, descendants), then `destroyed`, or `destroy_failed` with
    tart's error. When `tart delete` fails the registration is kept and the error shown; it can
    be tried again. After a success the record, grants, pending requests and the VM's entries in
    the share catalog are gone.
- **Preset delete** (unchanged) is the third word: it deletes tart's `preset-<name>` copy and the
  preset record (page: the preset's "관리 ⋯" toggle opens a row with "프리셋 삭제", which stays disabled until the preset name is typed in full; the server also requires `confirm` to repeat the name; CLI: `claunch vm preset rm`), never a VM.
- The daemon needs a restart for the new route; the page needs a reload. Verified with the fake
  tart only: no real `tart delete` was run.

### Clone lineage

The VM page has a read-only "복제 계보" section (always on the page) that draws which preset
each VM was cloned from and which VM each preset was saved from, as an indented tree: for
example `agent-mac-01` → preset `base-xcode` → `agent-t01`, `agent-share-test`. Each VM
node shows its state and lock, each preset node its saved time, source VM and description
(and "tart 복사본 없음" when the preset copy is gone from tart).

- **Reading the page.** The section is titled "전체 복제 계보(모든 VM·프리셋)": it is every VM and
  preset, not the selected VM's. It is folded by default and shows one line for the selected VM;
  "전체 트리 펼치기" opens the whole tree, where the selected VM is highlighted ("◀ 현재 선택", bold,
  accent bar), its ancestors are underlined, and a long tree (the box scrolls) is scrolled to it.
  The same line, "계보: t01 ← 프리셋 base ← mac-01", is also under the VM's title on the detail.
  Where the records stop it says why and fills nothing: "(출처 기록 없음)" after a VM with no record,
  "t01: 출처 기록 없음(직접 등록했거나 이 기능 이전에 만든 VM, 추측하지 않음)" for a VM with none,
  "삭제된 프리셋 X(이 프리셋 이전의 계보는 알 수 없음)", "원본 VM X(등록 해제됨)", "(원본 기록 없음)"
  for a preset saved without a source, "X(프리셋 P의 복사본: 프리셋 템플릿, 일반 VM이 아님)" for a
  registered preset copy (then followed to the preset's source), and for a VM that has presets
  of its own " · 이 VM에서 만든 프리셋: gold → kid". It is drawn from the existing `cloned_from` and
  `presets` fields; no new field or route, so a page reload is enough (no daemon restart).
- **What is recorded.** `Presets.create_vm` stores on the new VM record
  `cloned_from = {kind: "preset", preset, preset_created_at, at}` (saved in `vms.json`,
  `Vm.cloned_from`); a preset already records its source VM and saved time in
  `vm-presets.json`. Nothing else is recorded.
- **What is not guessed.** A VM registered by hand, and every VM made before this field
  existed, has `cloned_from = {}` and is shown as a root with "출처 기록 없음". The tart image
  (OCI) a base VM was cloned from is not shown: `tart list --source local` does not report
  it. A malformed record reads as no record.
- **Deleted presets.** A VM whose preset is gone sits under "삭제된 프리셋 <name>에서 복제". The
  record also keeps the preset's saved time, so a preset deleted and saved again under the same
  name is not taken for the one the VM came from. A preset whose source VM is not registered
  any more is shown under "원본 VM <id> (등록 해제됨)".
- **API.** No new route: the user's `GET /api/vm` rows carry `cloned_from` and the response has
  `presets` (`name`, `source`, `created_at`, `description`; read without `tart list`, so it
  is cheap to poll). A session's rows have neither. The page is static plus these fields: the
  daemon needs a restart to send them (until then the tree shows every VM as having no record
  and no presets), and a VM made before the restart has no record.

## Published ports and allowed ranges (per VM)

Each VM record has two lists the user sets, from the panel's "포트 노출 (softnet)"
section or `claunch vm expose <id> list|add EXTERNAL:GUEST|rm EXTERNAL` and
`claunch vm allow <id> list|add CIDR|rm CIDR`. `tart run` gets them as
`--net-softnet-expose=` and `--net-softnet-allow=`, and only on a VM that runs on
softnet.

- **expose** `external:guest`: tart forwards TCP from the external port on the
  host's egress interface to the guest port. **Anyone on the host's LAN can
  connect to it.** The external port is 1024-65535, the guest port 1-65535, an
  external number belongs to one VM only, and a VM has at most 16. A number the
  host already listens on is not checked here: `tart run` fails to start and its
  reason is what the panel shows.
- **allow** (IPv4 CIDR): a range softnet passes inside the blocked private ranges.
  The longest prefix wins, so a `/32` such as the nginx host opens that address
  and the rest of 192.168.0.0/16 stays blocked. Per tart's documentation an
  address also has to be allowed for the machines that connect to an exposed port.
  The guest can reach every allowed address. The host firewall is not changed.
  Refused: IPv6, `0.0.0.0/0` and anything shorter than `/8`, a range that holds a
  whole private block (10/8, 172.16/12, 192.168/16), and a range that holds one of
  this host's own addresses (read from `ifconfig -a` when the range is saved and
  again before every start), because that would open the host's ports to the
  guest. `/32` addresses outside the host (for example 192.168.1.213 when the host
  is 192.168.1.89) pass.
- **When it applies**: at the next start. A running VM shows "다시 시작하면 적용됩니다"
  when the saved lists differ from what it was started with; a VM adopted after a
  daemon restart is not known to have been started with them. The daemon never
  restarts a VM for this.
- **Who**: the user only (routes `POST /api/vm/{id}/expose` and `/allow`, the
  panel, the CLI); a session gets `vm_user_only`, does not see the lists, and no
  MCP tool exists. Every change is audited with the declared actor. Values read
  from `vms.json` go through the same checks and an entry that fails is dropped,
  so a hand-edited file does not lift the isolation.
- A VM made from a preset starts with both lists empty; they are not copied.
- Not measured here: that tart honours both options together with
  `--net-softnet-block` as its help text says (s5's real-VM check).

A daemon restart is needed (routes and the `tart run` arguments).

## VM lock

A locked VM (`Vm.locked`, saved in `vms.json`) is one the daemon will not run
for anyone but the user at an unlocked moment: for administration only, unlock,
work, lock again. It is not the host screen lock of the section above; the panel
calls this one "VM 잠금".

- **Set**: `claunch vm lock|unlock <id>` or the panel's toggle (🔒/🔓, with a
  badge and, while locked, a disabled start button and the reason). Only the
  user (`POST /api/vm/{id}/lock` and `/unlock`; a session gets `vm_user_only`; no
  MCP tool); each change is audited with the declared actor. A VM can be locked
  only while `stopped`: a running or suspended one is answered
  `vm_lock_needs_stop` ("먼저 정상 종료한 뒤 잠그십시오") and is never stopped
  for it. Unlocking works in any state.
- **Refused while locked** (`vm_locked`, HTTP 423, Korean text): `start` and
  resume (so also a preset's new-VM start), `vm_acquire`, every `vm_*` call
  (`vm_cua`, `vm_tools`, `vm_exec`, the screenshot frame) for a session that has
  the grant, and the user's takeover. Not refused: listing and reading a VM,
  stop, force-stop, release, reconcile, removing the record, saving a preset
  from it (it is stopped). The daemon does not open a guest channel to a locked
  VM it finds running.
- **Found running**: `reconcile` (and so a daemon restart) records a locked VM
  that is up as `running`, writes `locked_but_running` to the audit log and the
  reconcile output, and the panel says so. Nothing stops it automatically.
- **Survives** a daemon restart. In `vms.json` only a JSON `true` is a lock;
  `"true"`, `1`, `"yes"` and the rest read as unlocked.
- **Limit**: the lock is the daemon's. A user or process that runs `tart run`
  itself is not stopped by it, and the daemon cannot tell that apart from its
  own VM beyond what `reconcile` finds.

A daemon restart is needed for the routes and the checks; the panel's static
files need a refresh.

## File transfer (`files` scope)

A session with a `files` grant copies files between two folders and nothing
else: the VM's **host folder** `~/.claude-launcher/daemon/vm/<id>/files/` (mode
0700) and the VM's **guest work folder** (default `/Users/<account>/work`).

- **Scope.** `files` is the fourth scope (view, control, exec, files). It is off
  by default, only the user grants or revokes it (panel tick or
  `claunch vm grant <id> <session> files`), and there is no extra dialog (exec
  keeps its own). It implies neither view, control nor exec, and none of them
  implies it; revoking `view` takes control and exec with it and leaves `files`.
  It touches no `tool_scope` entry and no `run_steps` check: those are for the
  cua tools, and the four file tools do not go through `vm_cua`.
- **Tools** (`vm_mcp.py`, thin clients of `POST /api/vm/{id}/files/{put,get,ls,rm}`):
  `vm_put` (host folder to guest), `vm_get` (guest to host folder, the result
  has `host_path`), `vm_ls` (name, size, kind of a guest folder) and `vm_rm`
  (needs `confirm` = the path again; never the work folder itself). There is no
  edit tool. `put`/`get` refuse to replace an existing item unless `overwrite`
  is true. A session finds the two folders in its `vm_list` row (`files`).
- **Per call** the registry checks the grant, the VM lock and that the guest agent
  answers (`running` and `ssh_ready`: file transfer needs no screen and no cua
  daemon, and no control, epoch or look-first; a user driving the screen does not
  block it), then writes an audit row with the op, path, byte and item count and
  the result, never contents. Rows are kept per VM (the last 50) for the panel.
  One transfer per VM at a time (`vm_files_busy`).
- **Host paths.** A session path is relative to the host folder. Refused
  (`vm_files_unsafe`): absolute paths, `..` and `.`, empty parts, backslash,
  control characters (Cc), line/paragraph separators, lone surrogates and the
  bidirectional marks (U+200E/F, U+202A-E, U+2066-9, U+061C), more than 16 levels
  or 512 characters, and names starting with `.claunch-` in any letter case
  (the daemon's own staging). Other Unicode is fine (NO-BREAK and NARROW NO-BREAK
  SPACE, ZWJ emoji, Hangul, combining marks). The folder is reached by file
  descriptors: the root is opened once, then each part with `openat`
  (`O_NOFOLLOW|O_DIRECTORY`), files with `O_NOFOLLOW`, and the final move is
  `renameat` between two such descriptors. There is no path that is checked once
  and used later, so a folder under `files/` swapped for a symbolic link while a
  transfer runs (up to 600 s) is not followed: the call fails.
- **Guest work folder.** Set by the user only (panel, or
  `claunch vm files <id> --guest-dir PATH`, route `POST /api/vm/{id}/files_dir`,
  audited as `files_dir`). It must be an absolute path of plain characters in
  canonical form under `/Users/<account>/<folder>`, `/private/tmp/<folder>` or
  `/tmp/<folder>`: not a home itself, no hidden folder, no `Library`, no space.
  The file load applies the same check.
- **Guest paths, and why this method.** Each call runs one `/bin/sh -c` script by
  `tart exec`, with the paths embedded through `shlex.quote`. The script resolves
  the work folder with `cd -P && pwd -P` (refusing a work folder that is itself a
  link), then walks the requested path one component at a time and refuses a
  symbolic link at any step. A path with no `..` and no link on the way lies inside
  the real work folder, so no `realpath` binary is needed and nothing depends on
  how the guest's PATH is set. `rm` may remove a link itself (never what it
  points to). Not covered: a guest process that swaps a folder for a link
  while a transfer runs; the guest's own user can already do anything in that
  folder.
- **Transport.** `tar` through `tart exec -i`, `/usr/bin/tar` by absolute path.
  - *put*: the host scans the source first (regular files and folders only; a
    symbolic link, socket or device refuses the whole put; limits are checked here,
    before anything is sent) and writes a tar with owner and group zeroed, group/
    other bits and setuid/setgid/sticky removed. The guest unpacks into a staging
    folder `.claunch-put.*` inside the work folder, checks that it holds exactly the
    one expected item, no link or special file and the announced number of entries,
    drops setuid/setgid again, and only then moves it into place. Any failure
    removes the staging folder (a trap), and a stream cut anywhere fails the count.
  - *get*: the guest refuses a source holding a link or special file, reports its
    entry count on stderr and writes the tar. The host never runs `tar -x` on it.
    `extract_tar` reads it member by member: only regular files and folders, names
    under the one expected top name, no `..`, absolute path, control character,
    duplicate or parent that is a file, `O_EXCL|O_NOFOLLOW` under descriptors, modes
    reduced to the owner's rwx (no setuid, no group/other). A `._name` member is
    dropped only when it starts with the AppleDouble header (the guest also sets
    `COPYFILE_DISABLE=1`); a real file of that name is kept. The
    result is renamed into the host folder only after the whole stream was read, the
    guest exited 0 and the entry count matched; a staging folder
    `.claunch-get.*` is removed on any failure (and swept after a day if the daemon
    died).
  - *Limits* (config `vm_files_max_mb` 1-8192, `vm_files_max_items` 1-100000,
    `vm_files_timeout` 10-3600; a value outside its range is logged and the
    default is used): 512 MiB, 10000 items and 600 s per request; over a limit the call is refused
    (`vm_files_limit`, 413) or stopped (`vm_timeout`) and nothing is left.
- **Received files are not executed.** Nothing in the daemon opens, runs or
  imports what it receives. Open them with care yourself: they come from a guest
  that a session may have been driving.
- **Panel.** The VM tab's "파일" section shows the host folder, the guest work
  folder setting (user only), the "not executed" text and the recent transfers
  since the daemon started.
- **Errors**: `vm_files_unsafe` 400, `vm_files_missing` 404 (also: the guest work
  folder does not exist yet for `ls`/`get`; `put` creates it), `vm_files_exists`
  409, `vm_files_busy` 409, `vm_files_limit` 413, `vm_files_failed` 502, plus
  `vm_no_grant`, `vm_locked`, `vm_not_ready`, `vm_unavailable`, `vm_timeout`.

Known limits (not fixed): a request whose HTTP client went away keeps its
transfer thread until the transfer ends or the time limit stops it; and
`overwrite` is two renames, not one atomic step (the old item is put back if the
second fails, but a crash between them leaves it in a `.claunch-get.*` staging
folder), and parent folders made for a `get` stay if a later step fails (they are
empty).

A daemon restart is needed (new routes, scope and tools); MCP sessions pick up
the four tools when they start.

Not verified without a real VM: that `tart exec -i` passes binary stdin and
stdout without alteration (and keeps stderr apart), that the guest's `bsdtar`
takes the options used here, and the timing of a 500 MiB transfer.

## Shared host folders (`shared_dirs`)

A VM can mount host folders through `tart run --dir name:path[:ro]`. The guest sees
them at `/Volumes/My Shared Files/<name>`. A session edits files at the host path;
the VM builds from a local copy made with `vm_exec` (`rsync -a --delete
'/Volumes/My Shared Files/<name>/' ~/work/<name>/`). Building or running git inside
the shared folder is slower by about 10x for small files, and swiftc reports
"modified during the build" for files written just before (run d-009).

- **Record.** `Vm.shared_dirs` is a list of `{name, host_path, mode}` (`ro` default,
  `rw` only when written), at most 4 per VM, unique names (`check_name`). Only the
  user changes it: dashboard VM tab, `claunch vm share add|rm|ls [--rw]`, or
  `POST /api/vm/{id}/share`. A session sees name, mode, host and guest path in
  `vm_list` / `vm_acquire` (`shared_dirs`, `share_guidance`) and cannot change them.
- **Allowed roots.** Daemon setting `vm_share_roots` (default empty: nothing can be
  shared; error `vm_share_no_roots`, 409). `host_path` is resolved with `realpath`
  and must lie under a root (`vm_share_outside_root`, 400), so a symbolic link out of
  a root is refused. A path that is, lies under or contains `~/.ssh`, `~/.gnupg`,
  `~/.aws`, `~/Library`, `~/.claude`, `~/.claude-launcher` or the daemon state
  directory is refused (`vm_share_forbidden`, 400); this keeps the home folder out.
  Paths with `:` or control characters are refused.
- **Editing the roots.** The user sees and edits `vm_share_roots` without touching the
  settings file: the "공유 허용 루트" section of the VM page (always visible; each root
  with its state or the reason it is not in effect; an empty list says sharing is off),
  `claunch vm share-roots ls|add PATH|rm PATH`, or `GET/POST /api/vm-share-roots`
  (`{"action":"add"|"rm","path":...}`; user only, a session gets 403 `vm_user_only`).
  `add` resolves the path with `realpath` and stores the resolved path; it refuses a
  path that is not an absolute folder, is `/`, contains `:`, or is the home folder or
  holds or lies under one of the never-shared folders (`vm_bad_request` /
  `vm_share_forbidden`, 400); at most 16 roots (`vm_share_limit`). Only the
  `daemon.vm_share_roots` key is written, the rest of the settings file is kept. The
  manager reads the setting afresh at every share and start (no restart for the value;
  the new route itself needs the daemon to have been restarted once). A stored root
  that has since gone bad (folder removed, now forbidden) stays listed with the reason
  and is ignored until removed. Removing a root does not unmount a running VM or drop
  its shares; the shares are checked again at the next start and left out if they are
  no longer under a root. Sessions never receive the root list.
- **Risk.** The guest can read everything in a shared folder (`.env` files and keys
  included), also with `ro`. With `rw` a program in the guest can plant `.git/hooks/*`
  or a script that the host session runs at its next `git` or build, which is why
  `ro` is the default. Share a working tree that holds no secrets.
- **Apply.** Mounted at the next start. A change on a running VM shows
  `shares_pending` until it restarts. The rules are applied again at every start;
  a share that fails then is left out and listed in `shares_skipped` (and audited).
  A locked VM takes no share changes and does not start.
- **One-step access request.** `vm_request(vm, scopes, shares, reason)` records a
  request (at most 10 per VM, not saved to disk, dropped when the session exits or
  the VM is removed). Nothing is granted. The user sees it in the VM tab or with
  `claunch vm requests`, and `approve` / `deny` (`POST /api/vm/{id}/approve|deny`,
  user only) grants the scopes and adds the shares together after re-checking them.
  Each request has an `id`; `approve` and `deny` must send the id they were shown
  with, and a session that asked again meanwhile (which replaces its request) gets
  `vm_request_changed` (409), so the user never approves content they did not see.
- **Where the user sees a request.** (1) The VM page shows pending requests in a
  bordered banner at the top, whatever VM is selected (count, session, VM, scopes, shared
  folders with mode, a warning for `exec` and for `rw`, the reason, approve / deny; hidden
  when none). (2) The dashboard's VM nav item shows the count. (3) The dashboard raises one
  sticky notification card per request id (source `vm`, "VM access requests" in the
  notification settings; the card names the session and a summary with paths cut to their
  last two folders, and opens `#/vm`). It goes through the dashboard's existing notice
  mechanism (`notify()` in `app.js`, the same as QA questions), fed by the light
  `GET /api/vm-requests` (user only, no host query), and it comes down when the request is
  approved, denied, withdrawn or its session ends; the history row stays. A browser that
  never kept the list announces the requests already pending on its first poll. A session
  that asks again replaces its request: the old card goes down and a new one goes up.
  Needs the dashboard page reloaded and the daemon restarted once for the route.
- **Session guidance.** The MCP server instructions point at the prompt pack
  `mcp-vm`; `vm_list` and `vm_acquire` return `share_guidance`.
- **Not verified with a real VM:** `--dir` together with `--suspendable`.

### Share catalog and where a share came from

The catalog (`vms.json` key `share_catalog`, the user's) lists folders shared with every
VM (`scope: all`) or with listed VMs (`scope: vms`, `vms: [ids]`). Entries are
`{name, host_path, mode, scope, vms, origin}`; the path rules are the ones above,
applied again at every start. At start a VM gets its own shares followed by the
catalog entries that apply to it, up to 4 in all (a fifth is left out with
`over_limit`); a catalog entry whose name the VM already uses is left out with
`name_conflict`, so the VM's own entry wins. Left-out entries and entries that fail the rules at start show
in `share_rows` with the reason. `shares_pending` also turns on when the catalog
changes what a running VM would get.

- **Origin.** Each share carries `origin {kind, by_session, at}`: `vm` (set by the user on
  that VM), `request` (a session asked and the user approved; `by_session` names it),
  `catalog-all`, `catalog-vms`. An approved share stays when the session ends; only a
  pending request is dropped at session exit.
- **Routes** (user only): `GET|POST /api/vm-share-catalog` (`put` with optional `replace`,
  `rm`). A read-write entry for all VMs needs `confirm_rw: true` (`vm_share_confirm_rw`
  otherwise): the page asks with a dialog, the CLI takes `--yes-rw`.
- **CLI.** `claunch vm share catalog ls | add NAME PATH [--rw [--yes-rw]] [--vm ID ...]
  [--replace] | rm NAME`; `claunch vm share <vm> ls` shows each share's origin and state.
- **Page.** The VM page shows, below the selected VM and whatever is selected, the catalog
  (add, edit, delete, scope), the VM x share matrix (mode and state per cell, the reason for
  a left-out share), the VMs waiting for a restart, and the pending requests of all VMs.
- **Sessions** see in `vm_list` / `vm_acquire` only the shares that are mounted (name, mode,
  host and guest path) and the guidance, not the origin or the catalog.

## VM-only mode (`daemon/vm_guard.py`)

A per-session switch, off by default. While it is on, the session's Bash calls that start a build
(make, xcodebuild, swift build, cargo/go build|test|run, npm run|test, pytest ...), install packages
(npm/pnpm/yarn/pip/uv/brew/gem/pod/cargo/go install ...) or run a program (python, node, `./script`,
open, osascript, uv run ...) are refused with a message that says to use `vm_exec`. `git`,
`claunch` and read commands pass. Chained commands, `bash -c '...'`, `$(...)` and leading
`VAR=1`, `sudo`, `env` are looked through. **This is a nudge, not a security boundary:** the rules are
token prefixes and are bypassed by a script file, an alias or an indirect call. A session can also
**delete its own `vm-only.json`** (it is in the session's own directory) and so switch itself off; the daemon does not
guard that file. Wrapper options that take an argument (`env -u X`, `sudo -u U`, `nice -n N`, `env -S '…'`) are skipped, not read as the command.

- **Mechanism.** Every Claude Code spawn carries `--settings` with a `PreToolUse` hook for `Bash`
  (`harness.REBRIEF_HOOK_SETTINGS`). Its command is `if [ -e "$CLAUNCH_SCRATCH/../vm-only.json" ]; then exec
  claunch vm guard; fi`, so claunch starts only when the session's switch file exists. The switch is that
  file (`<session dir>/vm-only.json`, 0600), written by the daemon; the hook reads it on every call, so the
  change applies to the session's next Bash call and works with the daemon down. `claunch vm guard`
  reads the tool call from stdin and prints a `permissionDecision: deny` with the reason. **Fail-open:**
  any error in the hook allows the call.
- **Who can switch.** Only a session started with the hook (the daemon records this at every spawn,
  `vm-guard-hook` in the session dir). Sessions started before this version, with caller-supplied
  `--settings`, or on another harness are refused with the reason; the page shows it. Other harnesses
  need their own adapter: the rules, the switch and the history are in `vm_guard.py` and the Claude hook
  is only the adapter.
- **History.** Each block appends `{at, kind, rule, head}` (the first three words of the part
  that matched, no later arguments) to `vm-only-blocks.jsonl`; the last 20 are shown on the page.
- **Routes** (user only): `GET|POST /api/vm-only` (`{session, on}`; 409 `vm_only_refused`).
  CLI: `claunch vm only on|off SESSION`, `claunch vm only ls`.
- **Page.** A "VM 전용 모드" section on the default screen lists the sessions that hold a VM grant with
  the switch, its state, why it cannot be turned on, and the recent blocks; each session row of the
  permissions table has the same switch.

