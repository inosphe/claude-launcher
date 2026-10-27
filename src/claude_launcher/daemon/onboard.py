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

The same reading applies to its run (:func:`inherit_workflow`): saying nothing
means *the workflow the parent's own run pairs children with*
(``default_child_cflow``), so a leader flow and its worker flow are declared
as a pair once instead of retyped at every spawn — and a parent whose
workflow pairs with nothing gives its child nothing, rather than whatever its
role happened to volunteer.

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
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Optional, Tuple

from ..cflow import engine as cflow_engine
from ..cflow import state as cflow_state
from ..cflow.model import WorkflowError
from . import cflow2_web
from . import harness as harness_mod, mcp_ready, mesh_roles
from .harness import CLAUDE_HARNESS
from .mesh import LEADER_IMPLIES, LEADER_ROLE, MeshError
from .session import delivery_wake

log = logging.getLogger(__name__)

#: Decline the inherited team mesh while keeping the parent channel.
#: Omission means inherit, so opting out requires an explicit token.
NO_MESH = "-"

#: The same spelling, for the run: a child deliberately given none where its
#: parent's workflow would have paired it with one. Omission means *inherit*
#: here too, so refusing needs a word of its own.
NO_WORKFLOW = "-"

#: The role a child joins under when the spawn had to open a mesh for its
#: parent (:func:`_open_mesh_for`). A mesh opened on the spot has no team for
#: the child to be placed in, and the role the caller asked for describes a
#: place in a team that does not exist yet — the packaged vocabulary's
#: ``free-role`` is the one that means "nobody gave you a place here; do the
#: job you were given" (``mesh_roles.DEFAULT_YAML`` spells it as ``default``).
FREE_ROLE = "free-role"


class OnboardError(Exception):
    """A request that cannot be honoured, found before anything was created.

    Mapped to 400. Distinct from the per-leg failures reported in
    :func:`run`'s result: those happened to a session that exists.
    ``errors`` (when set) maps a v2 start input to why it was refused.
    """

    def __init__(self, message: str = "", errors: Optional[Dict[str, str]] = None) -> None:
        super().__init__(message)
        self.errors = dict(errors or {})


def error_body(exc: OnboardError) -> dict:
    """The 400 body for ``exc``: the message, plus per-field ``errors`` for
    the start form when the v2 inputs were refused."""
    out: dict = {"error": str(exc)}
    if getattr(exc, "errors", None):
        out["errors"] = dict(exc.errors)
    return out


def _subroles(raw) -> Tuple[str, ...]:
    """A request's ``subroles``: a list of names, or one comma-separated
    string. Lower-cased here so the early checks below and the join agree."""
    if isinstance(raw, str):
        raw = raw.split(",")
    if not isinstance(raw, (list, tuple)):
        return ()
    out = []
    for name in raw:
        name = str(name or "").strip().lower()
        if name and name not in out:
            out.append(name)
    return tuple(out)


@dataclass(frozen=True)
class Plan:
    """A validated onboarding request, ready to be applied to a new session."""

    mesh: str = ""
    handle: str = ""
    role: str = ""
    #: Further roles the member answers for besides ``role`` — see
    #: ``Member.subroles``. Resolved by the join like the primary is.
    subroles: Tuple[str, ...] = ()
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
    #: Which runtime ``workflow`` belongs to: 1 (``cflow``) or 2 (``cflow2``,
    #: docs/cflow-v2.md), settled in preflight by :func:`cflow2_web.choose`.
    workflow_version: int = 1
    #: A v2 run's params (``workflow_params`` in the request), checked against
    #: the chart's declarations in preflight.
    workflow_params: Optional[dict] = None
    #: A v2 run's start inputs (``cflow_inputs``), normalised against the
    #: chart's ``inputs`` schema in preflight; ``None`` when it declares none.
    cflow_inputs: Optional[dict] = None
    #: The new session's profile: a v2 run resolves its capabilities from it.
    profile: str = ""

    @property
    def wanted(self) -> bool:
        return bool(self.mesh or self.workflow or self.task or self.parent)


