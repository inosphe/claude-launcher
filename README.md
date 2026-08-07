# claude-launcher

`claude-launcher` (command: `claunch`) runs [Claude Code](https://claude.com/claude-code)
under **multiple isolated profiles**. Each profile owns its own login and
configuration by pointing `CLAUDE_CONFIG_DIR` at a dedicated directory.

Logging in uses `claude setup-token` (a long-lived OAuth token) instead of the
interactive `/login` flow, so each profile keeps its own credentials.

**Contents:** [Install](#install) · [Quick start](#quick-start) ·
[Commands](#commands) · [Login & tokens](#login--tokens) ·
[Seeding](#seeding-skip-onboarding) ·
[Env vars](#per-profile-environment-variables) ·
[Inheritance](#inheritance-parent-profiles) ·
[Providers](#api-providers-third-party-backends) ·
[Migrate](#migrating-skills--mcp-servers) ·
[Config file](#configuration-source-of-truth) · [Usage](#usage-reporting) ·
[Sessions](#managed-sessions-tmux-style-daemon) · [Web UI & API](#web-ui--http-api) ·
[Workflows](#cflow-declarative-agent-workflows) ·
[How it works](#how-it-works) · [Configuration](#configuration)

## Why

By default Claude Code keeps credentials and settings under a single config
directory. If you switch between accounts (personal vs. work, or multiple Max
subscriptions), they collide. `claunch` gives every profile its own
`CLAUDE_CONFIG_DIR`, so tokens and settings never mix.

## Install

```bash
uv tool install claude-launcher
# or, from a local checkout:
uv tool install .
```

This puts `claunch` on your PATH. The `claude` CLI must already be installed.

### Development / live patching

Install editable so the tool imports straight from this repo instead of a copy:

```bash
uv tool install --force --editable .
```

Or skip uv's tool venv entirely and put a shim on PATH that runs the launcher
from this checkout (Windows):

```powershell
pwsh -File stubs\install-shim.ps1
```

That renders [`stubs/claunch.bat`](stubs/claunch.bat) into `~\.local\bin\claunch.bat`
with this repo's path baked in, so every `claunch ...` becomes
`uv run --project <repo> claunch ...`. Nothing is copied and there is no second
environment to keep in sync — handy when an editable tool install has drifted or
broken. Useful flags:

| Flag | Effect |
| --- | --- |
| `-BinDir <dir>`     | Install somewhere other than `~\.local\bin` (or set `CLAUNCH_BIN_DIR`). |
| `-NoSync`           | Bake `--no-sync` in for a faster start; run `uv sync` yourself when deps change. |
| `-AddToPath`        | Append the bin directory to the persisted user PATH. |
| `-Force`            | Overwrite a foreign `claunch.bat`, and delete a `claunch.exe` that would shadow it. |
| `-Uninstall`        | Remove the shim. |

Set `CLAUNCH_PROJECT` in your environment to point the installed shim at a
different checkout without reinstalling.

Either way, source edits take effect on the **next** `claunch` invocation — no reinstall.
Because nothing is copied into uv's tool venv, the source files are never locked,
so you can patch the launcher **while a `claunch run` session is active**. The
running session keeps the code it started with (Python loads modules into memory
at launch); the patch applies to the next command you run. The `claude`
subprocess is independent of the launcher, so editing launcher code never
disturbs a live session.

## Quick start

```bash
claunch create work     # create a profile (seeds your global config)
claunch login work      # log in via `claude setup-token`
claunch run work        # launch Claude Code as that profile
claunch validate work   # confirm the login works (claude -p heartbeat)
claunch usage work      # show this profile's subscription usage
```

## Commands

| Command | Description |
| ------- | ----------- |
| `create <name>`        | Create a profile (`--parent` to inherit), seed config, apply template. |
| `login <name>`         | Run `claude setup-token` for the profile. |
| `run <name> [args...]` | Launch `claude` for the profile (`--borrow NAME`, `--provider NAME`, `--add-prompt`; extra args pass through). |
| `env <name> [...]`     | View/edit the profile's env vars (`--effective` for merged). |
| `parent <name> [p]`    | Show, set, or `--clear` a profile's parent. |
| `template [--init]`    | Show or write the default env template. |
| `migrate <name> [src]` | Copy skills/MCP servers from a global or local path. |
| `prune [--dry-run]`    | Delete local profile dirs not declared in `~/.claunch.yaml`. |
| `validate [name]`      | Health-check logins via `claude -p heartbeat` (all if no name). |
| `usage <name>`         | Query subscription usage (`--json` for the raw response). |
| `set-provider [p] <provider>` | Pin a provider globally or per profile (`--clear` to inherit). |
| `providers`            | List API providers from the config file and the active one. |
| `set-token <name> [t]` | Store a token manually (pasted, or piped via stdin). |
| `get-token <name>`     | Print the profile's OAuth token (resolves inheritance; `--own`). |
| `list`                 | List profiles and each login's state (alias: `ls`). |
| `path <name>`          | Print the profile's `CLAUDE_CONFIG_DIR`. |
| `remove <name>`        | Delete a profile and its tokens (aliases: `delete`, `rm`). |

Plus the **[managed-session commands](#managed-sessions-tmux-style-daemon)** —
`new-session`, `attach`, `sessions`, `send-keys`, `capture-pane`, `wait-for`,
`kill-session`, `resize`, `daemon ...`, `web` — which run harnesses in
daemon-owned PTYs instead of the current terminal, and the
**[cflow commands](#cflow-declarative-agent-workflows)** (`claunch cflow ...`)
for declarative agent workflows with human gates.

### Passing arguments to claude

Anything after the profile name on `run` is forwarded to `claude` as-is — no `--`
separator needed:

```bash
claunch run work --resume
claunch run work --teammate-mode
claunch run work -p "summarize this repo" --model opus
```

Use a leading `--` only if an argument would otherwise be read by `claunch`
itself (e.g. `claunch run work -- --help` to show claude's help).

### Appending context to the system prompt

`--add-prompt` opens your editor (`$VISUAL`/`$EDITOR`, or Notepad/vi) so you can
type multi-line context for a single run. What you save is forwarded to
`claude --append-system-prompt`, so it is **appended** to Claude Code's built-in
system prompt (it does not replace it, and it is separate from `CLAUDE.md`):

```bash
claunch run work --add-prompt
claunch run work --add-prompt --resume   # other args still pass through
```

Everything from the `# ---- >8 ----` scissors line down in the editor is
ignored, so Markdown `#` headings in your text are preserved. Save an empty body
to launch without adding anything. To forward a literal `--add-prompt` to
claude, put it after `--`.

### Borrowing another profile's token

Run a profile but authenticate with **another profile's** login, just for that
run — the running profile's config dir, env and skills are unchanged, only the
token is swapped:

```bash
claunch run company --borrow company2
claunch run company --borrow company2 --resume   # extra args still pass through
```

Nothing is persisted: it only sets `CLAUDE_CODE_OAUTH_TOKEN` from the borrowed
profile for this one launch. The borrowed profile must have a token (its own or
inherited). To forward a literal `--borrow` to claude, put it after `--`.

`--borrow` also borrows the lender's **[provider](#api-providers-third-party-backends)**:
if `company2` is configured to use a third-party backend, `--borrow company2`
adopts that backend (base URL, model overrides and its auth) for the run — so a
borrowed provider profile needs no Anthropic OAuth token of its own.

## Login & tokens

`claude setup-token` runs an interactive flow (it renders a full-screen TUI), so
`claunch login` hands the terminal straight to it — no output is intercepted.
When it finishes, the login is stored inside the profile's `CLAUDE_CONFIG_DIR`,
and `claunch run` uses it automatically.

`setup-token` is meant for non-interactive use via the `CLAUDE_CODE_OAUTH_TOKEN`
environment variable. If a run prints a token instead of persisting a login,
store it once and `claunch run` will inject it for you:

```bash
claunch set-token work sk-ant-oat01-...   # or omit the value to paste via stdin
```

The token is saved at `<profile>/.launcher-token` (`0600`) and exported as
`CLAUDE_CODE_OAUTH_TOKEN` on `claunch run`.

`get-token` prints it back out on stdout — the value alone, so it pipes cleanly:

```bash
claunch get-token work                       # resolves inheritance (own, then a parent's)
claunch get-token work --own                 # only the profile's own token, no inheriting
export CLAUDE_CODE_OAUTH_TOKEN="$(claunch get-token work)"
```

`claunch list` shows each profile's login state — `[logged in]`, `[token
expired]` (a `.credentials.json` past its `expiresAt`), or `[no token]`:

```text
work       [logged in    ]  .../profiles/work
personal   [no token     ]  .../profiles/personal
```

To check that a login actually works (not just that a token exists), run a live
heartbeat:

```bash
claunch validate work    # one profile
claunch validate         # all profiles
```

`validate` runs `claude -p "heartbeat"` for each profile (with its config, env
and token) and reports `OK` with a snippet of the reply, or `FAIL` with the
reason; it exits non-zero if any profile fails. Profiles without a token fail
fast without calling the API. Tune with `--prompt` and `--timeout`.

## Seeding (skip onboarding)

A profile is a fresh `CLAUDE_CONFIG_DIR`, so Claude Code would replay onboarding /
landing on first run. To avoid that, `claunch create` copies your global config
into the new profile — carrying over the onboarding flags
(`hasCompletedOnboarding` etc.), UI preferences and `settings.json`, while
**stripping** account- and project-specific data (`oauthAccount`, `projects`,
cached API-key responses) so profiles stay isolated. The `settings.json` `env`
block is also stripped — launcher env is owned by `~/.claunch.yaml`, and new
profiles get their defaults from the [template](#default-template), not from your
global env. Each profile still logs in with its own setup-token.

```bash
claunch create work                 # seed from CLAUDE_CONFIG_DIR or ~/.claude
claunch create work --seed-from DIR # seed from a specific config dir
claunch create work --no-seed       # start fully fresh (onboarding will run)
```

## Per-profile environment variables

Each profile can set Claude Code environment variables. They live in the central
config file (`~/.claunch.yaml`, the launcher's [source of truth](#configuration-source-of-truth)),
and `claunch run` exports them into claude's process, so they take effect
immediately and **override** any value inherited from your shell.

```bash
claunch env work                                  # list this profile's env vars
claunch env work CLAUDE_CODE_AUTO_COMPACT_WINDOW=200000   # set one or more
claunch env work --unset FOO BAR                  # remove vars
claunch env work --apply-template                 # merge the template defaults
```

### Default template

New profiles get a default env block from the `template` section of
`~/.claunch.yaml`. On a brand-new install that file is created from a bootstrap
seed, `<launcher home>/template.yaml`, whose built-in defaults are:

```yaml
template:
  env:
    CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS: "0"
    CLAUDE_CODE_AUTO_COMPACT_WINDOW: "400000"
```

`template.yaml` only *seeds* `~/.claunch.yaml` the first time; afterwards the
live `template` block in `~/.claunch.yaml` is authoritative (edit it directly, or
run `claunch template --init` to write the bootstrap seed). **Existing profiles
are not changed automatically** — apply the current defaults to one with:

```bash
claunch env <name> --apply-template
```

## Inheritance (parent profiles)

A profile can inherit from a **parent**, so you can build a base profile once and
spin off variants. Children inherit the parent's `env` (child keys win) and its
login token (when the child has none of its own) — log in once on the parent and
share it across working profiles.

```bash
claunch create company                       # base profile
claunch login company                        # log in once
claunch env company COMPANY_REGION=eu        # base env

claunch create company_work --parent company    # inherits env + login
claunch create company_review --parent company
claunch env company_work CLAUDE_CODE_AUTO_COMPACT_WINDOW=200000   # override

claunch parent company_work          # show parent / chain
claunch env company_work --effective # env actually used (merged)
```

`claunch list` marks children with `[inherited]` and their parent. A profile with
no token of its own resolves to the nearest ancestor that has one, so
`run`/`validate`/`usage` all work on children. (For a shared login, log the
parent in with `setup-token` — those tokens are long-lived.) Cycles and missing
parents are rejected. Use `claunch parent <name> <parent>` to re-parent an
existing profile or `--clear` to detach it.

**What inheritance covers.** `env` and the login token are resolved live at
launch, so changing them on a parent affects children immediately. **Skills and
MCP servers are *files* in each profile's own config dir**, which Claude Code
reads from a single `CLAUDE_CONFIG_DIR` — they can't be merged live, so they are
*copied*: `create --parent` copies the parent's skills + MCP into the new child,
and `claunch migrate <parent> --recursive` re-copies into the parent and every
descendant when you add more later.

| Inherited live (env, token) | Copied point-in-time (skills, MCP) |
| --------------------------- | ---------------------------------- |
| change parent → children see it next run | `create --parent` seeds from parent |
| `env --effective` shows the merge | `migrate <parent> --recursive` re-syncs the tree |

## API providers (third-party backends)

A **provider** points Claude Code at a particular API backend — Anthropic by
default, or a third party such as a GLM endpoint — by supplying a bundle of
environment variables (an `ANTHROPIC_BASE_URL`, model overrides and an auth
token). Providers are defined and selected **in the config file**
(`~/.claunch.yaml`, the launcher's [source of truth](#configuration-source-of-truth)),
which the launcher reads live at launch. You can edit that file directly, or use
`set-provider` (below), which just records the selection in it.

```yaml
providers:
  fireworks-glm5p2:
    env:
      ANTHROPIC_BASE_URL: "https://api.fireworks.ai/inference"
      ANTHROPIC_MODEL: "accounts/fireworks/models/glm-5p2"
      ANTHROPIC_DEFAULT_OPUS_MODEL: "accounts/fireworks/models/glm-5p2"
      ANTHROPIC_DEFAULT_SONNET_MODEL: "accounts/fireworks/models/glm-5p2"
      ANTHROPIC_DEFAULT_HAIKU_MODEL: "accounts/fireworks/models/glm-5p2"
      CLAUDE_CODE_SUBAGENT_MODEL: "accounts/fireworks/models/glm-5p2"
      ANTHROPIC_API_KEY: ""
      ANTHROPIC_AUTH_TOKEN: "fw_..."
      CLAUDE_CODE_OAUTH_TOKEN: ""

provider: fireworks-glm5p2     # use it for every profile by default (optional)

profiles:
  work:
    provider: fireworks-glm5p2  # ...or per profile (overrides the global one)
  personal:
    provider: default           # pin one profile back to plain Anthropic
```

**Selecting a provider.** The effective provider for a run is the first of:
the profile's own `provider`, an ancestor's (inheritance, like `env`), the
top-level `provider`, then the built-in `default`. Selecting `default` on a
profile is itself a choice — it **pins** that profile to plain Anthropic even
when a global or inherited provider is set (the `personal` example above). The
built-in `default` is plain Anthropic with no overrides — the launcher injects
the profile's OAuth token as usual. For any other provider the launcher applies its `env` as a
**low-priority backend default** — above the shell but *below* the profile's own
`env`, so a per-profile (or template/inherited) value always wins over the
provider for the same key. The provider carries its own auth, so the launcher
does **not** inject `CLAUDE_CODE_OAUTH_TOKEN` — supply the backend key with
`claunch set-token` (recommended; see *keeping backend keys out of the config
file* below) or as a plaintext `ANTHROPIC_AUTH_TOKEN` in the provider's `env`
(clearing `CLAUDE_CODE_OAUTH_TOKEN`/`ANTHROPIC_API_KEY` as above).

The resulting precedence for a run is: shell env < provider `env` < profile `env`
(template + inherited + own) < the injected OAuth token (for `default`).

**Keeping backend keys out of the config file.** Whenever a **non-default
provider is active** for the run (selected on the profile, inherited, the
global default, or forced with `run --provider`), the launcher looks up the
profile's **stored token** — the `set-token` value in the per-machine `0600`
`.launcher-token` file, resolved like a login token (own first, then inherited
from a parent; `--borrow` uses the lender's) — and injects it as
`ANTHROPIC_AUTH_TOKEN`, **overriding** any plaintext value in the yaml. So a
provider needs no secret in the file at all:

```yaml
providers:
  fireworks-glm5p2:
    env:
      ANTHROPIC_BASE_URL: "https://api.fireworks.ai/inference"
      ANTHROPIC_MODEL: "accounts/fireworks/models/glm-5p2"
      # no ANTHROPIC_AUTH_TOKEN here — supplied by set-token per machine
```

```bash
claunch set-provider work fireworks-glm5p2
claunch set-token work fw_...        # the backend API key, stored 0600
claunch run work
```

A plaintext `ANTHROPIC_AUTH_TOKEN` in the yaml still works when the profile has
no stored token (backwards compatible), but the stored token always wins when
both exist. The trigger is the **provider selection itself** — env vars like
`ANTHROPIC_BASE_URL` set in a profile's `env` (or inherited from the shell)
don't change auth handling on their own. `run` tells you when this happens:

```text
provider 'fireworks-glm5p2' active (set on profile 'work'); auth: stored set-token exported as ANTHROPIC_AUTH_TOKEN
```

**Selecting from the CLI.** `set-provider` writes the selection into the config
file for you — no manual YAML editing needed:

```bash
claunch set-provider fireworks-glm5p2        # global default (top-level provider:)
claunch set-provider work fireworks-glm5p2   # just the 'work' profile
claunch set-provider work default            # pin 'work' to plain Anthropic
claunch set-provider work --clear            # drop 'work's override (inherit)
claunch set-provider --clear                 # clear the global default
```

For a **single run**, override the resolution without touching the config file
(`default` works too, to force plain Anthropic for one run):

```bash
claunch run work --provider fireworks-glm5p2
claunch run work --provider default --resume     # other args still pass through
```

`run`/`validate` use the provider; **`login` always targets Anthropic** (it never
applies a provider, so `claude setup-token` keeps working). Inspect what's
configured with:

```bash
claunch providers
```

```text
config file: /home/you/.claunch.yaml
global provider: default
available providers:
  default
  fireworks-glm5p2  -> https://api.fireworks.ai/inference
profiles using a provider:
  work                 fireworks-glm5p2
```

> **Secrets.** Prefer keeping backend keys **out** of `~/.claunch.yaml` via
> `set-token` (above) — the file is meant to be copied between machines. If you
> do put an `ANTHROPIC_AUTH_TOKEN` in a provider's `env`, it is plaintext:
> treat the file as a secret when committing or copying it.

## Migrating skills & MCP servers

Seeding copies the global `settings.json`, so the MCP servers defined there come
along — but **skills live in a separate `skills/` directory** and **project/local
MCP servers live outside `settings.json`**, so they aren't seeded. `claunch
migrate` pulls those into a profile from any source path:

```bash
claunch migrate work                 # from ~/.claude (global skills + MCP)
claunch migrate work ./my-project    # from a project's .claude/ and .mcp.json
claunch migrate work --mcp           # MCP servers only (--skills for skills only)
claunch migrate work --plugins       # also copy the plugins/ directory
claunch migrate company --recursive  # also into every child profile (see Inheritance)
claunch migrate work --dry-run       # preview without copying
```

The source may be a Claude config dir (`~/.claude`, or another profile via
`claunch path <name>`) or a project directory. Skills are merged into the
profile's `skills/`; MCP servers are gathered from `settings.json`,
`settings.local.json`, `.claude.json` and a project-root `.mcp.json`, then merged
into the profile's `settings.json`. Default migrates skills + MCP; pass `--skills`
or `--mcp` to narrow it.

## Configuration source of truth

Every launcher-managed setting lives in **one file, `~/.claunch.yaml`**, which
the launcher reads live at launch — there is no separate "export" step, because
this file *is* the state. It holds the profile list, each profile's `env`,
`parent` and `provider`, the default `template`, and any provider definitions:

```yaml
version: 1
template:
  env:
    CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS: "0"
    CLAUDE_CODE_AUTO_COMPACT_WINDOW: "400000"
profiles:
  company:
    env:
      COMPANY_REGION: "eu"
  company_work:
    parent: company
    env:
      CLAUDE_CODE_AUTO_COMPACT_WINDOW: "200000"
  personal:
    env: {}
```

A profile **exists** when its directory exists; this file holds the config
attached to it. Commands write here as you go (`env`, `parent`, `set-provider`,
`create`, `remove`), and on every run the launcher reconciles: it **materializes**
any profile the file declares but whose directory is missing (creating and
seeding it), so a config copied to a new machine just works — no import command.

```bash
cp ~/.claunch.yaml  /backups/                 # back it up / version it / copy it
# on the new machine, drop it in place; the next command creates the dirs:
claunch list
claunch login work                            # tokens are per-machine (below)
```

**Login tokens are never stored here** — they are secrets, kept per-profile and
per-machine, so run `claunch login` on each machine. Provider auth tokens *are*
in this file (see the [secrets note](#api-providers-third-party-backends)).
Override the file's path with `CLAUDE_LAUNCHER_SYNC_FILE`.

**Pruning.** Reconciliation only ever *creates* directories. To delete local
profile directories that the file no longer lists (the destructive direction),
run it explicitly:

```bash
claunch prune --dry-run        # show orphan dirs (not declared in ~/.claunch.yaml)
claunch prune                  # delete them
```

## Managed sessions (tmux-style daemon)

`claunch` can run harnesses (`claude` first; any CLI agent via config) inside
**daemon-managed PTY sessions** — a tmux-server equivalent that also works on
Windows (ConPTY). Sessions belong to a background daemon, so they survive the
terminal that created them, can be driven programmatically (`send-keys` /
`capture-pane` / `wait-for`), and are viewable live in the browser.

```bash
claunch new-session -s work --profile work     # daemon auto-starts, claude spawns in a PTY
claunch send-keys work "fix the failing test" Enter
claunch wait-for work --idle --timeout 600     # block until claude stops producing output
claunch capture-pane work                      # print the rendered screen
claunch attach work                            # take over interactively (Ctrl+] detaches)
claunch sessions                               # list sessions + status
claunch kill-session work
```

`attach` is the tmux moment: your terminal goes raw and mirrors the session
1:1 — keystrokes go to the PTY, output paints locally, and the session resizes
to (and follows) your terminal. `Ctrl+]` detaches; the session keeps running
in the daemon, and you can reattach later from any terminal (or watch the same
session in the browser at the same time — viewers are just subscribers).
`new-session --attach` (`-a`) creates a session and drops you straight into
it, so `claunch new -a --profile work` feels like plain `claude` — except the
session survives closing the terminal.

While attached, `Ctrl+C` (and everything else) goes to the program inside,
exactly like tmux/ssh — so hitting it twice quits *claude itself*, ending the
session. That's not the attach killing anything, and it isn't fatal either:
`claunch respawn <name>` relaunches the session with `--resume` of its pinned
conversation, picking up where it left off (the web UI's **resume** button on
an exited session does the same). `Ctrl+]` is the one key the
bridge keeps for itself — chosen precisely because nothing else uses it.

That `send-keys → wait-for → capture-pane` triple closes the automation loop:
external scripts (or another agent) can drive interactive claude sessions
without a human at the keyboard.

### Session commands

| Command | Description |
| ------- | ----------- |
| `new-session` (`new`) | Spawn a harness in a managed PTY (`-s NAME`, `--profile P`, `--harness H`, `-c CWD`, `--cols/--rows`, `--env K=V`, `--restore/--no-restore`, `-a/--attach` to attach immediately, trailing args pass to the harness). |
| `sessions` (`lss`)    | List sessions: name, status (`starting/busy/idle/exited`), harness, profile, size, cwd. |
| `attach [S]` (`a`, `attach-session`) | Mirror a session into this terminal, tmux-style; detach with `Ctrl+]` (session keeps running). Omit `S` when exactly one session is running. `-t S` also accepted. |
| `respawn S [-a]`      | Relaunch an exited session under its own name — claude comes back with `--resume` of its pinned conversation, so quitting it by accident (double `Ctrl+C` while attached) is recoverable. `-a` attaches right away. Also a **resume** button in the [web UI](#web-ui--http-api). |
| `send-keys [-l] S KEYS...` | tmux semantics: `Enter`, `Escape`, `Tab`, `C-c`, `M-x`, `Up`... are keys; everything else is literal text. `-l` sends all args literally. `-t S` also accepted. |
| `capture-pane S`      | Print the current rendered screen (`--history` for scrolled-off lines, `--json` for lines + cursor + status). |
| `wait-for S`          | Block until `--idle` (default) or `--exited`; `--timeout SECS`, `--idle-threshold SECS`. Exits 1 on timeout. |
| `kill-session S`      | Terminate a running session, or deregister an exited one (`--force` skips graceful terminate). |
| `resize S COLS ROWS`  | Resize the session's terminal. |
| `daemon start\|stop\|status\|restart` | Explicit daemon control (session commands auto-start it, tmux-style). |
| `daemon token [--rotate]` | Print (or rotate) the API/web auth token. |
| `daemon config [KEY [VALUE]]` | Show or set daemon settings (stored in `~/.claunch.yaml`). |
| `daemon relay [KEY [VALUE]]` | Show or set the relay uplink (reach this daemon from outside the LAN — see below). |
| `web [--open]`        | Print (and open) the web UI URL. |

### Reaching the daemon from outside the LAN (relay uplink)

The web UI normally binds loopback. To reach it from your phone or another
network without opening an inbound port, the daemon can dial an outbound
WebSocket to a [mux-relay](https://github.com/inosphe/mux-relay) and register
itself as a named backend. A browser then logs into the relay and opens
`https://relay.example.com/t/<name>/` to get this daemon's full web UI. Because
the daemon only ever dials **loopback**, the tunnel can't widen its network
exposure, and its own token/cookie auth still applies — the relay login is a
second, outer gate.

```powershell
# on each machine running the daemon:
claunch daemon relay url wss://relay.example.com
claunch daemon relay name work-pc          # directory label (default: hostname)
$env:CLAUNCH_RELAY_TOKEN = "<backend_token>"   # matches relay.toml backend_token
#   (or persist it: claunch daemon relay token <backend_token>)
claunch daemon restart
```

The daemon starts the uplink automatically once `url` and a token are set, so a
plain `claunch daemon restart` brings it online — no separate process to run.
Then, from anywhere, open the relay, sign in, and pick the machine by its `name`
from the directory (`/dir`) — or go straight to
`https://relay.example.com/t/<name>/` for its full web UI. Every machine you
configure this way appears in the same directory, so one relay fronts many
daemons. This requires a running
[mux-relay](https://github.com/inosphe/mux-relay) with a `backend_token`; the
relay writes one into its `relay.toml` on first start if none is set.

The `backend_token` is a machine secret set on the relay (its `relay.toml`),
**separate** from the browser login password. Prefer the `CLAUNCH_RELAY_TOKEN`
env var so it need not live in `~/.claunch.yaml`. For a self-signed relay,
`claunch daemon relay verify_tls false` accepts its certificate. The uplink
reconnects on its own (keepalive ping, receive watchdog, backoff+jitter); while
the relay is down the local daemon is unaffected.

### Idle detection

Raw output never goes quiet under a TUI (claude animates a spinner and a
clock), so the daemon renders every session's output through a terminal
emulator and samples the *screen content*: rows that flap on most samples are
classified as animation and ignored; the session is **idle** once no other row
has changed for `idle_threshold` seconds (default 2.0; per-call override with
`wait-for --idle-threshold`). For cautious automation against claude, ~4s is a
good threshold.

### Scripted workflows (blocking, sequential)

Every session command is designed to be scripted: they **block until done and
report success in their exit code**, so plain `bat`/`sh` scripts (or a
Makefile, or CI) can chain steps with `&&` / `||` — no polling loops needed.

| Command | Blocks until | Exit code |
| ------- | ------------ | --------- |
| `new-session` | the daemon is up and the PTY is spawned | `0` created / non-zero on error |
| `send-keys`   | the bytes are written to the PTY | `0` written / non-zero on error |
| `wait-for --idle` | screen quiet for `--idle-threshold` secs (or session exit) | `0` reached / `1` timeout |
| `wait-for --exited` | the process exits | `0` exited / `1` timeout |
| `capture-pane` | output printed to stdout | `0` / non-zero on error |

The basic building block is the **send → wait → capture** loop:

```sh
claunch send-keys work "run the tests and fix failures" Enter
sleep 2                                                        # see "robustness" below
claunch wait-for work --idle --timeout 1800 --idle-threshold 5
claunch capture-pane work > step1.txt
```

#### Multi-step example (sh)

```sh
#!/usr/bin/env sh
set -e                       # abort the workflow on any failed step/timeout
S=wf1

claunch new-session -s "$S" --profile work -c ~/proj
claunch wait-for "$S" --idle --timeout 60          # wait for the TUI to boot

step() {                     # send a prompt, wait for the answer, dump it
  claunch send-keys "$S" "$1" Enter
  sleep 2
  claunch wait-for "$S" --idle --timeout 1800 --idle-threshold 5
  claunch capture-pane "$S"
}

step "run the tests and fix any failures"       > step1.txt
step "now update the README for those changes"  > step2.txt
step "summarize what you changed in one line"   > step3.txt

claunch kill-session "$S"
```

#### Multi-step example (bat)

```bat
@echo off
set S=wf1

claunch new-session -s %S% --profile work -c C:\proj || exit /b 1
claunch wait-for %S% --idle --timeout 60 || exit /b 1

claunch send-keys %S% "run the tests and fix any failures" Enter || exit /b 1
timeout /t 2 /nobreak >nul
claunch wait-for %S% --idle --timeout 1800 --idle-threshold 5 || exit /b 1
claunch capture-pane %S% > step1.txt

claunch send-keys %S% "now update the README for those changes" Enter || exit /b 1
timeout /t 2 /nobreak >nul
claunch wait-for %S% --idle --timeout 600 --idle-threshold 5 || exit /b 1
claunch capture-pane %S% > step2.txt

claunch kill-session %S%
```

#### One-shot jobs: prefer `--exited`

For batch prompts that don't need an interactive session, pass the prompt to
the harness itself (everything after the session options is forwarded) and
wait for **process exit** — this skips the idle heuristic entirely, so there
is nothing to misjudge:

```sh
claunch new-session -s job1 --profile work -- -p "summarize this repo"
claunch wait-for job1 --exited --timeout 600
claunch capture-pane job1 --history > result.txt   # include scrolled-off lines
claunch kill-session job1                          # deregister the exited session
```

Several one-shot jobs can fan out in parallel and then be joined one by one —
each `wait-for` simply returns immediately once its session is already done:

```sh
for i in 1 2 3; do
  claunch new-session -s "job$i" --profile work -- -p "task $i ..."
done
for i in 1 2 3; do
  claunch wait-for "job$i" --exited --timeout 900
  claunch capture-pane "job$i" --history > "result$i.txt"
  claunch kill-session "job$i"
done
```

#### Robustness notes

- **Don't `wait-for --idle` in the same instant as `send-keys`.** Between
  pressing Enter and the harness starting to render its answer there is a
  short quiet gap; with a small threshold that gap can be misread as idle.
  A `sleep 2` after `send-keys` plus `--idle-threshold 5` closes it in
  practice.
- **Idle means "stopped painting", not "succeeded".** For decisions, inspect
  the capture: ask the prompt to end with a marker and grep for it —

  ```sh
  claunch send-keys "$S" "... reply DONE-OK on success or DONE-FAIL" Enter
  sleep 2
  claunch wait-for "$S" --idle --timeout 900 --idle-threshold 5
  claunch capture-pane "$S" | grep -q "DONE-OK" || exit 1
  ```

  or parse `capture-pane --json` (`lines`, `cursor`, `status`) from a real
  scripting language. The same loop over the [HTTP API](#web-ui--http-api)
  (`/keys`, `/wait`, `/capture`) avoids shelling out entirely.
- **`wait-for --idle` also returns when the session exits** (so a crashed
  harness doesn't hang the script); check `claunch sessions` or the `--json`
  status if you need to tell the two apart.
- **Timeouts end the wait, not the session.** After a `wait-for` timeout the
  harness keeps running — decide in the script whether to keep waiting,
  capture what's there, or `kill-session`.

### Restore on daemon restart

Sessions die with the daemon (the tmux model), but their *definitions* persist.
On the next daemon start, sessions created with `--restore` (the default; flip
with `daemon config restore false`) are relaunched. A claude session's
conversation id is pinned at creation (`--session-id <uuid>`, recorded in the
definition), and a restore reopens exactly that conversation with
`--resume <uuid>` — never `--continue`, which would grab whatever conversation
in the same cwd + profile happens to be the most recent (and can belong to a
different session). If the session's own args already pick a conversation
(`--resume`/`--continue`/`--session-id`), they win and nothing is pinned. Raw
output logs survive under `~/.claude-launcher/daemon/sessions/<name>/` either
way.

### Other harnesses (codex, pi, ...)

Declare them in `~/.claunch.yaml`; the `claude` harness is built in:

```yaml
harnesses:
  codex:
    command: codex          # string or argv list
    args: []                # optional, before the session's own args
    env: {KEY: VALUE}       # optional overrides
```

```bash
claunch new-session -s cdx --harness codex -c ~/proj
```

Sessions inherit the **daemon's** environment (tmux-server semantics), then the
harness `env`, then the session's own `--env` overrides. Every session also
gets `CLAUNCH_SESSION=<name>` (tmux's `$TMUX` equivalent) — child processes
can tell which session they live in, and [cflow](#cflow-declarative-agent-workflows)
keys its run state by it. The claude harness
builds its environment exactly like `claunch run` (profile config dir,
provider, token) and additionally strips nested-session markers so a claude
launched from inside another claude session still persists transcripts.

## Web UI & HTTP API

The daemon doubles as a web server. `claunch web --open` prints/opens the UI:
a session list (status badges, create/kill) plus a **live xterm.js terminal**
attached over WebSocket — full input and output, multiple viewers allowed.

An **exited** session is not a dead end in the browser either: open it and the
header offers **resume**, the `claunch respawn` of the UI — the session comes
back under its own name, claude with `--resume` of its pinned conversation, and
the tab reattaches to the new terminal (a resume done elsewhere, from the CLI
or another tab, is followed automatically). There `kill` becomes **remove**,
which only drops the daemon's record — it asks first, since that is what makes
the session unresumable.

The sidebar also shows a **Workflows** panel monitoring every
[cflow](#cflow-declarative-agent-workflows) run started on this machine
(each `start` registers its directory; managed sessions running in that
directory are listed alongside). Clicking a run opens its **dashboard page**
(`#/wf/<dir>`): a live diagram of the workflow graph (current step
highlighted, visit counts, gate/verify/select markers, cycle back-edges),
the run's step **reports** with details, the journal, links to attach the
session's terminal — and action buttons: **Approve** for gates and loop
limits, and the option buttons for user-chooser selections. Web actions go
through the same authenticated human channel as the CLI; the agent still
has no way to approve.

- **Auth is mandatory** (even on loopback): the CLI reads the token from
  `~/.claude-launcher/daemon/token` automatically; the browser asks once for
  `claunch daemon token` and stores an HttpOnly cookie. API clients send
  `Authorization: Bearer <token>`. Tokens never appear in URLs.
- **Binding** defaults to `127.0.0.1`. For LAN/phone access:
  `claunch daemon config host 0.0.0.0` then `claunch daemon restart` (the
  token is then the only barrier — prefer a TLS reverse proxy on hostile
  networks).

REST endpoints (JSON, `Bearer` or cookie auth; `/api/health` is open):

| Method | Path | Purpose |
| ------ | ---- | ------- |
| GET    | `/api/health`                  | liveness (unauthenticated) |
| POST   | `/api/auth/session`            | token → HttpOnly cookie (browser login) |
| GET    | `/api/daemon`                  | version/uptime/session count |
| POST   | `/api/daemon/shutdown`         | graceful stop |
| GET/POST | `/api/sessions`              | list / create |
| GET/DELETE | `/api/sessions/{name}`     | info / kill (`?force=1`) |
| POST   | `/api/sessions/{name}/respawn` | relaunch an exited session (claude resumes its conversation) |
| POST   | `/api/sessions/{name}/keys`    | `{keys: [...], literal}` — send-keys |
| GET    | `/api/sessions/{name}/capture` | `?history=1&format=json&trim=0` |
| GET    | `/api/sessions/{name}/wait`    | long-poll `?state=idle\|exited&timeout=&threshold=` |
| POST   | `/api/sessions/{name}/resize`  | `{cols, rows}` |
| GET    | `/api/sessions/{name}/ws`      | terminal WebSocket (binary = PTY bytes, text = JSON control) |
| GET    | `/api/profiles`                | profile names (for the UI's create form) |
| GET    | `/api/cflow`                   | all registered cflow runs, keyed (cwd, scope), with status + step reports; `?cwd=[&scope=]` inspects explicitly |
| GET    | `/api/cflow/run`               | `?cwd=&scope=` — run detail: status, workflow graph, reports, journal |
| POST   | `/api/cflow/approve`           | `{cwd, scope}` — approve the gate / extend the loop limit |
| POST   | `/api/cflow/select`            | `{cwd, scope, option, reason?}` — confirm a user-chooser branch |
| POST   | `/api/cflow/nudge`             | `{cwd, scope}` — re-type the resume line into the run's own session |
| POST   | `/api/cflow/goto`              | `{cwd, scope, step, reason?}` — force the current step (`end` finishes) + nudge |

Daemon settings live under `daemon:` in `~/.claunch.yaml`
(`host`, `port`, `idle_threshold`, `scrollback_lines`, `restore`); runtime
state (pid/port file, auth token, session logs) stays machine-local under
`~/.claude-launcher/daemon/`.

## Usage reporting

`claunch usage <name>` reads the profile's OAuth token and queries the Anthropic
usage endpoint (the same one Claude Code uses), printing per-window utilization:

```text
usage for profile 'work'
  five_hour          [##------------------]   9.0%  (resets in 4h34m)
  seven_day          [--------------------]   2.0%  (resets in 5h44m)
```

Add `--json` for the raw API response. The query uses only that profile's token,
so each profile reports its own account's usage.

**setup-token note.** The free `/api/oauth/usage` endpoint requires the
`user:profile` scope, which `claude setup-token` tokens don't carry. For those
(the launcher's default), `usage` instead reads the `anthropic-ratelimit-unified-*`
headers from a minimal `claude` API call (1 output token) — the output is marked
`(via rate-limit headers)`. The throwaway model defaults to Haiku; override it
with `CLAUDE_LAUNCHER_USAGE_MODEL`.

## cflow (declarative agent workflows)

**cflow** runs an agent through a workflow you declare in YAML — N design
steps, M implementation steps, L test/review steps, then ship — **one step at
a time**, over MCP. The agent never sees the whole plan; it calls `cflow`
tools to receive each step, report results, and take branches, while humans
keep the controls that matter.

```bash
claunch cflow install --profile work   # register MCP server + /cflow skill
claunch cflow example                  # scaffold .claunch/workflows/feature-dev.yaml
# then, inside claude:
#   /cflow feature-dev add rate limiting to the API
```

The agent's loop (taught by the `/cflow` skill):
`start {workflow, context}` → work → `report {summary, details}` →
`next {}` → … → `done`. The **report is not optional**: `next` refuses to
advance until the step's completion report is filed, and a failed `verify`
discards the report (the outcome it described did not survive), so every
advance leaves an explicit, machine-checked account of what happened. Reports
land in `.cflow/journal.jsonl` and stream live to the
[web dashboard](#web-ui--http-api), so the finished run yields a full, honest
changelog (useful for the PR body).

Ready-to-copy workflow patterns (linear + verify, triage branching, review
loops, gated releases, unattended orchestration) live in the
[cflow cookbook](docs4users/cflow-cookbook.md).

### Workflow YAML: a graph, not a tree

Steps are defined **once** in a mapping and wired by id (`next` pointers), so
a workflow is a directed graph: branches can share steps without duplicating
content, and edges may point *backwards* — a cycle models iteration
("review → rework → review"), with a `select` as the loop's exit condition.

```yaml
name: feature-dev
description: design -> (triage) implement -> test -> review loop -> ship
start: design                     # optional (defaults to the first step)
max_visits: 25                    # optional loop guard, per step per run
steps:
  design:
    instructions: |
      Analyze the request and write a short design note before coding.
    next: triage

  triage:                         # a branch point
    select:
      prompt: Assess the risk of the planned change.
      chooser: user               # agent | user — who decides
      options:
        auto:  {description: low risk — go autonomous,  next: impl}
        human: {description: higher risk — human review, next: impl}

  impl:                           # shared by both options — defined once
    instructions: Implement the design (or address the latest feedback).
    next: test

  test:
    instructions: Run and extend the tests.
    verify: "uv run pytest -q"    # machine gate: next() refused until exit 0
    next: review

  review:
    gate: present the diff and wait for human review   # re-required per visit
    instructions: Relay the review feedback into follow-ups.
    next: verdict

  verdict:
    select:
      prompt: Ready, or another pass?
      chooser: user
      options:
        ready:  {description: ship it,           next: ship}
        rework: {description: loop back,         next: impl}   # a cycle

  ship:
    gate: approve committing and opening a PR
    instructions: Commit and draft the PR from the run journal.
    next: end                     # explicit termination ('end' is reserved)
```

**Termination & cycles.** Omitting `next` (or writing `next: end`) ends the
run. Loading validates the graph: unknown targets are **errors**; a start
that can never reach a termination (only possible with cycles) is an
**error** — at least one reachable end must be described. Cycles themselves
are legal but **warned** (shown by `cflow show` and in the `start` payload),
as are steps trapped in never-ending regions and unreachable steps.

**Loops at runtime.** Every step's visit count is tracked (`cflow status`
shows `loops: impl x3`). Gates and user-selections apply **per visit** — a
review gate inside a loop closes again on every pass. Arriving at a step
beyond `max_visits` (default 25) pauses the run like a gate until a human
extends it with `claunch cflow approve`, so an agent-driven loop cannot spin
forever.

Files live in `.claunch/workflows/*.yaml` (project) or
`~/.claude-launcher/workflows/` (global). Runs are keyed by **(directory,
session)**: the daemon exports `CLAUNCH_SESSION=<name>` into every managed
session (tmux's `$TMUX` equivalent), the claude → MCP chain inherits it, and
run state lands in `.cflow/runs/<session>/` — so three sessions in the same
project drive three independent runs, each 1:1 with its session. Outside a
managed session the scope falls back to `default` (one run per directory).
Human commands resolve the target the same way; from an unrelated terminal
pick one explicitly with `-t/--session` (ambiguity is an error, not a
guess). `start` snapshots the YAML, so editing it mid-run can't corrupt a
running position.

### Control points

| Mechanism | Who | Enforced how |
| --------- | --- | ------------ |
| `select` (`chooser: agent`) | the agent | picks an option with a journaled reason |
| `select` (`chooser: user`) | a human | the agent's pick is only a *proposal*; the run blocks until `claunch cflow select <option>` (or a dashboard option button) confirms — any option |
| `gate:` | a human | the step's instructions are withheld until `claunch cflow approve` (or the dashboard's Approve button) |
| `verify:` | a machine | the server runs the command on `next`; non-zero exit refuses to advance and returns the output |
| `report` | the agent | required before `next`; journaled, shown live on the web dashboard, discarded by a failed `verify` |

**Approvals are not agent-callable, by design.** The MCP surface is only
`start` / `report` / `next` / `select` / `status` — there is no approve tool,
so a gate cannot be talked past. Humans approve through the CLI or the
token-authenticated web dashboard; both are outside the agent's reach. While blocked, the agent stops its turn and tells you
how to unblock; inside a chat session you can approve without leaving:

```text
! claunch cflow approve
! claunch cflow select human
```

When the agent runs as a [managed session](#managed-sessions-tmux-style-daemon)
in the run's directory, approving/selecting (CLI or dashboard) also
**auto-nudges** it — a resume line is typed into the session via send-keys, so
the stopped agent picks the run back up on its own. The run page also has a
**Nudge session** button to re-send that line manually whenever the agent
stalls. Elsewhere (e.g. `!` inside the chat itself) nudge the agent with any
message. The same CLI works from
outside — a supervising script or another agent can watch
`claunch cflow status --json`, approve gates, and drive the worker session via
`claunch send-keys` for multi-agent orchestration.

### cflow commands

| Command | Description |
| ------- | ----------- |
| `cflow ls` / `show <wf>` | List workflows / print a workflow's step tree. |
| `cflow status [--json]`  | Active run: current step, state, how to unblock. |
| `cflow approve`          | Approve the current human gate (human-only: CLI or web dashboard). |
| `cflow select <opt> [--reason]` | Confirm (or override) a user-chooser branch. |
| `cflow goto <step> [--reason]` | Force the current step (`end` finishes; journaled, re-gates, auto-nudges). On the dashboard: click a diagram node. |
| `cflow journal [-n N]`   | Print the run journal (JSONL). |
| `cflow archive`          | Retire the run (finished or not) into `.cflow/.../archive/`, freeing the slot for a new start. Active runs are aborted first; a new `start` auto-archives finished runs. On the dashboard: the Archive button + start picker. |
| `cflow abort` / `reset`  | Abort the run / clear run state (journal kept). |
| `cflow install --profile P \| --project [DIR]` | Register the MCP server + `/cflow` skill. |
| `cflow example [name]`   | Scaffold the example workflow above. |
| `cflow mcp`              | The stdio MCP server itself (spawned by claude). |

## How it works

- Profiles live under `~/.claude-launcher/profiles/<name>` (override the base
  with `CLAUDE_LAUNCHER_HOME`). That directory **is** the profile's
  `CLAUDE_CONFIG_DIR`.
- `login` / `run` export `CLAUDE_CONFIG_DIR=<profile dir>` before invoking
  `claude`, keeping each profile's credentials and settings isolated.
- `run` exports the profile's `env` vars (from `~/.claunch.yaml`) into claude's
  process (overriding the inherited shell), plus `CLAUDE_CODE_OAUTH_TOKEN` when a
  token has been stored — so it authenticates and runs non-interactively.
- Launcher config (`env`, `parent`, `provider`, template, providers) lives in
  `~/.claunch.yaml`, **not** in the profile directory; the profile dir holds only
  Claude Code's own files plus per-machine login tokens.

A profile directory typically holds:

| File | Origin |
| ---- | ------ |
| `.claude.json`      | Seeded from your global config (onboarding flags, prefs). |
| `settings.json`     | Seeded global settings (and any migrated `mcpServers`). |
| `.launcher-token`   | OAuth token stored by `set-token` (`0600`). |
| `.credentials.json` | Written by Claude Code itself after an interactive login. |

## Configuration

| Environment variable        | Purpose |
| --------------------------- | ------- |
| `CLAUDE_LAUNCHER_HOME`      | Base directory for profiles (default `~/.claude-launcher`). |
| `CLAUDE_LAUNCHER_BIN`       | Path/name of the `claude` executable (default `claude`). |
| `CLAUDE_LAUNCHER_USAGE_URL` | Usage endpoint (default `https://api.anthropic.com/api/oauth/usage`). |
| `CLAUDE_LAUNCHER_USAGE_MODEL` | Model for the setup-token usage fallback call (default Haiku). |
| `CLAUDE_LAUNCHER_SEED`      | Config dir new profiles seed from (default `CLAUDE_CONFIG_DIR` or `~/.claude`). |
| `CLAUDE_LAUNCHER_SYNC_FILE` | The config source of truth (default `~/.claunch.yaml`). |

## License

MIT
