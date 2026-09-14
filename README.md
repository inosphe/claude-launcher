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

# One profile can run several harnesses. They share one set-token secret but
# keep harness-owned config/auth homes separate:
claunch set-token work
claunch run work:pi                      # token -> ANTHROPIC_API_KEY; selected
                                         # custom provider -> Pi model adapter
claunch login work:kimi                  # Kimi harness OAuth, token ignored
claunch run work:kimi
claunch run work:claude                  # explicit Claude selector
```

## Commands

| Command | Description |
| ------- | ----------- |
| `create <name>`        | Create a profile (`--harness`, `--parent` to inherit). Claude profiles seed/apply Claude config; other harnesses start clean. |
| `set-harness <name> [h]` | Show or pin the profile's harness; `--clear` inherits from its parent/default. |
| `login <name[:harness]>` | Run the selected OAuth harness's login flow (Claude setup-token, Codex/Kimi/Cursor login). |
| `run <name[:harness]> [args...]` | Launch the profile default or an explicit harness. `--borrow BASE_PROFILE` works for Claude and declared API-key harnesses; `--null`, `--provider` and `--add-prompt` are Claude-only. Other args pass through untouched. |
| `env <name> [...]`     | View/edit the profile's env vars (`--effective` for merged). |
| `parent <name> [p]`    | Show, set, or `--clear` a profile's parent. |
| `template [--init]`    | Show or write the default env template. |
| `migrate <name> [src]` | Copy skills/MCP servers from a global or local path. |
| `plugin [list\|install\|uninstall\|marketplace]` | Declare [plugins and marketplaces](#plugins--shared-settings-every-profile) for every profile, and install them. |
| `shared [KEY=VALUE ...]` | Show or declare the `settings.json` keys every profile carries (`--unset KEY`). |
| `apply [name]`         | Converge profiles onto the shared declaration (`--dry-run`, `--check`). |
| `prune [--dry-run]`    | Delete local profile dirs not declared in `~/.claunch.yaml`. |
| `sync [--mode ...]`    | Reconcile `~/.claunch.yaml` with the sync server (`merge`/`up`/`down`). |
| `search <query> [--kind beads\|sessions]` | Rank the repository board or the daemon's sessions by meaning (needs the `rag:` block; see [docs/rag-search.md](docs/rag-search.md)). |
| `rag status\|reindex`  | The semantic-search index: configuration, coverage, and a sync. |
| `validate [name[:harness]]` | Run the selected harness's declared non-interactive heartbeat (all bare profile defaults if no name). |
| `usage <name[:harness]>` | Query Claude, Codex, or managed Kimi subscription usage (`--json` for the raw response). |
| `set-provider [p] <provider>` | Pin a provider globally or per profile (`--clear` to inherit). |
| `providers`            | List API providers from the config file and the active one. |
| `routing [set\|clear\|stop]` | Show or change [request-body routing](#pinning-the-upstream-provider-openrouter-routing) (e.g. pin OpenRouter to CoreWeave). |
| `set-token <name> [t]` | Store the profile's one launcher token. The harness declaration chooses its destination env; OAuth harnesses ignore it. |
| `get-token <name>`     | Print the profile's OAuth token (resolves inheritance; `--own`). |
| `list`                 | List profiles and each login's state (alias: `ls`). |
| `path <name[:harness]>` | Print the profile root, or the explicit harness's namespaced home. |
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

Run a profile selector but authenticate with **another base profile's** shared
credential, just for that run. The running selector still owns the harness,
config dir, env and skills; the lender never selects the program:

```bash
claunch run company:claude --borrow company2
claunch run company:pi --borrow company2
```

`--borrow` always takes a bare name. `--borrow company2:claude` is rejected:
the harness belongs to the running selector and all variants of `company2`
share the one credential. For Claude, the lender's
**[provider](#api-providers-third-party-backends)** comes with its token, so a
Kimi/other backend brings its base URL, model pins and auth. For an `auth:
api-key` harness such as Pi, only the lender's launcher token crosses the
boundary and the packaged `token_env` decides its destination. Lender env,
storage and harness do not cross. OAuth harnesses (Codex, Kimi and Cursor)
cannot borrow: select `company2:HARNESS` to use that profile's namespaced OAuth
home instead.

The direct `run` form persists nothing. A missing or expired lender credential
is reported by the shared borrow validation; managed sessions expose the same
live verdict under **Borrowed auth** in the Web detail pane, so deleting a token
after creation turns the next poll into a warning without exposing its value.
To forward a literal `--borrow` to a harness, put it after `--`.

`--null` launches with **no OAuth token at all**: the profile's stored token is
not injected, and any `CLAUDE_CODE_OAUTH_TOKEN` inherited from the shell or set
in profile env is cleared, so claude starts unauthenticated (e.g. to `/login`
fresh):

```bash
claunch run company --null
```

Both flags exist on **managed sessions** too: `claunch new-session --borrow
NAME` / `--null` (and `claunch spawn`, under the [spawn
policy](#agents-that-build-their-own-team-spawn--hierarchy--member-graph)). There the choice
is part of the session's *definition*, so it holds across daemon restarts and
`respawn` — and the token is looked up fresh at every relaunch, so a restore
borrows what the lender holds *then*, not a copy from creation day. And
because it is the definition's, it can be changed later: `claunch reborrow
S NAME` stops the session and relaunches it on another profile's token
— same name, same conversation, same directory. Claude offers three answers:
borrow, `--none` (its own token again), or `--null` (no token). API-key
harnesses offer borrow or their own token; OAuth harnesses offer neither.

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

**Herdr pane labels.** When the pane a launch runs in is a Herdr pane, it is
relabelled with *who is running there, on what, and where* — session · branch
· directory:

| Launch | Pane label |
| ------ | ---------- |
| `claunch run nc` (main checkout) | `nc · master · F:\works\claude-launcher` |
| `claunch run nc --worktree=review` | `nc · review · F:\works\claude-launcher\.claude\worktrees\review` |
| `claunch attach api` (worktree `review`, since switched to `other`) | `api · other · …\worktrees\review` |
| `claunch new-session -s api --worktree=review -a` | `api · review · …\worktrees\review` |

`run` uses the profile, which is its nearest thing to a session name; a
session uses its own. The branch and the directory are read from wherever the
launch actually lands, so they are the same three facts whether the checkout
was cut a moment ago, entered from an earlier run, or never a worktree at all
— and the directory is what separates two checkouts of one branch, which is
the case a fleet runs into first. `$HOME` is written as `~`, and a label that
would run past 120 characters loses *leading* path segments: the tail is the
half that tells two worktrees of a repository apart.

The label is tied to **occupancy, not creation**: it is set while this pane
really is the agent's terminal, and cleared when that ends — when claude
exits, or when you detach. A `new-session` *without* `-a` leaves the pane
alone entirely: that session runs in the daemon's PTY, not here, and a label
for an agent that is somewhere else (or has since exited) is worse than no
label, because it still reads as true.

None of this is required. Outside Herdr every call is a silent no-op, and a
failed one never touches the launch.

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

New profiles get their defaults from the `template` section of
`~/.claunch.yaml`. The template is a profile *layer*: the same fields a
profile entry may carry (`models`, `context_window`, `auto_compact_at`,
`reasoning_effort`, `harness_options`; see
[API providers](#api-providers-third-party-backends)),
copied into each new Claude profile at `create` (a field the profile already
sets is kept, option maps merge). On a brand-new install the file is created
from a bootstrap seed, `<launcher home>/template.yaml`, whose built-in
defaults are:

```yaml
template:
  auto_compact_at: 400000
  harness_options:
    claude:
      env:
        CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS: "0"
```

A pre-schema `template.env` block still works (it is merged into a new
profile's raw `env`) and `claunch migrate-config` converts it.

`template.yaml` only *seeds* `~/.claunch.yaml` the first time; afterwards the
live `template` block in `~/.claunch.yaml` is authoritative (edit it directly, or
run `claunch template --init` to write the bootstrap seed). **Existing profiles
are not changed automatically** — apply the current defaults to one with:

```bash
claunch env <name> --apply-template
```

## Inheritance (parent profiles)

A profile can inherit from a **parent**, so you can build a base profile once and
spin off variants. Children inherit the parent's `harness`, `env` (child keys
win), API-key route and applicable login secret. Every `allowed_harnesses`
constraint in the parent chain also applies; a child list intersects it and
therefore cannot widen it. A local `set-harness` pin wins;
`set-harness --clear` returns to the inherited/default (`claude`) choice.

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

**What inheritance covers.** `harness`, `env` and applicable auth are resolved live at
launch, so changing them on a parent affects children immediately. **Skills and
MCP servers are *files* in each profile's own config dir**, which Claude Code
reads from a single `CLAUDE_CONFIG_DIR` — they can't be merged live, so they are
*copied*: `create --parent` copies the parent's skills + MCP into the new child,
and `claunch migrate <parent> --recursive` re-copies into the parent and every
descendant when you add more later.

| Inherited live (harness, env, auth) | Copied point-in-time (Claude skills, MCP) |
| --------------------------- | ---------------------------------- |
| change parent → children see it next run | `create --parent` seeds from parent |
| `env --effective` shows the merge | `migrate <parent> --recursive` re-syncs the tree |

## API providers (third-party backends)

A **provider** describes one API backend -- where it is, which models it
serves, how much context they carry -- in a vocabulary no harness owns. At
launch a per-harness *translator* turns that description into the harness's
own words: Claude Code's `ANTHROPIC_*` environment, Codex's `-c key=value`
overrides, Pi's in-process provider registration. Providers are defined and
selected **in the config file** (`~/.claunch.yaml`, the launcher's
[source of truth](#configuration-source-of-truth)), which the launcher reads
live at launch. You can edit that file directly, or use `set-provider`
(below), which just records the selection in it.

```yaml
providers:
  claude:
    # Named Anthropic provider; policy metadata is allowed here.
    allowed_harnesses: [claude]
  deepseek:
    service: custom
    allowed_harnesses: [claude, pi]     # optional compatibility/security boundary
    api_key: "sk-..."                   # or leave it out: `claunch set-token` per machine
    endpoints:                          # one URL per *protocol* the backend serves
      anthropic: https://api.deepseek.com/anthropic
      openai:    https://api.deepseek.com
    models:                             # role -> the id the backend accepts, undecorated
      default: deepseek-flash
      small:   deepseek-flash
      large:   deepseek-v4-pro
      # xlarge:   defaults to `large` (Claude's Fable slot; `large` is Opus)
      # subagent: defaults to `small`
    context_window: 1000000
    auto_compact_at: 900000
    reasoning_effort: high             # explicit low, medium, or high
    openai_reasoning_format: deepseek  # request encoding for the Pi adapter
    harness_options:                    # the one harness-keyed place (see below)
      claude:
        model_tag: "[1m]"

