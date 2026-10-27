"""Routing for mega threads: which ``(session, thread)`` a user message is for
(claunch-v5nhp).

The user writes once and the daemon decides who hears it. For each member the
daemon builds a state document -- the session's record, its cached briefing
(or the tail of its transcript when no briefing was composed), and the tail of
the member thread -- and asks Jev one Choice over ``mine`` and ``not_mine``.
The answer carries the whole distribution, so the decision is a number rather
than a string to parse::

    logit = ln p(mine) - ln p(not_mine)

Members whose logit clears ``megathread_threshold`` (log-odds; ``ln 9`` by
default, which is 90% against 10%) are candidates. One candidate routes the
message there; two or more are handed to the user to choose among, possibly
several; none offers the best-ranked members so the user can still pick one.
The members the user did not pick receive nothing -- no row, no notice -- so a
thread that was not chosen cannot refer to the conversation later.

Each member costs one Jev call. The calls run concurrently (bounded by
``megathread_concurrency``) and their ``usage.input_tokens`` are added to the
routing log, which is also the accounting behind
``megathread_jev_budget_tokens``: a routing whose projected cost would cross
the rolling 24-hour ceiling is refused before any call is made
(:class:`BudgetExceeded`). The prompt never calls an LLM of its own; the
briefing it reads is the one the dashboard already asked for, because a
summariser per member per message would multiply the cost of a cheap
decision.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import briefing, cflow2_web, jev

log = logging.getLogger(__name__)

QUESTION_ID = "route"
MINE = "mine"
NOT_MINE = "not_mine"

INSTRUCTIONS = (
    "The user writes into a mega thread: one input box that reaches several "
    "AI sessions, each with its own conversation thread. Decide whether this "
    "message is meant for THIS thread."
)
CRITERIA = {
    MINE: (
        "The message continues this thread's subject, answers or asks about "
        "the work described in it, or plainly belongs to this session's task."
    ),
    NOT_MINE: (
        "The message is about another session's work or a different task, or "
        "is too general to be this thread's."
    ),
}

#: The create form's Jev preview asks the same two options about a topic
#: instead of a message. The option ids stay ``mine``/``not_mine`` so one
#: scorer (``scores``) and one judge (``judge``) read both answers; only the
#: sentence the model is asked changes.
#:
#: The criteria count work the session has already done (claunch-wlk6g). A
#: mega thread gathers the places a subject lives, and a session waiting on
#: review or wrapping up is one of them; the first wording sent "already
#: finished here" to ``not_mine``, and on four topics over forty live sessions
#: the sessions that matched scored a logit of 1.5-2.4, under the ln 9
#: threshold, so on the widest topic the preview recommended one or two of
#: fifteen. With this wording eight or nine of the fifteen cleared it and no
#: other session did (measurement on the issue).
DRAFT_INSTRUCTIONS = (
    "The user is creating a mega thread: one input box that will reach several "
    "AI sessions, each with its own conversation thread. The user typed the "
    "topic below. Decide whether THIS session belongs in that mega thread."
)
DRAFT_CRITERIA = {
    MINE: (
        "The topic is this session's subject, or the work this session is "
        "doing or has done (its task, issues, branch, briefing) is about it."
    ),
    NOT_MINE: (
        "The topic is about other work than this session's, or too general "
        "to say this session is one of its places."
    ),
}
#: The key the state document gives the text being judged. A routing judges
#: a message; the preview judges a topic, and its instructions say "the topic
#: below", so the document names it that way.
MESSAGE_KEY = "message"
TOPIC_KEY = "topic"

#: What one member's prompt may carry. The Jev default is 32k tokens for the
#: state plus the question and 64k for the whole request; these caps keep a
#: ten-member routing in the low thousands of tokens.
SESSION_CHARS = 2000
BRIEFING_CHARS = 2000
MESSAGE_CHARS = 1000
THREAD_MESSAGES = 12
#: What the create form's preview adds to a session's record (claunch-wlk6g).
#: A topic is scored before any thread exists, so the thread tail that tells a
#: routing what a session is about is usually empty there, and the record has
#: to say it instead: which workspace the session works in, which branch it
#: stands on, and the titles of the issues it holds -- an issue id alone says
#: nothing to a model that has never read the board.
DRAFT_ISSUES = 4
ISSUE_TITLE_CHARS = 200
#: The cached briefing fields each prompt reads, in order. The preview also
#: reads ``progress``: it is the sentence that says what the session has
#: already done, which is how a topic about finished work tells two sessions
#: on the same subject apart. Routing keeps the shorter set it was measured
#: with (claunch-v5nhp).
BRIEFING_KEYS = ("one-line-job-description", "goal", "now", "state")
DRAFT_BRIEFING_KEYS = ("one-line-job-description", "goal", "now", "progress", "state")
#: How many members the "nobody cleared the threshold" answer offers.
TOP_N = 5
#: Characters per token, deliberately pessimistic: Korean text runs near one
#: character per token where English runs four, and a budget that guesses
#: high only refuses a little early.
CHARS_PER_TOKEN = 3

#: A probability of exactly zero would make the log-odds infinite.
EPS = 1e-6


class BudgetExceeded(Exception):
    """The rolling Jev budget for mega threads would be crossed."""

    status = 429

    def __init__(self, used: int, limit: int, estimate: int):
        self.used, self.limit, self.estimate = used, limit, estimate
        super().__init__(
            f"mega thread Jev budget: {used:,} of {limit:,} tokens used in the "
            f"last 24h; this routing needs about {estimate:,}"
        )


# --------------------------------------------------------------------------- #
# prompt pieces
# --------------------------------------------------------------------------- #
def clip(text: str, limit: int) -> str:
    """``text`` cut to ``limit`` characters, head and tail kept.

    The tail matters: a thread's last lines say what it is about now, and a
    head-only cut would answer with the opening question every time.
    """
    text = str(text or "").strip()
    if limit <= 0 or len(text) <= limit:
        return text
    head = max(1, limit // 3)
    tail = max(1, limit - head - 32)
    marker = f"\n[... {len(text) - head - tail} characters omitted ...]\n"
    return text[:head] + marker + text[-tail:]


def session_state(manager, name: str) -> Dict[str, Any]:
    """The session's record as the router shows it. A session that left the
    daemon keeps a name and a ``gone`` status rather than raising: it can be
    in a cluster, and its thread is still a real place to write."""
    try:
        session = manager.get(name)
    except Exception:  # noqa: BLE001 -- no such session any more
        return {"name": str(name), "status": "gone"}
    sdef = session.sdef
    info: Dict[str, Any] = {}
    try:
        info = session.info() or {}
    except Exception:  # noqa: BLE001 -- an exited session with no record
        pass
    state: Dict[str, Any] = {
        "name": sdef.name,
        "status": info.get("status") or "unknown",
        "cwd": sdef.cwd or "",
        "model": getattr(sdef, "model", "") or "",
        "project": getattr(sdef, "project", "") or "",
        "kind": getattr(sdef, "kind", "") or "",
        "role": getattr(sdef, "role", "") or "",
        "issue": getattr(sdef, "issue", "") or "",
        "task": clip(getattr(sdef, "task", "") or "", SESSION_CHARS),
    }
    try:
        run = cflow2_web.run_summary(sdef.cwd or "", sdef.name)
    except Exception:  # noqa: BLE001 -- a run that cannot be read has no line
        run = None
    if run:
        state["run"] = {
            key: run.get(key)
            for key in ("workflow", "status", "current", "reason")
            if run.get(key) not in (None, "", [])
        }
    return {key: value for key, value in state.items() if value not in ("", None)}


def briefing_text(name: str, sdef, keys: Sequence[str] = BRIEFING_KEYS) -> str:
    """What the session is doing, from the cache the dashboard filled.

    No composition here: ``briefing.compose`` would call an LLM per member per
    message. When nothing is cached, the transcript tail is cheap and says
    the same thing in the session's own words.
    """
    try:
        hit = briefing.cached_result(name)
    except Exception:  # noqa: BLE001 -- an unreadable cache is no briefing
        hit = None
    brief = (hit or {}).get("briefing")
    if isinstance(brief, dict):
        parts = [
            str(brief.get(key) or "").strip()
            for key in keys
        ]
        text = " / ".join(p for p in parts if p)
        if text:
            return clip(text, BRIEFING_CHARS)
    try:
        path = briefing.locate_transcript(sdef, deep=False)
    except Exception:  # noqa: BLE001 -- no transcript is no evidence
        path = None
    if path is None:
        return ""
    try:
        return clip("\n".join(briefing.tail_events(path, max_events=6)), BRIEFING_CHARS)
    except Exception:  # noqa: BLE001
        return ""


def git_branch(cwd: str) -> str:
    """The branch checked out in ``cwd``, read from git's own files.

    No ``git`` process: a preview scores up to forty sessions and a fork per
    session is the slow part of that. A linked worktree's ``.git`` is a file
    naming its gitdir, which holds that worktree's ``HEAD``. A detached head,
    a directory outside any checkout or an unreadable file is no branch.
    """
    if not cwd:
        return ""
    try:
        here = Path(cwd).resolve()
    except (OSError, RuntimeError):
        return ""
    for top in (here, *here.parents):
        dotgit = top / ".git"
        try:
            if dotgit.is_file():
                line = dotgit.read_text(encoding="utf-8").strip()
                if not line.startswith("gitdir:"):
                    return ""
                gitdir = Path(line[len("gitdir:"):].strip())
                if not gitdir.is_absolute():
                    gitdir = top / gitdir
            elif dotgit.is_dir():
                gitdir = dotgit
            else:
                continue
            head = (gitdir / "HEAD").read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            return ""
        prefix = "ref: refs/heads/"
        return head[len(prefix):] if head.startswith(prefix) else ""
    return ""


def draft_facts(name: str, sdef, *, workspace: str = "",
                issues: Sequence[dict] = ()) -> Dict[str, Any]:
    """What the preview adds to one session's record, beside
    :func:`session_state`.

    ``issues`` are the board rows that belong to the session
    (``beads.match`` order: the linked issue first, then the active ones);
    only their id, status and title travel. ``workspace`` is the registered
    workspace the session's directory belongs to -- a worktree path names
    its checkout, not the project, and two sessions in the same project can
    stand in directories that share nothing but that name.
    """
    facts: Dict[str, Any] = {}
    if workspace:
        facts["workspace"] = workspace
    branch = git_branch(getattr(sdef, "cwd", "") or "")
    if branch:
        facts["branch"] = branch
    parent = getattr(sdef, "parent", "") or ""
    if parent:
        facts["parent"] = str(parent)
    rows = []
    for row in issues:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        rows.append({
            key: value for key, value in (
                ("id", str(row.get("id"))),
                ("status", str(row.get("status") or "")),
                ("title", clip(row.get("title") or "", ISSUE_TITLE_CHARS)),
            ) if value
        })
        if len(rows) >= DRAFT_ISSUES:
            break
    if rows:
        facts["issues"] = rows
    return facts


def thread_state(threads, tid: str) -> Dict[str, Any]:
    """The tail of one member thread: title, status and the last messages.

    A draft scores a session that may have no thread yet (the create form
    previews before any cluster exists), and an empty id is that session: the
    document says so rather than raising on a lookup of ``""``.
    """
    if not tid:
        return {"title": "", "status": "open", "messages": []}
    thread = threads.get(tid)
    try:
        messages = threads.tail(tid, limit=THREAD_MESSAGES)
    except Exception:  # noqa: BLE001 -- a thread with no messages is still one
        messages = []
    return {
        "title": thread.get("title") or "",
        "status": thread.get("status") or "open",
        "messages": [
            {"author": m.get("author") or "", "text": clip(m.get("body") or "", MESSAGE_CHARS)}
            for m in messages
            if (m.get("body") or "").strip()
        ],
    }


def questions() -> Dict[str, dict]:
    return {QUESTION_ID: {"type": "choice", "instructions": INSTRUCTIONS, "criteria": dict(CRITERIA)}}


def draft_questions() -> Dict[str, dict]:
    return {QUESTION_ID: {"type": "choice", "instructions": DRAFT_INSTRUCTIONS,
                          "criteria": dict(DRAFT_CRITERIA)}}


def build_state(manager, threads, member: Dict[str, Any], text: str, *,
                max_chars: int = 12000, facts: Optional[Dict[str, Any]] = None,
                briefing_keys: Sequence[str] = BRIEFING_KEYS,
                text_key: str = MESSAGE_KEY) -> Dict[str, Any]:
    """One member's state document: the message being routed, who the session
    is, what it is doing, and what this thread has been about.

    ``facts`` are fields added to the session's record (the preview's
    :func:`draft_facts`); the record's own fields win a name they share.
    """
    name = member["session"]
    try:
        sdef = manager.get(name).sdef
    except Exception:  # noqa: BLE001
        sdef = None
    record = session_state(manager, name)
    if facts:
        record = {**facts, **record}
    state = {
        text_key: clip(text, MESSAGE_CHARS),
        "session": record,
        "briefing": briefing_text(name, sdef, briefing_keys) if sdef is not None else "",
        "thread": thread_state(threads, member["thread"]),
    }
    # One cap over the whole document: the sections are individually bounded,
    # so this only bites when all three are full at once.
    blob = json.dumps(state, ensure_ascii=False)
    if len(blob) > max_chars:
        state["thread"]["messages"] = state["thread"]["messages"][-(len(state["thread"]["messages"]) // 2 or 1):]
        state["briefing"] = clip(state["briefing"], max(200, max_chars // 6))
    return state


def estimate_tokens(state: Dict[str, Any], qs: Dict[str, dict]) -> int:
    blob = json.dumps(state, ensure_ascii=False) + json.dumps(qs, ensure_ascii=False)
    return max(1, len(blob) // CHARS_PER_TOKEN)


# --------------------------------------------------------------------------- #
# the judgment
# --------------------------------------------------------------------------- #
def scores(answer: Dict[str, Any]) -> Dict[str, Any]:
    """``p(mine)``, ``p(not_mine)``, log-odds and confidence from a Choice.

    Jev returns ``probabilities`` for every option. A response without both
    options (an older shape, or a model that answered oddly) falls back to
    ``choice`` and ``confidence``, so a routing decision still exists.
    """
    probs = answer.get("probabilities") or {}
    try:
        p_mine = float(probs.get(MINE))
        p_not = float(probs.get(NOT_MINE))
    except (TypeError, ValueError):
        p_mine = p_not = None
    choice = str(answer.get("choice") or "")
    try:
        confidence = float(answer.get("confidence"))
    except (TypeError, ValueError):
        confidence = 0.0
    if p_mine is None or p_not is None:
        if choice == MINE:
            p_mine, p_not = confidence, max(0.0, 1.0 - confidence)
        elif choice == NOT_MINE:
            p_mine, p_not = max(0.0, 1.0 - confidence), confidence
        else:
            p_mine, p_not = 0.5, 0.5
    p_mine = min(1.0 - EPS, max(EPS, p_mine))
    p_not = min(1.0 - EPS, max(EPS, p_not))
    return {
        "choice": choice or (MINE if p_mine >= p_not else NOT_MINE),
        "confidence": confidence,
        "p_mine": p_mine,
        "p_not_mine": p_not,
        "logit": math.log(p_mine) - math.log(p_not),
    }


def _failure(row: Dict[str, Any]) -> Dict[str, Any]:
    """One failed member, named the way a caller can put it back.

    The ``thread`` alone does not identify a member of the create form's
    preview: a session with no open thread is scored with an empty id, so
    every unthreaded session writes ``thread: ""`` and whoever reads these
    rows back attributes every failure to the first such member. ``session``
    is the identity that always exists; ``thread`` stays for the routing
    ledger, which is keyed by it.
    """
    return {
        "session": row.get("session") or "",
        "thread": row.get("thread") or "",
        "error": row.get("error"),
    }


def judge(results: List[Dict[str, Any]], threshold: float, *, top_n: int = TOP_N) -> Dict[str, Any]:
    """The verdict over every member's score.

    ``auto``  exactly one member cleared the threshold and every call answered
              -- that member.
    ``choose`` one cleared it while some call failed, or two or more did -- the
              user picks among the offered members (several may be picked at
              once). A failed call keeps the answer away from ``auto``: the
              member that could not be scored may have been the intended one,
              and delivering behind the user's back would hide that.
    ``none``  nobody cleared it -- the best ``top_n`` are offered, so the user
              still has a way to route without typing into a terminal.
    ``error`` every call failed -- nothing can be routed or offered.
    """
    scored = [r for r in results if not r.get("error") and r.get("logit") is not None]
    failed = [r for r in results if r.get("error")]
    if not scored:
        return {"verdict": "error", "threshold": threshold, "candidates": [], "options": [],
                "errors": [_failure(r) for r in failed]}
    ranked = sorted(scored, key=lambda r: r["logit"], reverse=True)
    hits = [r for r in ranked if r["logit"] >= threshold]
    if len(hits) == 1 and not failed:
        verdict, options = "auto", hits
    elif hits:
        verdict, options = "choose", hits
    else:
        verdict, options = "none", ranked[:top_n]
    return {
        "verdict": verdict,
        "threshold": threshold,
        "candidates": ranked,
        "options": options,
        "errors": [_failure(r) for r in failed],
    }


def ratio_of(cfg: Dict[str, Any]) -> float:
    """The odds ratio an operator sets: how many times likelier ``mine`` than
    ``not_mine`` a member must be to take a message alone."""
    try:
        ratio = float(cfg.get("megathread_threshold_ratio"))
    except (TypeError, ValueError):
        ratio = 9.0
    # A config file can hold NaN (written by an older build, or by hand); the
    # default is a working router, and ln(NaN) would make every comparison
    # false instead of raising anywhere an operator would see it.
    if not math.isfinite(ratio):
        ratio = 9.0
    return max(1.000001, ratio)


def threshold_of(cfg: Dict[str, Any]) -> float:
    """The logit threshold: the natural log of :func:`ratio_of` (``ln 9``)."""
    return math.log(ratio_of(cfg))


def policy(cfg: Dict[str, Any]) -> Dict[str, Any]:
    def num(key: str, default: float) -> float:
        try:
            value = float(cfg.get(key))
        except (TypeError, ValueError):
            return default
        return value if math.isfinite(value) else default

    return {
        "threshold": threshold_of(cfg),
        "threshold_ratio": ratio_of(cfg),
        "max_chars": int(num("megathread_max_chars", 12000)),
        "concurrency": max(1, int(num("megathread_concurrency", 8))),
        "budget_tokens": max(0, int(num("megathread_jev_budget_tokens", 10_000_000))),
        # The create form's preview has its own rolling ceiling: a topic the
        # user types and abandons spends real tokens, and charging them to the
        # routing budget would let a few previews stop real messages.
        "draft_budget_tokens": max(0, int(num("megathread_draft_budget_tokens", 2_000_000))),
        "model": jev.model_for(cfg, "megathread"),
    }


# --------------------------------------------------------------------------- #
# one routing
# --------------------------------------------------------------------------- #
async def default_ask(cfg: Dict[str, Any], state: Dict[str, Any], qs: Dict[str, dict],
                      model: str) -> Dict[str, Any]:
    return await jev.system_one(cfg, state, qs, model=model)


async def evaluate(
    manager, threads, members: List[Dict[str, Any]], text: str, *,
    cfg: Dict[str, Any], ask: Optional[Any] = None,
    qs_factory: Optional[Any] = None,
    facts: Optional[Dict[str, Dict[str, Any]]] = None,
    briefing_keys: Sequence[str] = BRIEFING_KEYS,
    text_key: str = MESSAGE_KEY,
) -> Dict[str, Any]:
    """Ask Jev about every member concurrently and judge the answers.

    ``ask`` is the Jev call, injected by tests; it takes
    ``(cfg, state, questions, model)`` and returns a response document.
    ``qs_factory`` is the question set to ask (``questions`` here,
    ``draft_questions`` for the create form's preview): the machinery below
    -- one document per member, one call, one judgment -- is the same for a
    message and for a topic. ``facts`` (session name -> fields),
    ``briefing_keys`` and ``text_key`` are the preview's record; a routing
    passes none of them.
    """
    pol = policy(cfg)
    call = ask or default_ask
    model = pol["model"]
    semaphore = asyncio.Semaphore(pol["concurrency"])
    build_questions = qs_factory or questions

    async def one(member: Dict[str, Any]) -> Dict[str, Any]:
        """One member's answer. Every failure is that member's own: a thread
        that was deleted under the cluster, a call that failed, a response
        without usage. Each row carries ``tokens`` -- the estimate when the
        call had no usage or never answered -- so the routing's token sum is
        the whole spend even when part of it failed, and the budget is not
        charged for calls the log cannot see.
        """
        qs = build_questions()
        try:
            state = build_state(manager, threads, member, text, max_chars=pol["max_chars"],
                                facts=(facts or {}).get(member.get("session") or ""),
                                briefing_keys=briefing_keys, text_key=text_key)
            estimate = estimate_tokens(state, qs)
        except Exception as exc:  # noqa: BLE001 -- no state, no call, no spend
            log.info("megathread: member %s has no state: %s", member.get("thread"), exc)
            return {**member, "error": f"{type(exc).__name__}: {exc}", "tokens": 0}
        async with semaphore:
            try:
                response = await call(cfg, state, qs, model)
            except jev.JevError as exc:
                return {**member, "error": f"jev: {exc}", "tokens": estimate}
            except Exception as exc:  # noqa: BLE001 -- one member must not sink the rest
                log.info("megathread: member %s failed: %s", member.get("thread"), exc)
                return {**member, "error": f"{type(exc).__name__}", "tokens": estimate}
        answer = (response.get("answers") or {}).get(QUESTION_ID) or {}
        usage = response.get("usage") or {}
        try:
            tokens = int(usage.get("input_tokens") or 0)
        except (TypeError, ValueError):
            tokens = 0
        return {
            **member,
            **scores(answer),
            "response_model": response.get("model") or model,
            "tokens": tokens or estimate,
        }

    results = await asyncio.gather(*(one(m) for m in members))
    judgment = judge(results, pol["threshold"])
    judgment["tokens"] = sum(int(r.get("tokens") or 0) for r in results)
    judgment["model"] = model
    return judgment


# --------------------------------------------------------------------------- #
# the create form's preview
# --------------------------------------------------------------------------- #
def draft_members(threads, sessions: List[str]) -> List[Dict[str, Any]]:
    """``(session, thread)`` pairs to score for a topic, without writing.

    A session joins a mega thread with its most recent open thread, or with a
    thread the daemon opens for it (``megathread_api.open_thread``). The
    preview must not open anything -- a topic the user abandons would leave
    threads behind -- so a session with no open thread is scored with an
    empty id, and ``thread_state`` reads that as "no thread yet".
    """
    out = []
    for name in sessions:
        name = str(name or "").strip()
        if not name:
            continue
        tid = ""
        try:
            listing = threads.list(name, closed=False)
            if listing["threads"]:
                tid = str(listing["threads"][0]["id"])
        except Exception:  # noqa: BLE001 -- a session with no readable threads
            tid = ""
        out.append({"session": name, "thread": tid})
    return out


async def draft(
    manager, threads, sessions: List[str], topic: str, *,
    cfg: Dict[str, Any], ask: Optional[Any] = None,
    facts: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Which of ``sessions`` the topic belongs to.

    Same judgment as a routing, asked before a cluster exists: every
    candidate is scored, and the ones over the threshold come back marked
    ``recommended``. The caller shows the list, the user edits it, and only
    then is a cluster created -- so nothing here writes a thread, a member or
    a delivery.

    ``facts`` is :func:`draft_facts` per session, gathered by the caller
    (it reads the board, which this module does not hold). Every member's
    ``logit`` comes back, recommended or not: the form moves its threshold
    over these scores without asking again (claunch-wlk6g).
    """
    members = draft_members(threads, sessions)
    judgment = await evaluate(
        manager, threads, members, topic, cfg=cfg, ask=ask, qs_factory=draft_questions,
        facts=facts, briefing_keys=DRAFT_BRIEFING_KEYS, text_key=TOPIC_KEY,
    )
    threshold = float(judgment.get("threshold") or 0.0)
    rows = []
    for row in judgment.get("candidates") or []:
        rows.append({
            "session": row.get("session"),
            "thread": row.get("thread") or "",
            "p_mine": row.get("p_mine"),
            "logit": row.get("logit"),
            # Only a scored member can be recommended: a failed call says
            # nothing about the topic, and ticking it on the user's behalf
            # would put an unasked session in the cluster.
            "recommended": row.get("logit") is not None and float(row["logit"]) >= threshold,
        })
    for row in judgment.get("errors") or []:
        rows.append({
            # The session travels on the error row now: looking it up by
            # ``thread`` matched the first unthreaded session for every
            # unthreaded failure (claunch review on d705569d).
            "session": row.get("session") or "",
            "thread": row.get("thread") or "",
            "p_mine": None,
            "logit": None,
            "recommended": False,
            "error": row.get("error"),
        })
    rows.sort(key=lambda r: (r["logit"] is None, -(r["logit"] or 0.0)))
    return {
        "threshold": threshold,
        "model": judgment.get("model"),
        "tokens": judgment.get("tokens") or 0,
        "members": rows,
        "recommended": [r["session"] for r in rows if r["recommended"]],
        # How many sessions were scored. Not `candidates`: in a routing that
        # word is the scored rows themselves, and one name for two shapes is
        # how a reader ends up iterating a number.
        "scored": len(members),
    }
