"""Onboarding: everything a new session needs before its first turn.

A session is rarely wanted on its own. It is wanted *in* a mesh, driving a
particular run, with an opening instruction — and until all three have landed
it is a terminal nobody is listening to, or an agent that does not know why it
exists. The agent-facing spawn endpoint has composed those steps into one call
from the beginning; this module is that composition lifted out so the human
paths (``POST /api/sessions``, ``claunch new-session``, the web form) do the
same thing, in the same order, with the same failure reporting.

Two ideas hold it together.

**Preflight before create.** Everything that can be checked without a session
is checked first: does the mesh exist, is the handle free, is the workflow
declared in this directory. That has to happen up front anyway — the system
prompt is fixed when the PTY starts, so anything going into it must be known
before the session is built — and it turns "the session came up, then the join
failed" into a 400 with nothing created.

**One opening block, not three.** The mesh briefing, the workflow assignment
and the opening task used to be three independently idle-gated pastes racing
each other into the same terminal; the race was real enough to need a settle
constant tuned against the paste-Enter delay. Composed here, they are one
message: ordered, atomic, and impossible to interleave.

**Arranged before the session runs.** :func:`arrange` does every leg — the
join, the run — while the session is registered but not yet started, and
returns the composed block. That is what lets the block be handed to ``claude``
as its positional prompt: a message on the command line is read before the
process reads a key, so it cannot be lost in the ten-odd seconds a TUI spends
between going quiet and being able to accept a submit (the failure
:meth:`Session._await_readable` describes and only partly mitigates).
Harnesses that take no such argument are typed into instead.

**A child is never alone.** A spawn that named no mesh used to produce exactly
that: a worker with no way to report back and a parent with no way to ask.
Saying nothing now means *the parent's mesh* (:func:`inherit_mesh`), and a
parent that is in none gets one opened for the pair — so "these two can talk"
is a property of being parent and child, not of having remembered a flag. The
child is then told whose it is, on both channels, because a session that does
not know who is waiting on it reports to nobody.

The split between the two channels follows what is *true for how long*:

* **System prompt** (:attr:`SessionDef.identity`, claude harness only) takes
  the unchanging half — the handle this session answers to, the run it drives.
  It survives compaction and is re-injected on every restore.
* **The opening block** takes everything derived: who is reachable right now,
  which step the run is on. The member graph is rewired mid-session by
  ``connect``/``disconnect``, so freezing a roster into a system prompt would
  have the agent addressing peers it cannot reach.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Tuple

from ..cflow import engine as cflow_engine
from ..cflow import state as cflow_state
from . import harness as harness_mod
from .harness import CLAUDE_HARNESS
from .mesh import MeshError

log = logging.getLogger(__name__)

#: The spelling that declines the inherited mesh — a child deliberately off
#: the air. A token rather than an omitted field, because omission is what now
#: means *inherit*: there has to be a way to say "no mesh" that is louder than
#: saying nothing.
NO_MESH = "-"


class OnboardError(Exception):
    """A request that cannot be honoured, found before anything was created.

    Mapped to 400. Distinct from the per-leg failures reported in
    :func:`run`'s result: those happened to a session that exists.
    """


@dataclass(frozen=True)
class Plan:
    """A validated onboarding request, ready to be applied to a new session."""

    mesh: str = ""
    handle: str = ""
    role: str = ""
    connect: Tuple[str, ...] = ()
    workflow: str = ""
    context: str = ""
    task: str = ""
    #: The session that asked for this one, by name — "" for a session a human
    #: created. Carried on the plan rather than passed alongside it because
    #: every leg wants it: the join keeps the child connected to it, the
    #: opening block names it, and the system prompt says who is waiting.
    parent: str = ""
    #: How the parent answers in :attr:`mesh` — usually its session name, but
    #: it may have joined under another handle, and a child told the wrong one
    #: addresses a member that is not there.
    parent_handle: str = ""
    #: The system-prompt block, or "" when there is nothing certain to say
    #: (no mesh and no workflow, a harness with no prompt to append to, or a
    #: remote join whose handle is not settled until the grant arrives).
    identity: str = ""

    @property
    def wanted(self) -> bool:
        return bool(self.mesh or self.workflow or self.task or self.parent)


async def inherit_mesh(body: dict, *, parent: str, mesh_mgr) -> None:
    """Settle which mesh a child joins when its parent named none.

    Rewrites ``body['mesh']`` in place, so everything downstream — preflight,
    the join, the briefing — sees an ordinary request that happens to name a
    mesh. Run before :func:`preflight`, which is where a mesh stops being
    negotiable: the identity block is built there and the system prompt is
    fixed the moment the harness starts.

    Silence means **the parent's mesh**. A spawn is one session asking for
    another, and the pair is the point; a child that could not be spoken to
    was the common outcome of forgetting one field, and it failed silently —
    the session came up, did its work, and had nowhere to put it.

    * **One mesh** — the answer, and the case nearly every fleet is in.
    * **None** — one is opened for the pair and the parent joined into it
      (:func:`_open_mesh_for`). A parent nobody put in a mesh is not a parent
      that wants an unreachable child; it is one that never needed a mesh
      until now. The mesh is named after the parent, so its whole subtree
      lands in the same one: the next spawn finds it in exactly one mesh and
      takes this branch no further.
    * **Several** — refused, naming them. Guessing here is the one mistake
      that cannot be walked back, because the wrong guess does not fail, it
      broadcasts: the child arrives in a room of strangers and reports its
      work to them.

    :data:`NO_MESH` opts out for a child that genuinely should be off the air.
    """
    named = str(body.get("mesh") or "").strip()
    if named == NO_MESH:
        body.pop("mesh", None)
        return
    if named:
        return
    mine = [m["mesh"] for m in mesh_mgr.meshes_for_session(parent)]
    if len(mine) > 1:
        raise OnboardError(
            f"session {parent!r} is in {len(mine)} meshes "
            f"({', '.join(sorted(mine))}) — name the one this child belongs "
            f"in, or pass mesh: {NO_MESH!r} to start it outside every mesh. "
            "A child is put in its parent's mesh by default, and with several "
            "there is no default to take"
        )
    body["mesh"] = mine[0] if mine else await _open_mesh_for(parent, mesh_mgr)


async def _open_mesh_for(parent: str, mesh_mgr) -> str:
    """Create a mesh for a parent that is in none, and put the parent in it.

    Named after the parent because the name has to mean something to a human
    reading ``claunch mesh ls`` later, and "the mesh s7 works in" is the only
    thing true about it at this point. A collision takes a suffix rather than
    joining whatever already holds that name — a mesh the parent is not in is
    somebody else's room.

    Deliberately *not* undone by :func:`unwind` when the spawn that triggered
    it goes on to fail. The child's leg is undone because its name returns to
    circulation and would be inherited; this one leaves a mesh holding the
    parent alone, which is not a leak but the state the next spawn wants —
    it finds the parent in exactly one mesh and joins it, instead of opening
    a second.
    """
    taken = {m.name for m in mesh_mgr.list()}
    candidates = (parent, *(f"{parent}-{i}" for i in range(2, 100)))
    name = next((c for c in candidates if c not in taken), "")
    if not name:
        raise OnboardError(
            f"could not open a mesh for {parent!r}: every name from {parent!r} "
            f"to {parent}-99 is taken — name a mesh for this child explicitly"
        )
    mesh_mgr.create(name)
    # The parent is briefed as usual: it is being put in a mesh it did not ask
    # for, and a session that discovers its own membership from a child's
    # first message has been told by the wrong party.
    await mesh_mgr.join(name, parent, handle=parent)
    log.info("opened mesh %r for %r and its children", name, parent)
    return name


def preflight(
    body: dict,
    *,
    mesh_mgr,
    session_name: str,
    cwd: str,
    harness: str,
    parent: str = "",
) -> Plan:
    """Check what can be checked without a session, and build its identity.

    Raises :class:`OnboardError` rather than creating anything, so a mistyped
    mesh or workflow costs nothing. ``session_name`` may be "" when the daemon
    will generate one — the handle then defaults at join time and the identity
    block simply omits it.

    ``parent`` names the session that asked for this one. Run
    :func:`inherit_mesh` first when there is one: by the time the identity is
    built the mesh has to be settled, because that is what decides whether the
    child can be told how to answer its parent at all.
    """
    mesh = str(body.get("mesh") or "").strip()
    handle = str(body.get("handle") or "").strip()
    role = str(body.get("role") or "").strip()
    workflow = str(body.get("workflow") or "").strip()
    connect = tuple(
        str(h).strip() for h in (body.get("connect") or []) if str(h).strip()
    )

    local_mesh = None
    if mesh:
        # An address (mesh@machine) is a join that may pend on another
        # operator, so nothing about it is certain here — it is left to the
        # join call, and contributes no identity.
        if "@" not in mesh:
            try:
                local_mesh = mesh_mgr.get(mesh)
            except MeshError:
                known = ", ".join(m.name for m in mesh_mgr.list()) or "(none)"
                raise OnboardError(
                    f"no mesh named {mesh!r} on this daemon — known: {known}"
                ) from None
            wanted_handle = handle or session_name
            if wanted_handle and wanted_handle in local_mesh.members:
                raise OnboardError(
                    f"handle {wanted_handle!r} is already taken in mesh "
                    f"{mesh!r} — pick another"
                )
    elif handle or connect:
        raise OnboardError(
            "'handle' and 'connect' only mean something with a 'mesh' to join"
        )

    if workflow:
        available = [name for name, _ in cflow_state.list_workflows(cwd or None)]
        if workflow not in available:
            raise OnboardError(
                f"no workflow named {workflow!r} in {cwd or 'the daemon cwd'} — "
                f"available: {', '.join(available) or '(none)'}"
            )
    elif body.get("context"):
        raise OnboardError("'context' describes a 'workflow' run — name one")

    # Resolved here, once, from the roster: the parent may have joined under a
    # handle that is not its session name, and every channel that tells the
    # child how to answer has to name the same one.
    parent_handle = ""
    if parent and local_mesh is not None:
        member = mesh_mgr.resolve_sender(mesh, parent)
        if member is not None:
            parent_handle = member.handle

    return Plan(
        mesh=mesh,
        handle=handle,
        role=role,
        connect=connect,
        workflow=workflow,
        context=str(body.get("context") or ""),
        task=str(body.get("task") or ""),
        parent=parent,
        parent_handle=parent_handle,
        identity=_identity_block(
            mesh=mesh if local_mesh is not None else "",
            handle=(handle or session_name) if local_mesh is not None else "",
            workflow=workflow,
            scope=session_name,
            harness=harness,
            parent=parent,
            parent_handle=parent_handle,
        ),
    )


def _identity_block(
    *,
    mesh: str,
    handle: str,
    workflow: str,
    scope: str,
    harness: str,
    parent: str = "",
    parent_handle: str = "",
) -> str:
    """The unchanging half, phrased for a system prompt.

    Empty for every harness but claude: ``--append-system-prompt`` is claude's
    flag and nothing else has a hook for it, which is why the opening block —
    not this — has to be sufficient on its own.
    """
    if harness != CLAUDE_HARNESS:
        return ""
    lines = []
    if mesh and handle:
        lines.append(
            f"You are a member of the claunch mesh {mesh!r}, with the handle "
            f"{handle!r}. That handle is yours for this whole session: it is "
            "how other members address you and how you sign what you send. "
            "Who you may reach changes as the mesh is rewired, so read the "
            "join briefing in your terminal for the current roster rather "
            "than assuming this one."
        )
    if parent:
        # Permanent because the relationship is: the graph is rewired all the
        # time, but who asked for this session never changes, and a compaction
        # that dropped it would leave an agent finishing work for nobody.
        lines.append(
            f"The claunch session {parent!r} created this one — it is your "
            "parent, and it is waiting on what you produce. Report to it: "
            "progress when the shape of the work changes, and the result when "
            "you are done. Finishing quietly is the one failure it cannot see."
            + (
                f" It answers in mesh {mesh!r} as {parent_handle!r}."
                if mesh and parent_handle
                else " You share no mesh with it, so you have no channel back "
                "— say so in your first turn rather than working on regardless."
            )
        )
    if workflow:
        lines.append(
            f"This session drives the cflow run of workflow {workflow!r}"
            + (f" (scope {scope!r})" if scope else "")
            + ". It is your run, not another session's: follow the /cflow "
            "protocol for it and do not start a second one in this directory."
        )
    return "\n\n".join(lines)


async def arrange(plan: Plan, *, name: str, cwd: str, mesh_mgr) -> Tuple[dict, str]:
    """Do every leg of a validated plan, for a session that does not exist yet.

    Returns ``(report, block)``: one report entry per leg attempted, so a
    partial success stays legible, and the opening message composed from
    whatever succeeded. Nothing here touches a terminal — ``name`` is a
    reserved session name, which is all a mesh join and a cflow run need — so
    the block is in hand before the harness is spawned and can be given to it
    directly.

    The caller owns the session that follows. If it fails to start, undo this
    with :func:`unwind`.
    """
    result: dict = {}
    sections: list = []

    # First, because it is the frame the rest is read in: a child that learns
    # its assignment before it learns who assigned it has already started
    # working out what to do with the result on its own.
    if plan.parent:
        sections.append(_parent_block(plan))

    if plan.mesh:
        joined = await _join(plan, name, mesh_mgr=mesh_mgr)
        result["mesh"] = joined
        if joined.get("briefing"):
            sections.append(joined.pop("briefing"))
        else:
            joined.pop("briefing", None)

    if plan.workflow:
        started = _start_workflow(plan, name=name, cwd=cwd)
        result["workflow"] = started
        if started.get("ok"):
            sections.append(
                "---\n"
                "# claunch cflow: run assigned at creation -- machine-generated\n"
                f"workflow: {plan.workflow}\n"
                f"scope: {started.get('scope')}\n"
                + (f"context: {plan.context}\n" if plan.context else "")
                + "protocol: this run is yours to drive. Follow /cflow — call "
                "the cflow 'status' tool to see where it stands, then "
                "continue from there.\n"
                "---"
            )
        else:
            # Say so in the terminal too. A run that failed to start is
            # otherwise invisible to the agent, which would sit waiting for
            # work that was never filed.
            sections.append(
                f"note: the workflow {plan.workflow!r} this session was "
                f"created with did not start ({started.get('error')})."
            )

    if plan.task:
        sections.append(plan.task)
        result["task"] = {"ok": True, "queued": True}

    return result, "\n\n".join(sections)


def _parent_block(plan: Plan) -> str:
    """Who is waiting, and the exact command that answers them.

    The system prompt carries the same fact and outlives this block, so the
    duplication is deliberate: the prompt is what survives a compaction, and
    this is what is on screen during the first turn — the turn where a child
    decides whether reporting back is part of the job.

    The reply line is a whole command rather than a description of one. An
    agent that has to assemble ``claunch mesh send`` from the briefing and the
    handle will sometimes assemble it wrong, and a misaddressed report reads
    to its parent exactly like no report at all.
    """
    reply = (
        f"claunch mesh send {plan.mesh} {plan.parent_handle} \"...\""
        if plan.mesh and plan.parent_handle
        else "(no shared mesh -- you have no channel back; say so in your reply)"
    )
    return (
        "---\n"
        "# claunch: the session that created you -- machine-generated\n"
        f"parent: {plan.parent}\n"
        + (f"parent_handle: {plan.parent_handle}\n" if plan.parent_handle else "")
        + f"reply: {reply}\n"
        "protocol: this session exists because that one asked for it. Send it "
        "progress when the shape of the work changes, and the result when you "
        "are done -- it cannot see your terminal, so silence reads as nothing "
        "happening.\n"
        "---"
    )


async def unwind(report: dict, *, name: str, cwd: str, mesh_mgr) -> None:
    """Undo an :func:`arrange` whose session then failed to start.

    Both legs leave state keyed to a session name, and that name goes back into
    circulation the moment the create fails — so a membership or a run left
    behind here does not merely leak, it is inherited by whatever session takes
    the name next. Best-effort: this runs while another error is already on its
    way to the caller, and a failure to tidy up must not replace it.
    """
    joined = report.get("mesh") or {}
    if joined.get("ok"):
        try:
            await mesh_mgr.leave(joined["mesh"], joined["handle"])
        except Exception as exc:  # noqa: BLE001 — best effort
            log.warning("could not undo mesh join for %r: %s", name, exc)
    if (report.get("workflow") or {}).get("ok"):
        try:
            cflow_engine.abort(by="claunch", cwd=cwd, scope=name)
        except Exception as exc:  # noqa: BLE001 — best effort
            log.warning("could not undo cflow run for %r: %s", name, exc)


async def _join(plan: Plan, name: str, *, mesh_mgr) -> dict:
    """Enrol the session, cut it down to the peers it should see, and return
    the briefing text for the opening block.

    A spawned child starts connected to its parent only — the conservative
    direction: a child that cannot yet reach a peer says so and asks, while a
    child wired to everyone by default has already broadcast to them by the
    time anyone notices the arrangement was wrong. A session with no parent
    (one a human created) keeps whatever ``connect`` names, and is otherwise
    left in the mesh unrestricted, because there is no "its own team" to be
    conservative about.
    """
    try:
        # The briefing is held back so it can go out inside the opening block
        # rather than as a paste racing it — and so it is built *after* the
        # isolation below, which decides what it is allowed to list.
        with mesh_mgr.defer_briefing(name):
            member = await mesh_mgr.join(
                plan.mesh, name, handle=plan.handle, role=plan.role,
            )
    except MeshError as exc:
        return {"ok": False, "error": str(exc)}
    if isinstance(member, dict):  # a remote join pended for approval
        return {"ok": False, "pending": member}

    out = {
        "ok": True,
        "mesh": plan.mesh,
        "handle": member.handle,
        "role": member.role,
    }
    keep = set(plan.connect)
    if plan.parent:
        parent_member = mesh_mgr.resolve_sender(plan.mesh, plan.parent)
        if parent_member is not None:
            keep.add(parent_member.handle)
        try:
            out["disconnected_from"] = await mesh_mgr.isolate_member(
                plan.mesh, member.handle, keep=keep
            )
        except MeshError as exc:
            out["isolate_error"] = str(exc)
        out["connected_to"] = sorted(keep)

    try:
        out["briefing"] = mesh_mgr.briefing_block(mesh_mgr.get(plan.mesh), member)
    except MeshError:
        pass  # deleted between the join and here; the session still exists
    return out


def _start_workflow(plan: Plan, *, name: str, cwd: str) -> dict:
    """Start a cflow run scoped to this session.

    Runs are keyed by ``(cwd, scope)`` and a session-scoped run uses the
    session name as its scope, so this session's agent picks it up as *its*
    run — not a sibling's, even though both sit in the same directory.
    """
    try:
        payload = cflow_engine.start(
            plan.workflow, context=plan.context or None, cwd=cwd, scope=name
        )
    except Exception as exc:  # noqa: BLE001 — the engine raises several types
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "workflow": plan.workflow,
        "scope": name,
        "step": payload.get("step") or payload.get("id"),
    }


def open_with(session, block: str) -> None:
    """Get ``block`` in front of the agent, whichever way this harness allows.

    A no-op when the harness already took the block on its command line — the
    good case, and the one :func:`arrange` exists to make possible. Otherwise
    it is typed in, in the background: the harness takes seconds to come up and
    a create call that waited for it would tie its caller to another program's
    startup time. Waiting for the terminal to be able to receive it is
    :meth:`Session.deliver`'s job, not this one's.
    """
    if not block or harness_mod.takes_opening_argv(session.sdef.harness):
        return
    asyncio.ensure_future(session.deliver(block))
