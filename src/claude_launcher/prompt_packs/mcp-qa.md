---
title: MCP — session QA
description: How to use the qa_* tools — put questions to the user as records in the QA pane, read their answers, and handle nudges and direction checks.
---
## Session QA (qa_* tools)

Put a question to the user as a record in your QA pane on the dashboard
instead of asking in the terminal, where reminders and messages bury it.

- `qa_ask` files it (title, optional body and options; the pane opens itself).
- `qa_update` rewrites it, `qa_close` settles it once acted on, `qa_delete`
  withdraws it, `qa_list` reads yours.
- Every change takes the `revision` you last read; a refusal (conflict)
  carries the current question — often the user just answered it.

When the user saves an answer, a `[claunch qa]` block is typed into your
terminal with the question and the answer (and the answer it replaced, for a
rewrite). Editing an answered question marks its answer stale and asks the
user again.

The user may also nudge you from the pane (`[claunch qa] ... nudge qn-...`):
file what you need to ask with `qa_ask`, or reply `qa_nothing` when you have
nothing to ask.

A closed question is not final: the user can rewrite its answer or ask for a
direction check against it (`[claunch qa] ... direction check`), which
reopens it. Compare your current work with the answer, change course where
you drifted, and `qa_close` it again with a note — the note is your reply.

### Questions and threads

A question that comes out of a thread conversation takes
`qa_ask thread=<id>`, so it links back to that thread; the user can also open
a thread about any question from its QA card (closed ones too), or by naming
its id in a thread's title. Each question's `links` lists those threads, and
an answer's block names them — carry the answer back into the thread there.
