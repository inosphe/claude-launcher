"""Session thread routes (claunch-doj98): sub-topic conversations between the
user and a session, held in the session's threads pane.

  GET  /api/threads?session=<name>[&closed=0]    one session's threads
  GET  /api/threads/rail                         per session: open, agent_seq, replies
  GET  /api/threads/forkable?session=<name>      can a thread be started in a fork? {ok, why}
  POST /api/threads                              open    {session | to, title?, text?, qa?, fork?}
  GET  /api/threads/{id}[?after=&limit=&reader=] the thread and its messages
  POST /api/threads/{id}/messages                post    {session?, text, ask_qa?}
  POST /api/threads/{id}/asks                    ask     {session, text, type?, choices?} (thread_ask)
  POST /api/threads/{id}/asks/{seq}/answer       answer  {decision?, text?} — the user answers
  POST /api/threads/{id}/close                   close   {session?}
  POST /api/threads/{id}/reopen                  reopen  {session?}
  POST /api/threads/{id}/title                   rename  {session?, title}
  POST /api/threads/{id}/guests                  invite  {session?, guest}
  POST /api/threads/{id}/guests/{guest}/remove   uninvite (or leave) {session?}
  POST /api/threads/images                       stage one image (raw body)
  GET  /api/threads/{id}/images/{name}           an image a message carries
  GET  /api/threads/{id}/files/{fid}/{name}      a file a message carries

Files (claunch-5gpcf). A message may also carry files of any kind (a
profiling capture): each is staged with ``POST /api/uploads`` and the post
names the ids in ``files``. They are kept in ``<daemon dir>/thread-files``
for 30 days (daemon/uploads.py); every message returns its ``files`` with
each one's ``path``, or ``expired: true`` once it is gone, and the block
typed into the session lists them as ``[claunch attachment]`` lines.

Images (claunch-shl2r). The user may attach images to a message: each is
staged first (``POST /api/threads/images``, the opening-image upload) and
the post names the staged ids in ``images``; a message may then have no
text. The files are kept for the thread's record (daemon/thread_images.py)
and every message returns its ``images`` with each file's ``path``. The
block typed into the session lists those paths; on a live session whose
harness takes images, the images are first pasted into its composer the
way the session line's image paste does, so the message arrives with them.
The agent attaches images the same way (claunch-euaz6): thread_reply /
thread_open stage each file it names through the same upload and post the
ids, and the pane shows them on the agent's message; nothing is typed into
the session for its own message.

A blank title opens the thread under the session's next default name
(``Thread #N``); without ``text`` the thread starts empty and nothing is
typed into the session until the first message.

Who is calling follows sim_api: a body naming ``session`` is that session
(the thread_* MCP tools send ``$CLAUNCH_SESSION``), a body without one is
the user (the dashboard), who names the session a new thread belongs to with
``to``. A read with ``reader=<session>`` moves that session's read position
when it owns the thread.

A message the user writes is typed into the owning session as a
``[claunch thread <id>]`` block carrying the thread's title and the message,
so the agent can answer into the right thread without a read; a held session
is woken for it. An exited session has no terminal to type into: the block
is queued durably (daemon/thread_notices.py) and typed in when the session
is launched again — a person's respawn or the daemon's restore — so a
message written while the session was gone still reaches it (claunch-sgxfh).
``delivered`` says the message was accepted for that
session, not that it is in the terminal yet: when something at the session
end is holding it back, ``hold`` names it (claunch-a41i5), so the pane can say
why nothing appeared instead of looking like a lost message.

QA links (claunch-78dpm, daemon/talk_links.py). A thread may be opened about
one of the session's QA questions (``qa`` on open); the question must exist
and be the same session's. Every thread this module returns carries
``links``: the question it is about and the questions filed out of it. A
user message about a question repeats the question in the delivered block,
and a post with ``ask_qa: true`` asks the agent to file what the thread
settled as a QA question linked back to it (qa_ask ``thread=``).
Opened without ``qa``, a thread whose title (or first message) names
exactly one of the session's question ids is linked to that question, and a
user message naming other questions of the session lists them in its
delivered block (claunch-236ei, talk_links.subject_in / mentioned).

Guests (claunch-p9wes). The user or the owner may invite another session
into a thread as a guest; the guest is told with a block that names its
role -- reviewer and advisor only: it reads the thread and writes advice in
it, and it does not do the thread's work or use the QA tools for it. The
owner's own replies are not typed into the guests -- they read them with
thread_read -- so two agents never answer each other in a loop. The guest's
session lists the thread apart from its own (``guest_threads`` of the list
route).

Mentions (claunch-p9wes.1.1). A message body may name who it is for:
``@all`` names every participant of the thread (its owner and its guests),
``@<session>`` names one. With no mention at all the message is typed into
the owner alone -- a thread with guests then behaves like one without them,
and the guests read it later with thread_read. With one or more mentions it
is typed only into the sessions named, the owner included only when it is
named; the author never receives its own message, and a name that is not a
participant of the thread is reported back as ``unknown_mentions`` instead
of delivered (daemon/threads_api.session_mentions, threads/store.py
``parse_mentions``).

Fork mode (claunch-uaxp8). ``fork: true`` on open starts the thread in a
COPY of the session's conversation: the daemon quick-forks the session
(:func:`daemon.api.stage_quick_fork`) and the copy owns the thread, with the
original session seated as its guest in the same store write. The fork's
name is the usual ``<origin>-qf<n>`` and the fork's opening task names the
thread, so the copy knows why it exists. The first message is typed into the
copy, not the origin. A fork is the user's press only -- an agent opening a
thread for itself has ``deep_think`` -- and it refuses a ``qa`` link: the
question belongs to the origin and the thread will belong to the copy. The
pane redirects to the copy, which is where the conversation now lives; its
checkbox is drawn from ``GET /api/threads/forkable``.

Asks (claunch-etw0s). The agent's ``thread_ask`` MCP tool posts a simple
question into one of its threads -- kind 'ask', type 'approve' | 'choice' |
'text', mirroring the operator conversation's ask (daemon/operators_v2.py).
The user answers in the threads pane (``POST .../asks/{seq}/answer``); the
answer is recorded on the entry (``data.answer = {decision, text, at}``)
and typed into the owning session as one block, the way a QA answer
arrives. Only the user answers; the asking session waits, and the pane
shows an unanswered ask as 답변 대기. A session may hold at most
store.MAX_OPEN_ASKS unanswered asks. Simpler than the QA tools on purpose:
no QA pane record, no revisions -- one question, one answer, kept in the
thread.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from aiohttp import web

from ..threads import store as thread_store
from . import paths, talk_links, thread_images, thread_notices, uploads

log = logging.getLogger(__name__)

PREFIX = "[claunch thread"

#: The agent's ask text is short -- the operator conversation's cap
#: (daemon/operators_v2.py).
ASK_TEXT_MAX = 4000
ASK_CHOICE_MAX = 300
ASK_CHOICES_MIN = 2
ASK_CHOICES_MAX = 10

#: Background flushes of notices queued for an ended session (see
#: :func:`flush_notices`); held so none is collected mid-flight.
_flush_tasks: set = set()


def notice_id(thread: dict, message: Optional[dict], kind: str = "m") -> str:
    """The id a queued notice is recorded under for one thread message.

    Derived from the store's own message seq, so a post that is queued
    twice is one notice (daemon/thread_notices.py).
    """
    seq = (message or {}).get("seq")
    return f"{thread['id']}:{kind}{seq}" if seq else ""


def _quote(text: str) -> str:
    return "\n".join("  > " + line for line in (text or "").splitlines()) or "  > (empty)"


def _ask_text(value) -> str:
    """The ask's question: nonempty, capped like the operator's."""
    if not isinstance(value, str) or not value.strip():
        raise thread_store.ThreadError("text is required")
    value = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(value) > ASK_TEXT_MAX:
        raise thread_store.ThreadError(f"text is longer than {ASK_TEXT_MAX} characters")
    return value


def _ask_kind(body: dict):
    """(type, choices): 'choice' takes 2..10 nonempty choices of at most
    ASK_CHOICE_MAX characters; the other types take none."""
    kind = body.get("type") or "approve"
    if kind not in thread_store.ASK_TYPES:
        raise thread_store.ThreadError("type is one of " + ", ".join(thread_store.ASK_TYPES))
    choices = body.get("choices") or []
    if kind == "choice":
        if (not isinstance(choices, list) or not ASK_CHOICES_MIN <= len(choices) <= ASK_CHOICES_MAX
                or any(not isinstance(c, str) or not c.strip() or len(c) > ASK_CHOICE_MAX for c in choices)):
            raise thread_store.ThreadError(
                f"a choice ask needs {ASK_CHOICES_MIN}..{ASK_CHOICES_MAX} nonempty choices, "
                f"each at most {ASK_CHOICE_MAX} characters")
    elif choices:
        raise thread_store.ThreadError("choices belong to a choice ask")
    return kind, choices


def _subject_line(thread: dict) -> list[str]:
    for link in thread.get("links") or []:
        if link["rel"] == "subject":
            if link["status"] == "deleted":
                return [f"This thread is about your QA question {link['id']} (since deleted)."]
            return [f"This thread is about your QA question {link['id']} ({link['status']}): \"{link['title']}\". "
                    "If the discussion changes the question, qa_update it; if it settles it, say so here."]
    return []


def _mention_lines(mentions: list[dict]) -> list[str]:
    return [f"The user's message names your QA question {q['id']} ({q['status']}): \"{q['title']}\"."
            for q in mentions]


def _file_lines(message: dict, who: str = "The user") -> list[str]:
    files = [f for f in message.get("files") or [] if f.get("path")]
    if not files:
        return []
    return [f"{who} attached {len(files)} file(s) to this message; each line ends with its path "
            "on this machine (read it from there):",
            *(uploads.line(f["name"], f["bytes"], f["path"]) for f in files)]


def _image_lines(message: dict, who: str = "The user") -> list[str]:
    images = message.get("images") or []
    if not images:
        return []
    return [f"{who} attached {len(images)} image(s) to this message (the files are on this machine; "
            "open them to see what is meant):",
            *(f"  - {img['path']}" for img in images)]


def user_wrote_text(thread: dict, message: dict, *, opened: bool = False, reopened: bool = False,
                    ask_qa: bool = False, mentions: Optional[list[dict]] = None) -> str:
    tid = thread["id"]
    if opened:
        head = "The user opened a new thread with you:"
    elif reopened:
        head = "The user reopened this thread and wrote:"
    else:
        head = "The user wrote in this thread:"
    lines = [f"{PREFIX} {tid} \"{thread['title']}\"] {head}",
             _body_lines(message),
             *_image_lines(message), *_file_lines(message), *_subject_line(thread),
             *_mention_lines(mentions or [])]
    if ask_qa:
        lines.append(
            "The user asks you to turn this thread into a QA question: file the decision it needs from them "
            f"with qa_ask(..., thread=\"{tid}\") so the question links back here, then reply in the thread "
            "with the question id. If there is nothing to decide, reply in the thread saying why.")
    lines.append(
        f"Answer in the thread, not the terminal: thread_reply(thread_id=\"{tid}\", text=...), "
        "the text in markdown (the pane renders it). "
        f"thread_read(thread_id=\"{tid}\") has the whole conversation; thread_list shows all your threads. "
        "Keep this sub-topic's discussion in this thread.")
    return "\n".join(lines)


def _body_lines(message: dict) -> str:
    if message["body"] or not (message.get("images") or message.get("files")):
        return _quote(message["body"])
    return "  > (images only)" if not message.get("files") else "  > (attachments only)"


def _guest_role(tid: str, owner: str) -> str:
    return (f"Your role here is reviewer and advisor only: read the conversation (thread_read(thread_id=\"{tid}\")) "
            f"and give review comments and advice with thread_reply(thread_id=\"{tid}\", text=...). "
            f"Do not carry out the thread's work yourself -- no edits, commits, commands or child sessions "
            f"for it; {owner} does the work. Do not use the QA tools (qa_*) for this thread: a question for "
            "the user goes into your reply here.")


def _named_line() -> str:
    """Why a guest that reads none of the thread's traffic by default got
    this one: the message named it (claunch-p9wes.1.1)."""
    return ("This message names you with an @-mention (or @all), so it is typed in here. Messages that name "
            "nobody stay in the thread: read them with thread_read, and thread_list's guest_threads row says "
            "how many you have not read.")


def guest_invited_text(thread: dict, by: str) -> str:
    tid, owner = thread["id"], thread["session"]
    inviter = "the user" if by == thread_store.USER else f"session {by}"
    return "\n".join([
        f"{PREFIX} {tid} \"{thread['title']}\"] You are invited by {inviter} as a guest to this thread of "
        f"session {owner}.",
        _guest_role(tid, owner),
        "A message is typed in here only when it names you (@<your session>) or names everyone (@all); the rest "
        f"-- {owner}'s replies included -- you read with thread_read. thread_list lists this thread under "
        "guest_threads, whose guest.owner_unread counts messages since your last read. You cannot close or "
        f"rename it; thread_uninvite(thread_id=\"{tid}\") leaves it."])


def guest_removed_text(thread: dict) -> str:
    return (f"{PREFIX} {thread['id']} \"{thread['title']}\"] You are no longer a guest of this thread of session "
            f"{thread['session']}: nothing more from it is typed in here, and you cannot write in it.")


def guest_ended_text(thread: dict, guest: str) -> str:
    tid = thread["id"]
    return (f"{PREFIX} {tid} \"{thread['title']}\"] Guest session {guest} ended, so it is no longer a guest of "
            f"this thread (the thread itself now says so). Nothing more is typed in here from it; its review "
            f"stays in the thread -- thread_read(thread_id=\"{tid}\") has it. The work stays yours.")


def owner_invited_text(thread: dict, guest: str) -> str:
    tid = thread["id"]
    return (f"{PREFIX} {tid} \"{thread['title']}\"] The user invited session {guest} into this thread as a guest "
            f"reviewer and advisor. Its messages are typed in here as advice: you decide what to act on, and "
            f"the work stays yours. Your replies here are not typed into {guest} unless you name it "
            f"(@{guest}, or @all); otherwise it reads the thread.")


def guest_user_wrote_text(thread: dict, message: dict, *, reopened: bool = False) -> str:
    tid = thread["id"]
    head = "The user reopened this thread and wrote:" if reopened else "The user wrote in this thread:"
    return "\n".join([
        f"{PREFIX} {tid} \"{thread['title']}\"] (you are a guest of session {thread['session']} here) {head}",
        _body_lines(message), *_image_lines(message), *_file_lines(message),
        _named_line(), _guest_role(tid, thread["session"])])


def guest_wrote_text(thread: dict, message: dict, guest: str) -> str:
    tid = thread["id"]
    who = f"Guest {guest}"
    return "\n".join([
        f"{PREFIX} {tid} \"{thread['title']}\"] Guest session {guest} (reviewer and advisor) wrote in this thread:",
        _body_lines(message), *_image_lines(message, who), *_file_lines(message, who),
        f"It is advice: you decide what to act on. If it needs an answer, thread_reply(thread_id=\"{tid}\", "
        f"text=...); name it (@{guest} or @all) in that reply to type it into {guest}'s terminal, otherwise it "
        "reads the thread."])


def guest_mentioned_text(thread: dict, message: dict, guest: str) -> str:
    """A guest named another guest: typed into the one named
    (claunch-p9wes.1.1)."""
    tid, owner = thread["id"], thread["session"]
    who = f"Guest {guest}"
    return "\n".join([
        f"{PREFIX} {tid} \"{thread['title']}\"] Guest session {guest} named you in this thread of session {owner}:",
        _body_lines(message), *_image_lines(message, who), *_file_lines(message, who),
        _named_line(), _guest_role(tid, owner)])


def owner_mentioned_text(thread: dict, message: dict, owner: str) -> str:
    """The owner named a guest in its message: typed into that guest."""
    tid = thread["id"]
    who = f"Session {owner}"
    return "\n".join([
        f"{PREFIX} {tid} \"{thread['title']}\"] Session {owner}, the owner of this thread, named you in a message:",
        _body_lines(message), *_image_lines(message, who), *_file_lines(message, who),
        _named_line(), _guest_role(tid, owner)])


def ask_answered_text(thread: dict, message: dict) -> str:
    """The block typed into the owning session when the user answers its
    ask (claunch-etw0s) -- the thread counterpart of a QA answer block."""
    tid = thread["id"]
    answer = ((message.get("data") or {}).get("answer")) or {}
    decision = answer.get("decision")
    said = {"approve": "approved", "deny": "denied"}.get(decision, decision)
    lines = [f"{PREFIX} {tid} \"{thread['title']}\"] The user answered your ask:"]
    if said:
        lines.append(f"  decision: {said}")
    if answer.get("text"):
        lines.append(_quote(answer["text"]))
    return "\n".join(lines)


def install(app, store: Optional[thread_store.ThreadStore] = None):
    manager = app["manager"]
    threads = store or thread_store.ThreadStore(paths.threads_db())
    app["threads"] = threads

    def guest_ended(session) -> None:
        """Exit hook: a session that ended is no guest any more
        (claunch-tee5g) -- off the chips, the delivery list and the cap --
        and each thread it was in says so and tells its owner
        (claunch-p9wes.1)."""
        try:
            left = threads.drop_guest(session.sdef.name)
        except Exception:  # noqa: BLE001 -- never break the exit fan-out
            log.exception("threads: could not drop the ended guest %s", getattr(session, "sdef", session))
            return
        for thread in left:
            try:
                deliver(thread["session"], guest_ended_text(thread, thread["guest"]))
            except Exception:  # noqa: BLE001 -- the notice is not the ending
                log.exception("threads: could not tell %s its guest ended", thread.get("session"))

    def paste_step(session, images: list[dict], text: str):
        """``(before, text)`` for a message carrying images: the images go
        into the composer first, the way the session line's image paste does
        (api._opening_image_step), and the text names any that could not be
        pasted. ``(None, text)`` when the harness declares no image transport
        -- the block's paths are then the only way the images arrive."""
        from .. import harnesses as harness_registry
        from . import api as api_mod, opening_images
        entry = harness_registry.registry().get(getattr(session.sdef, "harness", "") or "")
        if not images or not (entry and entry.takes_images):
            return None, text
        files = [opening_images.Image(Path(img["path"]), img["type"]) for img in images]
        return api_mod._opening_image_step(session, files, text, what="thread images")

    def deliver(name: str, text: str, images: Optional[list[dict]] = None,
                notice: str = "", thread: str = "") -> bool:
        """Type ``text`` into session ``name``; a held session is woken for
        it. An exited session has no terminal to type into, so a block that
        carries ``notice`` is queued (daemon/thread_notices.py) and typed in
        when the session is launched again — a person's respawn or the
        daemon's restore. False when there is nobody to tell (unknown). With
        ``images``, a live session gets them pasted in ahead of the text."""
        try:
            session = manager.get(name)
        except Exception:  # noqa: BLE001 -- gone or not deliverable
            return False
        if (notice and getattr(session, "exited", False)
                and not getattr(session, "held_at", None)):
            # A held one is only asleep: the wake queue below types it in.
            return thread_notices.queue(name, notice, text, thread=thread)
        try:
            before, body = paste_step(session, images or [], text)
            # ``before`` only when there is one, so a text-only message makes
            # exactly the call it made before images existed.
            if session.queue_delivery(body, **({"before": before} if before else {})):
                return True
        except Exception:  # noqa: BLE001 -- gone or not deliverable
            pass
        try:
            return bool(manager.wake_soon(name, by="thread", deliver=text))
        except Exception:  # noqa: BLE001
            log.info("threads: could not deliver to %s", name)
            return False

    def flush_notices(session) -> None:
        """Launch hook: type the notices queued while ``session`` was ended.

        Scheduled, not awaited: the flush waits for the program to take
        input, which is seconds after launch."""
        try:
            if not thread_notices.pending(session.sdef.name):
                return
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(thread_notices.flush(session))
        _flush_tasks.add(task)
        task.add_done_callback(_flush_tasks.discard)

    hooks = getattr(manager, "exit_hooks", None)
    if hooks is not None:
        hooks.append(guest_ended)
    launch_hooks = getattr(manager, "launch_hooks", None)
    if launch_hooks is not None:
        launch_hooks.append(flush_notices)

    def hold_of(name: str) -> Optional[str]:
        """Why a message just accepted for ``name`` is not typed in yet, read
        from the same signals :meth:`Session.deliver` waits on: ``asleep``
        (a held session being woken; it goes in once it is back), ``exited``
        (queued until a respawn or restore; daemon/thread_notices.py),
        ``draft`` (an unsent line in that terminal's composer; it goes in
        after the human sends or clears it), ``typing`` (keys in the last few
        seconds). None when nothing is in the way. A snapshot at accept time
        only."""
        try:
            s = manager.get(name)
        except Exception:  # noqa: BLE001 -- gone between deliver and here
            return None
        if getattr(s, "held_at", None):
            return "asleep"
        if getattr(s, "exited", False):
            return "exited"
        draft = getattr(s, "draft_open", None)
        if callable(draft) and draft():
            return "draft"
        busy = getattr(s, "keyboard_busy", None)
        if callable(busy) and busy():
            return "typing"
        return None

    def link(res_threads: list[dict], session: str) -> None:
        talk_links.attach_to_threads(res_threads, app.get("qa"), session)

    def question_of(qid: str) -> Optional[dict]:
        qa = app.get("qa")
        if qa is None:
            return None
        try:
            return qa.get(qid)
        except Exception:  # noqa: BLE001 -- no such question
            return None

    def qa_rows(session: str) -> list[dict]:
        """The session's questions as link rows; empty when QA is not
        installed or unreadable (no id is then matched, nothing refused)."""
        qa = app.get("qa")
        if qa is None:
            return []
        try:
            return qa.link_rows(session)
        except Exception:  # noqa: BLE001 -- the QA store is unreadable
            return []

    def mentions_in(thread: dict, message: Optional[dict]) -> list[dict]:
        if not message:
            return []
        return talk_links.mentioned(message["body"], qa_rows(thread["session"]),
                                    skip=[thread["qa"]] if thread.get("qa") else [])

    def session_mentions(thread: dict, message: Optional[dict], author: Optional[str]) -> dict:
        """Who a message's ``@``-mentions route it to (claunch-p9wes.1.1).

        ``tokens`` is every name the body carries; ``unknown`` those that are
        no participant of this thread -- reported back, not delivered.
        ``targets`` are the sessions it is typed into: with no mention at all
        the owner alone, so a thread with guests behaves like one without
        them and the guests read it later; with mentions only those named, by
        ``@<session>`` or by ``@all`` (the owner and every guest). A body that
        names only sessions that are no participant is typed into nobody, and
        the author never receives its own message.
        """
        tokens = thread_store.parse_mentions((message or {}).get("body") or "")
        owner = thread["session"]
        guests = [g["session"] for g in thread.get("guests") or []]
        everyone = [owner, *guests]
        all_named = thread_store.MENTION_ALL in tokens
        named = [n for n in everyone if n in tokens]
        if all_named:
            chosen = everyone
        elif named:
            chosen = named
        elif tokens:
            chosen = []          # names nobody here: reported, delivered to nobody
        else:
            chosen = [owner]     # no mention: as if the thread had no guests
        return {
            "all": all_named,
            "named": named,
            "tokens": tokens,
            "targets": [n for n in chosen if n != author],
            "unknown": [t for t in tokens if t != thread_store.MENTION_ALL and t not in everyone],
        }

    def mention_fields(res: dict, route: dict) -> None:
        """The routing a write returns: what the body named and what it did
        not match. Both only when there is a mention, so a message that names
        nobody answers as it always did."""
        if route["tokens"]:
            res["mentions"] = {"all": route["all"], "targets": route["targets"]}
        if route["unknown"]:
            res["unknown_mentions"] = route["unknown"]

    def tell(res: dict, name: str, text: str, notice: str = "", thread: str = "") -> None:
        res["delivered"] = deliver(name, text, (res.get("message") or {}).get("images"),
                                   notice=notice, thread=thread)
        res["hold"] = hold_of(name) if res["delivered"] else None

    def guest_holds(res: dict, delivered: dict) -> None:
        """``guests_hold``: why a guest that accepted the message is not typed
        into yet, read the same way as the owner's ``hold``. A caller that
        reports "delivered" as "typed into a terminal" (mega threads'
        ledger) needs it to tell a queued guest from a typed one
        (claunch-p9wes.1.1)."""
        holds = {name: hold_of(name) for name, ok in delivered.items() if ok}
        holds = {name: why for name, why in holds.items() if why}
        if holds:
            res["guests_hold"] = holds

    def tell_user_message(res: dict, thread: dict, *, ask_qa: bool = False) -> None:
        """Type a user message into the sessions it is meant for: the owner
        when the body names nobody, otherwise only the sessions it names
        (claunch-p9wes.1.1). ``guests_delivered`` says per named guest whether
        the message was accepted, and ``guests_hold`` why one that was
        accepted is not typed in yet -- the owner's ``hold`` for a guest."""
        route = session_mentions(thread, res.get("message"), None)
        mention_fields(res, route)
        targets = set(route["targets"])
        qa_mentions = mentions_in(thread, res.get("message"))
        notice = notice_id(thread, res.get("message"))
        if thread["session"] in targets:
            tell(res, thread["session"],
                 user_wrote_text(thread, res["message"], reopened=res.get("reopened", False),
                                 ask_qa=ask_qa, mentions=qa_mentions),
                 notice=notice, thread=thread["id"])
        images = (res.get("message") or {}).get("images")
        delivered = {g["session"]: deliver(g["session"], guest_user_wrote_text(thread, res["message"],
                                                                              reopened=res.get("reopened", False)),
                                           images, notice=notice, thread=thread["id"])
                     for g in thread.get("guests") or [] if g["session"] in targets}
        if delivered:
            res["guests_delivered"] = delivered
            guest_holds(res, delivered)

    def mention_delivery(res: dict, thread: dict, actor: str, *, guest: bool,
                         route: Optional[dict] = None) -> None:
        """Type a message its author wrote into the guests it names: the
        owner's or another guest's message reaches a guest only through
        ``@<guest>`` or ``@all``, so two agents never answer each other in a
        loop (claunch-p9wes.1.1)."""
        route = route or session_mentions(thread, res.get("message"), actor)
        mention_fields(res, route)
        images = (res.get("message") or {}).get("images")
        out = {}
        for g in thread.get("guests") or []:
            if g["session"] not in route["targets"]:
                continue
            text = (guest_mentioned_text(thread, res["message"], actor) if guest
                    else owner_mentioned_text(thread, res["message"], actor))
            out[g["session"]] = deliver(g["session"], text, images)
        if out:
            res["guests_delivered"] = out
            guest_holds(res, out)

    def owner_message(tid: str, text: str) -> dict:
        """Post a user message into one thread and type it into the sessions
        its mentions name -- a thread with guests and no mention behaves as
        one without guests (claunch-p9wes.1.1): the text-only half of
        :func:`h_post`, for callers inside the daemon that already hold a
        thread id (mega threads, claunch-v5nhp). Exposed as
        ``app["threads_post_user"]``."""
        res = threads.post(thread_store.USER, tid, text)
        t = res["thread"]
        link([t], t["session"])
        tell_user_message(res, t)
        return res

    # Handed to daemon/megathread_api.py: a mega thread delivers by posting a
    # user message into one thread, and that path (store row, owner and guest
    # typing, QA links) belongs here (claunch-v5nhp).
    app["threads_post_user"] = owner_message

    def known(name: str) -> bool:
        try:
            manager.get(name)
            return True
        except Exception:  # noqa: BLE001 -- no such session
            return False

    async def body_of(request) -> dict:
        try:
            body = await request.json()
        except (ValueError, UnicodeError):
            body = None
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text='{"error": "body must be a JSON object"}',
                                     content_type="application/json")
        return body

    def actor_of(body: dict) -> str:
        s = body.get("session")
        return s if isinstance(s, str) and s else thread_store.USER

    async def call(fn, *args, **kw):
        try:
            res = await asyncio.to_thread(fn, *args, **kw)
        except thread_store.ThreadError as exc:
            return None, web.json_response({"error": str(exc)}, status=exc.status)
        if isinstance(res, dict):
            # Every message leaves with its images' paths on this machine.
            for m in [res.get("message"), *(res.get("messages") or [])]:
                if m:
                    thread_images.attach(m)
                    uploads.attach(m)
        return res, None

    def operator_thread(tid: str):
        """A refusal when ``tid`` is an operator's thread: its writes go
        through the operator routes (daemon/operators_v2.py), which nudge
        the operator instead of typing into a session."""
        try:
            row = threads.get(tid)
        except thread_store.ThreadError:
            return None
        if str(row["session"]).startswith(thread_store.OPERATOR_PREFIX):
            return web.json_response(
                {"error": f"thread {tid} is an operator's conversation; write to it in the Operator view"},
                status=400)
        return None

    async def keep_images(body: dict):
        """``body["images"]`` (staged ids) moved into the kept folder, as
        the store's records; or an error response."""
        raw = body.get("images")
        if raw in (None, []):
            return [], None
        try:
            ids = thread_images.check(raw)
            return await asyncio.to_thread(thread_images.keep, ids), None
        except thread_images.ThreadImageError as exc:
            return None, web.json_response({"error": str(exc)}, status=400)

    async def keep_files(body: dict):
        """``body["files"]`` (staged ids) moved into the thread folder, as
        the store's records; or an error response."""
        raw = body.get("files")
        if raw in (None, []):
            return [], None
        try:
            ids = uploads.check_ids(raw, field="files", what="a thread message")
            return await asyncio.to_thread(uploads.keep_for_thread, ids), None
        except uploads.UploadError as exc:
            return None, web.json_response({"error": str(exc)}, status=400)

    async def keep_attachments(body: dict):
        """Images and files of a post, kept; ``(images, files, error)``."""
        images, err = await keep_images(body)
        if err:
            return None, None, err
        files, err = await keep_files(body)
        if err:
            await asyncio.to_thread(thread_images.unkeep, images)
            return None, None, err
        return images, files, None

    async def unkeep_attachments(images, files):
        await asyncio.to_thread(thread_images.unkeep, images)
        await asyncio.to_thread(uploads.unkeep_thread, files)

    async def h_list(request):
        session = request.query.get("session") or ""
        if not session:
            return web.json_response({"error": "session is required"}, status=400)
        closed = request.query.get("closed", "1") not in ("0", "false", "no")
        res, err = await call(threads.list, session, closed=closed)
        if err:
            return err
        await asyncio.to_thread(link, res["threads"], session)
        return web.json_response(res)

    async def h_rail(_request):
        res, err = await call(threads.rail)
        return err or web.json_response({"sessions": res})

    async def h_history(request):
        q = request.query
        res, err = await call(threads.history, page=q.get("page") or 1,
                              per_page=q.get("per_page") or thread_store.HISTORY_PAGE,
                              session=q.get("session") or None, status=q.get("status") or None,
                              since=q.get("since") or None)
        return err or web.json_response(res)

    async def h_index(request):
        q = request.query
        res, err = await call(threads.index, session=q.get("session") or None,
                              status=q.get("status") or None, since=q.get("since") or None)
        return err or web.json_response(res)

    def fork_blocked(name: str) -> str:
        """Why a thread cannot be started in a quick-fork of ``name``: "" when
        it can (claunch-uaxp8).

        The same facts :func:`daemon.api.stage_quick_fork` refuses on, asked
        *before* the thread is written so a refusal costs neither a thread nor
        a fork. The pane's checkbox asks the same question through
        ``GET /api/threads/forkable``; one function answers both, so the offer
        and the refusal cannot disagree.
        """
        from .. import spawn as spawn_mod

        try:
            session = manager.get(name)
        except Exception:  # noqa: BLE001 -- no such session
            return f"no session {name!r} on this daemon"
        definition = {"name": name}
        try:
            definition = session.sdef.to_dict()
        except Exception:  # noqa: BLE001 -- a record old or strange enough to have no dict
            pass
        if getattr(session, "exited", False):
            return f"session {name!r} has exited: an exited session cannot be quick-forked"
        if not spawn_mod.can_fork(definition):
            return (f"session {name!r} has no conversation to copy: a fork needs a Claude or Codex "
                    "session whose first turn has landed")
        try:
            report = manager.spawn_capabilities(name)
        except Exception:  # noqa: BLE001 -- the report is advice; the spawn gate is the authority
            report = {}
        blocked = list(report.get("blocked_by") or [])
        return "; ".join(blocked)

    async def open_forked(request, target: str, body: dict, images, files):
        """Open a thread owned by a quick-fork of ``target``, with ``target``
        as its guest (claunch-uaxp8). ``(res, error_response)``.

        The thread is written FIRST, under the name the fork will carry: the
        owner must exist in the store before the session does, because the
        fork's opening task names the thread. The name is chosen from the
        sessions this daemon knows and handed to the spawn, so a takeover of
        the same name is refused there (409) rather than reused. When the fork
        refuses, the thread written for it goes too -- by id, so a second fork
        that raced for the same name keeps its own.
        """
        from . import api as api_mod, handoff as handoff_mod

        name = handoff_mod.fork_name(target, {s.sdef.name for s in manager.list()})
        res, err = await call(threads.open, thread_store.USER, name, body.get("title"), body.get("text"),
                              images=images, files=files, guest=target)
        if err:
            return None, err
        tid = res["thread"]["id"]
        task = (
            f"You own thread {tid}: it was opened in fork mode, so this session is a copy of "
            f"{target}'s conversation made for it. The user's messages arrive as "
            f"'[claunch thread {tid}]' blocks -- answer them with thread_reply. "
            f"{target} is a guest in this thread."
        )
        try:
            forked = await api_mod.stage_quick_fork(request, target, {"name": name, "task": task})
        except api_mod.QuickForkError as exc:
            await asyncio.to_thread(threads.delete_thread, tid)
            return None, web.json_response({"error": str(exc)}, status=exc.status)
        except Exception:
            # Every refusal stage_quick_fork knows is a QuickForkError above;
            # anything else is the daemon breaking, and the thread it opened
            # must not outlive that. The fork itself is left alone: killing a
            # half-staged copy is not this route's decision to make.
            await asyncio.to_thread(threads.delete_thread, tid)
            raise
        res["fork"] = {"session": name, "origin": target, "thread": tid,
                       "marker": forked.get("marker"), "forked_at": forked.get("forked_at")}
        if forked.get("warnings"):
            res["fork"]["warnings"] = forked["warnings"]
        return res, None

    async def h_open(request):
        body = await body_of(request)
        actor = actor_of(body)
        target = actor if actor != thread_store.USER else str(body.get("to") or "")
        if not target:
            return web.json_response({"error": "to is required: the session the thread is with"}, status=400)
        if not known(target):
            return web.json_response({"error": f"no session {target!r} on this daemon"}, status=404)
        forking = body.get("fork") in (True, "true", "1", 1)
        qid = body.get("qa")
        if forking:
            # A fork hands the thread to a session that does not exist yet.
            # Two things the plain route allows are refused here: an agent
            # opening it (that would move the thread out of its own pane, and
            # a quick fork of itself is deep_think's job), and a QA link (the
            # question belongs to the origin; the thread will belong to the
            # copy). Neither is dropped silently.
            if actor != thread_store.USER:
                return web.json_response(
                    {"error": "fork is the user's: a session opens threads in its own conversation, "
                              "it does not copy itself into one"}, status=400)
            if qid:
                return web.json_response(
                    {"error": f"a thread about QA question {qid} cannot be forked: the question "
                              f"belongs to {target} and the thread would belong to the copy"}, status=400)
            why = await asyncio.to_thread(fork_blocked, target)
            if why:
                return web.json_response({"error": why}, status=400)
        elif qid:
            q = await asyncio.to_thread(question_of, str(qid))
            if q is None:
                return web.json_response({"error": f"no QA question {qid!r}"}, status=404)
            if q["session"] != target:
                return web.json_response(
                    {"error": f"question {qid} belongs to session {q['session']}, not {target}"}, status=400)
        else:
            # No explicit link: a question id in the title (or the first
            # message) is the subject when it names one of the session's
            # questions (claunch-236ei).
            rows = await asyncio.to_thread(qa_rows, target)
            qid = talk_links.subject_in(body.get("title"), body.get("text"), rows)
        images, files, err = await keep_attachments(body)
        if err:
            return err
        if forking:
            res, err = await open_forked(request, target, body, images, files)
        else:
            res, err = await call(threads.open, actor, target, body.get("title"), body.get("text"), qa=qid,
                                  images=images, files=files)
        if err:
            await unkeep_attachments(images, files)
            return err
        # A forked thread's owner is the copy, not the session the request
        # named: the link and the typed block follow the owner.
        owner = (res.get("fork") or {}).get("session") or target
        await asyncio.to_thread(link, [res["thread"]], owner)
        if actor == thread_store.USER and res["message"]:
            mentions = await asyncio.to_thread(mentions_in, res["thread"], res["message"])
            tell(res, owner, user_wrote_text(res["thread"], res["message"], opened=True, mentions=mentions),
                 notice=notice_id(res["thread"], res["message"]), thread=res["thread"]["id"])
        return web.json_response(res)

    async def h_forkable(request):
        """Whether a thread may be started in a fork of this session, and why
        not -- what the pane's checkbox is drawn from (claunch-uaxp8)."""
        session = request.query.get("session") or ""
        if not session:
            return web.json_response({"error": "session is required"}, status=400)
        why = await asyncio.to_thread(fork_blocked, session)
        return web.json_response({"session": session, "ok": not why, "why": why})

    async def h_read(request):
        q = request.query
        res, err = await call(threads.read, request.match_info["id"], after=q.get("after") or 0,
                              limit=q.get("limit") or thread_store.PAGE,
                              reader=q.get("reader") or thread_store.USER)
        if err:
            return err
        await asyncio.to_thread(link, [res["thread"]], res["thread"]["session"])
        return web.json_response(res)

    async def h_ask(request):
        """The agent's ask in one of its threads (thread_ask MCP tool,
        claunch-etw0s): kind 'ask', the user answers in the pane. Nothing is
        typed into the session for its own ask."""
        body = await body_of(request)
        actor = actor_of(body)
        tid = request.match_info["id"]
        err = await asyncio.to_thread(operator_thread, tid)
        if err:
            return err
        if actor == thread_store.USER:
            return web.json_response({"error": "session is required: a session asks, the user answers"},
                                     status=400)
        try:
            t = await asyncio.to_thread(threads.get, tid)
        except thread_store.ThreadError as exc:
            # call() maps store errors, but this read sits outside it; without
            # the mapping an unknown thread's ask dies as a 500 (claunch-etw0s
            # rework, reviewer s1389-rev).
            return web.json_response({"error": str(exc)}, status=exc.status)
        if actor != t["session"]:
            return web.json_response(
                {"error": "a session asks in its own thread; a guest advises and does not ask"}, status=403)
        try:
            text = _ask_text(body.get("text"))
            kind, choices = _ask_kind(body)
        except thread_store.ThreadError as exc:
            return web.json_response({"error": str(exc)}, status=exc.status)
        if await asyncio.to_thread(threads.open_asks, actor) >= thread_store.MAX_OPEN_ASKS:
            return web.json_response(
                {"error": f"{thread_store.MAX_OPEN_ASKS} unanswered asks; wait for the user before asking more"},
                status=400)
        data = {"type": kind, "choices": choices, "answer": None}
        res, err = await call(threads.post, actor, tid, text, kind="ask", data=data)
        if err:
            return err
        await asyncio.to_thread(link, [res["thread"]], res["thread"]["session"])
        return web.json_response(res)

    async def h_answer(request):
        """The user's answer to the agent's ask (the threads pane): recorded
        on the entry and typed into the owning session."""
        body = await body_of(request)
        actor = actor_of(body)
        tid, seq = request.match_info["id"], request.match_info["seq"]
        err = await asyncio.to_thread(operator_thread, tid)
        if err:
            return err
        if actor != thread_store.USER:
            return web.json_response({"error": "only the user answers an ask"}, status=403)
        try:
            seq = int(seq)
        except (TypeError, ValueError):
            return web.json_response({"error": "seq must be an integer"}, status=400)
        msg, err = await call(threads.answer, actor, tid, seq, decision=body.get("decision"),
                              text=body.get("text"))
        if err:
            return err
        res = {"message": msg}
        t = await asyncio.to_thread(threads.get, tid)
        tell(res, t["session"], ask_answered_text(t, msg),
             notice=notice_id(t, msg), thread=tid)
        await asyncio.to_thread(link, [t], t["session"])
        return web.json_response(res)

    async def h_post(request):
        body = await body_of(request)
        actor = actor_of(body)
        err = await asyncio.to_thread(operator_thread, request.match_info["id"])
        if err:
            return err
        images, files, err = await keep_attachments(body)
        if err:
            return err
        res, err = await call(threads.post, actor, request.match_info["id"], body.get("text"), images=images,
                              files=files)
        if err:
            await unkeep_attachments(images, files)
            return err
        t = res["thread"]
        await asyncio.to_thread(link, [t], t["session"])
        if actor == thread_store.USER:
            tell_user_message(res, t, ask_qa=body.get("ask_qa") is True)
        elif res.get("as") == thread_store.GUEST:
            route = await asyncio.to_thread(session_mentions, t, res["message"], actor)
            if t["session"] in route["targets"]:
                tell(res, t["session"], guest_wrote_text(t, res["message"], actor))
            await asyncio.to_thread(mention_delivery, res, t, actor, guest=True, route=route)
        else:
            await asyncio.to_thread(mention_delivery, res, t, actor, guest=False)
        return web.json_response(res)

    def status_route(status: str):
        async def handler(request):
            body = await body_of(request)
            err = await asyncio.to_thread(operator_thread, request.match_info["id"])
            if err:
                return err
            res, err = await call(threads.set_status, actor_of(body), request.match_info["id"], status)
            if err:
                return err
            await asyncio.to_thread(link, [res["thread"]], res["thread"]["session"])
            return web.json_response(res)
        return handler

    async def h_title(request):
        body = await body_of(request)
        err = await asyncio.to_thread(operator_thread, request.match_info["id"])
        if err:
            return err
        res, err = await call(threads.rename, actor_of(body), request.match_info["id"], body.get("title"))
        if err:
            return err
        await asyncio.to_thread(link, [res["thread"]], res["thread"]["session"])
        return web.json_response(res)

    async def h_invite(request):
        body = await body_of(request)
        actor = actor_of(body)
        tid = request.match_info["id"]
        err = await asyncio.to_thread(operator_thread, tid)
        if err:
            return err
        guest = str(body.get("guest") or "").strip()
        if guest and not known(guest):
            return web.json_response({"error": f"no session {guest!r} on this daemon"}, status=404)
        res, err = await call(threads.invite, actor, tid, guest)
        if err:
            return err
        t = res["thread"]
        await asyncio.to_thread(link, [t], t["session"])
        if res["added"]:
            tell(res, guest, guest_invited_text(t, actor))
            if actor == thread_store.USER:
                res["owner_delivered"] = deliver(t["session"], owner_invited_text(t, guest))
        return web.json_response(res)

    async def h_uninvite(request):
        body = await body_of(request)
        actor = actor_of(body)
        tid, guest = request.match_info["id"], request.match_info["guest"]
        err = await asyncio.to_thread(operator_thread, tid)
        if err:
            return err
        res, err = await call(threads.uninvite, actor, tid, guest)
        if err:
            return err
        t = res["thread"]
        await asyncio.to_thread(link, [t], t["session"])
        if actor != guest:
            tell(res, guest, guest_removed_text(t))
        return web.json_response(res)

    async def h_image(request):
        tid, name = request.match_info["id"], request.match_info["name"]
        try:
            path = thread_images.path_of(name)
        except thread_images.ThreadImageError as exc:
            return web.json_response({"error": str(exc)}, status=404)
        if not (await asyncio.to_thread(threads.has_image, tid, name)) or not path.is_file():
            return web.json_response({"error": f"thread {tid} has no image {name}"}, status=404)
        return web.FileResponse(path, headers={
            "Content-Type": thread_images.media_type(name),
            # The name is the file's random id and never names other bytes.
            "Cache-Control": "private, max-age=31536000, immutable",
        })

    async def h_file(request):
        tid, fid, name = request.match_info["id"], request.match_info["fid"], request.match_info["name"]
        if not await asyncio.to_thread(threads.has_file, tid, fid, name):
            return web.json_response({"error": f"thread {tid} has no file {fid}/{name}"}, status=404)
        path = await asyncio.to_thread(uploads.thread_file, fid, name)
        if path is None:
            return web.json_response(
                {"error": f"{name} has expired: thread files are kept {uploads.THREAD_TTL_SECONDS // 86400} days",
                 "expired": True}, status=410)
        return web.FileResponse(path, headers={
            "Content-Type": "application/octet-stream",
            "Content-Disposition": "attachment; filename*=UTF-8''" + quote(name),
        })

    r = app.router
    r.add_get("/api/threads", h_list)
    r.add_get("/api/threads/rail", h_rail)
    r.add_get("/api/threads/history", h_history)
    r.add_get("/api/threads/index", h_index)
    r.add_get("/api/threads/forkable", h_forkable)
    r.add_post("/api/threads", h_open)
    r.add_get("/api/threads/{id}", h_read)
    r.add_post("/api/threads/{id}/messages", h_post)
    r.add_post("/api/threads/{id}/asks", h_ask)
    r.add_post("/api/threads/{id}/asks/{seq}/answer", h_answer)
    r.add_post("/api/threads/{id}/close", status_route("closed"))
    r.add_post("/api/threads/{id}/reopen", status_route("open"))
    r.add_post("/api/threads/{id}/title", h_title)
    r.add_post("/api/threads/{id}/guests", h_invite)
    r.add_post("/api/threads/{id}/guests/{guest}/remove", h_uninvite)
    r.add_get("/api/threads/{id}/images/{name}", h_image)
    r.add_get("/api/threads/{id}/files/{fid}/{name}", h_file)
    return threads
