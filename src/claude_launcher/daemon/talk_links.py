"""Links between a session's threads and its QA questions (claunch-78dpm).

Threads (threads/store.py) and QA (qa/store.py) are separate features in
separate SQLite files. Each side records one fixed link at creation:

  thread.qa        the question a thread was opened to discuss
  question.thread  the thread a question was filed from

This module joins the two for the dashboard and the MCP tools: every thread
row gains ``links`` (the questions it touches) and every question row gains
``links`` (the threads that touch it). Each link names its relation:

  from a thread    rel "subject"    the question this thread is about
                   rel "filed"      a question filed out of this thread
  from a question  rel "discussion" a thread opened about this question
                   rel "source"     the thread this question was filed from

A link whose other end is gone (a deleted question) is kept with status
"deleted" and no title, so the reader sees that something was there.

Links never cross sessions: the routes refuse to create one, so the join
reads only the session's own rows.
"""

from __future__ import annotations

from typing import Optional


def _link(row: Optional[dict], rid: str, rel: str) -> dict:
    if row is None:
        return {"id": rid, "title": None, "status": "deleted", "rel": rel}
    return {"id": row["id"], "title": row["title"], "status": row["status"], "rel": rel}


def thread_links(thread: dict, questions: list[dict]) -> list[dict]:
    """The questions ``thread`` touches, ``questions`` being the link rows
    of its session (QaStore.link_rows)."""
    by_id = {q["id"]: q for q in questions}
    out = []
    if thread.get("qa"):
        out.append(_link(by_id.get(thread["qa"]), thread["qa"], "subject"))
    out += [_link(q, q["id"], "filed") for q in questions
            if q.get("thread") == thread["id"] and q["id"] != thread.get("qa")]
    return out


def question_links(question: dict, threads: list[dict]) -> list[dict]:
    """The threads that touch ``question``, ``threads`` being the link rows
    of its session (ThreadStore.link_rows)."""
    by_id = {t["id"]: t for t in threads}
    out = []
    if question.get("thread"):
        out.append(_link(by_id.get(question["thread"]), question["thread"], "source"))
    out += [_link(t, t["id"], "discussion") for t in threads
            if t.get("qa") == question["id"] and t["id"] != question.get("thread")]
    return out


def attach_to_threads(threads: list[dict], qa_store, session: str) -> None:
    """Add ``links`` to each thread row. Without a QA store every thread
    gets an empty list (the join is an extra, never a failure)."""
    rows = _rows(qa_store, session)
    for t in threads:
        t["links"] = [] if rows is None else thread_links(t, rows)


def attach_to_questions(questions: list[dict], thread_store, session: str) -> None:
    rows = _rows(thread_store, session)
    for q in questions:
        q["links"] = [] if rows is None else question_links(q, rows)


def _rows(store, session: str) -> Optional[list[dict]]:
    """The other side's link rows, or None when they cannot be read -- then
    no link is drawn, rather than every link drawn as deleted."""
    if store is None or not session:
        return None
    try:
        return store.link_rows(session)
    except Exception:  # noqa: BLE001 -- the other store is unreadable
        return None