provider: deepseek             # use it for every profile by default (optional)

profiles:
  work:
    provider: deepseek         # ...or per profile (overrides the global one)
    models: {default: deepseek-v4-pro}   # same field names, one layer up
    auto_compact_at: 600000
    reasoning_effort: high     # overrides the provider value when present
  personal:
    provider: default          # pin one profile back to plain Anthropic
```

A profile overlays `models`, `context_window`, `auto_compact_at`,
`reasoning_effort` and `harness_options` on its provider (root ancestor first,
the profile itself last); `api_key`, `endpoints` and
`openai_reasoning_format` identify the backend and stay on the provider.

**What each harness receives** (`claunch providers` prints the description;
`claunch run PROFILE:HARNESS` prints a `note:` for anything the harness
cannot carry):

| spec field | claude | codex | pi |
|---|---|---|---|
| `api_key` | `ANTHROPIC_AUTH_TOKEN` (below the profile's `set-token`) | not translated yet | `ANTHROPIC_API_KEY` (below `set-token`) |
| `endpoints.anthropic` | `ANTHROPIC_BASE_URL` | -- | -- |
| `endpoints.openai` | -- | not translated yet | the registered provider's base URL (`/v1` appended) |
| `models` | `default` -> `ANTHROPIC_MODEL` + `..._DEFAULT_SONNET_MODEL`, `small` -> `..._DEFAULT_HAIKU_MODEL`, `large` -> `..._DEFAULT_OPUS_MODEL`, `xlarge` -> `..._DEFAULT_FABLE_MODEL`, `subagent` -> `CLAUDE_CODE_SUBAGENT_MODEL` | the session's own `--model` | the registered model list, `default` launched |
| `context_window` | appends `[1m]` to every model id when >= 1,000,000 | `-c model_context_window=N` | each registered model's `contextWindow` |
| `auto_compact_at` | `CLAUDE_CODE_AUTO_COMPACT_WINDOW` | `-c model_auto_compact_token_limit=N` | `compaction.reserveTokens = context_window - auto_compact_at` in the profile's `pi/settings.json` |
| `reasoning_effort` | `CLAUDE_CODE_EFFORT_LEVEL` | `-c model_reasoning_effort=...` | `--thinking ...`, `defaultThinkingLevel`, and the outgoing request |
| `openai_reasoning_format` | -- | -- | the registered model's OpenAI Chat Completions reasoning encoding |

`harness_options.<harness>` is the one place keyed by harness name, for what
no neutral field expresses. Each translator accepts its own channels and
rejects any other key at load time:

| harness | channels |
|---|---|
| `claude` | `env` (raw variables, applied last), `model_tag` (`"[1m]"` to force the tag, `""` to suppress it) |
| `codex` | `env`, `config` (each key becomes `-c key=value`, strings quoted) |
| `pi` | `env`, `settings` (dotted keys merged into the profile's `pi/settings.json`), `tools` (`{full_read: false}` switches a claunch builtin tool off) |

`reasoning_effort` accepts the common cross-harness values `low`, `medium`,
and `high`. Claude Code receives the value through
`CLAUDE_CODE_EFFORT_LEVEL`; Codex receives `model_reasoning_effort`; Pi
receives both a launch-time `--thinking` argument and a persisted
`defaultThinkingLevel`. A session argument written later on the command line
retains precedence.

Pi also requires `openai_reasoning_format` whenever `reasoning_effort` is set
on a custom provider. The currently implemented format is `deepseek`. It
registers the model with `reasoning: true`, enables DeepSeek-compatible
reasoning history, and sends both `thinking: {type: enabled}` and
`reasoning_effort: high` for a `high` request. The adapter rejects a provider
that declares only one member of this pair, so a backend default cannot supply
the missing value silently. Kimi and Agent have no reasoning-effort translator;
a profile carrying `reasoning_effort` is rejected for those harnesses. Use
`allowed_harnesses` to expose only the provider/harness combinations whose
endpoint and authentication adapters are implemented.

Pi's custom provider defaults to an output limit of 16,384 tokens. Set an
explicit budget per provider or profile through its Pi environment channel:

```yaml
profiles:
  ds4-official:
    context_window: 1000000
    reasoning_effort: high
    harness_options:
      pi:
        env:
          CLAUNCH_PI_MAX_TOKENS: "384000"
```

This sets both the registered model's `maxTokens` and the outgoing OpenAI
`max_tokens` field. Pi otherwise caps its default request at 32,000 even if
the model declares a larger limit. The value must be a positive integer
within the context window and the backend's supported output limit. It is
an upper bound per response. Start a new Pi session after changing the profile;
an existing process retains its launch environment.

Translated launch arguments go before the session's own, so an explicit
`-c`/`--model` from the session still wins. The `env` channel of a harness is
applied after the launcher's Claude-namespace filter: it is the one way to
hand an `ANTHROPIC_*` variable to another harness on purpose.

**Older files.** A provider written as a Claude `env:` bundle (schema
version 1) is still read: Claude receives the variables verbatim, and every
other harness reads a reverse translation of them (`endpoints.openai` is
assumed only when the Anthropic URL has no path). `claunch migrate-config
--dry-run` shows how such a file would be rewritten to the schema above, and
`claunch migrate-config` does it, keeping a `~/.claunch.yaml.v1.bak`. The
rewrite keeps the Claude outcome: any variable the translation would not
reproduce is pinned under `harness_options.claude.env`, the empty
`ANTHROPIC_API_KEY`/`CLAUDE_CODE_OAUTH_TOKEN` pins are dropped (the launcher
enforces both), and a model alias the roles imply but the env never set is
reported. `claunch providers` marks a provider still in the old form.

**Selecting a provider.** The effective provider for a run is the first of:
the profile's own `provider`, an ancestor's (inheritance, like `env`), the
top-level `provider`, then the built-in `default`. Selecting `default` on a
profile is itself a choice — it **pins** that profile to plain Anthropic even
when a global or inherited provider is set (the `personal` example above). The
built-in `default` is plain Anthropic with no overrides — the launcher injects
the profile's OAuth token as usual. For any other provider the launcher applies its `env` as a
**low-priority backend default** — above the shell but *below* the profile's own
`env`, so a per-profile (or template/inherited) value always wins over the
provider for the same key. The implicit `default` provider and named `claude`
provider both use Anthropic OAuth. Other providers carry their own auth, so the launcher
does **not** inject `CLAUDE_CODE_OAUTH_TOKEN` — supply the backend token with
`claunch set-token PROFILE` (recommended; see *keeping
backend tokens out of the config file* below) or as a plaintext
`ANTHROPIC_AUTH_TOKEN` in the provider's `env`
(`ANTHROPIC_API_KEY` is forced to `""` by the packaged Claude harness).

The resulting precedence for a run is: shell env < the provider's Claude
translation < each profile layer's translation (its `models`/`auto_compact_at`,
then its raw `env` and `harness_options.claude.env`) < the projected
`set-token` value < the final harness auth boundary. For Claude that last
boundary always forces `ANTHROPIC_API_KEY=""`.

For `PROFILE:pi` with a non-default provider, the packaged adapter registers a
process-local Pi provider from `endpoints.openai` and the configured `models`
roles. It selects `models.default` first and passes the stored profile token
through Pi's declared `ANTHROPIC_API_KEY` route. The adapter appends `/v1` to
the OpenAI endpoint and uses OpenAI Chat Completions with Bearer authentication;
the Claude harness continues to use the same provider through
`endpoints.anthropic`.
The registration is loaded from a packaged Pi extension for each launch,
including managed-session restores and `validate`; it does not edit Pi's
`models.json`. Explicit Pi
`--provider`, `--model` or `--models` arguments retain model-selection
precedence. A non-default provider selected for Pi therefore needs both
`endpoints.openai` and at least `models.default`.

A provider may declare `allowed_harnesses`. When present, selecting that
provider is only valid for the listed harnesses; `set-provider` refuses an
atomic config change that would make a profile's current harness illegal.

`service` identifies the provider's account and authentication service; it is
separate from `allowed_harnesses` and a profile's selected harness. `default`
and `claude` use `anthropic`; providers without `service` retain the `custom`
backend behaviour. Future integrations can declare another service, such as
`openai`, without coupling provider identity to an executable harness.

**Keeping backend tokens out of the config file.** Whenever a **non-default
provider is active** for the run (selected on the profile, inherited, the
global default, or forced with `run --provider`), the launcher looks up the
profile's one **stored token** — the `set-token` value in the per-machine
`.launcher-token` file, resolved own first and then inherited (`--borrow`
uses the lender's) — and injects it as
`ANTHROPIC_AUTH_TOKEN`, **overriding** any plaintext value in the yaml. While
launching Claude, the packaged declaration always forces
`ANTHROPIC_API_KEY=""` so Claude Code cannot send a competing `X-Api-Key`
header. A provider therefore needs no secret in the file:

```yaml
providers:
  fireworks-glm5p2:
    endpoints: {anthropic: "https://api.fireworks.ai/inference", openai: "https://api.fireworks.ai/inference"}
    models: {default: "accounts/fireworks/models/glm-5p2"}
    # no api_key here — supplied by set-token per machine
```

```bash
claunch set-provider work fireworks-glm5p2
claunch set-token work fw_...  # Claude provider route -> ANTHROPIC_AUTH_TOKEN
claunch run work
```

A plaintext `api_key` in the yaml still works when the profile has no stored
token, but the stored token always wins when both exist -- for every harness
the provider is used with. The trigger is the **provider selection itself** — env vars like
`ANTHROPIC_BASE_URL` set in a profile's `env` (or inherited from the shell)
don't change auth handling on their own. `run` tells you when this happens:

```text
provider 'fireworks-glm5p2' active (set on profile 'work'); auth: stored profile token exported as ANTHROPIC_AUTH_TOKEN
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

### Pinning the upstream provider (OpenRouter routing)

A provider's `env` can say *which backend* to talk to, but not *which upstream
compute inside it* serves the request. OpenRouter decides that from a `provider`
object in the **request body** — and nothing else does it. A `:coreweave`
suffix on the model slug is accepted and quietly ignored (your request lands on
whichever endpoint the default router picks), and `@coreweave` is rejected
outright. Environment variables cannot reach the body.

So a provider may declare a **routing** spec, and the launcher runs a small
loopback shim that merges it into every JSON request on the way out:

```yaml
providers:
  openrouter:
    routing:
      order: [coreweave]        # try CoreWeave first
      allow_fallbacks: false    # ...and fail rather than use anyone else
    env:
      ANTHROPIC_BASE_URL: "https://openrouter.ai/api/"
      ANTHROPIC_MODEL: "deepseek/deepseek-v4-flash-0731"
```

