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
| `run <name> [args...]` | Launch `claude` for the profile (`--borrow NAME`, `--provider NAME`, `--add-prompt`, `--worktree[=NAME]`/`--no-worktree`; extra args pass through). |
| `env <name> [...]`     | View/edit the profile's env vars (`--effective` for merged). |
| `parent <name> [p]`    | Show, set, or `--clear` a profile's parent. |
| `template [--init]`    | Show or write the default env template. |
| `migrate <name> [src]` | Copy skills/MCP servers from a global or local path. |
| `prune [--dry-run]`    | Delete local profile dirs not declared in `~/.claunch.yaml`. |
| `sync [--mode ...]`    | Reconcile `~/.claunch.yaml` with the sync server (`merge`/`up`/`down`). |
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
daemon-owned PTYs instead of the current terminal, the
**[mesh commands](#mesh-session-to-session-messaging)** (`claunch mesh ...`)
for session-to-session (and cross-machine) agent messaging, and the
**[cflow commands](#cflow-declarative-agent-workflows)** (`claunch cflow ...`)
for declarative agent workflows with human — and delegated — approvals.

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

### Running in a git worktree

Two agents in the **same checkout** is the failure mode this exists for: they
edit each other's files mid-edit, one's build races the other's, and a branch
switch by either silently rewrites what the other is looking at. A git
worktree is the cheap fix — a second checkout of the same repository on its
own branch, sharing one object store.

So `run` and `new-session` ask, at the one moment the answer is free:

```
$ claunch run work
create a git worktree for this launch, so it does not share claude-launcher
with other agents? [y/N]: y
worktree name [w4-p4-20260818-173005]:
created worktree 'w4-p4-20260818-173005' on branch 'w4-p4-20260818-173005':
  F:\works\claude-launcher\.claude\worktrees\w4-p4-20260818-173005
```

The suggested name is **the Herdr pane you are in plus the time**, so it is
unique per pane per second and `git worktree list` afterwards says which pane
made which checkout, and when. Outside Herdr the
managed session's name is used instead, and outside both, `wt`.

Answer ahead of time — or from a script — with either flag:

```bash
claunch run work --worktree=review      # name it yourself
claunch run work --worktree             # name it after this pane and the time
claunch run work --no-worktree          # this checkout, and do not ask
claunch new-session --profile work --worktree=review -a
```

Naming the **same worktree twice** returns to it, branch and uncommitted work
intact — `--worktree=review` is a place you go back to, not a new checkout
each time. An existing *branch* of that name is checked out rather than recut.
Launching from *inside* a worktree makes the next one a **sibling**, not a
nested checkout inside the one an agent is editing.

Worktrees are created under `<repo>/.claude/worktrees/<name>` — beside the
ones Claude Code makes itself, so one `git worktree list` shows every checkout
an agent is working in, whoever made it. Point them elsewhere with
`CLAUNCH_WORKTREE_DIR` (absolute, or relative to the repository root).

**A resume decides the directory by itself.** Claude Code keeps transcripts
**per working directory**, so a conversation resumed in a checkout that has
never been worked in resolves to nothing — bare `--resume` opens an empty
picker, and `--resume <uuid>` finds no such conversation. So a launch carrying
`--resume`, `--continue`, `-r`, `-c` or `--session-id` is not asked the
question at all: `claunch run nc --resume` means *carry on where I was*, and
where it was is this directory.

Pairing one with a **new** worktree is refused rather than silently obeyed:

```bash
claunch run nc --resume                       # stays put, no question
claunch run nc --worktree=fresh --resume      # error: nothing there to resume
claunch run nc --worktree=review --resume     # fine — that checkout has a history
```

The last one is the useful case, and the reason this is a rule about *new*
worktrees only: go back to a checkout you worked in before and carry on the
conversation you had there.

**Who gets asked.** Only a human at an interactive terminal. A managed session
runs on a PTY, so an agent's stdin passes every `isatty()` test there is — a
prompt printed into one is not answered, it hangs the launch. `$CLAUNCH_SESSION`
is what tells them apart, so an agent, the daemon, the web UI, a restore after
a restart and any script all skip the question and stay put unless a flag says
otherwise. `claunch spawn` never asks at all: a **child inherits its parent's
directory**, so it is already in whatever worktree the parent was launched
into.

If the launch is inside Herdr, the pane is relabelled with the worktree and
its branch (`review`, or `review [other]` when they differ), so a wall of
panes says which branch each agent is on.

A worktree that was *asked for* and could not be made fails the launch rather
than quietly falling back to the shared checkout — that fallback is the exact
collision the flag was used to avoid.

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

Past two or three machines, copying stops being fun — point them all at a
[profile sync server](#profile-sync-server) and run `claunch sync` instead.

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

## Profile sync server

Copying `~/.claunch.yaml` by hand works for two machines and stops scaling at
three. `claunch sync` reconciles that file with a **sync server** — a small
service that holds one shared document per namespace — so every machine ends up
with the same profiles, providers and template.

What travels is **configuration only**. Login tokens never leave the machine
(they are per-profile secrets — run `claunch login` on each host), and the
`daemon` and [`workspaces`](#workspaces-where-a-session-may-be-spawned) blocks
stay local too: ports, bind host, the relay token and absolute directory paths
describe *that* machine, not the profile set.

### Client setup

Describe the server in `~/.claunch.yaml`:

```yaml
sync:
  url: https://sync.example.com
  namespace: alice              # which document on the server
  token: "..."                  # better: CLAUNCH_SYNC_TOKEN in the environment
  # sections: [template, provider, providers, profiles, harnesses]   # the default
  # verify_tls: true
  # allow_insecure: false       # required to sync over plain http off-loopback
```

Then:

```bash
claunch sync                    # --mode merge (the default)
claunch sync --mode up          # local wins: push this machine's config
claunch sync --mode down        # server wins: overwrite local config
claunch sync --dry-run          # show both sides' changes, write nothing
claunch sync --status           # local config + pending changes, no network
```

| Mode | Direction | What it does |
| ---- | --------- | ------------ |
| `merge` | both | Three-way merge, then push the result. The default. |
| `up`    | local → server | Replaces the server document with this machine's sections. |
| `down`  | server → local | Replaces the local sections with the server's. Local-only edits are discarded. |

### How `merge` decides

It is a real three-way merge, not a union. Each machine caches the last state it
agreed on with the server (`<launcher home>/sync-base.yaml`) and uses it as the
merge base, which is what makes **deletions propagate**: a profile you removed
here is *gone*, not resurrected by the next machine that still has it.

- Changed on one side only → that change is taken.
- Changed on both sides, identically → nothing to decide.
- Changed on both sides, differently → a **conflict**: reported by path, and
  resolved by `--prefer local` (default) or `--prefer remote`.

Pushes are guarded by a revision. If another machine wrote while you were
merging, the server rejects the push and `claunch sync` merges again on top of
the winner and retries — so a race costs a round trip, never a lost edit.

```console
$ claunch sync
synced 'alice' with https://sync.example.com  (mode: merge)
  conflicts (1, kept local):
    ! profiles.work.env.REGION   local='apac'  remote='us'
  local changes (~/.claunch.yaml):
    + profiles.lab
  pushed to server:
    ~ profiles.work.env.REGION
  revision: 5
```

A pulled profile is **materialized immediately** — its `CLAUDE_CONFIG_DIR` is
created and seeded, so it is usable right after the sync (it still needs
`claunch login`). A pulled *deletion* only removes the declaration: as everywhere
else in the launcher, deleting a directory is explicit, so run `claunch prune`
to finish the job.

### Running the server

```bash
claunch sync-server user add alice       # prints the token once; only its hash is stored
claunch sync-server serve --port 8378    # foreground; put it behind TLS in production
```

| Command | Description |
| ------- | ----------- |
| `sync-server serve` | Run the server (`--host`, `--port`). |
| `sync-server user add <name>` | Create an account, print its token once (`--namespace NS`, repeatable, `*` for all). |
| `sync-server user ls` | List accounts and the namespaces they may sync. |
| `sync-server user token <name>` | Issue a new token, invalidating the old one. |
| `sync-server user namespaces <name> <ns>...` | Replace an account's namespace list. |
| `sync-server user rm <name>` | Remove an account (documents are kept). |
| `sync-server docs` | List stored documents, revisions and last writer. |

Accounts are stored in `<data dir>/users.yaml` (default
`<launcher home>/sync-server`, override with `CLAUNCH_SYNC_SERVER_DIR` or
`--data-dir`); documents live beside them under `docs/`. **Tokens are stored
SHA-256 hashed**, so a leaked `users.yaml` does not hand over anyone's config;
the plaintext is shown once at `user add` / `user token` time. An account may
only touch its own namespaces — anything else is a 403, whether or not the
namespace exists.

The server also runs standalone, without the rest of the CLI:

```bash
python -m claude_launcher.syncserver --host 0.0.0.0 --port 8378
```

It speaks plain JSON over HTTP and stores documents opaquely, so a launcher
upgrade that adds config keys needs no server change:

| Route | Purpose |
| ----- | ------- |
| `GET /api/sync/health` | Liveness (the only unauthenticated route). |
| `GET /api/sync/whoami` | The calling account and its namespaces. |
| `GET /api/sync/doc/{ns}` | `{"revision": N, "doc": {...}, "updated_at": ..., "updated_by": ...}`; revision `0` when the namespace has no document. |
| `PUT /api/sync/doc/{ns}` | Body `{"revision": <what you read>, "doc": {...}}`; `409` with the winning document if the revision is stale. |
| `DELETE /api/sync/doc/{ns}` | Drop a namespace's document. |

**Secrets note.** Provider auth tokens live in `~/.claunch.yaml` (see the
[providers section](#api-providers-third-party-backends)), so they are part of
what syncs. `claunch sync` therefore refuses plain `http` to anything but
loopback unless you set `sync.allow_insecure: true`; put the server behind TLS,
or keep provider tokens out of the synced sections.

### Worked scenarios

#### 1. One person, several machines

A desktop that already has the profiles, a laptop that should match it, and a
small VPS in between. **On the VPS, once:**

```bash
uv tool install claude-launcher
claunch sync-server user add alice
#   created user 'alice' (namespaces: alice)
#   token (shown once — the server stores only its hash):
#     N2I6WmX2r7pQ...                       <- copy this now; it is never shown again
claunch sync-server serve --host 127.0.0.1 --port 8378
#   then front it with nginx/caddy for TLS -> https://sync.example.com
```

**On the desktop** (the machine whose config wins first). Add to `~/.claunch.yaml`:

```yaml
sync:
  url: https://sync.example.com
  namespace: alice
```

```bash
export CLAUNCH_SYNC_TOKEN=N2I6WmX2r7pQ...    # ~/.bashrc, or a secret manager
claunch sync --dry-run                       # look before you leap
claunch sync --mode up                       # publish this machine as the baseline
#   synced 'alice' with https://sync.example.com  (mode: up)
#     pushed to server:
#       + profiles
#       + template
#     revision: 1
```

**On the laptop** — same `sync:` block, same token, then:

```bash
claunch sync --mode down     # the server is authoritative on a fresh machine
claunch list                 # the profiles are here, directories already created
claunch login work           # ...but log in per machine: tokens never sync
```

**From then on, on either machine**, one command in both directions:

```bash
claunch sync                 # merge
claunch sync --status        # what is pending locally, without touching the network
```

#### 2. A team sharing one profile set

Two people, one shared namespace `team-infra`, plus a private namespace each.
**On the server:**

```bash
claunch sync-server user add alice --namespace alice --namespace team-infra
claunch sync-server user add bob   --namespace bob   --namespace team-infra
claunch sync-server user ls
#   alice  namespaces: alice, team-infra
#   bob    namespaces: bob, team-infra
#   documents: (none yet)
```

Two accounts, two tokens, and both may write the *same* document — that is the
whole point. Each member puts the shared namespace in their `~/.claunch.yaml`:

```yaml
sync:
  url: https://sync.example.com
  namespace: team-infra
```

```bash
claunch sync                 # first run pulls the team's profiles
```

Bob adds a provider definition to his `~/.claunch.yaml` and shares it:

```bash
claunch sync
#   synced 'team-infra' with https://sync.example.com  (mode: merge)
#     pushed to server:
#       + providers
#     revision: 2
```

Alice picks it up on her next sync, without having touched providers herself:

```bash
claunch sync
#   local changes (~/.claunch.yaml):
#     + providers
#   revision: 2
```

If they both changed the *same* key since their last sync, the second one to
run gets a conflict and keeps their own value:

```console
$ claunch sync
synced 'team-infra' with https://sync.example.com  (mode: merge)
  conflicts (1, kept local):
    ! profiles.work.env.REGION   local='apac'  remote='us'
  pushed to server:
    ~ profiles.work.env.REGION
  revision: 5
note: re-run with '--prefer remote' to resolve conflicts the other way
```

**Keeping a personal set *and* the team set on one machine:** give them separate
launcher homes rather than switching `namespace` back and forth. The merge base
is one file per launcher home, so alternating namespaces in a single home throws
it away each time — merges silently degrade to a union and deletions stop
propagating (`claunch sync --status` says `no base for this server yet`).

```bash
# personal (the default home)
claunch sync

# team, fully separate state (its own profiles, config file and merge base)
export CLAUDE_LAUNCHER_HOME=~/.claude-launcher-team
export CLAUDE_LAUNCHER_SYNC_FILE=~/.claunch-team.yaml
export CLAUNCH_SYNC_URL=https://sync.example.com   # the new file has no sync: block
export CLAUNCH_SYNC_NAMESPACE=team-infra
claunch sync --mode down                           # first run on an empty home
```

#### 3. Disposable machines (CI, containers)

A fresh container needs the config but has no `~/.claunch.yaml` to edit and must
never push. Every setting has an env override, so **no file editing at all**:

```bash
export CLAUNCH_SYNC_URL=https://sync.example.com
export CLAUNCH_SYNC_NAMESPACE=team-infra
export CLAUNCH_SYNC_TOKEN="$SYNC_TOKEN"        # from the CI secret store

claunch sync --mode down                       # config only, one way
claunch list                                   # the synced profiles, dirs created

claunch set-token work "$CLAUDE_OAUTH_TOKEN"   # the login is a separate secret
claunch run work -- -p "review the diff on this branch"
```

`--mode down` is the whole contract here: it pulls and never pushes, so a
throwaway machine cannot corrupt the shared document. It also refuses to run
when the namespace has no document yet, rather than "winning" with an empty one
and undeclaring every profile. No `sync:` block is ever written to disk — the
env vars are read fresh on each command.

Give CI its own account if you want to be able to revoke it alone:

```bash
claunch sync-server user add ci-runner --namespace team-infra
claunch sync-server user token ci-runner   # rotate; the old token dies instantly
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

### Building one from a form (`--wizard`)

`new-session` spells every field out as a flag, which is what makes it
scriptable and what makes it hard to type — the harness, profile, directory,
role, mesh, workflow and worktree are all **closed sets the daemon already
publishes**, and typing them from memory is guessing at names a picker could
show. `--wizard` opens exactly that picker in the terminal you are standing
in: the web dashboard's create form, minus the browser.

```bash
claunch new-session --wizard          # every field, from its list
claunch new -s api --wizard           # flags typed alongside pre-fill the form
```

```
claunch new-session

   Name            api
   Harness         claude  Claude Code
 > Profile         work
   Directory       this directory  F:\works\claude-launcher
   Worktree        (none) - work in the directory as it stands
   Role            (no role)
   Resume          (new conversation)
   Fork            needs a conversation to fork
   Args            (extra harness flags)

  START IT WORKING
   Mesh            (none)
   Workflow        (none)
   Opening task    typed in once it has booted - what it is for

  AFTERWARDS
   Restore         (daemon default)
   Attach          no - leave it running in the daemon
   [ Create session ]

  which login and config the harness runs under ('claunch list')
  up/down move   left/right change   Enter open   Ctrl+S create   Esc cancel
```

`↑`/`↓` move, `←`/`→` change an answer in place, `Enter` opens the full list
for the field under the cursor (type a letter to jump inside it), `Ctrl+S`
creates, `Esc` backs out having created nothing. The form paints on the
alternate screen, so it leaves your scrollback as it found it.

**Everything with an answer set is multiple choice**, including the two
questions the flags can barely ask:

- **Worktree** — offered only inside a repository, with *no worktree*, *a new
  one* (auto-named after the pane and the time), *a new one you name*, and
  every launcher worktree already on disk, since returning to one is the
  common case a name exists for. It replaces the `[y/N]` prompt that would
  otherwise fire *after* the command line was already committed.
- **Attach** — whether to take this terminal over the moment it starts
  (`Ctrl+]` detaches, and the session lives on either way).

The rest is the same list the daemon would have checked afterwards, so a mesh
that does not exist or a workflow not declared in that directory is never
offered rather than refused once the session is half arranged. Fields that do
not apply grey out rather than vanish: `Fork` says *needs a conversation to
fork* until you pick one under `Resume`, and `Role`/`Resume` say *the claude
harness only* under any other harness.

Flags the form does not show — `--env KEY=VALUE`, `--cols/--rows`,
`--detached` — are left exactly as you typed them.

It is a form, so it needs somebody to fill it in: `--wizard` is refused
outside an interactive terminal (and, like `new-session` itself, from inside a
managed session — an agent building a session uses `spawn`).

### Session commands

| Command | Description |
| ------- | ----------- |
| `new-session` (`new`) | Spawn a harness in a managed PTY. `--wizard` picks every field from a form in this terminal instead (see [Building one from a form](#building-one-from-a-form---wizard)); by flag: (`-s NAME`, `--profile P`, `--harness H`, `-c CWD`, `--cols/--rows`, `--env K=V`, `--restore/--no-restore`, `--role R`, `--resume [S]`, `--fork-session`, `--worktree[=NAME]`/`--no-worktree`, `-a/--attach` to attach immediately, trailing args pass to the harness). Also **what it is for**, in the same call: `--mesh M --as HANDLE --connect H`, `--workflow W --context C`, `--task "..."` — see [Created with a job](#created-with-a-job-mesh--workflow--opening-task). **Yours, not an agent's**: refused from inside a managed session, which should use `spawn` (`--detached` overrides). |
| `spawn`               | Create a **child** of a session by hand, exactly as its agent would — same endpoint, same policy (`--parent S`, `-s NAME`, `--mesh M`, `--as HANDLE`, `--role R`, `--connect HANDLE`, `--workflow W`, `--task "..."`, `--harness H`, `-w/--workspace NAME`). `--mesh` defaults to the parent's own. See [Agents that build their own team](#agents-that-build-their-own-team-spawn--hierarchy--member-graph). |
| `sessions` (`lss`)    | List sessions: name, status (`starting/busy/idle/exited`), harness, profile, size, cwd. Children are indented under the session that spawned them. |
| `attach [S]` (`a`, `attach-session`) | Mirror a session into this terminal, tmux-style; detach with `Ctrl+]` (session keeps running). Omit `S` when exactly one session is running. `-t S` also accepted. |
| `respawn S [-a]`      | Relaunch an exited session under its own name — claude comes back with `--resume` of its pinned conversation, so quitting it by accident (double `Ctrl+C` while attached) is recoverable. `-a` attaches right away. Also a **resume** button in the [web UI](#web-ui--http-api). |
| `send-keys [-l] S KEYS...` | tmux semantics: `Enter`, `Escape`, `Tab`, `C-c`, `M-x`, `Up`... are keys; everything else is literal text. `-l` sends all args literally. `-t S` also accepted. |
| `capture-pane S`      | Print the current rendered screen (`--history` for scrolled-off lines, `--json` for lines + cursor + status). |
| `wait-for S`          | Block until `--idle` (default) or `--exited`; `--timeout SECS`, `--idle-threshold SECS`. Exits 1 on timeout. |
| `kill-session S`      | Terminate a running session, or drop the record of an exited one (`--force` skips graceful terminate). |
| `clear-sessions` (`clear`) | Drop the records of **all** exited sessions at once — running ones are untouched. They are kept indefinitely otherwise (a restart never discards them), so this is the explicit cleanup; `--logs` also deletes their output logs, freeing their auto-generated names. |
| `resize S COLS ROWS`  | Resize the session's terminal. |
| `harnesses`           | List the declared harnesses (`claude`, `codex`, `pi`, plus your own) and whether each is installed here. |
| `workspace add\|ls\|rm` (`ws`) | Register / list / unregister the directories a session may be spawned in — the web UI's Directory picker is exactly this list, and (unless `spawn.allow_workspace` is off) where an agent may send a child (see [Workspaces](#workspaces-where-a-session-may-be-spawned)). `add` defaults to the current directory and refuses one that does not exist. |
| `daemon start\|stop\|status\|restart` | Explicit daemon control (session commands auto-start it, tmux-style). |
| `daemon token [--rotate]` | Print (or rotate) the API/web auth token. |
| `daemon config [KEY [VALUE]]` | Show or set daemon settings (stored in `~/.claunch.yaml`). |
| `daemon relay [KEY [VALUE]]` | Show or set the relay uplink (reach this daemon from outside the LAN — see below). |
| `web [--open]`        | Print (and open) the web UI URL. |

### Named daemon instances (tmux `-L`)

Like tmux's `-L socket-name`, `claunch -L NAME ...` (or `CLAUNCH_DAEMON=NAME`)
targets a separate **daemon instance**: an independent server with its own
state directory (`~/.claude-launcher/daemons/NAME/` — sessions, meshes, auth
token, lock), its own ephemeral port (discovered via its `daemon.json`;
pin one with `CLAUNCH_DAEMON_PORT`), and its own relay identity (defaults to
`<hostname>-NAME`; override with `CLAUNCH_RELAY_NAME`). The default instance
keeps the classic `~/.claude-launcher/daemon/` directory and fixed port, so
nothing changes unless you opt in.

```bash
claunch -L test new-session -s scratch   # auto-starts the 'test' instance daemon
claunch -L test sessions                 # separate world from the default daemon
claunch -L test daemon stop
claunch daemon restart --all             # restart every RUNNING instance
```

`daemon restart --all` restarts the default instance and every named one that
is currently serving (stopped instances are skipped, not started) — the "pick
up new code everywhere" verb after an upgrade.

Instances make multi-endpoint setups testable on one machine: two named
instances are two full daemons that can join the same mesh through a relay,
exactly like two hosts would (`tests/test_multi_daemon_mesh.py` drives that
end-to-end).

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

### Workspaces (where a session may be spawned)

A **workspace** is a directory you have vouched for once, on this machine:

```bash
claunch workspace add .                  # register the current directory
claunch workspace add D:\works\hq --name hq
claunch workspace ls
claunch workspace rm hq                  # unregisters; the directory stays
```

The registry lives under `workspaces:` in `~/.claunch.yaml` (name → path) and
is read live, so a workspace added in a terminal shows up in an open browser
tab within a couple of seconds. It is **machine-local by default** — absolute
paths mean nothing on another machine, so `workspaces` is deliberately absent
from the [synced sections](#profile-sync-server); add it to `sync.sections`
if your machines really do share a layout.

`add` refuses a directory that is not there, which is the whole point: **the
web UI's Directory field is a picker over the registry, not a text box.** A
working directory typed free-hand is the easiest thing in the create form to
get wrong — a typo, a stale path, the wrong drive — and it used to fail late,
as `could not spawn 'claude'`. A session's directory is now checked before
anything spawns either way, so even `new-session -c` reports the bad path
instead of blaming the harness.

The CLI's `-c/--cwd` still takes **any** directory: it is typed by someone
already standing in the filesystem, with a shell that completes paths. The
registry is what the *browser* offers, which has neither. The daemon's own
directory is always available in the picker as `(daemon cwd)`, so the form
works before you register anything.

The browser can edit the registry too, on the
[`#/workspaces` page](#web-ui--http-api) — and that is not a contradiction of
the picker. A path is typed **once**, at registration, where the daemon checks
it against the filesystem and answers immediately; what the registry removes
is the same path being retyped at every spawn, where a typo surfaces late.
Vouching has to be spellable somewhere. The point is that nowhere else is.

An **agent** spawning a child is in the browser's position, not the shell's,
so it gets the picker too, and registering a directory is what puts it within
reach of one — see
[`spawn.allow_workspace`](#agents-that-build-their-own-team-spawn--hierarchy--member-graph),
which is on unless you turn it off.

**A [worktree](#running-in-a-git-worktree) is not a second workspace.** A
session launched with `--worktree` sits in `<repo>/.claude/worktrees/<name>`,
which is the repository you already vouched for with another branch checked
out — so it is *attributed* to the enclosing workspace and shown as `in
workspace hq / .claude/worktrees/review`, not as a directory nobody approved.
Containment is how a directory is described, never how one is chosen: what the
browser offers and what an agent may name stays exactly the list you
registered. To make a worktree itself pickable — so a child can be spawned
straight into it — register it like any other directory:

```bash
claunch workspace add .claude/worktrees/review --name review
```

### Spawning with a role, or from another session's conversation

Two things are decided at spawn and cannot be typed in afterwards: **who the
session is**, and **which conversation it opens**. Both are options on
`new-session` and controls in the web UI's create form (claude harness only —
they are spelled in claude's own flags):

```bash
claunch new-session -s rev --profile work --role reviewer   # spawn as the adversary
claunch new-session -s side --profile work --resume rev --fork-session
claunch new-session -s pick --profile work --resume         # claude's own picker
```

**`--role NAME`** takes a role from the same vocabulary the
[mesh](#mesh-session-to-session-messaging) uses — `leader`, `operator`,
`worker`, `reviewer`, `specialist`, and their aliases (`--role mod` is
`leader`). The role's **stance is injected into the system prompt** at spawn
(`--append-system-prompt`, which *adds to* claude's built-in prompt rather
than replacing it), so the session knows what it is before its first turn —
no priming message, no turn spent. It is re-injected on every restore, since
an appended system prompt lives in the process, not in the transcript. A role
is optional; without one nothing is injected. Unknown names are refused
rather than silently ignored, and `GET /api/roles` lists the vocabulary with
each stance (that is what fills the web picker, stance and all).

This is *not* the same thing as a mesh role: joining a mesh resolves a role
for the roster, on a vocabulary the mesh's authority can override. This one is
about a single session's own system prompt, so it always reads the packaged
set.

**`--resume [SESSION|UUID]`** opens an existing conversation instead of a new
one. Name a session this daemon knows and the registry maps it to that
session's pinned conversation; pass a uuid and it goes through as-is; pass the
flag bare and claude opens its interactive picker. **`--fork-session`** (a
checkbox in the web form, and claude's own flag) resumes into a *copy*: the
original conversation is left untouched, and the copy is minted at an id
claunch pins — so the fork restores and respawns like any other session. Both
are refused alongside raw args that already steer the conversation, rather
than silently letting one win.

Resuming *without* a fork means the two sessions share one conversation, which
is the point when you are picking up an exited session's work elsewhere — and
a footgun if the source is still running. The web picker shows each session's
status next to its name for exactly that reason.

### Created with a job (mesh · workflow · opening task)

A session is rarely wanted on its own. It is wanted **in** a mesh, driving a
particular run, with an opening instruction — and until all three have landed
it is a terminal nobody is listening to, or an agent that does not know why it
exists. So they are options on the create call, not three steps after it:

```bash
claunch new-session -s w1 --role worker \
    --mesh dev --as worker_1 \
    --workflow feature-dev --context "the export path" \
    --task "take the API half; report when the design note is up"
```

The same keys on `POST /api/sessions`, the same fields in the web form's
**Start it working** box (mesh and workflow are pickers, not text boxes), and
the same set an agent's [`spawn`](#agents-that-build-their-own-team-spawn--hierarchy--member-graph)
tool has always had — that path composed these from the start, and this is
that composition shared rather than a second one.

Two properties are worth knowing:

- **Nothing is built until the request is known to be honourable.** A mesh
  that is not here, a handle already taken, a workflow not declared in that
  directory: each is a `400` with no session left behind, instead of a live
  session whose join failed after the fact. (It has to work this way — the
  system prompt is fixed when the PTY starts, so anything going into it must
  be known before the session exists.)
- **One opening block, not three.** The mesh briefing, the workflow assignment
  and the task arrive as a single message. They used to be three independently
  idle-gated pastes racing into the same terminal, which needed a settle
  constant tuned against the paste-Enter delay to keep the task from being
  glued onto the briefing's closing fence.
- **It is not typed in at all.** The join and the run happen while the session
  is registered but not yet started, so the block they compose is handed to
  `claude` as its positional prompt (`claude [options] [prompt]`) and *is* the
  first turn. A TUI spends about ten seconds between going quiet and being
  able to accept a submit — long enough that an opening message typed into it
  reliably ended up in the composer, unsent. A message on the command line is
  read before the process reads a key, so that window does not exist. Harnesses
  with no such argument are still typed into, and `Session.deliver` waits them
  out (see [docs/mesh-design.md](docs/mesh-design.md#why-delivery-is-always-send-keys)).

What goes in the **system prompt** versus the opening block follows what is
true for how long. The handle this session answers to and the run it drives
hold for its whole life, so they are appended to claude's system prompt beside
the role stance and survive compaction and restore. Who it can reach right now
does *not*: `connect`/`disconnect` rewire the member graph mid-session, and a
frozen roster would have the agent addressing peers it cannot reach and
reading the refusal as a bug. That half stays in the briefing, which is
re-derived every time it is sent. Only claude has `--append-system-prompt`, so
the briefing is the channel that must be sufficient on its own; the system
prompt is the reinforcement where there is one.

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

**A restart never loses a session.** Whatever is *not* relaunched — it had
already exited, it was created `--no-restore`, or its relaunch failed — comes
back as an **exited record** rather than being forgotten: still listed, still
carrying its pinned conversation, so `claunch respawn <name>` (or the web UI's
resume) revives it days later. Attaching to such a record shows the final
screen it left behind, replayed from its log.

Records therefore accumulate, and only you drop them:

```bash
claunch kill-session s0     # drop one record (or kill it, if still running)
claunch clear-sessions      # drop every exited record; running sessions stay
claunch clear-sessions --logs   # ...and delete their output logs too
```

Dropping a record is the one thing that makes a session unresumable, which is
why nothing does it automatically. Auto-generated names (`s0`, `s1`, ...) skip
anything still taken — including exited records and the session directories
left on disk — so a name is never silently recycled onto another session's
log; `--logs` is what frees those numbers again.

### Other harnesses (codex, pi, ...)

Which harnesses exist is **declared, not hard-coded**. The packaged set ships
`claude`, `codex` and `pi`; `claunch harnesses` shows it, along with whether
this machine can actually run each one:

```
$ claunch harnesses
declared harnesses:
  claude     [ready        ] profile-managed
  codex      [ready        ] codex
  pi         [not installed] pi
```

**Declared is not installed.** `pi` ships in the set whether or not you have
it — the web UI lists it as a *disabled* option rather than hiding it, since a
missing option reads as "claunch does not support pi", which is the wrong
thing to learn. Spawning one that is not installed is refused up front, naming
the program it looked for, instead of failing later as `could not spawn`.

`~/.claunch.yaml` overrides or extends the set. Overriding is **per harness,
not per field** — a name in the config replaces that harness's whole
definition, so a half-merged declaration (new command, inherited flags) cannot
happen:

```yaml
harnesses:
  codex:
    command: codex          # string or argv list
    args: []                # optional, before the session's own args
    env: {KEY: VALUE}       # optional overrides
    description: "..."      # optional, shown in the picker
  pi: null                  # a tombstone: drop a packaged harness
```

`claude` is the one harness the document does not describe a command for: it
runs through the profile machinery, and its executable is `CLAUDE_LAUNCHER_BIN`.

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

## Toolkit commands (what an agent gets)

Everything an agent can drive — workflows, mesh messaging, creating and wiring
up child sessions — arrives in **one install**:

```bash
claunch install --project .        # .mcp.json + .claude/skills (default: cwd)
claunch install --profile work     # or a profile's config dir
```

| Command | Description |
| ------- | ----------- |
| `install --profile P \| --project [DIR]` | Register the MCP server and write the `/cflow`, `/cflow-author` and `/mesh` skills. Supersedes the separate `cflow`/`mesh` server entries an earlier version registered — they are removed, not left running alongside. Restart claude afterwards. |
| `mcp` | The stdio MCP server itself (spawned by claude, not by hand): `start`/`report`/`next`/`select`/`status` from cflow, `send`/`members`/`history` plus `spawn`/`children`/`connect`/`disconnect` from mesh. |

| Skill | Triggers on | Teaches |
| ----- | ----------- | ------- |
| `/cflow` | running or resuming a workflow | the execution protocol: one step at a time, report before advance, and every way a run can stop — including answering a decision put to *you* by somebody else's run |
| `/cflow-author` | writing or revising a workflow file | how to choose control points: the weakest one that holds, and the decisions the driving agent must never be the one to answer |
| `/mesh` | joining a mesh, or needing another agent | the member protocol, and the only correct way to create a session from inside one |

**One server, several skills** — the asymmetry is deliberate. A skill's body is
loaded whole when it triggers, so merging them would make every session
running a workflow carry messaging rules it will never use (and authoring
rules it needs only when writing YAML), and each `description` would have to
cover enough ground to stop triggering precisely. The server has no such cost
(its tool schemas are in context either way), and splitting it had a real one:
the team-building tools ride with mesh, so a cflow-only install used to leave
an agent with no way to create a helper at all.

`cflow install` / `cflow mcp` and `mesh install` / `mesh mcp` still work —
the first two now install everything, and the `mcp` pair keeps serving its own
half so an install written before the merge is not broken by an upgrade.

## Mesh (session-to-session messaging)

Group sessions into a **mesh** and let the agents inside them message each
other. Delivery is the daemon **typing into the recipient's terminal**
(bracketed paste + Enter, coalesced while the recipient is mid-turn using the
idle tracker) — receivers need no watcher, no polling, no hooks and no MCP
server; arrival *is* the wake-up. Any harness works. Design notes:
`docs/mesh-design.md`.

```bash
claunch mesh create dev
claunch mesh join dev --session alpha --as leader     # or from inside a
claunch mesh join dev --as worker_1                   # session: $CLAUNCH_SESSION
claunch mesh send dev '*' "kickoff: read the plan in docs/"   # broadcast
claunch mesh send dev worker_1 "build the thing"              # direct
claunch mesh send dev leader "done" --type ack --reply-to msg-a1b2c3d4e5f6
claunch mesh send dev worker_1,worker_2 "sprint goal"     --section worker_1="you take the login API"     --section worker_2="you take token refresh"  # batch: each gets own slice
claunch mesh members dev          # members + peers + reachability
claunch mesh history dev          # ids, [type] tags, [re <id>] threading
claunch mesh policy dev --set heartbeat.enabled=true   # nudge policies
claunch mesh roles dev            # the vocabulary its handles resolve into
claunch mesh roles dev --yaml > roles.yaml   # edit, then --file roles.yaml
claunch mesh stance dev           # what your role is on this mesh
claunch mesh join dev@work-pc     # cross-machine: join the mesh owned there
claunch mesh requests             # ...pending joins: inbound and outbound
claunch mesh approve dev req-3f2a # ...the owner admits (or 'deny')
claunch mesh add dev              # owner-side wizard: pick a relay daemon ->
                                  #   pick its session -> enrolled, no codes
claunch mesh peers                # the other daemons registered on the relay
claunch mesh peers dev            # ...or this mesh's daemons in RANK order
claunch mesh rank dev laptop 0    # move a peer; position 0 hands it authority
claunch mesh cut dev laptop pc-b  # drop one direct link (falls back to rank 0)
claunch spawn --mesh dev --as worker_2 --role worker --task "take the API"
                                  # ...an agent can do this itself (MCP 'spawn')
claunch mesh connect dev worker_1 worker_2      # let two MEMBERS talk directly
claunch mesh disconnect dev worker_1 worker_2   # ...or stop them (send refused)
claunch mesh invite dev           # optional ticket that pre-approves one join
claunch mesh join dev@work-pc --code <ticket>   # ...admitted without waiting
claunch mesh revoke dev other-pc  # unlink a guest machine (persistent until then)
claunch install --project .       # MCP tools + the /mesh and /cflow skills
                                  # (and /cflow-author, for writing workflows)
```

- Inside a session, `join`/`send`/`leave` need no identity flags —
  `$CLAUNCH_SESSION` names the caller. Handles default to the session name;
  roles are inferred from the handle's leading word (`worker_1` → worker,
  `moderator` → leader).
- The recipient sees one fenced YAML block per burst (marked
  `machine-generated, not typed by the user`) listing sender, body and how to
  reply. Undelivered messages persist (per-member cursors survive daemon
  restarts) and land after `respawn` if the member's session was down.
- The web UI has a **Mesh** panel. Sidebar: create a mesh, or type
  `mesh@machine` (or paste an invite code — it is decoded in place) to join
  a remote one with a session/handle picker; meshes carry a `mirror` badge
  and a pending join-request count, and your own outbound requests are
  listed with a cancel. Mesh page: enrol sessions with handle/role, watch
  per-member reachability and pending counts, read the log, send as the
  human operator — and, on a mesh you own, **invite a remote session**
  (pick a daemon on the relay, pick one of its live sessions — the web
  equivalent of `claunch mesh add`, with nothing to copy by hand),
  approve/deny join requests, mint invite tickets for unattended joins, and
  revoke guest machines; a mirror shows its primary and keeps roster/policy
  controls read-only.
- Every session/mesh command prints a **relay status** line
  (`relay: connected as 'work-pc'` / `relay: DISCONNECTED ...`), because a
  mesh can only span machines while the relay uplink is registered.
- **Message intents** (ported from interconnect): `--type say` (default) or
  `ask` invite a reply; `fyi` / `ack` do not — the delivery block then says
  `needs_reply: false` / "no reply expected", which is what stops every agent
  from politely answering every utterance. fyi/ack deliveries also never arm
  the heartbeat nudge, and stall warnings go out as `fyi`. Unknown types are
  accepted but draw an advisory (a role name in `type` silently invites
  reply-all). Available on the CLI (`--type`), MCP `send`, the web send box,
  and the API (`type` field).
- **Batch sections** (ported from interconnect): one send can carry a shared
  preamble (`body`) plus per-recipient addenda — each recipient's terminal
  receives only the shared part and *its own* slice, never another member's
  instructions, while history keeps one composite message (one id). A section
  may override the intent per recipient (`fyi` for the peer who only needs to
  know, `ask` for the one who must act). CLI: repeatable
  `--section HANDLE=TEXT`; MCP/API: a `sections` object (`{handle: text}` or
  `{handle: {text, type}}`). Sending an un-batched body that @-addresses
  several recipients draws an advisory suggesting a batch. Every message also
  carries an **id** (shown in delivery blocks and history), and `--reply-to
  MSGID` / the `reply_to` field threads an answer to it.
- **Join briefing**: newly enrolled members get an idle-gated briefing block
  typed into their terminal (mesh, their handle/role, member list, how to
  send) — so a session enrolled from the web knows it joined something. It
  points at `claunch mesh stance <mesh>` rather than pasting the stance, so
  the member always reads the *current* one.
- **Roles**: a role is what a member **is** — its stance, who hears about a
  stall (`stall_watch`), its task-poll wording. The packaged vocabulary is
  interconnect's (`leader`/`operator`/`worker`/`reviewer`/`specialist`, with
  aliases, so `coder1` is a worker and `mod` leads; anything unrecognised
  defaults to `reviewer`, which audits rather than rubber-stamps). Each mesh
  may upload its own YAML — `claunch mesh roles <mesh> --file roles.yaml`,
  `PUT /api/mesh/{mesh}/roles`, or the web panel. A role in the upload
  replaces that role whole, `<name>: null` deletes one, `replace: true`
  swaps the lot; the **authority owns it** (a mirror's edit is forwarded) so
  every daemon reads the same handle the same way. **Uploads are not
  retroactive**: members keep the role they joined with, and one holding a
  role the new set dropped is surfaced as an *orphan* rather than migrated.
- **MCP tools + /mesh skill**: `claunch install` (`--project [DIR]` or
  `--profile NAME`) registers the stdio MCP server — whose mesh half is
  `send`/`members`/`history` (deliberately no receive tool: incoming messages
  arrive by injection) plus the team-building `spawn`/`children`/`connect`/
  `disconnect` (see [Agents that build their own
  team](#agents-that-build-their-own-team-spawn--hierarchy--member-graph)) —
  and writes the `/mesh` skill, the member protocol: idempotent join, how to
  read delivery blocks (`needs_reply`, intents, ids), sending discipline
  (direct over broadcast, batch sections for fan-outs, reply threading),
  role stances, growing a team, and membership recovery after a context
  compaction. The join briefing the daemon types into a new member's terminal
  tells the agent to activate this skill.
- **Cross-machine meshes (primary/mirror)**: every mesh has ONE owner — the
  daemon that created it is its **primary**, holding the authoritative
  roster, the single message log, the policy engine and invite minting.
  With both daemons registered on the same relay (and
  `allow_backend_peering` enabled on it), a session elsewhere joins by
  **address**: `claunch mesh join dev@work-pc`. Its daemon becomes a
  **guest** holding a *mirror* — a synced copy of roster + history for its
  UI and agents. Guest members are secondary: their joins,
  leaves and sends (even a DM between two members of the same guest daemon)
  are forwarded to the primary, which decides, sequences and fans out — so
  every daemon's history is identical. Credentials are mesh-scoped
  tokens (never daemon API tokens); members show as `work-pc/s0`-style
  addresses. If the primary is unreachable, the mirror stays readable,
  sends queue durably in its outbox (senders see `queued` immediately) and
  drain in order on reconnect; joins fail fast — membership is an
  authoritative decision.
- **The daemons form a graph, not a star**: `claunch mesh peers dev` lists
  them in **rank** order, and the order *is* the authority — `peers[0]`
  sequences the log, owns the roster and runs the policy engine, with no
  per-link role to declare anywhere. Every pair is linked directly (the
  authority brokers each edge's credentials, so no two daemons ever have to
  trust an unauthenticated first contact), and every link is duplex. When
  the authority is unreachable a send still goes **straight** to the daemons
  hosting its recipients — it reaches their terminals immediately and is
  folded into the log at its authoritative position once sequencing catches
  up — so an outage stops the record, not the conversation. Move the
  authority with `claunch mesh rank dev <machine> 0`; cut a single edge with
  `claunch mesh cut` (its traffic falls back to the authority's fanout).
  An edge belongs to both its ends, so **either end may cut or restore it**
  from its own CLI, while an edge between two other daemons stays the
  authority's call. The dashboard does not cut them at all: the peer graph is
  meant to be a full interconnect, so the mesh page shows it as a status
  board — linked, queued, unreachable — with a Restore button on any edge
  somebody cut, and spends its editing on the graph that *is* somebody's
  decision, the member one. Each daemon on that ring is drawn as a **cluster
  holding its agents**, arranged as the tree of who spawned whom, so one
  picture answers all three questions a mesh raises: which daemons are
  linked, who reports to whom, and who may message whom. The last of those
  is drawn as the pairs that **can** talk (a join wires a member to its
  parent and to whatever the mesh's rules match, and leaves the rest shut),
  clicking an agent lights up everyone it can currently reach — and, with
  one selected, every other agent wears a ⊕/⊗ that connects or disconnects
  the pair, mirrored row by row in a **Connections** list below.
- **Joining is asking to be admitted**: the first join from a machine is a
  *request* the mesh's owner sees in `claunch mesh requests` (and in the web
  UI) and answers with `approve`/`deny`; the grant is delivered back over
  the relay to the **claimed machine name**, so only the daemon actually
  registered under it can complete the join. `claunch mesh invite dev` mints
  an optional single-use ticket (24h) that pre-approves exactly one join —
  the unattended path, for automation that cannot wait for a human. Once a
  machine is admitted its link is **persistent**: further sessions there
  join with no ceremony, a lost mirror is re-granted automatically, and the
  owner ends it with `claunch mesh revoke dev <machine>`, which drops that
  machine's members and its mirror.
- **Owner-initiated invitations** (`claunch mesh add dev`): the mesh's owner
  can also *pull* a remote session in with no code changing hands — the
  wizard lists the other daemons registered on the relay (`mesh peers`,
  needs a PEER_LIST-capable relay), browses the chosen daemon's sessions,
  and pushes an invitation carrying an embedded one-shot ticket; the remote
  daemon validates its session and joins back through the ordinary
  join-by-address path. Trust model: one relay = one operator's machines
  (a single backend token), so the remote side does not re-confirm. On the
  web, pasting an invite code into the sidebar's mesh field decodes it in
  place and turns the form into a ready-made join.

### Agents that build their own team (spawn · hierarchy · member graph)

An agent inside a session can create **more** sessions, enrol them in its
mesh and decide who they may talk to — via the `spawn`, `children`,
`connect` and `disconnect` MCP tools, or `claunch spawn` by hand.

- **`spawn` is the door from inside a session; `new-session` is yours.**
  They build the same thing by different rights: `new-session` spells every
  field out, inherits nothing, records no lineage and obeys no policy —
  because the caller is the person who owns the machine. `spawn` gives the
  child its parent's program, its parent's mesh, a place in the tree, and a
  budget. Run from inside a managed session (`$CLAUNCH_SESSION` set),
  `new-session` is **refused** and prints the `spawn` command it would have
  been, flags translated — including `-c DIR` into the `--workspace` name
  that stands for it. The daemon cannot enforce this (an HTTP request carries
  no caller environment, and the web UI uses the same endpoint), so the CLI
  does. `--detached` creates one anyway, as nobody's child.
- **A child inherits what it runs** — harness, profile, working directory,
  args, env — from the session that spawned it. The agent chooses *who* it
  is: name, mesh handle, role, an opening `task`, optionally a `workflow`
  (a cflow run scoped to the child's own session). Each inherited field has
  its own unlock in `~/.claunch.yaml`, alongside the limits:

  ```yaml
  spawn:
    max_children: 4          # direct children per session
    max_depth: 3             # root session = depth 0
    allow_harness: [codex]   # [] = the parent's harness only
    allow_workspace: true    # ...the one that starts open (see below)
    allow_cwd: false         # ...allow_profile / allow_args / allow_env too
  ```
- **A child may be sent to another directory — by name, not by path.**
  `allow_workspace` lets the agent pass a `workspace` from your
  [registry](#workspaces-where-a-session-may-be-spawned); `allow_cwd` lets it
  pass a raw path. Separate unlocks, because they are separate risks — and
  the first is the **only one that defaults to on**. Every other field lets an
  agent invent a value; this one only lets it pick from a list you vouched
  for, an unknown name is refused *with the known ones* rather than spawning
  somewhere nobody chose, a directory that is not mounted right now is caught
  in the policy instead of surfacing three layers down as a harness that
  could not start — and if you have registered nothing, there is nowhere to
  send a child and the parent's directory is inherited as before. It is the
  picker the web UI's Directory field already is, handed to an agent, which
  has neither a filesystem in front of it nor a shell that completes paths.

  What it does widen is **reach**: a child can be sent into another
  registered repository and will edit the files there. If you registered your
  workspaces for the browser and would rather agents stayed put, set
  `allow_workspace: false`.

  An agent cannot read the registry, so the names come to it: `children`
  reports them alongside its budget.

  ```bash
  claunch spawn --workspace hq --task "port the API client"
  ```

  This is a **surface, not a sandbox**: an agent holds the daemon's API
  token, so the limits are blast-radius protection against runaway recursion
  and fan-out loops, not a boundary against a hostile session.
- **Sessions form a tree.** `claunch sessions` and the web sidebar both
  indent children under the session that spawned them. Authority runs *down*
  the tree only: a session may act on its descendants, never its parent and
  never its siblings. Children are restored on daemon restart like any other
  session, inheriting their parent's `restore` — so `--no-restore` on a root
  marks its whole subtree ephemeral.
- **A child lands in its parent's mesh without being asked to.** Naming no
  `mesh` on a spawn means *the parent's own*; a parent that is in none gets
  one opened for the pair, named after it, so the whole subtree ends up in
  one room. A parent in several has to say which — guessing there does not
  fail, it broadcasts, and the child would report its work to strangers.
  `--mesh -` starts a child in no mesh at all.

  The child is then told **whose it is**, on both channels: its system prompt
  carries the parent's name and handle (so it survives a compaction), and its
  opening block leads with the same fact plus the exact `claunch mesh send`
  that answers it. A child that does not know who is waiting reports to
  nobody, which is indistinguishable from having done nothing.
- **A spawned child starts connected to its parent and nobody else**, and
  the parent wires it up from there. This member graph is a different thing
  from the peer-daemon links `cut`/`uncut` edit: members are never routed,
  so a disconnected pair simply **cannot speak** — a direct send is refused
  and `'*'` skips them. `claunch mesh members dev` lists the disconnected
  pairs; the join briefing tells a member only who it can actually reach.

  ```
  lead spawns w1, w2  ->        lead            then: mesh connect dev w1 w2
                               /    \                        lead
                             w1      w2                     /    \
                          (w1 and w2 cannot talk)         w1 ---- w2
  ```

### Nudge policies (heartbeat · task-poll · stall warnings)

Per-mesh policies evaluated roughly **once a second** — on the mesh's
**primary daemon only** (a mirror's engine is a guarded no-op; its policy
copy is read-only). Local members are observed through their sessions
directly; remote (guest) members through the activity reports their daemons
piggyback on sync acks, and their nudges are shipped as fanout instructions
the guest daemon injects (re-checking idleness at fire time). The observable
state per member is: whether its session is *idle* (the screen-quiet
tracker), when the daemon last **delivered** into its terminal
(`last_delivered`), when the member last **sent** a mesh message
(`last_sent`), and how many messages are still *pending* injection. Two
derived states drive everything:

- **unanswered** — something was delivered and the member has sent nothing
  since (`last_sent < last_delivered`);
- **caught up** — not unanswered *and* nothing pending.

| policy | fires when | first fire | action |
| ------ | ---------- | ---------- | ------ |
| **heartbeat** | member is *unanswered* **and** its session is idle (a busy member is presumed working) | `last_delivered` + `interval` (default 180s) | injects a `kind: heartbeat` block into that member's terminal — never logged; for a guest member it ships as a fanout instruction that member's daemon injects |
| **task-poll** | member is idle **and** *caught up* **and** its role is in `roles` (default `worker` — leaders/reviewers have no queue to pull from) | last activity + `interval` (default 600s) | injects a `kind: task-poll` block whose text is `bodies[role]` (this mesh's override) → the **role set's** `task_poll` → a `{role}`-interpolated fallback |
| **stall warning** | a member the vocabulary does not mark `stall_watch` has held one state for `warn_secs` (default 600s): either *idle-stalled* (idle + caught up that long) or *behind* (pending messages whose injection never lands because the session never goes idle) | after `warn_secs` | sends a **real mesh message** from the external `policy` sender to every member whose role **is** `stall_watch` (the leader by default) — it enters the log, is delivered by injection, and **crosses machines over federation**; needs at least one such member to exist |

Each policy repeats with a per-member **doubling backoff** (`interval` → 2× →
4× … capped at `max_interval`; stall warnings double from `warn_secs`), and
resets the moment the trigger clears — a heartbeat stops as soon as the member
sends anything, a task-poll stops when work arrives, a stall warning stops
when the member becomes active. Example with heartbeat on (`interval` 180):
delivery at 10:00, member stays silent → nudges at 10:03, 10:09, 10:21, …
converging to one per `max_interval`; the first `claunch mesh send` from the
member ends the series.

All three are **off by default**: unlike interconnect's socket appends, every
nudge is a terminal injection that consumes the recipient agent's turn, so
enabling is a deliberate choice. Timers are in-memory (they restart with the
daemon); only the config persists, in `mesh.json`. There is no escalation
tier by design — delivery already *is* the escalation. Edit in the web mesh
view ("Nudge policy"), via
`claunch mesh policy <mesh> --set heartbeat.enabled=true ...`, or
`PUT /api/mesh/{mesh}/policy`.

The web mesh view's **Unanswered** box lists the same debt per message, and
lets an operator act on a row without waiting for a timer: **nudge** sends the
heartbeat's block to that member now (idleness is not re-checked — you are
looking at the row, and the automatic heartbeat's next fire is pushed out so
it does not pile on), and **dismiss** writes mail off that is never going to
be answered, one message or the lot. A dismissal is the only closure that is
not a reply: the message stays in the log, it just stops counting as a debt —
and it settles the heartbeat with it, so the row and the nudger never
disagree. A member hosted on another daemon can be nudged (its own daemon
injects) but is dismissed there, where its mail is counted.

## Web UI & HTTP API

The daemon doubles as a web server. `claunch web --open` prints/opens the UI:
a session list (status badges, create/kill) plus a **live xterm.js terminal**
attached over WebSocket — full input and output, multiple viewers allowed.

That socket **repairs itself**. A daemon restart, a laptop waking up or a
relay dropping its tunnel takes the terminal's connection with it, and the tab
goes and gets another one: a chip in the header counts the attempts down while
it retries on a backoff, and each attempt first asks the open `/api/health`
endpoint whether the daemon is even back — which also renews the login cookie,
since those live in the daemon's memory and die with it. Reconnecting is
cheap and lossless because every socket opens with a full repaint, so the
screen comes back as it now *is*, scrollback intact. Keystrokes typed while it
was down are held and replayed, but only into the same child — a session
relaunched under the same name gets a clean prompt and a note saying so.
The retries are bounded rather than endless: when they run out the chip says
`disconnected` and waits, and pressing it (or the network coming back, or the
daemon answering with a boot id the page has not seen) tries again. The rail's
version readout says `daemon offline` for as long as nothing answers, so a
list of sessions is never mistaken for a list of *current* sessions.

Both fields that used to take free text are now pickers. **Harness** lists the
[declared set](#other-harnesses-codex-pi-) — one that is declared but not
installed on this machine (`pi`, out of the box) is shown greyed out as
`pi (not installed)` rather than hidden.

The create form's **Directory** is a picker over your
[workspaces](#workspaces-where-a-session-may-be-spawned) — free-text paths are
deliberately not accepted here, since a mistyped one is both easy and
expensive. The **manage** link beside the field opens `#/workspaces`, the page
that edits that registry: register a directory (checked against the *daemon's*
filesystem before it is stored, so a bad path is refused with the reason
instead of failing later at spawn), see which entries are missing right now
and how many sessions are running in each, and unregister one — the directory
itself is never touched, and sessions already in it keep running. The list
refreshes in place, so a `claunch workspace add` in a terminal shows up here
too, and `(daemon cwd)` is always available in the picker.

A **Start it working** box carries what the session is *for*: a mesh picker
(with a handle field once one is chosen), a workflow picker over the runs
declared in the chosen directory, and an opening task — all applied in the
same call that creates it, so the agent's first turn already knows its mesh
identity and its run. See
[Created with a job](#created-with-a-job-mesh--workflow--opening-task).

The form otherwise spawns sessions the same way the CLI does, including the
two choices that can only be made at spawn (see
[Spawning with a role…](#spawning-with-a-role-or-from-another-sessions-conversation)):
a **Role** picker that shows the stance it would inject before you commit to
it, and a **Resume** picker offering claude's own conversation picker or any
session this daemon knows — exited ones included, since their conversations
outlive them — with **`--fork-session`** as a checkbox that only unlocks once
there is something to fork.

An **exited** session is not a dead end in the browser either: open it and the
header offers **resume**, the `claunch respawn` of the UI — the session comes
back under its own name, claude with `--resume` of its pinned conversation, and
the tab reattaches to the new terminal (a resume done elsewhere, from the CLI
or another tab, is followed automatically). There `kill` becomes **remove**,
which only drops the daemon's record — it asks first, since that is what makes
the session unresumable.

The same four verbs are available for the *whole* rail at once, under the
session list: **■ stop N**, **▶ resume N**, **clear N exited** and
**✕ delete all N**. Each is labelled with what it would touch and is hidden
when that is nothing, so the bar reads as a summary of the rail rather than a
fixed row of controls. They differ in what survives, which is why there are
four and not two: `stop` ends the programs and keeps every record respawnable
(the whole fleet comes back with `resume`), while `clear` and `delete` are the
ones that make a session unresumable — those two ask first, and a record a
mesh row still names is kept back and reported rather than dropped. `delete`
stops the running sessions and *waits them out* before forgetting anything,
which is why it is one call (`DELETE /api/sessions?running=1`) and not a stop
followed by a clear: a session that has just been signalled is not yet
`exited`, and a clear sent straight after would skip exactly the sessions it
was meant to remove.

Every session row carries an **ⓘ** (and the terminal header a **details**
button) that opens the session's own page (`#/s/<name>`) — what the session
*is*, as opposed to what it is printing: harness, profile, role (with the
stance it injects), directory and the workspace it belongs to, pinned
conversation, resume/fork, size, pid and timestamps, the meshes it is a
member of — and its **workflow**. That last one is exact rather than
guessed: a cflow run is keyed by (directory, scope) and the scope *is* the
session name, so the page shows the one slot this session owns — the live
run's step and latest reports (with a link to the run page), or, when it is
idle, the picker that starts one. See
[Who starts a run](#who-starts-a-run--two-paths-one-writer) for why the
picker offers *Ask the agent to start* and *Start directly* as two different
buttons.

Above the memberships the same panel carries a **Send message** box: the
[mesh](#mesh-session-to-session-messaging) send with the recipient already
answered, since the panel knows which session it is about. Pick the mesh (a
session is called something different in each one it joined) and the intent —
`say`, `ask` (which puts the answer on the sender's ledger), `fyi`, `ack` —
and it goes in as a message from you, the operator: sequenced into that mesh's
log and typed into the agent's terminal by the daemon between turns, rather
than pasted blind into the terminal beside it. A session in no mesh has
nothing to carry a message, and the box says so instead of offering a dead
form.

Beside the memberships sits **Message trace**, which opens the third reading
of a session (`#/msg/<name>`, one tab per mesh it is in): not what it is doing
and not how far its run has got, but who it has been working *with*, drawn as
a sequence — a lane per party, the operator's lane beside the members rather
than among them, and time down the page. It is the whole room, not this
session's mailbox: a question from `lead` to `reviewer` is often why the next
message arrived here, so traffic this session is not a party to is faded
rather than dropped. Each arrow says where it got to — a filled head and a
filled mark per recipient for a message typed into that terminal, a dashed
line and hollow marks for one still queued, and a distinct mark for a
recipient on another daemon, whose consumption only that daemon can report. An
`ask` nobody answered carries a **⚠ n unanswered** chip in the right-hand
margin, and the same **Unanswered** box the mesh page shows sits above the
diagram with its nudge and dismiss buttons — but only while something is
owed. Runs of silence longer than five minutes fold into
one `⋯ 14m quiet ⋯` marker, each member's arrival opens its lane (with who
spawned it), and this session's own [cflow](#cflow-declarative-agent-workflows)
steps and reports are marked on its lane, so "it answered and then moved on to
review" is one thing to read rather than two pages. Click a message to unfold
its whole body. Only what travelled *through* the mesh is there — words typed
straight into a terminal leave no record, and the page says so rather than
implying it has everything.

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
has no way to approve. A slot with a pending start request is listed there
too, so the wait between asking and the agent picking it up is visible.

A mesh's page (`#/mesh/<name>`) leads with the **topology diagram** — a
cluster per daemon on the rank ring, each holding its spawn forest, with cuts
overlaid and reachability on demand. Its **flow view** link opens the same
mesh read a second way (`#/mesh/<name>/flows`), where every agent is a card
carrying its whole [cflow](#cflow-declarative-agent-workflows) run as a
track: one pip per step in the run page's own order, visited behind it, a
haloed pip where it is now, hollow ahead; a diamond for a branch, bars either
side of a pip for the gate you must be let through and the verify you must
pass to leave, and an arc under the rail wherever the workflow loops back.
The two pictures place the same mesh identically — the layout is literally
the same code — so they read as two zoom levels of one thing rather than two
diagrams.

What it is *for* decides its styling: an agent waiting on a **human** is the
loudest thing on the page, and those cards are repeated in a **Waiting on
you** strip above the canvas with the Approve and option buttons that clear
them, so the answer is in the same place as the question. An agent-chooser
select is deliberately not in that strip — that branch is the agent's own
call — and neither is a **delegated** decision, which is stopped but on a
peer rather than on you: it gets its own colour and its own word, so the
strip stays a list of things you actually have to do. Clicking a card opens the full state machine underneath it, unchanged
from the run page, alongside who spawned it, who it may message and a link to
the run. Members with no run say so rather than showing an empty track, and
members on another daemon say that their state lives over there.

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
| GET    | `/api/health`                  | liveness + `boot_id` (unauthenticated — a client whose login died in a restart can still tell "not back yet" from "back, log in again") |
| POST   | `/api/auth/session`            | token → HttpOnly cookie (browser login) |
| GET    | `/api/daemon`                  | version/`boot_id`/uptime/session count |
| POST   | `/api/daemon/shutdown`         | graceful stop |
| GET/POST | `/api/sessions`              | list / create (`{name?, harness?, profile?, cwd?, args?, env?, role?, resume?, fork_session?}`; `resume` = session name, conversation uuid, or `""`/`true` for claude's picker). Onboarding is optional and composed in the same call: `{mesh?, handle?, connect?, workflow?, context?, task?}` — checked before anything is built, and reported per leg beside the session's own fields |
| DELETE | `/api/sessions`                | clear all exited records (`?logs=1` deletes their logs; `?running=1` first shuts down and waits out every running session, so this drops *all* of them — `stopped` names what it ended). Records a mesh still names are kept back and reported in `kept` |
| POST   | `/api/sessions/kill`           | stop every running session (`?force=1`). Records stay, so all of them are still respawnable; `killed`/`failed` name both halves |
| POST   | `/api/sessions/respawn`        | relaunch every exited session under its own name, in creation order; `respawned`/`failed` |
| GET/DELETE | `/api/sessions/{name}`     | info / kill (`?force=1`) |
| GET    | `/api/sessions/{name}/meta`    | everything known *about* one session: definition, workspace, harness, role stance, mesh memberships, its cflow slot and the workflows startable in it |
| POST   | `/api/sessions/{name}/respawn` | relaunch an exited session (claude resumes its conversation) |
| POST   | `/api/sessions/{name}/keys`    | raw keyboard: `{keys: [...], literal}` — send-keys; or `{paste, enter}` — one bracketed paste (multiline-safe) |
| POST   | `/api/sessions/{name}/deliver` | `{text}` — hand the agent a message (paste + separately-written Enter). What every automated sender uses; `/keys` is for a human at a keyboard |
| GET    | `/api/sessions/{name}/capture` | `?history=1&format=json&trim=0` |
| GET    | `/api/sessions/{name}/wait`    | long-poll `?state=idle\|exited&timeout=&threshold=` |
| POST   | `/api/sessions/{name}/resize`  | `{cols, rows}` |
| GET    | `/api/sessions/{name}/ws`      | terminal WebSocket (binary = PTY bytes, text = JSON control) |
| GET    | `/api/profiles`                | profile names (for the UI's create form) |
| GET    | `/api/roles`                   | the roles a session can be spawned with, each with its aliases, stance and the exact system-prompt injection |
| GET    | `/api/workspaces`              | registered directories, for the create form's picker and the manage page |
| POST   | `/api/workspaces`              | register one — `{"path": "...", "name": "..."}`; `400` (with the reason) if the directory is not there |
| DELETE | `/api/workspaces/{name}`       | unregister one; the directory itself is untouched |
| GET    | `/api/harnesses`               | declared harnesses, each with `available` (is it installed on this machine) and `builtin` |
| GET/POST | `/api/mesh`                  | list meshes (+ relay status) / create `{name}` |
| GET/DELETE | `/api/mesh/{mesh}`         | members + reachability / remove |
| POST   | `/api/mesh/{mesh}/members`     | `{session, handle?, role?, code?}` — enrol a session; `{mesh}` may be `mesh@machine` (201 admitted, 202 pending approval) |
| DELETE | `/api/mesh/{mesh}/members/{handle}` | remove a member |
| GET/POST | `/api/mesh/{mesh}/messages`  | history (`?limit=`) / send `{from, to, body, external?}` |
| GET    | `/api/mesh/{mesh}/flows`       | every member's cflow run, plus the workflow graphs behind them (deduplicated per `workflow@cwd`) — the roster/run join the flow view is drawn from |
| GET    | `/api/mesh/{mesh}/owed`        | unanswered mail per member: who was asked what, and how long ago |
| POST   | `/api/mesh/{mesh}/members/{handle}/nudge` | ask that member about it now (`{body?}` overrides the heartbeat's wording) |
| DELETE | `/api/mesh/{mesh}/members/{handle}/owed[/{id}]` | dismiss its unanswered mail — one message, or all of it |
| GET/PUT | `/api/mesh/{mesh}/policy`     | read / edit the mesh's nudge policy (heartbeat, task-poll, stall warnings) |
| GET/PUT | `/api/mesh/{mesh}/roles`      | read / upload the mesh's role set (`{yaml}` or `{roles}`; either null resets to the packaged vocabulary) |
| POST   | `/api/mesh/{mesh}/invite`      | mint a single-use ticket pre-approving one join |
| GET/DELETE | `/api/mesh/{mesh}/invites[/{prefix}]` | list / revoke outstanding tickets |
| POST   | `/api/mesh/{mesh}/requests/{id}/approve\|deny` | decide a pending join request |
| DELETE | `/api/mesh/{mesh}/guests/{machine}` | unlink a guest machine (drops its members + mirror) |
| DELETE | `/api/mesh/outgoing/{id}`      | forget one of our own pending join requests |
| POST   | `/api/mesh/{mesh}/invitations` | `{machine, session, handle?, role?}` — owner pushes an invitation to a relay peer's session |
| GET    | `/api/relay/peers`             | the other daemons registered on the relay (PEER_LIST) |
| GET    | `/api/relay/peers/{machine}/sessions` | that daemon's live session names (proxied over the bridge) |
| POST   | `/peer/mesh/*`                 | daemon↔daemon federation (join_request/grant/invite/unlink/join/leave/send/sync) — authenticated by per-link mesh tokens, not the API token |
| POST   | `/peer/sessions`               | live session names for same-relay peers (wizard browsing) |
| GET    | `/api/cflow`                   | all registered cflow runs, keyed (cwd, scope), with status + step reports; `?cwd=[&scope=]` inspects explicitly |
| GET    | `/api/cflow/run`               | `?cwd=&scope=` — run detail: status, workflow graph, reports, journal |
| POST   | `/api/cflow/request`           | `{cwd, scope, workflow, context?}` — **ask** the scope's agent to start a workflow (records the request + nudges; the agent runs the start) |
| POST   | `/api/cflow/request/cancel`    | `{cwd, scope}` — withdraw a pending start request |
| POST   | `/api/cflow/start`             | `{cwd, scope, workflow, context?}` — start a run **directly** (the fallback: no live session to ask) |
| POST   | `/api/cflow/approve`           | `{cwd, scope}` — approve the entry approval / extend the loop limit / override a decline |
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
claunch install --profile work         # MCP server + /cflow skill + the shipped workflows
claunch cflow ls                       # what this directory can run, and from which file
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
    ask:                          # approval to ENTER; re-required per visit
      prompt: the diff is up — allow the review pass?
    instructions: Relay the review feedback into follow-ups.
    next: verdict                 # no 'from' = nobody is asked: a human gate

  verdict:
    select:
      prompt: Ready, or another pass?
      chooser: user               # agent | user | a delegation (see below)
      options:
        ready:  {description: ship it,           next: ship}
        rework: {description: loop back,         next: impl}   # a cycle

  ship:
    ask:
      prompt: approve committing and opening a PR?
      from:                       # WHO is asked, in preference order
        - {role: reviewer}        # anyone reachable holding that role
        - {role: leader, scope: ancestor}   # ...else up the chain of command
      otherwise: human            # ...and if none of them answers: a person
      timeout: 900                # per group; then it moves down the list
      on_decline: impl            # where a refusal goes
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
shows `loops: impl x3`). Approvals and non-agent selections apply **per
visit** — a review approval inside a loop closes again on every pass, and a
delegated one is asked again (the second answer may differ from the first).
Arriving at a step beyond `max_visits` (default 25) pauses the run the same
way until a human extends it with `claunch cflow approve`, so an agent-driven
loop cannot spin forever.

**Where workflows live.** Two layers, nearest first:

| | | |
|---|---|---|
| `<cwd>/.claunch/workflows/*.yaml` | project | this directory only; **wins** |
| `~/.claude-launcher/workflows/*.yaml` | shared | every directory on the machine |

`claunch install` seeds the shared layer with the workflows that ship in the
package (`feature-dev`, `delegated-dev`), and never overwrites one you have
edited — it says `kept; yours differs` and leaves it. Put your own there with

```bash
claunch cflow add ./ops.yaml           # a file
claunch cflow add ecs-change           # or promote this project's copy
claunch cflow add ops --project        # the other direction: fork it locally
```

which parses the workflow before installing it, so a broken YAML is refused
where you are standing rather than in someone else's picker a week later.

A project only needs a file of its own when it wants to **differ**; that copy
then shadows the shared one. Nothing about that is ambiguous — the project
always wins — but the loser is *named* everywhere the winner appears
(`claunch cflow ls`, the dashboard's start picker, and a running run's
header), because two copies of one workflow drift silently otherwise:

```
$ claunch cflow ls
ecs-change       15 steps  LSP recon -> ... -> ship  [F:\works\ShelterZero\.claunch\workflows\ecs-change.yaml]
                 project copy overrides [C:\Users\me\.claude-launcher\workflows\ecs-change.yaml]
```

Runs are keyed by **(directory,
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
| `select` (`chooser: {from: …}`) | another agent, else a human | the run blocks until a responder calls `answer {ask, decision, reason}` with one of the declared options; the driving agent may not `select` at all |
| `ask:` | another agent, else whatever `otherwise` says | the step's instructions are withheld until an approval is recorded — by a responder's `answer`, or by `claunch cflow approve` |
| `gate:` | a human | **deprecated** spelling of `ask: {prompt: …}`; still works, and `cflow show` says where you still use it |
| `verify:` | a machine | the server runs the command on `next`; non-zero exit refuses to advance and returns the output |
| `report` | the agent | required before `next`; journaled, shown live on the web dashboard, discarded by a failed `verify` |

**Delegated decisions** have two independent axes. `from` is **who is
asked**: an ordered list of roles, read one group at a time. A group that
matches nobody is skipped with its reason; a group that matches several is
asked at once and the first valid answer wins; a group that runs out of
`timeout`, or whose members all answer `abstain`, hands on to the next.
`otherwise` is **what happens when that list is exhausted** — `human` (the
default: the run holds for `claunch cflow approve|select`) or `self` (the
driving agent carries on alone, journaled as *unanswered*, never as an
approval). A human is never an entry in `from`: nothing resolves them,
nothing notifies them, and they answer through a different door — so `ask:`
with no `from` at all is exactly a human gate, which is what `gate:`
deprecates into.

The responder answers with `answer {ask, decision, reason}` from its own
session — never receiving the asking step's instructions — and which session
that is comes from the environment the daemon set, not from the call's
arguments. The question itself arrives as a `decide` message on the mesh,
which is only a doorbell: it is recorded and answerable via `asks` whether or
not the message landed.

Three things make this an approval rather than a formality:

- **A candidate is never something the run made.** The pool is what the
  asking session can reach over the mesh, *minus itself and everything below
  it in the spawn tree*. It can spawn a child and wire itself to it; it can
  neither spawn a sibling nor wire itself to one (`connect` requires
  authority *over* both ends), so a sibling reviewer is as trustworthy as an
  ancestor — and is the common shape. `scope: ancestor` narrows to the chain
  of command when a workflow wants only that.
- **The answer set is closed** — the declared options plus `abstain` — so
  nothing is parsed out of an LLM's prose.
- **Nothing fails open.** No daemon, no mesh membership, an ambiguous mesh, a
  candidate on another machine, a member nobody wired you to, a decline with
  no declared route: each ends with the question in front of a human. The one
  exception is explicit, per-workflow and journaled as unanswered:
  `otherwise: self`.

`start` and `claunch cflow request` both report a `delegation_check` — what
each delegated step resolves to *right now* — without blocking on it: a
leader that has not spawned yet is legitimate, and the step may be an hour
away. Which mesh to resolve responders in is a property of the *run*, not the
workflow (`start {workflow, context, mesh}`), and is only needed when the
driving session belongs to more than one.

### Who starts a run — two paths, one writer

A run can be created from two places, and they are not symmetric:

- **the agent** calls the MCP `start` tool (`/cflow <workflow>`), or
- **a human** creates one from the dashboard / CLI.

Only the first is safe on its own: the agent that will drive the run is the
process that created it. If the dashboard wrote the run instead, the agent's
next `report`/`next` would land in a run it has never read — cflow's tools
name no run id, so nothing would notice.

So the human path is a **request**, not a write. `claunch cflow request <wf>`
(or the dashboard's *Ask the agent to start*) records
`.cflow/runs/<session>/request.json`, nudges the session, and stops. The
agent sees `pending_start` in its next `status` — the call the `/cflow`
protocol already makes it do after any nudge — and performs the `start`
itself, which consumes the request. One writer, and an agent that always
knows what it is running. Withdraw an unclaimed request with
`claunch cflow request --cancel` (or *Withdraw request* on the dashboard).

*Start directly* remains for the case with nobody to ask: a slot whose
session is not live (an agent that attaches later, an orchestrator script).
It writes the run and nudges whatever is there.

Underneath, three mechanisms keep the two writers — the agent's MCP server
and the daemon — from corrupting each other:

| Hazard | Guard |
| ------ | ----- |
| two starts interleaving (one workflow's snapshot, another's cursor) | every state transition holds the slot's `.cflow/runs/<scope>/.lock`; a lock left by a killed process is reclaimed after 2 minutes |
| a `verify` command (minutes to an hour) committing into a run a human moved meanwhile | verify runs **outside** the lock and commits only if run id / step / visit are unchanged — otherwise the result is discarded and journaled as `verify_discarded` |
| an agent writing into a run that was archived and replaced under it | the MCP server fences on the run id it last handed out: the call is refused, nothing is applied, and the agent is told to re-read `status` |

**A run never approves itself, by design.** For the run it drives, the MCP
surface is only `start` / `report` / `next` / `select` / `status` — there is
no approve tool, so an approval cannot be talked past. `asks` and `answer`
exist alongside them, but they act on *other* sessions' runs and refuse both
a request that was not put to this session and one from its own run: there is
no arrangement of tool calls that unblocks a step gated on the agent making
them. Humans approve through the CLI or the token-authenticated web
dashboard; both are outside the agent's reach. While blocked, the agent stops
its turn and tells you how to unblock; inside a chat session you can approve
without leaving:

```text
! claunch cflow approve
! claunch cflow select human
```

When the agent runs as a [managed session](#managed-sessions-tmux-style-daemon)
in the run's directory, approving/selecting (CLI or dashboard) also
**auto-nudges** it — a resume line is delivered into the session as a user
message, so the stopped agent picks the run back up on its own. The run page also has a
**Nudge session** button to re-send that line manually whenever the agent
stalls. Elsewhere (e.g. `!` inside the chat itself) nudge the agent with any
message. The same CLI works from
outside — a supervising script or another agent can watch
`claunch cflow status --json`, approve gates, and drive the worker session via
`claunch send-keys` for multi-agent orchestration.

### cflow commands

| Command | Description |
| ------- | ----------- |
| `cflow ls` / `show <wf>` | List workflows (with the file each name resolves to, and what it overrides) / print a workflow's step tree. |
| `cflow status [--json]`  | Active run: current step, state, how to unblock (plus any pending start request). |
| `cflow request <wf> [-c CTX]` / `--cancel` | Ask this session's agent to start a workflow / withdraw the request. The agent runs the `start` itself. On the dashboard: the session page's start picker. |
| `cflow approve`          | Approve the current entry approval or loop guard — including overriding a responder's decline, or taking a delegated question away from an agent that is stuck (human-only: CLI or web dashboard). |
| `cflow select <opt> [--reason]` | Confirm (or override) a user-chooser branch, or settle a delegated one. |
| `cflow asks [--session S]` | What decisions other runs are waiting on a session for (read-only; humans answer via `approve`/`select`, so an override is recorded as one). |
| `cflow goto <step> [--reason]` | Force the current step (`end` finishes; journaled, re-gates, auto-nudges). On the dashboard: click a diagram node. |
| `cflow journal [-n N]`   | Print the run journal (JSONL). |
| `cflow archive`          | Retire the run (finished or not) into `.cflow/.../archive/`, freeing the slot for a new start. Active runs are aborted first; a new `start` auto-archives finished runs. On the dashboard: the Archive button + start picker. |
| `cflow abort` / `reset`  | Abort the run / clear run state (journal kept). |
| `cflow example [name]`   | Scaffold the example workflow above into this project. |
| `cflow add <wf>... [--name N] [--project] [--force]` | Install a workflow (a `.yaml` path, or a name findable from here) into the shared layer, so every directory can run it — `--project` installs into this one instead. Parses it first; refuses to replace a different file without `--force`. |
| `cflow install` / `cflow mcp` | Aliases kept for installs written before the servers merged — see `install` and `mcp` in [Toolkit commands](#toolkit-commands-what-an-agent-gets). |

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
| `CLAUNCH_SYNC_URL`          | [Sync server](#profile-sync-server) URL, overriding `sync.url`. |
| `CLAUNCH_SYNC_TOKEN`        | Sync auth token, overriding `sync.token` (the preferred place for it). |
| `CLAUNCH_SYNC_NAMESPACE`    | Synced document's namespace, overriding `sync.namespace`. |
| `CLAUNCH_SYNC_SERVER_DIR`   | Server side: documents + accounts (default `<launcher home>/sync-server`). |

## License

MIT