async def inherit_mesh(
    body: dict, *, parent: str, mesh_mgr, project: str = "", child: str = ""
) -> None:
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
    * **Several** — the parent's **main mesh**: the one it joined first
      (``joined_at``, then the name, so the answer does not depend on a
      tie). A session collects a second mesh the moment it opens one for its
      own children (a reviewer's mesh of its own), so "several" is the normal
      state of a session that has spawned before, not a mistake in the
      request. Refusing here made every spawn from such a session fail, and
      the cflow2 delegation above it fall back to a sub-agent; the first mesh
      is the team the session was created into, and it is recorded, so it is
      a fact to read rather than a guess to make. A session that means its
      *other* mesh names it on the request.
    * **None** — one is opened for the pair and the parent joined into it
      (:func:`_open_mesh_for`). A parent nobody put in a mesh is not a parent
      that wants an unreachable child; it is one that never needed a mesh
      until now. The mesh is named after the parent, so its whole subtree
      lands in the same one: the next spawn finds it in exactly one mesh and
      takes this branch no further. The child joins it as
      :data:`FREE_ROLE`: there is no team here to hold a place in, and the
      role the caller asked for would claim one.

    :data:`NO_MESH` opts out of a team mesh. The parent channel is still set
    up by :func:`arrange`.

    ``child`` is the new session's name, when the caller already knows it —
    it is the one this decision is about, and a caller that reports the
    fallback above needs to name it.
    """
    named = str(body.get("mesh") or "").strip()
    if named == NO_MESH:
        body.pop("mesh", None)
        return
    if named:
        return
    mine = [m for m in mesh_mgr.meshes_for_session(parent)
            if not m.get("parent_channel")]
    if not mine:
        body["mesh"] = await _open_mesh_for(parent, mesh_mgr, project=project)
        body["role"] = FREE_ROLE
        return
    body["mesh"] = min(mine, key=_main_mesh_key)["mesh"]


def _main_mesh_key(entry: dict):
    """Sort key for "the mesh this session works in".

    Its first join, then its name. ``joined_at`` is written once, at the
    member's creation, and a session cannot join the same mesh twice
    (``MeshManager._join_local`` refuses both a taken handle and a second
    join of one session) — so the value is stable for as long as the
    membership is, and moves only if the session leaves and comes back.
    Seconds resolution means two joins can tie; the name then decides, so
    the answer never depends on the order a list happened to arrive in.

    The instants are compared as instants: a stamp written with another
    offset would sort wrongly as a string. A stamp that cannot be read is
    placed last rather than allowed to win.
    """
    stamp = str(entry.get("joined_at") or "")
    try:
        instant = datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, AttributeError):
        instant = float("inf")
    return (instant, str(entry.get("mesh") or ""))


def inherit_workflow(body: dict, *, parent: str, parent_cwd: str, cwd: str) -> None:
    """Settle which run a child drives when its parent named none.

    Rewrites ``body['workflow']`` in place, the way :func:`inherit_mesh`
    rewrites the mesh, so preflight sees an ordinary request that happens to
    name a workflow. Run before it, for the same reason: the run goes into the
    system prompt, which is fixed the moment the harness starts.

    Silence means **the pair the parent's own run declares**
    (``default_child_cflow``). A leader flow and a worker flow are two halves
    of one procedure, and until now nothing said so: the child's *role* picked
    its run, so a session driving some unrelated workflow still handed its
    children the worker flow because their role was worker. Reading it off the
    parent's run instead makes the pairing a fact of the procedure being run —
    and makes "this workflow pairs with nothing" a legible answer, delivered
    as a child with no run rather than as a wrong one.

    Nothing is inherited when the parent has no live run, when its workflow
    declares no pair, or when the paired workflow is not declared where the
    child will stand — the last one logged, never raised. Inheritance is
    implicit, and an implicit default must not turn a spawn that would have
    worked (a child sent to a workspace where that workflow is not declared)
    into a refusal; the child simply starts runless, which is what it got
    before this existed.

    :data:`NO_WORKFLOW` declines the pair for one child.
    """
    named = str(body.get("workflow") or "").strip()
    if named == NO_WORKFLOW:
        body.pop("workflow", None)
        return
    if named:
        return
    paired, version = _paired(parent=parent, parent_cwd=parent_cwd, cwd=cwd)
    if paired:
        body["workflow"] = paired
        if version == 2:
            body["workflow_version"] = 2


def paired_child_workflow(*, parent: str, parent_cwd: str, cwd: str) -> str:
    """The run ``parent`` pairs a child standing in ``cwd`` with, "" for none.

    Split out of :func:`inherit_workflow` so the pickers can *show* what the
    spawn would do: the wizard's Workflow row and the dashboard's spawn modal
    preselect this, and a person who then leaves the row alone gets exactly
    what the answer here says. One reading, two surfaces -- a picker that
    computed the pair itself would be a second one, free to drift.

    A parent driving a cflow v2 run pairs through its chart's
    ``meta.default_child_cflow`` (``.cflow2/runs/<parent>/chart.json``).
    """
    return _paired(parent=parent, parent_cwd=parent_cwd, cwd=cwd)[0]


def _paired(*, parent: str, parent_cwd: str, cwd: str) -> Tuple[str, int]:
    """``(name, version)`` of the pair: the v1 run's when the parent drives
    one, else its v2 run's. The version is 2 when the paired name is a v2
    workflow the v1 layers do not also declare, or when only a v2 parent
    named it and the v2 layers declare it."""
    paired = _paired_v1(parent=parent, parent_cwd=parent_cwd, cwd=cwd)
    from_v2 = False
    if paired is None:
        paired = cflow2_web.paired_child(parent_cwd, parent)
        from_v2 = True
    if not paired:
        return "", 1
    v1 = cflow2_web.v1_names(cwd or None)
    try:
        v2 = cflow2_web.v2_names(cwd or "")
    except Exception as exc:  # noqa: BLE001 - a broken v2 layer must not break the spawn
        log.debug("could not list v2 workflows in %s: %s", cwd, exc)
        v2 = []
    if from_v2 and paired in v2:
        return paired, 2
    if paired in v1:
        return paired, 1
    if paired in v2:
        return paired, 2
    try:
        broken = cflow2_web.v2_error(paired, cwd or "")
    except Exception:  # noqa: BLE001 - the log line is all this feeds
        broken = None
    if broken:
        log.warning(
            "%r's run pairs children with %r, which does not compile in %s (%s) "
            "— child of %r starts with no run",
            parent, paired, cwd or "the daemon cwd", broken, parent,
        )
        return "", 1
    log.info(
        "%r's run pairs children with %r, which is not declared in %s "
        "— child of %r starts with no run",
        parent, paired, cwd or "the daemon cwd", parent,
    )
    return "", 1


def _paired_v1(*, parent: str, parent_cwd: str, cwd: str) -> Optional[str]:
    """The v1 run's pair: ``None`` when the parent drives no v1 run (so a v2
    run is consulted), "" when its v1 run pairs with nothing."""
    # Scoped to the parent's session name, never falling back to the
    # directory's default-scope run: that run belongs to whoever started it,
    # and pairing off a run the parent does not drive would hand the child a
    # stranger's procedure.
    try:
        status = cflow_engine.status(parent_cwd, scope=parent)
    except Exception as exc:  # a broken run must not break the spawn
        log.debug("no run to pair a child against for %r: %s", parent, exc)
        return None
    if not status or status.get("status") == "idle":
        return None
    try:
        return cflow_state.load_snapshot(parent_cwd, parent).default_child_cflow or ""
    except Exception as exc:
        log.debug("could not read %r's workflow snapshot: %s", parent, exc)
        return ""


async def _open_mesh_for(parent: str, mesh_mgr, *, project: str = "") -> str:
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
    # Filed where the parent is: the mesh exists for the parent's subtree,
    # and a project's session list that could not reach its own meshes
    # would be the tier failing at the first spawn.
    mesh_mgr.create(name, project=project)
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
    parent_cwd: str = "",
    profile: str = "",
) -> Plan:
    """Check what can be checked without a session, and build its identity.

    Raises :class:`OnboardError` rather than creating anything, so a mistyped
    mesh or workflow costs nothing. ``session_name`` may be "" when the daemon
    will generate one — the handle then defaults at join time and the identity
    block simply omits it.

    ``parent`` names the session that asked for this one. Run
    :func:`inherit_mesh` first when there is one: by the time the identity is
    built the mesh has to be settled, because that is what decides whether the
    child can be told how to answer its parent at all. ``parent_cwd`` is that
    session's directory — the second place a *generated* workflow file may
    come from (see below).

    ``workflow`` is normally a declared name. It may instead be a ``.yaml``
    path into a generated directory (:func:`cflow_state.generated_workflow`):
    that is how a planning session hands a child a procedure it wrote for
    that one child. The file is composed here, before any session exists, so
    an overlay that does not load is a 400 and not a terminal stuck at start;
    the plan then carries the resolved absolute path, which is what the run
    records as its source.
    """
    mesh = str(body.get("mesh") or "").strip()
    handle = str(body.get("handle") or "").strip()
    role = str(body.get("role") or "").strip()
    subroles = _subroles(body.get("subroles"))
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
            if wanted_handle and role:
                try:
                    # Resolve now, before a session record or mesh membership
                    # exists. The join repeats the authority-owned check, but
                    # an explicit stale/custom role can already be refused
                    # without leaving a half-onboarded terminal behind.
                    local_mesh.roleset.resolve(wanted_handle, role)
                except mesh_roles.RoleError as exc:
                    raise OnboardError(str(exc)) from None
            for sub in subroles:
                # Same early refusal for a subrole the vocabulary lacks.
                if local_mesh.roleset.canonical(sub) is None:
                    known = ", ".join(sorted(local_mesh.roleset.roles))
                    raise OnboardError(
                        f"unknown subrole {sub!r} (known: {known})"
                    )
            # An exclusive role already held live fails the CREATE, not just
            # the join: a session built anyway would come up outside its mesh,
            # and a half-arrived member is worse than a refused request. The
            # join re-checks (it is the authority), so a race between here and
            # there still cannot seat two — this is the early, legible no.
            if wanted_handle:
                holder = mesh_mgr.exclusive_holder(
                    local_mesh, wanted_handle, role, subroles
                )
                if holder is not None:
                    raise OnboardError(
                        f"mesh {mesh!r} already has a live "
                        f"{holder.role!r} ({holder.handle!r}), and that role "
                        f"is exclusive — this session was not created. Join "
                        f"under another role (a crew of your own makes you a "
                        f"worker that integrates upward), or retire "
                        f"{holder.handle!r} first"
                    )
            # A leader always holds merger (claunch-lhgfx). Added to the plan
            # here, not only at the join, so the opening's role packs and
            # what the request reports back already carry it.
            try:
                primary = local_mesh.roleset.resolve(wanted_handle, role) if wanted_handle else ""
            except mesh_roles.RoleError:
                primary = ""
            if (primary == LEADER_ROLE and LEADER_IMPLIES not in subroles
                    and local_mesh.roleset.get(LEADER_IMPLIES) is not None):
                subroles = (*subroles, LEADER_IMPLIES)
    elif role or handle or connect or subroles:
        raise OnboardError(
            "'role', 'subroles', 'handle' and 'connect' only mean something "
            "with a 'mesh' to join"
        )

    # cflow v2 first (docs/cflow-v2.md): a name only the v2 layers declare,
    # a file in a .cflow2/generated directory (or a v1 generated file whose
    # text says `cflow: 2`), or any name when the request says
    # workflow_version 2. Everything else falls through to v1 untouched --
    # including a name both formats declare, which v1 keeps while they run
    # side by side.
    workflow_version = 1
    workflow_params = None
    cflow_inputs = None
    if workflow:
        try:
            chosen = cflow2_web.choose(
                workflow, body.get("workflow_version"), cwd=cwd, roots=(parent_cwd,)
            )
            if chosen is not None:
                workflow, chart = chosen
                if chart.kind != "workflow":
                    raise cflow2_web.ChoiceError(
                        f"{chart.name} is a {chart.kind}; only a workflow can be started"
                    )
                workflow_params = cflow2_web.params_for(
                    chart, body.get("workflow_params"), str(body.get("context") or "")
                )
                cflow2_web.check_params(chart, workflow_params)
                cflow_inputs = cflow2_web.check_inputs(chart, body.get("cflow_inputs"),
                                                       profile or None)
                workflow_version = 2
        except cflow2_web.InputsInvalid as exc:
            raise OnboardError(str(exc), exc.errors) from None
        except cflow2_web.ChoiceError as exc:
            raise OnboardError(str(exc)) from None
        except Exception as exc:  # noqa: BLE001 - compile errors of every kind
            raise OnboardError(f"v2 workflow {workflow!r}: {exc}") from None
    if workflow_version != 2 and body.get("cflow_inputs"):
        raise OnboardError(
            "'cflow_inputs' are the start inputs of a cflow v2 workflow; "
            f"{workflow or 'no workflow'!r} is not one"
        )
    if workflow_version == 2:
        pass  # resolved, compiled and its params checked above
    elif workflow and workflow.endswith((".yaml", ".yml")):
        try:
            path = cflow_state.generated_workflow(
                workflow, cwd=cwd or None, roots=(parent_cwd,)
            )
            cflow_state.load_workflow(str(path), cwd or None)
        except WorkflowError as exc:
            raise OnboardError(f"generated workflow {workflow!r}: {exc}") from None
        workflow = str(path)
    elif workflow:
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
        member = mesh_mgr.member_for_session(local_mesh, parent)
        if member is not None:
            parent_handle = member.handle

    return Plan(
        mesh=mesh,
        handle=handle,
        role=role,
        subroles=subroles,
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
            workflow_version=workflow_version,
        ),
        workflow_version=workflow_version,
        workflow_params=workflow_params,
        cflow_inputs=cflow_inputs,
        profile=profile,
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
    workflow_version: int = 1,
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
                else " The opening block names your parent channel and reply "
                "command; use rebrief to recover them when needed."
            )
        )
    if workflow and workflow_version == 2:
        lines.append(
            f"This session drives the cflow v2 run of workflow {workflow!r}"
            + (f" (scope {scope!r})" if scope else "")
            + ". It is your run, not another session's: drive it with the flow_* "
            "tools (flow_status, then flow_report / flow_select / flow_delegate) "
            "and do not start a second one in this directory."
        )
    elif workflow:
        lines.append(
            f"This session drives the cflow run of workflow {workflow!r}"
            + (f" (scope {scope!r})" if scope else "")
            + ". It is your run, not another session's: follow the /cflow "
            "protocol for it and do not start a second one in this directory."
        )
    return "\n\n".join(lines)


def role_default_workflow(
    entries: list, role: str, overrides: Optional[dict] = None
) -> Tuple[str, int]:
    """The workflow ``role`` selects on its own, as ``(value, version)`` --
    ``("", 0)`` when it selects none.

    The same answer the create form's picker and the CLI wizard pre-fill
    (:func:`wizard._workflow_default`): Settings' per-role override, else the
    highest-priority workflow declaring ``default_role: <role>`` that its own
    ``filter_roles`` admits. ``entries`` is the startable list the daemon
    serves (``/api/cflow/workflows``); ``value`` is what the picker would send
    (a bare name, or ``v2:<name>``).
    """
    from .. import wizard

    if not role:
        return "", 0
    normal = [wizard._workflow_entry(e) for e in entries or ()]
    value = wizard._workflow_default(normal, role, overrides or {})
    if not value:
        return "", 0
    if value.startswith("v2:"):
        return value, 2
    versions = {e.get("version") for e in normal if e["name"] == value}
    return value, (1 if 1 in versions or not versions else 2)


def compose_cflow_offer(*, role: str, workflow: str, version: int) -> str:
    """The opening block for a session that holds ``role``, whose role selects
    ``workflow``, and that was created without a run (claunch-val3l.1).

    Nothing is started for it: a run is begun only on its user's explicit
    yes. The block says so, says how to start it on a yes and how to record a
    no, so a mesh leader reading the roster can tell the two apart.
    """
    name = workflow[3:] if workflow.startswith("v2:") else workflow
    start = (
        f"the flow_start tool with workflow {name!r}" if version == 2
        else f"the cflow 'start' tool with workflow {name!r}"
    )
    lines = [
        "---",
        "# claunch cflow: no run -- machine-generated",
        f"role: {role} -- this role's workflow is {workflow!r}, and this "
        "session was created without a run of it.",
        "protocol: before you start the work, ask your user explicitly whether "
        f"to start {workflow!r} for this session (one question, with the two "
        "answers below). Never start it without their yes -- not on your own "
        "judgement, not because a mesh leader asks for its markers.",
        f"  yes -> start it with {start}, then follow /cflow.",
        "  no  -> record the answer: claunch cflow decline --workflow "
        f"{workflow} --reason \"<the user's answer>\". The mesh roster then "
        "shows the leader that this session runs no cflow by its user's "
        "decision, and you work the task without a run. If they change their "
        "mind later, run 'claunch cflow decline --clear' before starting it.",
        "---",
    ]
    return "\n".join(lines)


def compose_cflow_declined_note(*, workflow: str = "") -> str:
    """The opening line for a session whose operator declined a run at
    creation: recorded already, nothing to ask (claunch-val3l.1)."""
    what = f" ({workflow!r})" if workflow else ""
    return (
        f"cflow: the operator declined a workflow run{what} for this session "
        "when creating it, and that refusal is recorded on the session (the "
        "mesh roster shows it to the leader). Do not start a run; if a mesh "
        "leader asks for a run's markers, point it at the roster. If your user "
        "later asks for the run, withdraw the refusal first (claunch cflow "
        "decline --clear), then start it."
    )


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

    if plan.mesh:
        joined = await _join(plan, name, mesh_mgr=mesh_mgr)
        result["mesh"] = joined
        if joined.get("briefing"):
            sections.append(joined.pop("briefing"))
        else:
            joined.pop("briefing", None)

    if plan.parent:
        # Establish the route before launch; its reply command leads the
        # opening even when the requested team join failed or is pending.
        try:
            channel = await mesh_mgr.ensure_parent_channel(name, plan.parent)
        except Exception:
            await unwind(result, name=name, cwd=cwd, mesh_mgr=mesh_mgr)
            raise
        result["parent_channel"] = channel
        sections.insert(0, _parent_block(plan, channel=channel))

    if plan.workflow:
        started = _start_workflow(plan, name=name, cwd=cwd)
        result["workflow"] = started
        if started.get("ok") and plan.workflow_version == 2:
            sections.append(
                "---\n"
                "# claunch cflow2: run assigned at creation -- machine-generated\n"
                f"workflow: {plan.workflow} (cflow v2)\n"
                f"scope: {started.get('scope')}\n"
                + (f"context: {plan.context}\n" if plan.context else "")
                + "protocol: this run is yours to drive. Follow /cflow (its "
                "'cflow v2 runs' section) — call the flow_status tool to see "
                "the active tasks and choices, do each task, and report it "
                "with flow_report (flow_select for a choice, flow_delegate for "
                "a delegated node).\n"
                "new task: a user message that brings a new task while this "
                "run waits on something else is not work to do outside the "
                "run. Call flow_status first; then take it in through the "
                "run (an open question's new-task option, or "
                "flow_request_move to a force_target node) and stop until a "
                "person answers.\n"
                "---"
            )
        elif started.get("ok"):
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


def _parent_block(plan: Plan, *, channel: Optional[dict] = None) -> str:
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
    mesh = channel["mesh"] if channel else plan.mesh
    parent_handle = channel["parent_handle"] if channel else plan.parent_handle
    reply = (
        f"claunch mesh send {mesh} {parent_handle} \"...\""
        if mesh and parent_handle
        else "(no shared mesh -- you have no channel back; say so in your reply)"
    )
    return (
        "---\n"
        "# claunch: the session that created you -- machine-generated\n"
        f"parent: {plan.parent}\n"
        + (f"parent_handle: {parent_handle}\n" if parent_handle else "")
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
    channel = report.get("parent_channel") or {}
    if channel.get("created"):
        try:
            mesh_mgr.delete(channel["mesh"])
        except Exception as exc:  # noqa: BLE001 — best effort
            log.warning("could not undo parent channel for %r: %s", name, exc)
    joined = report.get("mesh") or {}
    if joined.get("ok"):
        try:
            await mesh_mgr.leave(joined["mesh"], joined["handle"])
        except Exception as exc:  # noqa: BLE001 — best effort
            log.warning("could not undo mesh join for %r: %s", name, exc)
    started = report.get("workflow") or {}
    if started.get("ok") and started.get("version") == 2:
        try:
            from ..cflow2 import runtime as cflow2_runtime

            cflow2_runtime.abort("the session failed to start", cwd=cwd, scope=name)
        except Exception as exc:  # noqa: BLE001 — best effort
            log.warning("could not undo cflow2 run for %r: %s", name, exc)
    elif started.get("ok"):
        try:
            cflow_engine.abort(by="claunch", cwd=cwd, scope=name)
        except Exception as exc:  # noqa: BLE001 — best effort
            log.warning("could not undo cflow run for %r: %s", name, exc)


async def _join(plan: Plan, name: str, *, mesh_mgr) -> dict:
    """Enrol the session and return the briefing text for the opening block.

    Who the new member may talk to is **the join's** decision, not this
    function's: it connects the child to its parent and to whatever the mesh's
    ``auto_link`` rules match, and everything else stays closed (see
    ``MeshManager._wire_member``). The conservative direction — a child that
    cannot yet reach a peer says so and asks, while a child wired to everyone
    by default has already broadcast to them by the time anyone notices the
    arrangement was wrong.

    What is left here is ``connect``: the extra peers the *spawner* named, one
    call at a time so each goes through the same authority check a hand edit
    does. They are opened after the join rather than passed into it because
    they are the caller's decision about this one child, not a property of the
    mesh's shape.
    """
    try:
        # The briefing is held back so it can go out inside the opening block
        # rather than as a paste racing it — and so it is built *after* the
        # isolation below, which decides what it is allowed to list.
        with mesh_mgr.defer_briefing(name):
            member = await mesh_mgr.join(
                plan.mesh, name, handle=plan.handle, role=plan.role,
                subroles=plan.subroles,
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
        "subroles": list(member.subroles),
    }
    mesh = mesh_mgr.get(plan.mesh)
    for peer in sorted(set(plan.connect)):
        if peer == member.handle or mesh.connected(member.handle, peer):
            continue
        try:
            await mesh_mgr.set_member_link(
                plan.mesh, member.handle, peer, enabled=True
            )
        except MeshError as exc:
            out.setdefault("connect_errors", {})[peer] = str(exc)
    out["connected_to"] = mesh.neighbours(member.handle)

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

    A v2 plan (``workflow_version`` 2) starts on the cflow2 runtime instead,
    in ``<cwd>/.cflow2/runs/<name>/``, with capabilities resolved for the
    session's profile.
    """
    if plan.workflow_version == 2:
        return _start_v2(plan, name=name, cwd=cwd)
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


