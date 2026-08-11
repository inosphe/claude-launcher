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
from typing import Optional, Tuple

from ..cflow import engine as cflow_engine
from ..cflow import state as cflow_state
from . import harness as harness_mod
from .harness import CLAUDE_HARNESS
from .mesh import MeshError

log = logging.getLogger(__name__)


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
    #: The system-prompt block, or "" when there is nothing certain to say
    #: (no mesh and no workflow, a harness with no prompt to append to, or a
    #: remote join whose handle is not settled until the grant arrives).
    identity: str = ""

    @property
    def wanted(self) -> bool:
        return bool(self.mesh or self.workflow or self.task)


def preflight(
    body: dict,
    *,
    mesh_mgr,
    session_name: str,
    cwd: str,
    harness: str,
) -> Plan:
    """Check what can be checked without a session, and build its identity.

    Raises :class:`OnboardError` rather than creating anything, so a mistyped
    mesh or workflow costs nothing. ``session_name`` may be "" when the daemon
    will generate one — the handle then defaults at join time and the identity
    block simply omits it.
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

    return Plan(
        mesh=mesh,
        handle=handle,
        role=role,
        connect=connect,
        workflow=workflow,
        context=str(body.get("context") or ""),
        task=str(body.get("task") or ""),
        identity=_identity_block(
            mesh=mesh if local_mesh is not None else "",
            handle=(handle or session_name) if local_mesh is not None else "",
            workflow=workflow,
            scope=session_name,
            harness=harness,
        ),
    )


def _identity_block(
    *, mesh: str, handle: str, workflow: str, scope: str, harness: str
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
    if workflow:
        lines.append(
            f"This session drives the cflow run of workflow {workflow!r}"
            + (f" (scope {scope!r})" if scope else "")
            + ". It is your run, not another session's: follow the /cflow "
            "protocol for it and do not start a second one in this directory."
        )
    return "\n\n".join(lines)


async def arrange(
    plan: Plan, *, name: str, cwd: str, mesh_mgr, parent: Optional[str] = None
) -> Tuple[dict, str]:
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

    if plan.mesh:
        joined = await _join(plan, name, mesh_mgr=mesh_mgr, parent=parent)
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


async def _join(plan: Plan, name: str, *, mesh_mgr, parent: Optional[str]) -> dict:
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
    if parent is not None:
        parent_member = mesh_mgr.resolve_sender(plan.mesh, parent)
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