The spec is forwarded verbatim, so every field the backend understands
(`order`, `only`, `ignore`, `sort`, `max_price`, ...) works. Provider slugs come
from the model's *Providers* tab, or from
`https://openrouter.ai/api/v1/models/<author>/<slug>/endpoints`; a base slug
(`coreweave`) matches all of that provider's endpoints, a full one
(`coreweave/fp8`) pins one variant.

Set it from the CLI instead of editing YAML:

```bash
claunch routing set openrouter --order coreweave --no-fallbacks
claunch routing                      # what is pinned, and which shims are live
claunch routing clear openrouter     # back to the backend's own routing
```

**How the shim behaves.** At launch, a provider with a `routing` block gets its
`ANTHROPIC_BASE_URL` swung to `http://127.0.0.1:<port>/`; the shim forwards
everything to the real upstream, adding the routing field to JSON object bodies
and leaving every other request byte-identical. It streams responses, so
token-by-token output is unaffected. One shim serves every session using the
same (upstream, spec) pair — the port is derived from that pair, so simultaneous
launches share one process, and editing the spec produces a different one that
the next launch picks up. A body that already carries a `provider` field is left
alone.

The shim is loopback-only and holds no credentials of its own — it forwards the
caller's. It outlives the session that started it; `claunch routing stop --all`
ends them. If it cannot start, the **launch fails** rather than falling back to
the direct URL: silently unpinning the request is the exact failure this
feature exists to prevent.

### Throughput records for API-key providers (`claunch tps`)

Sessions on the Anthropic OAuth route (a subscription profile, `provider:
default`/`claude`) and on Codex's own login talk to their backend directly.
Every **API-key provider** — anything with `endpoints`/`ANTHROPIC_BASE_URL` of
its own: DeepSeek, Fireworks, OpenRouter, a Kimi key, a self-hosted gateway —
is launched through the same loopback shim instead, whether or not it declares
a `routing` spec, and the shim writes **one record per `/v1/messages` call**:
when the request started, when the first byte and the first token came back,
when it ended, the model that answered, the token counts the response carried
(input, cache read/write, output), and the tokens per second those give —
`tps` over the generation (from the first token to the end) and `tps_total`
over the whole request.

```bash
claunch tps                     # totals, per model, per session, the last 10 calls
claunch tps --session s12 -n 30 # one session's calls
claunch tps --upstream deepseek --json
claunch tps --clear             # drop every record file
```

Records live under `~/.claude-launcher/metering/<shim fingerprint>.jsonl`, one
JSON object per line, so they are also easy to read with `jq`. A daemon-managed
session is named in its records: the daemon sends `X-Claunch-Session: <name>`
in every request through Claude Code's `ANTHROPIC_CUSTOM_HEADERS`, and the
shim strips that header before the upstream sees it. A request the shim cannot
count (an error answer, a compressed body it did not ask for) is still
recorded with its timing and `counted: false`.

Switch it off for one provider, for everything, or for one shell:

```yaml
metering: false                 # top level: no shim for metering-only providers
providers:
  fireworks:
    metering: false             # this provider talks to its upstream directly
```

```bash
CLAUNCH_METERING=0 claunch run work   # this launch only (1 forces it on)
```

Metering is observability, so it fails soft: if the shim cannot start, the
launch prints a one-line warning and uses the upstream URL directly (a
`routing` spec keeps the hard failure described above).

The `pi` harness goes through the same shim: its OpenAI base URL
(`endpoints.openai` + `/v1`) is fronted the same way, the packaged extension
registers `X-Claunch-Session` as a provider header for daemon-managed
sessions, and the shim records each `/v1/chat/completions` call. An OpenAI
stream only carries token counts when the request asks for them, so behind
the shim claunch lets Pi send `stream_options.include_usage` (a backend that
rejects that field can opt out with `providers.<name>.metering: false`).

The web UI shows the same records: each session's rail row carries a
throughput line (`⚡ 38.1 tok/s · ttft 715ms · 45s ago`), an open briefing
card a chip with the same figure, and the attached session's header a badge
with a twin overlay drawn over the top-right corner of its terminal. All of
them read the `tps` block the daemon hangs on the session (`GET /api/sessions`
and `/api/sessions/<name>/meta`); `GET /api/metering?session=<name>` returns
the records behind it. A reading older than ten minutes dims, and a session
that never went through the shim shows nothing rather than a zero.

## Migrating skills & MCP servers

Seeding copies the global `settings.json`, so the MCP servers defined there come
along — but **skills live in a separate `skills/` directory** and **project/local
MCP servers live outside `settings.json`**, so they aren't seeded. `claunch
migrate` pulls those into a profile from any source path:

```bash
claunch migrate work                 # from ~/.claude (global skills + MCP)
claunch migrate work ./my-project    # from a project's .claude/ and .mcp.json
claunch migrate work --mcp           # MCP servers only (--skills for skills only)
claunch migrate work --plugins       # copy the plugins/ directory as-is
claunch migrate company --recursive  # also into every child profile (see Inheritance)
claunch migrate work --dry-run       # preview without copying
```

The source may be a Claude config dir (`~/.claude`, or another profile via
`claunch path <name>`) or a project directory. Skills are merged into the
profile's `skills/`; MCP servers are gathered from `settings.json`,
`settings.local.json`, `.claude.json` and a project-root `.mcp.json`, then merged
into the profile's `settings.json`. Default migrates skills + MCP; pass `--skills`
or `--mcp` to narrow it.

`--plugins` copies the `plugins/` directory verbatim, which is a one-off move
between two profiles on this machine. To keep plugins the same across *every*
profile, declare them instead — see the next section.

## Plugins & shared settings (every profile)

A profile *is* its own `CLAUDE_CONFIG_DIR`, so everything Claude Code keeps there
exists once per profile: installed plugins, the marketplaces they came from, and
the global `settings.json` keys. Seeding copies that state at **creation** time
only, so a plugin installed afterwards reaches the one profile it was installed
in — and the set drifts apart without anything reporting it.

One declaration fixes that: the `shared` block of `~/.claunch.yaml` says what
every Claude Code profile should have, and `claunch apply` converges the
profiles onto it.

```yaml
shared:
  marketplaces: [snflkd/fluent-korean]
  plugins: [fluent-korean@fluent-korean]
  settings:
    outputStyle: fluent-korean:fluent-korean
```

You do not edit that block by hand — three commands write it and apply it in the
same call.

### Commands

| Command | What it does |
| ------- | ------------ |
| `plugin install <plugin@marketplace>` | Declare a plugin and install it in every profile (alias: `add`). |
| `plugin uninstall <plugin@marketplace>` | Drop the declaration and uninstall it from the profiles (alias: `remove`). |
| `plugin marketplace add <source>` | Declare a marketplace (URL, directory path or `owner/repo`) and register it everywhere. |
| `plugin marketplace remove <source>` | Stop declaring a marketplace; the profiles keep the one they have. |
| `plugin list [--json]` | The declaration, plus which profiles have drifted from it. |
| `shared` | List the declared `settings.json` keys. |
| `shared KEY=VALUE ...` | Declare settings keys and write them to every profile. |
| `shared --unset KEY` | Stop managing a key; each profile keeps the value it has. |
| `apply [NAME]` | Converge every profile, or just `NAME`. |
| `apply --dry-run` | Show what applying would do, and do nothing. |
| `apply --check` | Report drift and exit 1 if any profile is missing something. |

`plugin install`, `plugin marketplace add` and `shared KEY=VALUE` apply straight
away. `--no-apply` declares without touching the profiles, and `--profile NAME`
narrows one call to a single profile.

### A worked run

```bash
claunch plugin install fluent-korean@fluent-korean      # declared + installed everywhere
claunch shared outputStyle=fluent-korean:fluent-korean  # a settings.json key, everywhere
claunch apply --check                                   # every profile matches, exit 0
```

`plugin install` needs the plugin's marketplace to be known. When any existing
profile already has it registered, the source is read back from there and
declared for you; otherwise declare it first with `plugin marketplace add`.

### What it does and does not touch

Installing runs Claude Code's own `claude plugin` CLI once per profile with
`CLAUDE_CONFIG_DIR` pointed at it — never a file copy. One install writes three
places that must agree (the plugin's files under `plugins/`, the two JSON indexes
beside them, and `enabledPlugins`/`extraKnownMarketplaces` in `settings.json`),
and those indexes record absolute install paths naming the profile they were
written for, so a copied index sends every other profile back to the profile it
came from. This is why `claunch migrate --plugins` is a file copy for one-off use
and this is the path for keeping profiles in step.

Convergence is **additive**: `apply` installs what is declared and missing and
never removes a plugin a profile has on its own. Removal is explicit —
`plugin uninstall` drops the declaration and uninstalls it from the profiles in
the same call, and `shared --unset KEY` only stops managing the key, leaving the
value each profile already has (the value it replaced was never recorded, so it
could not be restored).

Every command is idempotent, and a no-op really is one: re-declaring something
already declared writes neither `~/.claunch.yaml` nor any profile, and runs no
`claude` at all. That matters beyond tidiness, because rewriting the config file
is refused outright while another process holds it open — an editor with
`~/.claunch.yaml` loaded is enough on Windows — so a command with nothing to do
must stay off that path.

A profile created later gets the declaration during `claunch create`, so it is
never born drifted. Profiles on another harness are skipped: `claude` never reads
their config dir.

Values given to `shared` parse as JSON when they can, so `autoCompactEnabled=false`
stores a boolean and anything unparseable stays a string.

## Configuration source of truth

Every launcher-managed setting lives in **one file, `~/.claunch.yaml`**, which
the launcher reads live at launch — there is no separate "export" step, because
this file *is* the state. It holds the profile list, each profile's `harness`,
`env`, `parent` and Claude `provider`, the default `template`,
and provider/harness definitions:

```yaml
version: 2
template:
  auto_compact_at: 400000
  harness_options:
    claude:
      env:
        CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS: "0"
profiles:
  company:
    harness: claude
    env:
      COMPANY_REGION: "eu"
  company_work:
    parent: company
    env:
      CLAUDE_CODE_AUTO_COMPACT_WINDOW: "200000"
  personal:
    harness: pi
    env: {}
shared:
  marketplaces: [snflkd/fluent-korean]
  plugins: [fluent-korean@fluent-korean]
  settings:
    outputStyle: fluent-korean:fluent-korean
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

