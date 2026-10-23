"""Assign to tree: a session is placed in its workspace's category tree by an LLM.

A person presses "Assign to tree" on a session's category pane. When the
session's conversation can be copied (:func:`spawn.can_fork` -- a Claude or
Codex session whose transcript is on disk) the daemon quick-forks it
(:func:`api.stage_quick_fork`, the same copy deep_think makes) and gives the
copy the job: read the tree with the ``category_*`` MCP tools, pick the node
(or propose a new one), call ``category_assign`` once, and hand in one to
three lines with the ``handoff`` tool. That merge is typed into the session
as a short "category assigned" block and ends the copy. The copy knows
everything the session knew, so the session spends none of its own context.

Any other harness gets the same job typed into the session itself
(in-place); it calls ``category_assign`` and carries on with what it was
doing.

The pane's "제안으로 등록" checkbox decides what a *new* category becomes: kept
off (the default), the node is created and the items are placed at once;
checked, the job carries ``proposal`` and the new node is filed for review on
the Category page as before. ``category_assign`` called outside a job (a
workflow step) makes the same choice with its own ``proposal`` argument.

An ended session is not refused (claunch-p2v4m.1): a held one is woken as
opening its terminal would, and any other ended record (killed, paused, ...)
answers 409 ``needs_wake`` until the person confirms with ``wake: true``,
then it is relaunched. Ending it again afterwards is the person's call.

The job's state lives in the workspace's category file
(:func:`category.job_start`), so a merge that arrives after a daemon restart
is still recognised.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from aiohttp import web

from .. import category, spawn as spawn_mod, store
from . import actors
from . import handoff as handoff_mod

log = logging.getLogger("claunch.category")

TAG = "cat"
DEFAULT_DELIVER_TIMEOUT = 120.0


class AssignError(Exception):
    def __init__(self, status: int, message: str, extra: Optional[dict] = None) -> None:
        super().__init__(message)
        self.status = status
        if extra:
            self.extra = extra


def _deliver_timeout() -> float:
    try:
        cfg = store.daemon_config() or {}
        return float(cfg.get("handoff_deliver_timeout") or DEFAULT_DELIVER_TIMEOUT)
    except Exception:
        return DEFAULT_DELIVER_TIMEOUT


def items_for(session) -> list:
    """What "this session" means in the tree: the session and its issue."""
    out = [{"kind": "session", "ref": session.sdef.name}]
    issue = getattr(session.sdef, "issue", None)
    if issue:
        out.append({"kind": "issue", "ref": issue})
    return out


def _describe(items) -> str:
    return ", ".join(f"{i['kind']} {i['ref']}" for i in items)


def compose_task(*, origin: str, workspace: str, items: list, fork: Optional[str],
                 proposal: bool = False) -> str:
    """The job, typed into the fork (``fork`` set) or into the session itself."""
    who = (f"This session ({fork}) is a quick-fork copy of {origin}, made only for "
           f"this job. {origin}'s task, cflow run, board issues and mesh "
           f"conversations stay {origin}'s: do not continue them, do not edit files."
           if fork else
           "The operator pressed 'Assign to tree' on this session. Do this job now, "
           "then carry on with what you were doing before this block arrived.")
    end = ("finish: hand in one to three lines with the MCP 'handoff' tool {text: ...} "
           "and no 'to' -- the category you chose (or proposed) and why. The daemon "
           f"types it into {origin} and ends this session."
           if fork else
           "finish: nothing to hand in; the category_assign call is the whole result.")
    return "\n".join([
        "---",
        "# claunch category: ASSIGN TO TREE (machine-generated, not typed by the user)",
        f"session: {origin}",
        f"workspace: {workspace}",
        f"items: {_describe(items)}",
        f"role: {who}",
        "job: place the items above in the workspace's category tree -- the "
        "keyword index of what sessions and issues are about. It is not the "
        "glossary.",
        "how:",
        "1. Read the tree and its vocabulary with the MCP 'category_tree' tool; "
        "look up candidate keywords with 'category_search'; 'category_items' "
        "shows what already sits in a node.",
        "2. Pick the most specific existing node whose keyword names what this "
        "work is about. Reuse the existing vocabulary: a synonym of an existing "
        "label is that node, not a new one.",
        ("3. When no existing node fits, do not stretch one: propose a new child "
         "of the closest node (or a top-level node when nothing is close) with a "
         "short keyword label, aliases for its other spellings, and the reason. "
         "A person reviews new categories; until then the items wait in the "
         "parent you named." if proposal else
         "3. When no existing node fits, do not stretch one: create a new child "
         "of the closest node (or a top-level node when nothing is close) with a "
         "short keyword label, aliases for its other spellings, and a one-line "
         "description. It is created at once -- a spelling that already names a "
         "node is refused with that node, and you assign to it instead."),
        ("4. Call 'category_assign' exactly once: {node_id} for an existing node, "
         "or {new: {parent_id, label, aliases, description}, reason} for a "
         "proposal -- the person asked for review, so pass proposal: true. "
         "Leave 'items' out -- it defaults to the items above." if proposal else
         "4. Call 'category_assign' exactly once: {node_id} for an existing node, "
         "or {new: {parent_id, label, aliases, description}, reason} to create "
         "the node now. Leave 'items' out -- it defaults to the items above."),
        end,
        "---",
    ])


def compose_result(*, fork: str, job: dict, text: str) -> str:
    result = job.get("result") or {}
    verb = "ASSIGNED" if job.get("state") == category.ASSIGNED else "NOT ASSIGNED"
    lines = [
        "---",
        f"# claunch category: {verb} by {fork} (machine-generated, not typed by the user)",
        f"session: {job.get('session', '?')}",
    ]
    if result.get("path"):
        lines.append("category: " + " / ".join(result["path"]))
    if result.get("created"):
        lines.append(f"created: {result['created']} (a new category, applied now)")
    if result.get("proposed"):
        lines.append(f"proposed: {result['proposed']} (a new category, waiting for review)")
    lines.append("note: this is information about your session; no action is needed.")
    lines += ["---", "", (text or "").strip()]
    return "\n".join(lines).rstrip()


# ---- the job ---------------------------------------------------------------- #

def _workspace(session):
    try:
        return category.workspace(cwd=session.sdef.cwd or "")
    except category.CategoryError as exc:
        raise AssignError(exc.status, f"{session.sdef.name}: {exc}") from None


def _fork_alive(manager, name: Optional[str]) -> bool:
    if not name:
        return False
    try:
        session = manager.get(name)
    except Exception:
        return False
    return not session.exited or bool(getattr(session, "held_at", None))


def refresh(manager, ws, name: str) -> Optional[dict]:
    """The session's job, with a fork that ended without merging, or an
    in-place session that ended before assigning, marked failed."""
    job = category.job(ws, name)
    if not job or job["state"] not in (category.RUNNING, category.ASSIGNED):
        return job
    if job["mode"] == "fork" and not _fork_alive(manager, job["fork"]):
        job = category.job_update(ws, name, category.FAILED,
                                  f"{job['fork']} ended without handing in")
    elif job["mode"] == "inplace" and job["state"] == category.RUNNING \
            and not _fork_alive(manager, name):
        job = category.job_update(ws, name, category.FAILED,
                                  f"{name} ended before assigning")
    return job


async def _awake(manager, session, wake: bool):
    """The live session to assign. A held one is woken, as opening its
    terminal would; any other ended record (killed, paused, failed,
    archived) is relaunched only when the person said so (``wake``) --
    the pane asks first. Ending it again afterwards is the person's call."""
    from .session import session_category

    name = session.sdef.name
    if not session.exited:
        return session
    held = bool(getattr(session, "held_at", None))
    if not held and not wake:
        life = session_category(session)
        raise AssignError(409, f"session {name!r} has ended ({life}); wake it to assign it",
                          extra={"needs_wake": True, "life": life})
    try:
        if held:
            session = await manager.wake_async(name, by="category-assign")
        else:
            session = await manager.respawn_async(name)
    except Exception as exc:
        raise AssignError(503, f"could not wake {name!r}: {exc}") from None
    if session.exited:
        raise AssignError(503, f"session {name!r} did not come back up; try again")
    return session


