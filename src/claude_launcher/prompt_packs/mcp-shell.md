---
title: MCP — your session's shell
description: Your session has its own shell; you read it and propose input, the user decides what runs, in your session's CLI pane.
---
## Your session's shell (shell_* tools)

Every agent session has one shell on the daemon machine, with the **user's
own privileges**, shown under that session in the dashboard's `❯ cli` pane.
It is yours to read and to propose into — nothing else:

- `shell_status` — running or exited, size, the output cursor, where your
  last read ended (`last_read`) and how many bytes are unread after it,
  pending proposals.
- `shell_read` — **without `offset` you get what arrived since your session
  last read.** The daemon keeps that cursor, so it survives a compacted
  context: you do not have to carry a number across turns. Pass a previous
  response's `offset` only to read from somewhere else. `dropped` says how
  many bytes fell out of the ring before you asked; `exited` says the shell
  is gone.
- `shell_suggest` — queue input for the user. It appears in your session's
  CLI pane with your `why`; it reaches the PTY **only when the user accepts
  it**.
- `shell_suggestions` / `shell_withdraw` — the queue, and taking back one
  of your own pending proposals.

When the user does something in your session's shell and wants you to look,
they press **nudge** in that pane. You receive a block starting
`[claunch nudge]` in your terminal: it carries their optional note and the
offset your unread output starts at. Answer it with `shell_read` and no
`offset` — that reads exactly those bytes. Nothing was typed into the shell
by the nudge.

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