**Launcher-managed tokens are never stored here** — each profile has one
per-machine secret file. Run `claunch login` for OAuth harnesses or
`claunch set-token` for Pi/Claude providers on each machine. A plaintext provider
token placed manually in an `env` block is still plaintext (see the
[secrets note](#api-providers-third-party-backends)).
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

What travels is **configuration only**. OAuth credentials and launcher tokens
never leave the machine (run `claunch login` or `set-token` on each host), and the
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
created (Claude profiles are seeded), so it is usable after the matching
`claunch login`/`set-token`. A pulled *deletion* only removes the declaration: as everywhere
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
scriptable and what makes it hard to type — the profile, directory,
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
 > Profile : Harness  work/claude
   Borrow          (this profile's own token)
   Null token      no - inject the profile's token
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
  otherwise fire *after* the command line was already committed. Picking an
  existing one reveals **Update** — a checkout you come back to is as far
  behind as the day you left it — which rebases it onto a branch you pick
  (defaulting to the one the checkout was cut from, usually `master`; local
  only, no fetch). Uncommitted work, or a rebase that conflicts, **refuses the
  launch**: the rebase is aborted, nothing is created, and the checkout stays
  exactly as you left it — an agent must never wake up mid-conflict in a mess
  it did not make. By flag: `--worktree=NAME --rebase-onto BRANCH`.
- **Attach** — whether to take this terminal over the moment it starts
  (`Ctrl+]` detaches, and the session lives on either way).

The rest is the same list the daemon would have checked afterwards, so a mesh
that does not exist or a workflow not declared in that directory is never
offered rather than refused once the session is half arranged. Fields that do
not apply grey out rather than vanish: `Fork` says *needs a conversation to
fork* until you pick one under `Resume`; `Role`/`Resume`/`Null token` are
Claude-only; `Borrow` remains open for Claude and API-key harnesses and greys
for OAuth/none harnesses. `Profile : Harness` is the only execution selector;
use `claunch set-harness` to change a profile's default. `Borrow` candidates
are base profiles validated for the selected harness and current credential
state. New-session and reborrow forms fold the selected base profile into
their own-token choice. Spawn forms retain that profile as an explicit lender
because their empty choice inherits the parent's authentication; selecting it
lets the child use the selected profile's authentication when the parent is
borrowing another profile or running with `--null`. `Borrow` and `Null token`
are
`--borrow`/`--null` as rows — and since the daemon refuses
the pair outright, saying yes to null greys the borrow row and resets it,
so the form can never offer a combination the flags would error on.

Flags the form does not show — `--env KEY=VALUE`, `--cols/--rows`,
`--detached` — are left exactly as you typed them.

**`spawn` has one too**, and it is deliberately shorter — a child is a copy of
its parent, so the form only asks what a child may be asked:

```bash
claunch spawn --wizard
```

```
claunch spawn

 > Parent          lead  idle, work, /work/repo
   Name            (auto)
   Profile : Harness  the child inherits its parent's selection (spawn.allow_profile)
   Borrow          the child authenticates as its parent does (spawn.allow_profile)
   Null token      no - authenticate as the parent does
   Workspace       (the parent's directory: /work/repo)
   Args            the child runs its parent's args (spawn.allow_args)

  START IT WORKING
   Mesh            (the parent's: team)
   Handle          (the session name)
   Role            (no role)
   Connect         (the whole mesh)
   Workflow        (none)
   Opening task    typed into the child once it has booted - what it is for

  AFTERWARDS
   Attach          no - leave it running in the daemon
   [ Spawn child ]

  child of this one: 1 running, 3 left (depth 0/3)
  up/down move   left/right change   Enter open   Ctrl+S create   Esc cancel
```

Every inherited field the policy can unlock is a row here, and a locked one
is **greyed out with the key that opens it** rather than hidden — the form is
also how a person learns what the policy currently is. `Profile` and
`Borrow` open together under `spawn.allow_profile` (both decide whose login
the child holds); `Null token` (`--null`) is never gated, because it takes a
credential away rather than granting one — and saying yes to it greys the
borrow row, exactly as in the other form; `Args` opens under
`spawn.allow_args` and **replaces** the inherited command line.
There is still no free-text directory row (a registered **workspace** is the
vouched-for exception). But there *is* a **Worktree** row, and it is the row
a fleet needs: two children
of one parent in the parent's checkout edit each other's files mid-edit, and a
worktree of that repository is the only directory a child may have that is
nobody's workspace — allowed because it is derived from where the parent
already stands, not a path anybody typed (`spawn.allow_worktree`, on by
default; the same reasoning as `allow_workspace`). The daemon cuts it from the
parent's repository — or from the workspace the child was sent to — so it
works with a remote daemon, and agents get the same field on their `spawn`
tool. An unnamed one is auto-named after the child (`helper-20260818-2048`),
since the daemon has no Herdr pane to name a checkout after. Reusing one
offers the same **Update** rebase as `new-session`'s form, defaulting to the
parent's own branch — and a rebase that cannot be done cleanly refuses the
spawn with nothing created. By flag: `claunch spawn --worktree NAME
--rebase-onto BRANCH`.

**Parent** is the field that has no equivalent in the other form, and it is
why this one exists. `spawn` normally reads its parent from
`$CLAUNCH_SESSION`, which is set for agents and for nobody else — a person at
a terminal has to name one. The form also reads that parent's
[spawn budget](#agents-that-build-their-own-team-spawn--hierarchy--member-graph)
and prints it under the cursor, so a session with no slots left says so on its
own row instead of refusing a filled-in form. What
[`spawn.allow_profile` / `allow_workspace`](#agents-that-build-their-own-team-spawn--hierarchy--member-graph)
leave locked is greyed out with the config key that would open it, rather than
hidden.

The list is ordered by what can actually take a child. Your own session leads
when you have one (`spawn` inside a session means *this* session, so the form
opens on the answer the bare command would have given); then the sessions that
can spawn; then the ones that cannot. An **exited** session is greyed out with
`respawn it first` — the daemon refuses a child of one outright, and offering
it as the default is offering a launch that is already lost. Names break the
tie and are read as numbers, so `s9` comes before `s10` instead of after it.

**Mesh** defaults to *the parent's own* (opening one for the pair if it is in
none) — that is what `spawn` means by leaving it out, so it is the first
entry, not an empty box. `- no mesh at all` is a deliberate answer beside it,
and picking it takes the handle and the roster with it. **Connect** lists that
mesh's members minus the parent, since a child can always reach its parent
anyway.

It is a form, so it needs somebody to fill it in: both are refused outside an
interactive terminal, and both are refused from inside a managed session —
an agent has its parent in `$CLAUNCH_SESSION` and needs no list to pick from,
and a form painted into its PTY would hang the session it was creating.

### Session commands

| Command | Description |
| ------- | ----------- |
| `new-session` (`new`) | Spawn the harness owned by required `--profile P` in a managed PTY. `--wizard` uses one `Profile : Harness` picker and picks every other field (see [Building one from a form](#building-one-from-a-form---wizard)); by flag: (`-s NAME`, `--profile P`, `--model M`, `-c CWD`, `--cols/--rows`, `--env K=V`, `--restore/--no-restore`, `--role R` with `--mesh`, Claude-only `--resume [S]`/`--fork-session`, `--worktree[=NAME]`/`--no-worktree`, `--rebase-onto BRANCH`, `-a/--attach`; trailing args pass through). Also **what it is for**: `--mesh M --as HANDLE --connect H`, `--workflow W --context C`, `--task "..."`. `--harness` remains only as a deprecated, refused compatibility flag. **Yours, not an agent's**: refused from inside a managed session, which should use `spawn` (`--detached` overrides). |
| `spawn`               | Create a **child** of a session by hand, exactly as its agent would — same endpoint, same policy. `--wizard` uses the inherited or allowed replacement `Profile : Harness`; by flag: (`--parent S`, `-s NAME`, `--profile P` when allowed, `--model M` when `spawn.allow_args` permits it, `--mesh M`, `--as HANDLE`, `--role R`, `--connect HANDLE`, `--workflow W`, `--task "..."`, `-w/--workspace NAME`, `--worktree NAME --rebase-onto BRANCH`). `--harness` is refused; changing an allowed profile is the only way to change the child harness. `--mesh` defaults to the parent's own. |
| `sessions` (`lss`)    | List sessions: name, status (`starting/busy/idle/exited`), harness, profile, size, cwd. Children are indented under the session that spawned them. |
| `attach [S]` (`a`, `attach-session`) | Mirror a session into this terminal, tmux-style; detach with `Ctrl+]` (session keeps running). Omit `S` when exactly one session is running. `-t S` also accepted. |
| `respawn S [-a]`      | Relaunch an exited session under its own name — claude comes back with `--resume` of its pinned conversation, so quitting it by accident (double `Ctrl+C` while attached) is recoverable. `-a` attaches right away. Also a **resume** button in the [web UI](#web-ui--http-api). |
| `migrate-session S`   | Move a session to another checkout: `--worktree [NAME]` cuts (or reuses) a worktree of its own repository, `--to DIR` moves it anywhere else — the daemon stops it, carries its claude conversation's transcript, and relaunches it there. `--children` moves the descendants standing in the same directory too; `-a` attaches. Also a **Move to worktree** picker in the web UI's session panel. |
| `reborrow S [NAME]`   | Restart a token-capable session on another answer to "whose token": base profile `NAME` borrows its shared token (`--borrow`), `--none` uses the runtime profile's own, and Claude also supports `--null`. Stops and relaunches the session — same name, same conversation, same directory. `-a` attaches. The Web detail pane's **Borrowed auth** picker includes live credential validation. |
| `skip-permissions S on\|off` | Restart a session with claude's `--dangerously-skip-permissions` added (`on`) or removed (`off`) — the flag lives in the definition's args, so toggling it stops and relaunches the session: same name, same conversation, same directory. `-a` attaches. Also a **Permissions** toggle in the web UI's session panel. |
| `send-keys [-l] S KEYS...` | tmux semantics: `Enter`, `Escape`, `Tab`, `C-c`, `M-x`, `Up`... are keys; everything else is literal text. `-l` sends all args literally. `-t S` also accepted. |
| `notice S TEXT...`    | Show a line for a few seconds to whoever is *watching* `S` -- drawn over row 1 of a `claunch attach` terminal, as a banner on the web terminal -- without typing anything into the session (`--ttl SECS`, `--level info\|warn\|error`). The operator's form of `POST /api/sessions/S/notice`; `deliver`/`send-keys` are for the agent, this is for the person. |
| `capture-pane S`      | Print the current rendered screen (`--history` for scrolled-off lines, `--json` for lines + cursor + status). |
| `wait-for S`          | Block until `--idle` (default) or `--exited`; `--timeout SECS`, `--idle-threshold SECS`. Exits 1 on timeout. |
| `rebrief [--session S]` | Print the session's briefing re-derived from current daemon state: mesh memberships and roster, replies it owes, the cflow run it drives, parent/children, and its recorded opening `--task`. Managed claude sessions run it **automatically** — a `SessionStart` hook injected at spawn fires it after `/compact` and `/clear`, and claude reads the output back into context — so an agent's lost context is restored without the agent having to remember to ask. Defaults to `$CLAUNCH_SESSION`; also a **rebrief** button in the web UI, which types the same block into the session's terminal. |
| `kill-session S`      | Terminate a running session (`--force` skips graceful terminate). Idempotent: an already-exited session is left alone — dropping a record is a different verb, below. |
| `reparent S PARENT`   | Move a session — with everything spawned under it — under another parent. The operator's form of the agents' `reparent` MCP tool, which is scoped to the caller's own subtree; this one is not. Refused for a cycle, an exited parent, or a move that would push any session past `spawn.max_depth`. Opens the session's edge to its new parent in every mesh the two share. |
| `clear-sessions` (`clear`) | Drop the records of **all** exited sessions at once — running ones are untouched. They are kept indefinitely otherwise (a restart never discards them), so this is the explicit cleanup; `--logs` also deletes their output logs, freeing their auto-generated names. |
| `resize S COLS ROWS`  | Resize the session's terminal. |
| `harnesses`           | List the declared harnesses (`claude`, `codex`, `pi`, `kimi`, `agent`, plus your own) and whether each is installed here. |
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

### Who may restart or stop the daemon

`claunch daemon restart` and `claunch daemon stop` are **operator commands**.
A restart takes every attached terminal and every managed session down with
the daemon, so **an agent session has no authority to run either command on
its own** — it asks the operator instead, and this holds however the agent
comes across the command (this README, `--help`, a hint printed by another
`claunch` command). The CLI enforces the split for `restart`: run from inside
a managed session (`CLAUNCH_SESSION` set) it restarts nothing on the spot and
files a **restart request** that the operator approves or rejects in the web
UI; an unanswered request counts as approved after its timeout
(`daemon/restart_gate.py`). `stop` is not gated, so an agent must not run it
at all. A cflow step that declares a `restart:` script (this repository's
`tools/restart_live.*`) is the one sanctioned path from a workflow: the
daemon's RestartClock runs the script, not the agent's shell.

The gate is enforced in the CLI, by design: an operator's shell keeps its
immediate `claunch daemon restart`, and the daemon does not try to tell a
human's Bearer token from a session's. That leaves ways around the gate that
are technically open and **off-limits to an agent all the same**: running the
command with `CLAUNCH_SESSION` unset, running `claunch daemon stop`, posting to
`/api/daemon/shutdown` or `/api/daemon/restart` with the token, or wrapping the
command in a script. Each of them is the operator's call, and an agent that
needs a restart asks for one.

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
claunch clear-sessions                             # drop the exited record afterwards
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
done
claunch clear-sessions
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

### Joining with a role, or opening another session's conversation

Two creation-time choices sit beside each other in the form. A **role** is the
new session's membership in a mesh and works with every harness. **Resume**
selects a Claude conversation and therefore remains Claude-specific:

```bash
claunch new-session -s rev --profile work --mesh team --role reviewer
claunch new-session -s side --profile work --resume rev --fork-session
claunch new-session -s pick --profile work --resume         # claude's own picker
```

**`--role NAME`** requires `--mesh` and resolves against that mesh's current
vocabulary — packaged roles such as `leader`, `worker` and `reviewer`, or a
custom role the mesh authority installed. The join briefing carries the full
stance and a content id before the first task. Later session reminders name
that id; when the attached text is no longer in the conversation, `rebrief`
or `recall` returns the current stance. Claude, Codex and declared harnesses
therefore receive the same role text through the same path. Unknown names are
refused before the session is built. `GET /api/roles` serves the packaged
preview and `GET /api/mesh/<mesh>/roles` serves the selected membership's
authoritative options.

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
`claude`, `codex`, `pi`, `kimi` and Cursor's `agent`; `claunch harnesses` shows
whether this machine can actually run each one.

The packaged source is `claude_launcher/harnesses.yaml`. It carries command,
storage, auth mode, shared-token route and conflicting-key rules together and is
included in the wheel alongside the existing packaged workflow YAML files.

For example:

```
$ claunch harnesses
declared harnesses:
  claude     [ready        ] profile-managed
  codex      [ready        ] codex
  pi         [not installed] pi
  kimi       [ready        ] kimi
  agent      [ready        ] agent
```

**Declared is not installed.** `pi` ships in the set whether or not you have
it. `claunch harnesses` reports availability, and a session form shows the
selected profile's missing harness as unavailable without offering an
alternative picker. Spawning one that is not installed is refused up front,
naming the program it looked for, instead of failing later as `could not spawn`.

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
    home_env: CODEX_HOME     # optional isolated per-profile home
    auth: oauth              # claude, oauth, api-key, or none
    clear_env:               # variables forbidden by this auth mode
      - OPENAI_API_KEY
    empty_env: []            # variables forced to the empty string
    login_args: [login]      # optional interactive login argv
    description: "..."      # optional, shown in status surfaces
  pi: null                  # a tombstone: drop a packaged harness
```

An `auth: api-key` declaration must add `token_env: SOME_API_KEY`; this is the
destination of the profile's shared `set-token`, not another stored secret.
The packaged Pi declaration also sets `provider_adapter: pi`; this adapter is
valid only with `auth: api-key` and projects a selected claunch provider into
Pi's native provider/model registration.

Every new user-facing session requires a **profile selector**, and that
selector is the only source of its harness. A bare profile uses the
`harness:` default stored under `profiles.<name>` in `~/.claunch.yaml`
(or inherits it, then defaults to Claude). An explicit
`PROFILE:HARNESS` selects a harness for that execution without changing the
YAML. Managed sessions resolve either form once and persist the canonical
`PROFILE:HARNESS`, so later default changes do not reinterpret a restore:

```bash
claunch create ds4 --no-seed
claunch set-harness ds4 claude       # writes profiles.ds4.harness in YAML
claunch run ds4                      # uses that stored default
claunch run ds4:pi                   # one-run override; YAML is unchanged
claunch login ds4:codex              # OAuth in ds4/codex/
claunch new-session -s pi --profile ds4:pi -c ~/proj
```

### Restricting harnesses by profile or provider

Both profiles and providers accept an optional `allowed_harnesses` list:

```yaml
providers:
  kimi-api:
    allowed_harnesses: [claude]
    env:
      ANTHROPIC_BASE_URL: "https://api.kimi.example/"

profiles:
  account:
    allowed_harnesses: [claude, pi]
  ds4:
    parent: account
    provider: kimi-api
    # Intersects account + kimi-api, so ds4 effectively allows only claude.
    allowed_harnesses: [claude, kimi]
```

The field being absent means unrestricted. An explicit `[]` allows no
harnesses. Profile constraints are intersected from the root ancestor through
the selected profile, then intersected with the effective provider's list
(own → ancestor → global → default). Unknown future harness names may remain in
the list, but only currently declared harnesses are offered.

The rule is enforced for bare defaults, explicit `PROFILE:HARNESS`, provider
overrides, `set-harness`, direct runs, managed creation/spawn, restore and
borrowed auth (the lender must also allow the consuming harness). API and UI
selector lists omit denied combinations; `profile_details[].harness_policy`
retains the denial reason for diagnostics.

The Web create form and Spawn modal show linked **Profile** and **Harness**
pickers: each base profile appears once, and changing it rebuilds Harness from
that profile's policy-filtered choices. The browser submits their combination
as canonical `PROFILE:HARNESS`, such as `ds4:pi`. The terminal
`new --wizard` and `spawn --wizard` forms show the same choices in one
`Profile : Harness` picker, labelled `PROFILE/HARNESS`, such as `ds4/pi`.
The effective default and every non-default alternative go through the same
profile/provider `allowed_harnesses` intersection. Denied combinations are
omitted, so a `codex` profile restricted to `[codex]` offers only `codex` in
its Harness picker. Sending a separate `harness` field/flag to the API is
rejected. A session saves the canonical selector, so
`ds4:pi` restores as Pi even if `profiles.ds4.harness` later changes.
The colon is logical only and never becomes part of a Windows path.

The same creation surfaces offer a **Model** picker. Claude declares
`haiku`, `sonnet`, `opus`, and `fable`; Codex declares `luna`, `terra`,
`sol`, and `astra`. The selected alias is saved with the session and launched as
`--model=<alias>`. A profile's `.claunch.yaml` `env` remains authoritative,
so Claude aliases can continue to resolve through values such as
`ANTHROPIC_DEFAULT_OPUS_MODEL`. Omitting Model uses the harness default. A
child inherits its parent's selection; changing or clearing it is governed
by `spawn.allow_args`.

`claude` is the one harness whose executable is `CLAUDE_LAUNCHER_BIN`; it uses
the profile root as `CLAUDE_CONFIG_DIR` for backwards compatibility. Other
packaged harnesses receive a namespaced home/config path below that root when
their CLI documents an override. The external CLI decides exactly which files
follow that variable (Cursor documents it for CLI config, not every credential):

| Harness | Authentication | Profile-specific path |
| --- | --- | --- |
| Claude Code | launcher token / Claude provider | profile root (`CLAUDE_CONFIG_DIR`) |
| Codex | `codex login` OAuth | `codex/` (`CODEX_HOME`) |
| Pi | `claunch set-token PROFILE` → packaged `ANTHROPIC_API_KEY`; selected custom provider → process-local Pi provider | `pi/` (`PI_CODING_AGENT_DIR`) |
| Kimi harness | `kimi login` OAuth | `kimi/` (`KIMI_CODE_HOME`) |
| Cursor agent | `agent login` OAuth | `agent/` (`CURSOR_CONFIG_DIR`, CLI config) |

There is one launcher-managed secret per base profile:
`<profile>/.launcher-token`, written by `set-token`. The packaged harness
document owns its projection. A non-default Claude provider routes it to
`ANTHROPIC_AUTH_TOKEN`; Pi routes the same value to
`ANTHROPIC_API_KEY` and, when its `provider_adapter: pi` is active, registers
the selected custom endpoint and models without storing the token in Pi
configuration. Claude always forces `ANTHROPIC_API_KEY=""`, after provider,
profile and session env have been layered. A custom API-key harness declares
its own `token_env`; OAuth harnesses declare no token route and instead remove
ambient API-key variables. There is no `set-key`, separate API-key file or
per-profile env-route metadata.

Upgrade note: a short-lived build wrote `.launcher-api-key`. If that file is
the profile's only launcher secret, the next bootstrap atomically moves it to
`.launcher-token`. If both files exist, `.launcher-token` is authoritative
and the old file is left untouched for manual review rather than deleting one
of two different secrets.

Kimi has two deliberately separate uses. `ds4:kimi` runs the Kimi CLI with
its own OAuth home. `ds4:claude` may use a Kimi-compatible API endpoint
through the existing Claude provider mechanism (`ANTHROPIC_*` and
`CLAUDE_CODE_*`); in that route the shared profile token becomes
`ANTHROPIC_AUTH_TOKEN`. The latter remains Claude Code, not the Kimi
harness.

Codex/Kimi/Cursor never receive the launcher token. Existing Claude-oriented
`ANTHROPIC_*` and `CLAUDE_CODE_*` profile values remain intact for Claude, but
are filtered from non-Claude harness environments. Each harness then receives
the translation of the selected provider's description (see
[API providers](#api-providers-third-party-backends)): Pi's adapter registers
a process-local provider from `endpoints.openai`, `models` and
`context_window` under launcher-owned `CLAUNCH_PI_*` names, Codex gets its
`-c` overrides, and `harness_options.<harness>.env` is the declared way to
hand such a harness a raw variable. A provider that only declares its
Anthropic-compatible endpoint cannot launch Pi and says so before spawning.

Every Pi session claunch launches also loads a packaged tools extension
(`pi_tools.mjs`) with claunch's builtin tools, whatever provider it runs on:

- `full_read` -- return a whole file with 1-based line numbers, never
  truncated (Pi's built-in `read` caps its output). Text files only; a
  directory, a missing file or a binary is refused with a reason, and the
  header names the line and byte counts so the model can see what it just
  spent.

Which builtin tools a session gets is decided in two layers:

- **Per profile (the default):** `harness_options.pi.tools: {full_read: false}`
  in `~/.claunch.yaml`, or `claunch tools PROFILE --off full_read` /
  `--on full_read`, which writes that block for you and prints the current
  defaults.
- **Per session (an override):** `--tools full_read,…` or `--tools none` on
  `claunch run PROFILE:pi`, `claunch new-session` and `claunch spawn`; the
  wizard's *Pi tools* row; and the web form's / spawn modal's *Pi tools*
  panel, which are pre-checked from the profile default and send `tools`
  only when you change them. A spawned child inherits its parent's choice
  and may change it only under `spawn.allow_args`, like `model`/`effort`.
  The choice is stored on the session record (`tools`) and survives a
  daemon restart.
The key an API-key harness receives is the profile's `set-token` secret;
without one, the provider's `api_key` is used, so a provider configured once
authenticates every harness. This prevents changing a profile's harness from
silently carrying a Claude backend or OAuth token into another CLI.

Sessions inherit the **daemon's** environment (tmux-server semantics), then the
harness/profile safe env and the session's `--env`. Auth and home boundaries
are re-applied last. Every session also
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
claunch install                    # this project: .mcp.json + .claude/skills
claunch install --global           # the user: ~/.claude/skills + user-scope MCP
claunch install --profile work     # or a profile's config dir
claunch install --all-profile      # every existing profile (alias: --all)
```

Profile installation follows the selected harness. Claude uses the profile
root, Codex uses `codex/config.toml`, Kimi Code uses `kimi/mcp.json`, and
Cursor Agent uses `agent/mcp.json`; skills are written below each harness
home. Pi receives the skills, but its install reports that MCP is unavailable
because Pi does not provide an MCP client. A one-off harness selection is also
supported, for example `claunch install --profile work:kimi`.

| Command | Description |
| ------- | ----------- |
| `install [--project [DIR] \| --global \| --profile P \| --all-profile]` | Register the MCP server and write the `/cflow`, `/cflow-author`, `/mesh` and `commit-stamp` skills into one scope: a project (the default), the user globally, or a profile. `--all-profile` (alias `--all`) is a profile install into every profile that exists — profiles are isolated config dirs, so a global install never reaches them; it does not touch the user's global setup. `--global`, `--profile` and `--all-profile` also seed the global workflow layer. Supersedes the separate `cflow`/`mesh` server entries an earlier version registered — they are removed, not left running alongside. Restart claude afterwards. |
| `mcp` | The stdio MCP server itself (spawned by claude, not by hand): `start`/`report`/`next`/`select`/`status` from cflow, `send`/`members`/`history` plus `spawn`/`children`/`connect`/`disconnect` from mesh. |

| Skill | Triggers on | Teaches |
| ----- | ----------- | ------- |
| `/cflow` | running or resuming a workflow | the execution protocol: one step at a time, report before advance, and every way a run can stop — including answering a decision put to *you* by somebody else's run |
| `/cflow-author` | writing or revising a workflow file | how to choose control points: the weakest one that holds, and the decisions the driving agent must never be the one to answer |
| `/mesh` | joining a mesh, or needing another agent | the member protocol, and the only correct way to create a session from inside one |
| `commit-stamp` | being about to `git commit` inside a managed session | ending every commit message with `Claunch-Session:` / `Claunch-Worktree:` trailers, so `git log` says which agent made each commit and in which checkout |

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
claunch mesh ops file dev worker_1 src/app.py      # read a member's file, here or
claunch mesh ops git dev worker_1 status           #   over the relay (read-only:
claunch mesh ops git dev worker_1 diff --base master --stat   # status/diff/log/show/branch)
claunch mesh lease dev acquire path:src/app.py --note "refactor"  # one holder at a
claunch mesh lease dev ls                          #   time, mesh-wide; exit 2 = held
claunch mesh lease dev release path:src/app.py
claunch mesh rank dev laptop 0    # move a peer; position 0 hands it authority
claunch mesh cut dev laptop pc-b  # drop one direct link (falls back to rank 0)
claunch spawn --mesh dev --as worker_2 --role worker --task "take the API"
                                  # ...an agent can do this itself (MCP 'spawn')
claunch mesh connect dev worker_1 worker_2      # let two MEMBERS talk directly
claunch mesh disconnect dev worker_1 worker_2   # ...or stop them (send refused)
claunch mesh wire-requests dev    # ...who was refused a peer and is waiting on
                                  # you; 'connect' grants one, --decline says no
claunch mesh invite dev           # optional ticket that pre-approves one join
claunch mesh join dev@work-pc --code <ticket>   # ...admitted without waiting
claunch mesh revoke dev other-pc  # unlink a guest machine (persistent until then)
claunch install                   # MCP tools + the /mesh and /cflow skills
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
  Delivery waits for the recipient's turn to end — and for its *keyboard* to
  go quiet: a human typing in that terminal (attach, web) parks the injection
  until no keystroke has landed for `CLAUNCH_TYPING_GUARD` seconds (default
  5), so a delivery never submits a message someone was mid-composing. The
  web terminal reports keys an IME is still composing (Hangul, a phone
  keyboard mid-word) as well, so the hold covers typing that has not become
  bytes yet; and `send-keys` with *text* (`claunch send-keys s "do X" Enter`
  from a script or another agent) queues behind the keyboard the same way —
  bare keys (`Enter`, `C-c`, `Escape`, arrows) never wait.
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
  interconnect's (`leader`/`operator`/`worker`/`reviewer`/`specialist`, plus
  `free-role`; aliases work, so `coder1` is a worker and `mod` leads, and
  anything unrecognised defaults to `free-role` — no role's powers, but free
  to carry out the task its creator gave it). Each mesh
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
mesh, decide who they may talk to, and re-draw the tree it built — via the
`spawn`, `children`, `connect`, `disconnect` and `reparent` MCP tools, or
`claunch spawn` / `claunch reparent` by hand. Two skills carry the
procedure for the re-drawing: `mesh-wire` (when to connect two peers who
keep needing each other through you) and `mesh-delegate` (spawn a nested
worker for a crowded area and `reparent` that area's workers under it, so
their branches land on its branch as a stacked pull request and the lead
integrates once — the nested worker runs the bundled `improv-mid`
workflow).

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
    max_children: 4          # direct children per session -- SOFT: warns
    max_depth: 3             # root session = depth 0 -- hard
    allow_workspace: true    # ...the one that starts open (see below)
    allow_cwd: false         # ...allow_profile / allow_args / allow_env too
  ```

  `allow_profile` unlocks both `--profile` and `--borrow` — the same
  question, whose login does the child hold — and when it is open the
  `children` report names the profiles, since an agent cannot read that
  registry. `--null` (spawn a child logged out) is never gated: it takes a
  credential away rather than granting one. A child otherwise
  **authenticates the way its parent does**, a parent's borrow included.
  Harness itself has no spawn unlock: it is derived from the inherited or
  allowed replacement profile. If that profile changes the harness, inherited
  args, model selection and Claude-only auth choices are dropped because they
  belonged to the parent's program.
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

- **`reparent` re-draws the tree after the fact.** A lead whose workers
  crowded into one area spawns a nested worker for it and moves those
  workers under it — `reparent` (MCP, scoped to the caller's own subtree)
  or `claunch reparent S PARENT` (the operator's, unscoped). The moved
  session keeps its terminal, conversation, handle, worktree and cflow run;
  its edge to the new parent is opened in every shared mesh, its edge to
  the old one is left for the mover to cut. Refused for a cycle, an exited
  parent, or any moved session landing past `spawn.max_depth`.

  ```
  lead spawns w1, w2, w3 (all on app.js)     then: spawn mid; reparent w1..w3 -> mid
                lead                                      lead
             /   |   \                                     |
           w1   w2    w3                                  mid  (lands w1..w3 on its branch as a stack)
        (three branches, three sweeps)                  / | \
                                                      w1 w2  w3
  ```

- **The nested worker runs `improv-mid`: a stacked pull request.** Its
  branch is the stack base and takes merge commits only; each child branch
  declares a base (the mid's branch, or a sibling's when it builds on that
  sibling), requests integration from the mid measured against that base,
  and lands with one `--no-ff` in order — after each landing the mid sends
  the rest a restack notice (`git rebase <base>`; commits already on the
  base are skipped). Children the mid spawns start on the stack via
  `spawn`'s `rebase_onto: <mid branch>`, which cuts a new worktree from that
  branch instead of the trunk. When the stack is complete the mid aligns
  the base on master with `git rebase --rebase-merges` (a plain rebase would
  flatten it) and sends the lead ONE request carrying the stack table; the
  lead merges it with one `--no-ff`. Landing gates stay where they were —
  each worker's and the mid's own are the user's — while landing a child on
  the mid's own branch is the mid's call, as master is the lead's.

- **A worker whose landing is a pull request runs `improv-worker-remote`.**
  It is a *layer* over `improv-worker` (`extends: improv-worker`), so the
  round has the same shape and the same gates; what changes is the medium.
  A `remote-setup` step after `branch-setup` reads the remote from
  `git config claunch.pr.remote` (and the base from `claunch.pr.base`,
  default `master`), checks `gh auth status --hostname <host>` — a private
  GitHub Enterprise host is just a hostname to `gh` — and points the branch's
  upstream at `<remote>/<base>`, which is how the unchanged
  `merge_ready.py` / `landed_check.py --target @{upstream}` gates come to
  measure against the remote. After peer review a `pr-open` step pushes the
  reviewed tip with an explicit refspec (a bare `git push` is forbidden for
  the round) and opens the PR with `gh pr create -R <host>/<owner>/<repo>`;
  the landing request's marker carries `pr: <url>`, and the `landed`
  checklist fetches before asking whether the remote base contains the tip.
  The lead's `improv-leader` reads a `pr:` row as one more candidate in the
  same integration table: same `merge_ready` screening, same 5-minute window,
  same one sweep per batch — it merges it with `gh pr merge --merge
  --match-head-commit <tip>` instead of a local `--no-ff`, fast-forwards
  master from the remote, and pushes master back so the remote base stays the
  push copy of master. Spawn it explicitly (`workflow: improv-worker-remote`);
  it does not volunteer for the worker role, so the wizard keeps picking the
  local variant by default.

### Delivery policies (heartbeat · task-poll · stall warnings · backpressure)

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

All three nudges are **off by default**: unlike interconnect's socket appends,
every nudge is a terminal injection that consumes the recipient agent's turn,
so enabling is a deliberate choice. Timers are in-memory (they restart with the
daemon); only the config persists, in `mesh.json`. There is no escalation
tier by design — delivery already *is* the escalation. Edit in the web mesh
view ("Delivery policy"), via
`claunch mesh policy <mesh> --set heartbeat.enabled=true ...`, or
`PUT /api/mesh/{mesh}/policy`.

**Backpressure** is the fourth section and the odd one out — not a nudge, but
the gate that bounds what the three above (and every member's traffic) can
hand a terminal. A fan-in of a dozen children at one leader had nothing
holding it: every `send` was accepted, the backlog only grew, and `busy_hold`
guaranteed that after a minute the daemon typed into the running turn anyway,
so twelve reports cost twelve interruptions and each sender was told `sent`.

- **the door** — a recipient whose undelivered backlog has reached
  `inbox_max` (default 4) stops *accepting*. The send is **refused, not
  queued**, and the sender is told so synchronously: refused for every
  recipient is `429` with `Retry-After` (`MeshBusy`; the CLI and MCP surface
  its sentence), refused for some is an ordinary send naming them in
  `deferred`. A partial refusal also narrows the address it stores — the log
  keeps the *address* and delivery re-derives recipients from it, so a `"*"`
  left intact would reach the refused member on the next tick and make the
  bounce a lie.
- **pacing** — at most one block typed into one terminal per `min_gap`
  (default 15s). Last of the automatic delivery gates, so it still binds
  after `busy_hold` has given up; the wait costs nothing, because arrivals
  in the meantime join the next block instead of interrupting separately.

Two carve-outs: an **external** send is the human at the dashboard (not the
fan-in this bounds, and they already have "deliver now"), and a send that
arrived over the wire carrying an id has already been accepted somewhere —
refusing it would lose it rather than un-send it.

Unlike the nudges this ships **on**: those *spend* a recipient's turn, so
switching one on is a choice; this is the only thing that stops a fan-in from
spending them for it. `inbox_max: 0` or `enabled: false` restores the old
unbounded queue. A refusal leaves no message anywhere — not in the log, not
in a queue — so it is counted on the recipient instead, and that count is
what the terminal header's delivery chip and the session panel's
**Mesh backpressure** box read: past the cap the backlog *stops growing*,
which looks exactly like calm on every other field.

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

**Profile** and **Harness** are linked execution pickers in the Web create and
Spawn forms. Profile lists each base name once; Harness reflects its default
and allowed explicit alternatives. The browser combines them into the API's
single canonical profile selector. Configure the default with
`claunch set-harness`. The terminal wizards present the same source as one
`Profile : Harness` picker. The adjacent **Model** picker follows the selected
harness and is available in the create form and both Spawn forms.

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

The same Settings page carries a **GitHub CLI (gh)** card, the machine side
of the [PR-landing worker](#workflows-cflow): whether `gh` resolves on the
*daemon's* PATH (a spawned worker inherits that environment, not the
browser's), the `gh auth status` verdict for every GitHub host the
registered repositories push to, and whether each repository names its
pull-request remote in `git config claunch.pr.remote`. Hosts are never
guessed from `origin`: a repository with the key unset lists every remote
and says the choice is still yours. Whatever is missing becomes a **What to
run** list — the platform's install command, `gh auth login --hostname
<host>` (or `GH_ENTERPRISE_TOKEN` in the daemon's environment for an
unattended daemon), the `git config` line — because a worker that reaches
`remote-setup` without them can only stop and ask. The card is read-only
and never shows a token; **Re-check** asks the daemon again after you have
acted. The JSON behind it is `GET /api/tools/gh`.

A session's detail panel carries an **Open PR** button for the other side of
that: push what the session's *directory* holds under a new branch and open
the pull request with `gh`, from a small wizard (remote, base, branch name,
title, draft). The daemon never touches the session's checkout -- no branch
is switched, nothing is committed on the one it is on: the push is
`git push <remote> <sha>:refs/heads/<name>`, and when *include uncommitted
changes* is on the `<sha>` is a commit built through a scratch index on top
of HEAD, on no local branch. Two checkboxes are about the session rather
than the push: **report** types one machine-generated block (branch, tip,
PR url, and the line that its checkout was not changed) into the session's
terminal, and **monitor** -- greyed until the PR-monitor workflow lands --
will spawn a child session that watches the PR and reports back. The wizard
shows the daemon's step list (inspect, snapshot, push, pr) whatever
happened, so a push that went through before `gh` refused is a green row
above a red one. Routes: `GET /api/sessions/{name}/pr/preview`,
`POST /api/sessions/{name}/pr` (engine: `prflow.py`).

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

**Embedding a terminal.** `/?embed=1#/s/<name>` shows that session's terminal
alone — the rail, the phone bars and the detail column are hidden — for another
page to hold in an iframe (issue-gen's board does this for its agent panel).
It is a display mode only: auth, routing and the socket are unchanged, and the
parent page picks the session by rewriting the hash. The login cookie is
per host name, so open both pages under the same name.

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
| POST   | `/api/daemon/shutdown`         | graceful stop — operator only; an agent session has no authority to call it (nor `/api/daemon/restart`), see *Who may restart or stop the daemon* |
| POST   | `/api/daemon/restart`          | stop and hand the port to a fresh daemon — operator only, same rule as `shutdown` |
| GET/POST | `/api/daemon/restart-request` | the approval gate a managed session's `claunch daemon restart` files into; `…/approve` and `…/reject` are the web UI's answers |
| GET/POST | `/api/sessions`              | list / create (`profile` is required and owns the harness; a submitted `harness` is refused; session fields include `{name?, cwd?, args?, env?, resume?, fork_session?}`). Onboarding is optional and composed in the same call: `{mesh?, handle?, role?, connect?, workflow?, context?, task?}` — checked before anything is built, and reported per leg beside the session's own fields |
| DELETE | `/api/sessions`                | clear all exited records (`?logs=1` deletes their logs; `?running=1` first shuts down and waits out every running session, so this drops *all* of them — `stopped` names what it ended). Records a mesh still names are kept back and reported in `kept` |
| POST   | `/api/sessions/kill`           | stop every running session (`?force=1`). Records stay, so all of them are still respawnable; `killed`/`failed` name both halves |
| POST   | `/api/sessions/respawn`        | relaunch every exited session under its own name, in creation order; `respawned`/`failed` |
| GET/DELETE | `/api/sessions/{name}`     | info / kill (`?force=1`) |
| GET    | `/api/sessions/{name}/meta`    | everything known *about* one session: definition, workspace, harness, role stance, mesh memberships, its cflow slot and the workflows startable in it; borrowed sessions also include a secret-free live `borrowed_auth` validation |
| POST   | `/api/sessions/{name}/respawn` | relaunch an exited session (claude resumes its conversation) |
| POST   | `/api/sessions/{name}/migrate` | move to another checkout: exactly one of `{worktree: NAME-or-""}` / `{cwd: DIR}`; `{children: true}` moves the descendants standing in the same directory. The claude transcript is carried to the new directory's slug |
| POST   | `/api/sessions/{name}/reborrow` | restart on another answer to "whose token": `{borrow: NAME-or-null, null_token?}` — picking one clears the others; the session is relaunched with the definition's auth swapped, the directory untouched |
| POST   | `/api/sessions/{name}/skip-permissions` | restart with permission prompts toggled: `{skip: true|false}` adds/removes `--dangerously-skip-permissions` in the definition's args and relaunches |
| POST   | `/api/sessions/{name}/keys`    | raw keyboard: `{keys: [...], literal}` — send-keys; or `{paste, enter}` — one bracketed paste (multiline-safe). Text (and any paste) waits out a human typing at that terminal (`CLAUNCH_TYPING_GUARD` quiet, bounded by `CLAUNCH_TYPING_HOLD_TIMEOUT`); bare keys go through at once |
| POST   | `/api/sessions/{name}/deliver` | `{text}` — hand the agent a message (paste + separately-written Enter). What every automated sender uses; `/keys` is for a human at a keyboard |
| GET    | `/api/sessions/{name}/pr/preview` | the PR wizard's preview of the session's directory: checkout branch, HEAD, uncommitted counts, remotes (with `claunch.pr.remote` / `claunch.pr.base` honoured), `gh` install + per-host auth, and `blockers` -- what would stop the push |
| POST   | `/api/sessions/{name}/pr` | push what the session's directory holds under a NEW branch and open the pull request with `gh` -- never touching its checkout. Body `{remote, base, branch, title, body, draft, include_uncommitted, force, report, monitor}`; `report` types the outcome into the session's terminal; `monitor` is answered with a warning until the PR-monitor workflow exists. Always 200 with `ok` and a `steps` list (`failed`/`error` name a refused step) |
| GET    | `/api/sessions/{name}/capture` | `?history=1&format=json&trim=0` |
| GET    | `/api/sessions/{name}/wait`    | long-poll `?state=idle\|exited&timeout=&threshold=` |
| POST   | `/api/sessions/{name}/resize`  | `{cols, rows}` |
| GET    | `/api/sessions/{name}/ws`      | terminal WebSocket (binary = PTY bytes, text = JSON control) |
| GET    | `/api/profiles`                | base profile names, policy-filtered execution selectors, labelled default options, and diagnostic selector details |
| GET    | `/api/borrow-options`          | `?profile=PROFILE[:HARNESS]` — secret-free lender validation; returns every base-profile option, including the runtime base profile, with policy/credential status and `selectable` |
| GET    | `/api/roles`                   | packaged role preview (name, aliases, stance); a selected mesh's `/roles` resource is authoritative |
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
| GET/PUT | `/api/mesh/{mesh}/policy`     | read / edit the mesh's delivery policy (heartbeat, task-poll, stall warnings, backpressure) |
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
| GET    | `/api/cflow/reminder`          | the reminder clock's machine defaults (`{defaults: {enabled, interval}}`) |
| PUT    | `/api/cflow/reminder`          | `{enabled?, interval?}` — set those defaults; the clock re-reads them every tick, so this applies without a restart |
| POST   | `/api/cflow/reminder`          | `{cwd, scope, enabled?, interval?}` or `{cwd, scope, clear: true}` — one run's override, stored (and archived) with the run |
| POST   | `/api/cflow/reminder/skip`     | `{cwd, scope}` — let ONE of that run's reminders go by: re-arms the clock's timer (and drops one held for a stopped session) without writing an override. Answers `{skipped}`; `false` = the clock was keeping no timer there |
| GET    | `/api/sessions/{name}/reminder` | the session-level pause and Role-source timer shown in the attached terminal header |
| POST   | `/api/sessions/{name}/reminder` | `{paused: bool}` — pause/resume this session's repeating Role and Cflow reminder deliveries; persisted with the session |
| POST   | `/api/sessions/{name}/reminder/skip` | re-arm this session's active Role and Cflow reminder sources for one interval without changing their settings |

**Session reminders.** One service coordinates independent sources per
session. The Role source is keyed by `(mesh, role, stance id)` and remains
active without a cflow run. The Cflow source is keyed by
`(run, status, step, visit)` and becomes due after that position stops moving.
When both are due, one terminal delivery carries peer `## Role` and
`## Cflow` sections; each source advances only after the delivery succeeds.
Mutable situation data (owed replies, open decisions, children and parent)
is recomputed at delivery time and appears in its own section.

The first Cflow reminder at a position carries the step text, while repeats
carry its content id and recovery command. The Role section always carries
the current membership and stance id; its longer `cflow_reminder` correction
appears on the first Cflow fire at that position. Progress resets only the
Cflow timer. A role or stance update resets only the Role timer. Reminders are
typed only while the session is **working** (busy); a due reminder for an idle
or suspended session is held until it is working again. Defaults:
`claunch daemon config cflow_reminder true|false` /
`cflow_reminder_interval 600` — these two keys are read live, no restart —
with a per-run override on the run's web page (or the POST above). Role uses
`role_reminder true|false` / `role_reminder_interval 600`. On a role-bearing
session, the terminal header's pause/resume control gates both repeating
sources at their shared delivery boundary, and **⏭** re-arms both active
source timers for one interval. Cflow signals and stall pings remain active.
The pause survives daemon restart and respawn. A Cflow-only session retains
the original per-run Cflow pause and skip controls.

**Resuming what a restart stopped.** A daemon restart brings restorable
sessions back (`--resume` of the pinned conversation), but a restored session
is *alive and idle*: the turn it was in the middle of died with the old
daemon, and nothing starts the next one — an agent only acts when something is
put in front of it. So the daemon puts something there. The sessions that were
**working** in the moment before shutdown (recorded alongside `was_running` in
`sessions.json`) are told, once, that the gap in their conversation was a
restart and to carry on. Two things narrow it: a session that was idle before
the restart hears nothing (its agent had finished — "continue" would invent
work), and neither does one whose cflow run is parked behind a human gate, a
user's selection or somebody else's answer (the run is stopped exactly where
the workflow wants it, and the agent cannot open that guardrail anyway). A run
on a `step` or `select` — and a session with no run at all — is nudged. The
nudge also waits for the TUI to be able to take it, and is dropped if the
session starts working on its own first. `claunch daemon config resume_nudge
true|false` (read at restore, so an edit applies to the next restart).

Daemon settings live under `daemon:` in `~/.claunch.yaml`
(`host`, `port`, `idle_threshold`, `scrollback_lines`, `restore`,
`cflow_reminder`, `cflow_reminder_interval`, `role_reminder`,
`role_reminder_interval`, `resume_nudge`); runtime
state (pid/port file, auth token, session logs) stays machine-local under
`~/.claude-launcher/daemon/`.

## Usage reporting

`claunch usage <name[:harness]>` follows the profile selector. An explicit
selector always determines the harness. For a bare profile created before
harness selectors existed, claunch selects an initialized same-name usage
harness when the configured default has no usable usage credential. This lets
an existing `codex` profile resolve to `codex:codex` while preserving a
credentialed provider on a profile such as `kimi`.

The supported cases are deliberately narrow:

- `usage work:claude` queries Anthropic only when the profile's effective
  provider is `default`. A Kimi/other Claude-compatible provider is refused;
  claunch does not pretend Anthropic's counters describe that backend.
- A Claude-compatible profile whose effective endpoint is the managed Kimi
  Code service (`api.kimi.com/coding`) queries its authenticated `/v1/usages`
  endpoint. This covers existing Kimi profiles that run through Claude Code.
- `usage work:codex` uses that profile's isolated `CODEX_HOME` and Codex
  app-server's documented `account/rateLimits/read` RPC.
- `usage work:kimi` starts the Kimi Code local server under that profile's
  isolated `KIMI_CODE_HOME`; the server refreshes OAuth and returns its managed
  account usage response.
- Pi and Cursor return an explicit unsupported-harness error.

Supported paths print per-window utilization:

```text
usage for profile 'work'
  five_hour          [##------------------]   9.0%  (resets in 4h34m)
  seven_day          [--------------------]   2.0%  (resets in 5h44m)
```

Add `--json` for the raw response. Each query uses only that profile's isolated
auth/home, so profiles cannot report another profile's account accidentally.

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
claunch install --global               # MCP server + /cflow skill + the shipped workflows
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

**Service loops (`recur: true`).** A cycle models work that must *converge*;
a session whose rounds must **keep happening** (a leader's standing
A → B → C, again and again) declares top-level `recur: true` instead of a
back edge. Every round still reaches a real `end` — the reachability rule is
untouched — and a run that finishes normally files the start request for its
next round through the same channel a human's `claunch cflow request` uses:
the driving agent starts round N+1 itself, each round is its own run with its
own visit counters and journal (so `max_visits` keeps meaning the rework
budget *within* a round), and payloads carry `round: N` from the second pass
on. The agent cannot end the loop. A human stops it between rounds
(`claunch cflow request --cancel`, or the dashboard) or mid-round
(abort/archive) — an aborted run never recurs.

**Where workflows live.** Two layers, nearest first:

| | | |
|---|---|---|
| `<cwd>/.claunch/workflows/*.yaml` | project | this directory only; **wins** |
| `~/.claude-launcher/workflows/*.yaml` | global | every directory on the machine |

`claunch install --global` (or `--profile`) seeds the global layer with the
workflows that ship in the package (`feature-dev`, `delegated-dev`), and never
overwrites one you have edited — it says `kept; yours differs` and leaves it.
Put your own there with

```bash
claunch cflow add ./ops.yaml           # a file
claunch cflow add ecs-change           # or promote this project's copy
claunch cflow add ops --project        # the other direction: fork it locally
```

which parses the workflow before installing it, so a broken YAML is refused
where you are standing rather than in someone else's picker a week later.

A project only needs a file of its own when it wants to **differ**; that copy
then shadows the global one. Nothing about that is ambiguous — the project
always wins — but the loser is *named* everywhere the winner appears
(`claunch cflow ls`, the dashboard's start picker, and a running run's
header), because two copies of one workflow drift silently otherwise:

```
$ claunch cflow ls
ecs-change       15 steps  LSP recon -> ... -> ship  [F:\works\ShelterZero\.claunch\workflows\ecs-change.yaml]
                 project copy overrides [C:\Users\me\.claude-launcher\workflows\ecs-change.yaml]
```

**A project layer can be a *layer*, not a copy.** Shadowing is whole-file, and
that is the wrong shape when the two files are not two workflows: the prose,
the graph and the protocol are written once and ship everywhere, while what a
step *checks* names tools that exist in one repository. Give the project file
an `extends:` and it carries only what it changes:

```yaml
# .claunch/workflows/improv-worker.yaml — the whole file
extends: improv-worker          # the global copy of the same name
steps:
  review:
    verify: 'python tools/changed_tests.py --base master'
  landed:
    verify: 'python tools/landed_check.py'
```

Merging is per property, and there are three rules: two mappings merge
recursively (so naming one field of one step leaves the other thousand lines
alone), anything else replaces (a list replaces wholesale — a half-merged list
has no reading), and an explicit `null` **deletes** an inherited property,
which is how a layer says "no verify here" where omitting means "inherit".
A base is a workflow name — searched from the extending file's own layer
downward, so a project file may extend the global copy of *the same name*, and
a global workflow can never reach up into some project's file — or a
`.yaml`/`.yml` path, resolved against the extending file's own directory.
Chains are allowed; a cycle is named rather than followed.

`claunch cflow add <name> --project --overlay` writes the stub. `cflow ls`,
`cflow show`, the dashboard's picker and a run's own `status` say what a file
extends, and `start` snapshots the **merge**, so editing a base cannot move a
position that is already running.

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
| `cflow add <wf>... [--name N] [--global \| --project [DIR]] [--overlay] [--force]` | Install a workflow (a `.yaml` path, or a name findable from here) into the global layer (the default), so every directory can run it — `--project` installs into a project instead (DIR defaults to the current one). Parses it first; refuses to replace a different file without `--force`. `--overlay` writes a *layer* (`extends: <name>`) instead of a copy, for a project that only needs to change a property or two. |
| `cflow install` / `cflow mcp` | Aliases kept for installs written before the servers merged — see `install` and `mcp` in [Toolkit commands](#toolkit-commands-what-an-agent-gets). |

## How it works

- Profiles live under `~/.claude-launcher/profiles/<name>` (override the base
  with `CLAUDE_LAUNCHER_HOME`). `name:harness` is an execution selector,
  not another directory; all variants share that base root and token.
- Claude uses the root as `CLAUDE_CONFIG_DIR`; Codex, Pi, Kimi and Cursor
  receive a namespaced child directory through their documented home/config
  variable (the external harness determines which data follows it).
- `run` exports the profile's safe `env` plus the authentication appropriate
  to its harness. Claude keeps the existing `ANTHROPIC_*`/`CLAUDE_CODE_*`
  precedence; those namespaces are not copied to unrelated harnesses.
- Launcher config (`harness`, `env`, `parent`, Claude provider,
  templates/definitions) lives in `~/.claunch.yaml`. Secrets stay local.

A profile directory typically holds:

| File | Origin |
| ---- | ------ |
| `.claude.json`      | Seeded from your global config (onboarding flags, prefs). |
| `settings.json`     | Seeded global settings (and any migrated `mcpServers`). |
| `.launcher-token`   | The one shared launcher token stored by `set-token` (`0600`); the selected harness declares its env projection. |
| `.credentials.json` | Written by Claude Code itself after an interactive login. |
| `codex/`, `pi/`, `kimi/`, `agent/` | Non-Claude harness auth/config homes, created as needed. |

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
