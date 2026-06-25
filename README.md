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
[Sync](#sync-profiles-export--import) · [Usage](#usage-reporting) ·
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

Now source edits take effect on the **next** `claunch` invocation — no reinstall.
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
| `run <name> [args...]` | Launch `claude` for the profile; any extra args pass through. |
| `env <name> [...]`     | View/edit the profile's env vars (`--effective` for merged). |
| `parent <name> [p]`    | Show, set, or `--clear` a profile's parent. |
| `template [--init]`    | Show or write the default env template. |
| `export [path]`        | Write all profile settings to YAML (default `~/.claunch.yaml`). |
| `import [path]`        | Apply profile settings from YAML (`--prune`, `--no-seed`). |
| `validate [name]`      | Health-check logins via `claude -p heartbeat` (all if no name). |
| `usage <name>`         | Query subscription usage (`--json` for the raw response). |
| `set-token <name> [t]` | Store a token manually (pasted, or piped via stdin). |
| `list`                 | List profiles and each login's state (alias: `ls`). |
| `path <name>`          | Print the profile's `CLAUDE_CONFIG_DIR`. |
| `remove <name>`        | Delete a profile and its tokens (aliases: `delete`, `rm`). |

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
cached API-key responses) so profiles stay isolated. Each profile still logs in
with its own setup-token.

```bash
claunch create work                 # seed from CLAUDE_CONFIG_DIR or ~/.claude
claunch create work --seed-from DIR # seed from a specific config dir
claunch create work --no-seed       # start fully fresh (onboarding will run)
```

## Per-profile environment variables

Each profile can set Claude Code environment variables, stored in its
`settings.json` `"env"` block. `claunch run` also exports them into claude's
process, so they take effect immediately and **override** any value inherited
from your shell.

```bash
claunch env work                                  # list this profile's env vars
claunch env work CLAUDE_CODE_AUTO_COMPACT_WINDOW=200000   # set one or more
claunch env work --unset FOO BAR                  # remove vars
claunch env work --apply-template                 # merge the template defaults
```

### Default template

New profiles get a default env block from `<launcher home>/template.json`. The
built-in defaults are:

```json
{
  "env": {
    "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "0",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "400000"
  }
}
```

Edit that file (or run `claunch template --init` to create it) to change the
defaults for future profiles. **Existing profiles are not changed automatically**
— apply the current defaults to one with:

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

## Sync profiles (export / import)

Keep every profile's settings in one YAML file — `~/.claunch.yaml` by default —
so you can version it or copy it between machines.

```bash
claunch export                 # write ~/.claunch.yaml
claunch import                 # recreate/update profiles from ~/.claunch.yaml
claunch import other.yaml      # use a specific file
claunch import --prune         # also delete local profiles absent from the file
claunch import --no-seed       # don't seed global config into new profiles
```

The file captures the profile list, each profile's `env`, and the default
template:

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

`import` is authoritative: it creates missing profiles (seeded from your global
config), sets each profile's `env` to exactly what the file says, restores
`parent` links, and with `--prune` removes profiles the file doesn't list. **Login tokens are never
exported** — they are secrets and stay per-machine, so run `claunch login` on
each machine after importing. Override the default path with
`CLAUDE_LAUNCHER_SYNC_FILE`.

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

## How it works

- Profiles live under `~/.claude-launcher/profiles/<name>` (override the base
  with `CLAUDE_LAUNCHER_HOME`). That directory **is** the profile's
  `CLAUDE_CONFIG_DIR`.
- `login` / `run` export `CLAUDE_CONFIG_DIR=<profile dir>` before invoking
  `claude`, keeping each profile's credentials and settings isolated.
- `run` exports the profile's `settings.json` `env` vars into claude's process
  (overriding the inherited shell), plus `CLAUDE_CODE_OAUTH_TOKEN` when a token
  has been stored — so it authenticates and runs non-interactively.

A profile directory typically holds:

| File | Origin |
| ---- | ------ |
| `.claude.json`      | Seeded from your global config (onboarding flags, prefs). |
| `settings.json`     | Seeded global settings + the profile's `env` block. |
| `.launcher-token`   | OAuth token stored by `set-token` (`0600`). |
| `.launcher.json`    | Launcher metadata (e.g. the profile's `parent`). |
| `.credentials.json` | Written by Claude Code itself after an interactive login. |

## Configuration

| Environment variable        | Purpose |
| --------------------------- | ------- |
| `CLAUDE_LAUNCHER_HOME`      | Base directory for profiles (default `~/.claude-launcher`). |
| `CLAUDE_LAUNCHER_BIN`       | Path/name of the `claude` executable (default `claude`). |
| `CLAUDE_LAUNCHER_USAGE_URL` | Usage endpoint (default `https://api.anthropic.com/api/oauth/usage`). |
| `CLAUDE_LAUNCHER_SEED`      | Config dir new profiles seed from (default `CLAUDE_CONFIG_DIR` or `~/.claude`). |
| `CLAUDE_LAUNCHER_SYNC_FILE` | YAML file for `export`/`import` (default `~/.claunch.yaml`). |

## License

MIT
