# claude-launcher

`claude-launcher` (command: `claunch`) lets you run [Claude Code](https://claude.com/claude-code)
under **multiple isolated profiles**. Each profile owns its own login token and
configuration by pointing `CLAUDE_CONFIG_DIR` at a dedicated directory.

Logging in uses `claude setup-token` (a long-lived OAuth token) instead of the
interactive `/login` flow, so each profile keeps its own credentials.

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
claunch create work            # create a profile named "work"
claunch login work             # run `claude setup-token` for that profile
claunch run work               # launch Claude Code using that profile
claunch run work -- --help     # pass args through to claude
claunch usage work             # query subscription usage for the profile
claunch list                   # list profiles
claunch path work              # print the profile's CLAUDE_CONFIG_DIR
claunch remove work            # delete a profile (and its tokens)
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

### How it works

- Profiles live under `~/.claude-launcher/profiles/<name>` (override the base
  with `CLAUDE_LAUNCHER_HOME`).
- `claunch login <name>` and `claunch run <name>` both export
  `CLAUDE_CONFIG_DIR=<profile dir>` before invoking `claude`, so credentials
  written by `setup-token` are reused at launch.

## Configuration

| Environment variable   | Purpose                                            |
| ---------------------- | -------------------------------------------------- |
| `CLAUDE_LAUNCHER_HOME`      | Base directory for profiles (default `~/.claude-launcher`). |
| `CLAUDE_LAUNCHER_BIN`       | Path/name of the `claude` executable (default `claude`).    |
| `CLAUDE_LAUNCHER_USAGE_URL` | Usage endpoint (default `https://api.anthropic.com/api/oauth/usage`). |

## License

MIT
