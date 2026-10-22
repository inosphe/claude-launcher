---
title: MCP — shared user shell
description: The CLI tab's shell is the user's own; agents read it and propose input, the user decides what runs.
---
## Shared user shell (shell_* tools)

The dashboard's CLI tab holds one persistent shell with the **user's own
privileges**. It is shared with agent sessions read-only.

- `shell_status` — running or exited, size, the output cursor, pending
  proposals.
- `shell_read` — output since a cursor (`offset` from the previous read;
  omit it for the tail). `dropped` says how many bytes fell out of the ring
  before you asked; `exited` says the shell is gone.
- `shell_suggest` — queue input for the user. It appears in the CLI tab
  with your `why`; it reaches the PTY **only when the user accepts it**.
- `shell_suggestions` / `shell_withdraw` — the queue, and taking back one
  of your own pending proposals.

There is no tool that types into the shell. That is the design: the shell
runs with the user's privileges and commands it runs are the user's act.
Use a proposal when you need a command whose output only that shell can
show (an interactive login, a machine-local tool, a destructive step the
user must own), and read its output with `shell_read` afterwards.

Never say a command ran because you proposed it. Say you proposed it and
which suggestion id; the output appears only if the user accepts.
