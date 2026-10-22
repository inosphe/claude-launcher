---
title: MCP — session threads
description: How to use the thread_* tools — answer a thread where it was asked, open your own topics, and alert the user in a thread when they must act now (every agent message there raises a dashboard notice).
---
## Session threads (thread_* tools)

The user can talk to you in several sub-topic threads at once from the
threads pane beside your terminal. A message they write there is typed into
your terminal as a `[claunch thread th-xxxxxx "title"]` block; answer it with
`thread_reply` on that thread_id, not in the terminal, so each topic keeps its
own history. Keep each thread to its topic (below); carry the main task on
in the terminal as usual.

- `thread_read` returns a thread's whole conversation (use it to pick a topic
  back up).
- `thread_list` shows your threads with unread counts.
- `thread_open` starts a new topic yourself (a side question to the user, a
  parallel line of work to report on).
- `thread_close` settles one.

### Asking the user: thread_ask vs qa_ask

`thread_ask` asks the user a simple question in a thread — `type` 'approve'
(they approve or deny), 'choice' (they pick one of `choices`), or 'text' (a
free-text answer). They answer in the threads pane and the answer is typed
back to you as a thread block. It is deliberately lighter than a QA
question: no QA pane record, no revisions — one question, one answer, kept
in the thread. Reach for `qa_ask` when the question belongs in the QA pane
(a decision you need recorded, re-askable, linked to threads) or when the QA
guidance says so; reach for `thread_ask` for the small in-topic question.
Unanswered asks are bounded (50) — do not stack asks the user has not seen.

Write every `thread_reply` and `thread_open` text as
GitHub-flavored markdown: the pane renders it (headings, lists, **bold**,
`code`, fenced code blocks, tables, links) and keeps single line breaks; it
shows raw HTML as text, and a reply wrapped whole in one code fence shows as
a code block.

To show the user a picture (a screenshot, a rendered chart, a diagram), pass
the image files' paths in `images` on `thread_reply` or `thread_open` (PNG,
JPEG, GIF or WebP, at most 10). The pane shows them under the message's text,
and a message may be images alone. A markdown image link to a local path does
not render; attach the file instead.

### One thread, one topic

A thread's title names its topic, and the user finds a conversation by that
title in the threads pane and the notice history. A thread that carries
messages about other subjects hides them under a title that no longer
describes them. So before every `thread_reply`, compare what you are about
to write with the thread's title and subject. When the content has diverged
from them, do not write it into that thread: `thread_open` a new thread
whose title names the new subject, and write it there.

The content has diverged when, for example:

- it is a new task, instruction or issue that arrived after the thread was
  opened about something else;
- it asks for an approval, an answer or a decision about a different issue,
  step or run than the one the thread is about;
- the thread's subject is settled (approved, answered, done) and you have
  something else to tell the user.

Being the thread you used last, or the one the user is reading now, does not
make a thread the right place. When the new subject follows from the old
one, name the old thread id in one line of the new thread's first message;
do not copy the old conversation over. Close the old thread with
`thread_close` once its own subject is settled. A session keeps at most 50
open threads; when `thread_open` is refused for that, close settled threads
and open again rather than writing into an unrelated one.

`thread_reply` stays the answer for anything on the thread's subject: the
user's message there, a follow-up on the same issue or decision, the result
of the action the thread asked for. When the user raises a new subject in a
thread, answer that message in the thread, and carry any further work on it
in a new thread named in that answer.

### Guests: another session in a thread

The user, or you as the owner, may invite another session into one thread
as a guest (`thread_invite`; `thread_uninvite` removes it). A guest is a
reviewer and advisor in that thread and nothing more.

- As the owner: a guest's message is typed into your terminal as a thread
  block. It is advice: you decide what to act on, and the work stays
  yours. Your replies are not typed into the guest; it reads the thread.
- As the owner, when a guest's session ends: each thread it was in gets a
  system message saying so, and you get one line here. Its messages stay;
  read them before deciding what to act on.
- As a guest: `thread_list` shows the thread under `guest_threads`, apart
  from your own. Each row says how many messages you have not read
  (`unread`) and how many of those the owner wrote after your last read
  (`owner_unread`): the owner's replies are not typed into your terminal,
  so a nonzero `owner_unread` means read the thread again. Read it with
  `thread_read` and answer with `thread_reply` on that thread. Do not carry
  out the thread's work yourself (no edits, commits, commands or child
  sessions for it), and do not use the QA tools
  (`qa_*`) for it: a question for the user goes into your reply. You cannot
  close, reopen or rename the thread (a reply in a closed one is kept and
  the thread stays closed); `thread_uninvite` with only the thread id
  leaves it.

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

1. Use an existing thread only when it is about this subject.
   `thread_list` (add `closed=true` when an older topic may match); if a
   thread's title and subject are this same matter (the same issue, the
   same decision), `thread_reply` there — a reply to a closed thread reopens
   it. A thread about another matter is not reused for the alert (see
   "One thread, one topic").
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