async def start(request: web.Request, name: str, body: Optional[dict] = None) -> dict:
    from . import api  # local: api imports this module

    manager = request.app["manager"]
    try:
        session = manager.get(name)
    except Exception:
        raise AssignError(404, f"no session named {name!r}") from None
    ws = _workspace(session)
    if category.job_for_fork(ws, name):
        raise AssignError(400, f"{name!r} is itself a category fork")
    session = await _awake(manager, session, bool((body or {}).get("wake")))
    refresh(manager, ws, name)
    items = items_for(session)
    # The pane's "제안으로 등록": unchecked (the default) creates a new category
    # at once, checked files it for review as before.
    proposal = bool((body or {}).get("proposal"))
    if spawn_mod.can_fork(session.sdef.to_dict()):
        taken = {s.sdef.name for s in manager.list()}
        fork = handoff_mod.fork_name(name, taken, tag=TAG)
        try:
            category.job_start(ws, name, "fork", fork, proposal=proposal)
        except category.CategoryError as exc:
            raise AssignError(exc.status, str(exc)) from None
        try:
            await api.stage_quick_fork(
                request, name,
                {"name": fork, "task": compose_task(
                    origin=name, workspace=ws.name, items=items, fork=fork,
                    proposal=proposal)},
                tag=TAG, self_merge_allowed=True,
                created_by=actors.with_reason(actors.from_request(request), "category-assign"),
            )
        except api.QuickForkError as exc:
            category.job_update(ws, name, category.FAILED, str(exc))
            raise AssignError(exc.status, str(exc)) from None
        return {"mode": "fork", "fork": fork, "workspace": ws.name, "items": items,
                "proposal": proposal, "job": category.job(ws, name)}
    try:
        # A request the session never acted on does not block the next press:
        # the new request replaces it and the block is typed again.
        category.job_start(ws, name, "inplace", replace_inplace=True,
                           proposal=proposal)
    except category.CategoryError as exc:
        raise AssignError(exc.status, str(exc)) from None
    block = compose_task(origin=name, workspace=ws.name, items=items, fork=None,
                         proposal=proposal)
    try:
        delivered = bool(await asyncio.wait_for(session.deliver(block), _deliver_timeout()))
    except asyncio.TimeoutError:
        delivered = False
    if not delivered:
        category.job_update(ws, name, category.FAILED, "the session did not take the request")
        raise AssignError(503, f"{name!r} did not take the request (its keyboard is busy); try again")
    return {"mode": "inplace", "fork": None, "workspace": ws.name, "items": items,
            "proposal": proposal, "job": category.job(ws, name)}