def _start_v2(plan: Plan, *, name: str, cwd: str) -> dict:
    """The v2 half of :func:`_start_workflow`."""
    try:
        from ..cflow2 import runtime as cflow2_runtime

        chart = cflow2_web.load(plan.workflow, cwd)
        payload = cflow2_web.start_run(
            chart, inputs=plan.cflow_inputs, cwd=cwd, scope=name, params=plan.workflow_params,
            caps=cflow2_runtime.resolve_caps(plan.profile or None),
            # the mesh the session was enrolled in is the one its run asks in,
            # pinned so a mesh it joins later does not make that ambiguous
            # (claunch-5itbf)
            mesh=plan.mesh or None,
        )
    except Exception as exc:  # noqa: BLE001 — compile, params, a live run...
        return {"ok": False, "error": str(exc), "version": 2}
    return {
        "ok": True,
        "workflow": plan.workflow,
        "scope": name,
        "version": 2,
        "run": payload.get("run"),
        "active": payload.get("active") or [],
    }


def open_with(session, block: str, *, before=None) -> None:
    """Get ``block`` in front of the agent, whichever way this harness allows.

    A no-op when the harness already took the block on its command line — the
    good case, and the one :func:`arrange` exists to make possible. Otherwise
    it is typed in, in the background: the harness takes seconds to come up and
    a create call that waited for it would tie its caller to another program's
    startup time. Waiting for the terminal to be able to receive it is
    :meth:`Session.deliver`'s job, not this one's.

    Read off the argv the session actually started with, not off the harness's
    declared transport: a block too long for the command line is left off it
    (:func:`harness.carries_opening`), and a declaration alone would drop it.

    ``before`` is the pre-paste step :meth:`Session.deliver` runs once the
    terminal is ready. An opening with images passes one that puts the images
    in the composer (claunch-jmrmc); such an opening is always typed in -- the
    caller kept it off the command line -- and goes out even with no text.
    """
    if before is None and (
        not block or harness_mod.carries_opening(session.argv, block)
    ):
        return
    # ``before`` is passed only when there is one, so an opening without
    # images makes exactly the calls it made before the step existed.
    kw = {"before": before} if before is not None else {}
    plan = (
        mcp_ready.plan_for(session.sdef)
        if session.sdef.harness == harness_mod.CLAUDE_HARNESS
        else mcp_ready.Plan()
    )
    if plan.waits:
        asyncio.ensure_future(_deliver_after_mcp(session, block, plan, **kw))
    else:
        asyncio.ensure_future(_deliver_until_it_lands(session, block, **kw))


