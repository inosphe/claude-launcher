---
title: MCP — session threads
description: How to use the thread_* tools — answer a thread where it was asked, open your own topics, and alert the user in a thread when they must act now (every agent message there raises a dashboard notice).
---
## Session threads (thread_* tools)

The user can talk to you in several sub-topic threads at once from the
threads pane beside your terminal. A message they write there is typed into
your terminal as a `[claunch thread th-xxxxxx "title"]` block; answer it with
`thread_reply` on that thread_id, not in the terminal, so each topic keeps its
own history. Keep each thread to its topic; carry the main task on in the
terminal as usual.

- `thread_read` returns a thread's whole conversation (use it to pick a topic
  back up).
- `thread_list` shows your threads with unread counts.
- `thread_open` starts a new topic yourself (a side question to the user, a
  parallel line of work to report on).
- `thread_close` settles one.

Write every `thread_reply` and `thread_open` text as
GitHub-flavored markdown: the pane renders it (headings, lists, **bold**,
`code`, fenced code blocks, tables, links) and keeps single line breaks; it
shows raw HTML as text, and a reply wrapped whole in one code fence shows as
a code block.

### Threads and QA questions

Threads and QA questions link to each other: a thread opened about a question
(`thread_open qa=`, or the user's "discuss in a thread" on a QA card, or a
title naming one of your question ids, e.g. `qa-78302411: ...`) carries it as
its subject; a thread block says which question it is about and which of your
questions the user's message names — read them with `qa_list` (`closed=true`
for a settled one) before answering. When a thread reaches a decision the
user must make, file it with `qa_ask thread=<id>` so the question links back
to the conversation. Each thread's `links` lists the questions it touches.

### When the user must act now: alert them in a thread

Every message you write in a thread raises a notice card on the dashboard and
a row in its notification history, so the user sees it even when they are not
watching this terminal. Text in the terminal does not. So when your work needs
the user's action urgently, tell them in a thread.

Urgent means the work is stopped or something is at risk until a person acts,
for example:

- a step only a person may take: approving an irreversible or outward-facing
  action, a daemon restart, an interactive login, a permission or gate that
  refuses you;
- something the user owns is failing now: the live daemon or dashboard, a
  shared branch, data that may be lost;
- a decision whose delay costs more than the interruption (other sessions are
  waiting on it, a time window closes).

How:

1. Use an existing thread first. `thread_list` (add `closed=true` when an
   older topic may match); if a thread is already about this subject,
   `thread_reply` there — a reply to a closed thread reopens it.
2. Otherwise `thread_open` with a title you choose: one line naming the
   subject (e.g. `Daemon restart needed to apply claunch-xxxx`), and the
   message as `text`.
3. The message states what is blocked or at risk, the exact action you need
   from the user, the evidence (command output, file path, issue id), and what
   you do meanwhile.
4. When the action is a choice, also file it with `qa_ask thread=<id>` so the
   answer is recorded against the question; the thread message is the alert.

Send one alert and then wait; the user's reply is typed into your terminal as
a thread block. Do not repeat it on a loop. Routine progress and questions
that can wait stay in the terminal or the QA pane: a thread message for every
step makes the notices unreadable.