def agent_assign(manager, caller: str, body: dict) -> dict:
    """The ``category_assign`` MCP call. ``caller`` is the calling session: a
    category fork assigns its origin's items, anyone else its own."""
    try:
        session = manager.get(caller)
    except Exception:
        raise AssignError(404, f"no session named {caller!r}") from None
    ws = _workspace(session)
    job = category.job_for_fork(ws, caller)
    origin = session
    if job:
        try:
            origin = manager.get(job["session"])
        except Exception:
            raise AssignError(400, f"the session {job['session']!r} is gone") from None
    else:
        own = category.job(ws, caller)
        job = own if own and own["state"] == category.RUNNING and own["mode"] == "inplace" else None
    items = body.get("items") or items_for(origin)
    reason = str(body.get("reason") or "")
    actor = f"agent {caller}"
    try:
        if body.get("node_id"):
            placed = category.assign(ws, items, body["node_id"], actor, reason)
            result = {"path": placed["node"]["path"], "node_id": placed["node"]["id"]}
        elif isinstance(body.get("new"), dict):
            new = dict(body["new"])
            # The job -- opened by "Assign to tree" -- records what the person
            # asked for with its checkbox, and that decides. A direct call (a
            # workflow step, a fork) has no such record unless the call itself
            # carries `proposal`; without either, the node is created now.
            wants_proposal = (bool(job.get("proposal")) if job
                              else bool(body.get("proposal")))
            if wants_proposal:
                proposal = category.propose(
                    ws, {**new, "reason": new.get("reason") or reason, "items": items}, actor)
                parent_path = []
                if proposal.get("parent_id"):
                    where = category.where(ws, "session", origin.sdef.name)
                    parent_path = (where.get("node") or {}).get("path") or []
                result = {"proposed": proposal["label"], "proposal_id": proposal["id"],
                          "path": parent_path,
                          "similar": [n["path"] for n in proposal["similar"]]}
            else:
                node = category.create_now(
                    ws, {**new, "reason": new.get("reason") or reason}, items, actor, reason)
                result = {"created": node["label"], "node_id": node["id"],
                          "path": node["path"]}
        else:
            raise AssignError(400, "give node_id (an existing category) or new {label, ...}")
    except category.CategoryError as exc:
        err = AssignError(exc.status, str(exc))
        err.extra = exc.extra  # type: ignore[attr-defined]
        raise err from None
    if job:
        state = category.ASSIGNED if job["mode"] == "fork" else category.DONE
        category.job_update(ws, job["session"], state, "", result)
    out = {"workspace": ws.name, "items": items, **result}
    if job and job["mode"] == "fork":
        out["next"] = "hand in one to three lines with the 'handoff' tool {text: ...} now"
    return out


# ---- the merge, seen from here ------------------------------------------------ #

def _fork_job(manager, source):
    try:
        session = manager.get(source)
        ws = category.workspace(cwd=session.sdef.cwd or "")
        return ws, category.job_for_fork(ws, source)
    except Exception:
        return None, None


def _composer(manager, source, settled, text, marker):
    if settled.get("kind") != handoff_mod.MERGE:
        return None
    _, job = _fork_job(manager, source)
    if not job:
        return None
    return compose_result(fork=source, job=job, text=text)


def _completed(manager, source, settled, text) -> None:
    if settled.get("kind") != handoff_mod.MERGE:
        return
    ws, job = _fork_job(manager, source)
    if not job:
        return
    if job["state"] == category.ASSIGNED:
        category.job_update(ws, job["session"], category.DONE, (text or "").strip()[:500])
    else:
        category.job_update(ws, job["session"], category.FAILED,
                            "handed in without calling category_assign")


def install_hooks(app) -> None:
    """How a category fork's merge is written and settled. A bare app (tests
    of the routes alone) has no handoff registry and gets no hooks."""
    handoffs = app.get("handoff")
    if handoffs is None:
        return
    handoffs.composers.append(_composer)
    handoffs.completed_hooks.append(_completed)