async def _deliver_after_mcp(session, block, plan, **kw) -> None:
    """Hold a claude opening back until the session's MCP servers are up.

    The first turn of a claude session that goes out before they connect has
    no tools (claunch-xixag); see :mod:`mcp_ready`.
    """
    try:
        await mcp_ready.wait(session, plan)
    except Exception:
        log.exception("waiting for MCP before the opening of %r failed",
                      session.sdef.name)
    await _deliver_until_it_lands(session, block, **kw)


#: How long :func:`open_with` keeps trying to hand over the opening block.
#: Generous, and paid at most once per session: the block is the only thing
#: telling the agent what it exists for, so a few minutes of retrying costs
#: nothing next to a session that starts with nothing to do.
OPENING_RETRY_WINDOW = 300.0


async def _deliver_until_it_lands(session, block, *, before=None) -> None:
    """Type the opening block in, retrying while the terminal is not free.

    ``deliver`` declines rather than typing over a human composing at that
    keyboard (:meth:`Session.deliver`), and one refused attempt used to be
    the end of this one's only message. So it asks again until it lands or
    the window closes — the session is brand new, so in practice the first
    attempt is also the last.
    """
    deadline = time.monotonic() + OPENING_RETRY_WINDOW
    while time.monotonic() < deadline:
        if session.exited and not getattr(session, "held_at", None):
            return
        kw = {"before": before} if before is not None else {}
        with delivery_wake("notice"):
            delivered = await session.deliver(block, **kw)
        if delivered:
            return
        await asyncio.sleep(1.0)
    log.warning(
        "opening block for %r never landed within %.0fs",
        session.sdef.name, OPENING_RETRY_WINDOW,
    )
