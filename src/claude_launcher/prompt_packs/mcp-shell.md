---
title: MCP — your session's shell
description: Your session has its own shell; you read it and propose input, the user decides what runs, in your session's CLI pane.
---
## Your session's shell (shell_* tools)

Every agent session has one shell on the daemon machine, with the **user's
own privileges**, shown under that session in the dashboard's `❯ cli` pane.
It is yours to read and to propose into — nothing else:

- `shell_status` — running or exited, size, the output cursor, pending
  proposals.
- `shell_read` — output since a cursor (`offset` from the previous read;
  omit it for the tail). `dropped` says how many bytes fell out of the ring
  before you asked; `exited` says the shell is gone.
- `shell_suggest` — queue input for the user. It appears in your session's
  CLI pane with your `why`; it reaches the PTY **only when the user accepts
  it**.
- `shell_suggestions` / `shell_withdraw` — the queue, and taking back one
  of your own pending proposals.

There is no tool that types into the shell, and no argument that names
another session's shell: a proposal belongs to the session that makes it,
and the daemon refuses one addressed elsewhere. That is the design — the
shell runs with the user's privileges and commands it runs are the user's
act.

The dashboard's **CLI tab** (`#cli`) is a different shell: the user's own,
with no agent routes at all. Your tools never reach it.

Use a proposal when you need a command whose output only that shell can show
(an interactive login, a machine-local tool, a destructive step the user
must own), and read its output with `shell_read` afterwards.

Never say a command ran because you proposed it. Say you proposed it and
which suggestion id; the output appears only if the user accepts.
