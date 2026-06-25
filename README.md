# claude-launcher

`claude-launcher` (command: `claunch`) lets you run [Claude Code](https://claude.com/claude-code)
under **multiple isolated profiles**. Each profile owns its own login token and
configuration by pointing `CLAUDE_CONFIG_DIR` at a dedicated directory.

Logging in uses `claude setup-token` (a long-lived OAuth token) instead of the
interactive `/login` flow, so each profile keeps its own credentials.

> **Where the token lives.** `claude setup-token` only *prints* the token — it
> does not write `.credentials.json`. Claude Code consumes it via the
> `CLAUDE_CODE_OAUTH_TOKEN` environment variable. So `claunch login` captures the
> printed token and stores it at `<profile>/.launcher-token` (`0600`), and
> `claunch run` injects it as `CLAUDE_CODE_OAUTH_TOKEN`. You don't store it
> anywhere yourself.

## Why

Claude Code stores credentials and settings under a single config directory by
default. If you need to switch between accounts (e.g. personal vs. work, or
multiple Max subscriptions), they collide. `claunch` gives each profile its own
`CLAUDE_CONFIG_DIR`, so tokens and settings never mix.

## Install

```bash
uv tool install claude-launcher
# or, from a local checkout:
uv tool install .
```

This puts `claunch` on your PATH. Requires the `claude` CLI to be installed.

## Usage

```bash
claunch create work            # create a profile (seeds global config; see below)
claunch login work             # run `claude setup-token`, capture + store the token
claunch run work               # launch Claude Code using that profile
claunch run work -- --help     # pass args through to claude
claunch usage work             # query subscription usage for the profile
claunch list                   # list profiles
claunch path work              # print the profile's CLAUDE_CONFIG_DIR
claunch remove work            # delete a profile and its tokens (alias: delete)
```

### Seeding (skip onboarding)

A profile is a fresh `CLAUDE_CONFIG_DIR`, so Claude Code would replay onboarding /
landing on first run. To avoid that, `claunch create` copies your global config
into the new profile — carrying over the onboarding flags (`hasCompletedOnboarding`
etc.), UI preferences and `settings.json`, while **stripping** account- and
project-specific data (`oauthAccount`, `projects`, cached API-key responses) so
profiles stay isolated. Each profile still logs in with its own setup-token.

```bash
claunch create work                 # seed from CLAUDE_CONFIG_DIR or ~/.claude
claunch create work --seed-from DIR # seed from a specific config dir
claunch create work --no-seed       # start fully fresh (onboarding will run)
```

### Usage reporting

`claunch usage <name>` reads the profile's OAuth login token and queries the
Anthropic usage endpoint (the same one Claude Code uses), printing per-window
utilization:

```text
usage for profile 'work' (subscription: max)
  five_hour          [##------------------]   9.0%  (resets in 4h34m)
  seven_day          [--------------------]   2.0%  (resets in 5h44m)
```

Add `--json` to get the raw API response. The query runs against the token of
that profile only, so each profile reports its own account's usage.

If auto-capture ever fails (e.g. the interactive flow renders the token in a way
the launcher can't read), store it manually:

```bash
claude setup-token                       # run it yourself, copy the token
claunch set-token work sk-ant-oat01-...  # or omit the value to paste via stdin
```

### How it works

- Profiles live under `~/.claude-launcher/profiles/<name>` (override the base
  with `CLAUDE_LAUNCHER_HOME`). That directory is the profile's `CLAUDE_CONFIG_DIR`.
- `claunch login`/`run` export `CLAUDE_CONFIG_DIR=<profile dir>` before invoking
  `claude`, so each profile's settings stay isolated.
- The setup-token is stored at `<profile>/.launcher-token` and injected as
  `CLAUDE_CODE_OAUTH_TOKEN` on `claunch run`, which is how Claude Code
  authenticates non-interactively.

## Configuration

| Environment variable   | Purpose                                            |
| ---------------------- | -------------------------------------------------- |
| `CLAUDE_LAUNCHER_HOME`      | Base directory for profiles (default `~/.claude-launcher`). |
| `CLAUDE_LAUNCHER_BIN`       | Path/name of the `claude` executable (default `claude`).    |
| `CLAUDE_LAUNCHER_USAGE_URL` | Usage endpoint (default `https://api.anthropic.com/api/oauth/usage`). |
| `CLAUDE_LAUNCHER_SEED`      | Config dir new profiles seed from (default `CLAUDE_CONFIG_DIR` or `~/.claude`). |

## License

MIT
