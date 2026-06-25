# claude-launcher

`claude-launcher` (command: `claunch`) runs [Claude Code](https://claude.com/claude-code)
under **multiple isolated profiles**. Each profile owns its own login and
configuration by pointing `CLAUDE_CONFIG_DIR` at a dedicated directory.

Logging in uses `claude setup-token` (a long-lived OAuth token) instead of the
interactive `/login` flow, so each profile keeps its own credentials.

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

## Quick start

```bash
claunch create work     # create a profile (seeds your global config)
claunch login work      # log in via `claude setup-token`
claunch run work        # launch Claude Code as that profile
claunch usage work      # show this profile's subscription usage
```

## Commands

| Command | Description |
| ------- | ----------- |
| `create <name>`        | Create a profile, seed global config, apply the env template. |
| `login <name>`         | Run `claude setup-token` for the profile. |
| `run <name> [-- ...]`  | Launch `claude` for the profile; args after `--` pass through. |
| `env <name> [...]`     | View/edit the profile's claude env vars (see below). |
| `template [--init]`    | Show or write the default env template. |
| `usage <name>`         | Query subscription usage (`--json` for the raw response). |
| `set-token <name> [t]` | Store a token manually (pasted, or piped via stdin). |
| `list`                 | List profiles and whether each is logged in. |
| `path <name>`          | Print the profile's `CLAUDE_CONFIG_DIR`. |
| `remove <name>`        | Delete a profile and its tokens (alias: `delete`). |

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
`CLAUDE_CODE_OAUTH_TOKEN` on `claunch run`. `claunch list` shows `[logged in]`
once a profile has either a stored token or a `.credentials.json`.

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
- `run` also exports `CLAUDE_CODE_OAUTH_TOKEN` when a token has been stored for
  the profile, so it authenticates non-interactively.

## Configuration

| Environment variable        | Purpose |
| --------------------------- | ------- |
| `CLAUDE_LAUNCHER_HOME`      | Base directory for profiles (default `~/.claude-launcher`). |
| `CLAUDE_LAUNCHER_BIN`       | Path/name of the `claude` executable (default `claude`). |
| `CLAUDE_LAUNCHER_USAGE_URL` | Usage endpoint (default `https://api.anthropic.com/api/oauth/usage`). |
| `CLAUDE_LAUNCHER_SEED`      | Config dir new profiles seed from (default `CLAUDE_CONFIG_DIR` or `~/.claude`). |

## License

MIT
