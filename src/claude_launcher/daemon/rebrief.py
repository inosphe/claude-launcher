"""Re-briefing: the derived half of a session's briefing, said again on demand.

A claude session loses the conversation-carried half of what it knows twice in
an ordinary life: ``/compact`` squeezes it into a summary, ``/clear`` throws it
away. The unchanging half survives — handle, parent, the run it drives are in
the system prompt, re-injected on every spawn (see
:func:`harness.build_command`) — but the half that was *derived* at onboarding
lived only in the transcript: who is reachable right now, what replies are
owed, where the run stands, what the opening task said. This module recomposes
that half from current daemon state, whenever asked.

One composition, three doors:

* **The SessionStart hook** every claude session carries
  (:data:`harness.REBRIEF_HOOK_SETTINGS`) runs ``claunch rebrief`` on
  ``compact`` and ``clear``, and claude reads the command's stdout back into
  context. The automatic door, and the load-bearing one: the agent that most
  needs a re-briefing is the one that no longer remembers it should ask.
* **``claunch rebrief`` and the ``rebrief`` MCP tool** — the agent's own pull,
  for the moments the mesh skill's recovery procedure used to cover with five
  separate commands.
* **``POST /api/sessions/{name}/rebrief``** — an operator pushing the same
  text into the terminal from the web UI, for a session they can see is lost.

Pointer style throughout, for the reason the join briefing points at
``claunch mesh stance`` instead of pasting it (:meth:`MeshManager._stance_lines`):
state that can move is fetched at read time, not frozen into context. It also
keeps the block inside the hook's stdout budget (about 10k characters) no
matter how big the fleet around this session has grown.

Everything here is re-derived; nothing is stored for it. The one exception is
the opening task, which used to be true once and gone — it is now recorded on
the definition (:attr:`SessionDef.task`) precisely so this module can restate
it. Restated, not replayed: the record feeds this text and nothing else, and
the first-spawn ``opening`` argv path is untouched.
"""

from __future__ import annotations

import logging
from typing import Optional

from ..cflow import engine as cflow_engine
from ..cflow import state as cflow_state
from .mesh import MeshError

log = logging.getLogger(__name__)

#: Claude caps hook stdout (~10k characters). A block that outgrows the cap is
#: cut here, visibly and at a section boundary's worth of margin, rather than
#: by the harness, silently and mid-sentence.
BLOCK_LIMIT = 9500

#: An opening task is restated, not re-run: a page of it is enough to say what
#: the job was, and the cap keeps one long task from crowding out the sections
#: that cannot be looked up anywhere else.
TASK_LIMIT = 2000


def compose(name: str, *, manager, mesh_mgr) -> str:
    """The whole re-briefing for one session, from current daemon state.

    Returns ``""`` when there is nothing to say — no mesh, no run, no kin, no
    recorded task — so the hook prints nothing and a bare session's ``/clear``
    stays as quiet as it always was. Raises :class:`ManagerError` for an
    unknown session, which the API maps the same way every session route does.

    Section order follows :func:`onboard.arrange`, and for its reason: who is
    waiting is the frame the rest is read in, so the parent comes before the
    mesh, the mesh before the run, and the task — the thing all of it serves —
    last, where it stays on screen closest to the agent's next turn.
    """
    sdef = manager.get(name).sdef

    def assemble(inline_stance: bool) -> list:
        return [
            s
            for s in (
                _header(name),
                _parent_section(sdef, manager, mesh_mgr),
                *_mesh_sections(name, mesh_mgr, inline_stance=inline_stance),
                _cflow_section(sdef),
                _asks_section(name),
                _children_section(name, manager),
                _task_section(sdef.task or "", issue=sdef.issue),
            )
            if s
        ]

    sections = assemble(True)
    if len(sections) == 1:
        return ""
    block = "\n\n".join(sections)
    if len(block) > BLOCK_LIMIT:
        # Over budget: give up the pasted stance FIRST, and only then cut.
        # The join briefing pastes a stance no system prompt is carrying
        # (``MeshManager._stance_lines``), which is right where it is typed
        # into a terminal — but here it competes with the owed ledger, the
        # open decisions and the opening task, and it is the only one of the
        # four that ``claunch mesh stance`` can hand back on demand. Dropping
        # it costs the agent one command; a blind tail-cut costs it whichever
        # section happened to be last, which is the task.
        block = "\n\n".join(assemble(False))
    if len(block) > BLOCK_LIMIT:
        block = block[:BLOCK_LIMIT] + (
            "\n[rebrief cut at the hook's output budget -- run 'claunch "
            "rebrief' in a ! shell to read it whole]"
        )
    return block


def _header(name: str) -> str:
    """Why this text is arriving, before any of it does.

    A block that starts mid-roster reads like a peer's message or a stray
    paste; this one names itself first, and tells the agent the one rule that
    makes the rest safe to act on — the daemon's answer beats the summary's.
    """
    return (
        "---\n"
        "# claunch rebrief: session state, re-derived -- machine-generated\n"
        f"session: {name}\n"
        "note: your context was compacted or cleared. Everything below is "
        "read from the daemon's live state just now -- where it disagrees "
        "with what you remember, it is right and the memory is stale.\n"
        "---"
    )


def _parent_section(sdef, manager, mesh_mgr) -> str:
    """Who is waiting, and the exact command that answers them.

    The system prompt carries the relationship permanently; what it cannot
    carry is the live half — whether the parent is still running, and the
    reply command with the mesh and handle filled in. The nudge to report
    *now* is deliberate: a reset the parent cannot see reads to it exactly
    like a child that went quiet.
    """
    if not sdef.parent:
        return ""
    parent = sdef.parent
    mesh_name = handle = ""
    try:
        for row in mesh_mgr.meshes_for_session(sdef.name):
            member = mesh_mgr.resolve_sender(row["mesh"], parent)
            if member is not None:
                mesh_name, handle = row["mesh"], member.handle
                break
    except MeshError:
        pass
    try:
        alive = not manager.get(parent).exited
    except Exception:  # noqa: BLE001 — a cleared record is just "not alive"
        alive = False
    reply = (
        f'claunch mesh send {mesh_name} {handle} "..."'
        if mesh_name and handle
        else "(no shared mesh -- you have no channel back; say so in your "
        "next report, whoever it goes to)"
    )
    return (
        "---\n"
        "# claunch: who you report to -- machine-generated\n"
        f"parent: {parent}" + ("" if alive else " (currently exited)") + "\n"
        f"reply: {reply}\n"
        "protocol: that session created this one and cannot see your "
        "terminal. If you were mid-task, send it one progress line now -- "
        "to it, the reset you just had looks exactly like you going quiet.\n"
        "---"
    )


def _mesh_sections(name: str, mesh_mgr, *, inline_stance: bool = True) -> list:
    """The join briefing again, per membership, plus the ledger it cannot know.

    :meth:`MeshManager.briefing_block` is reused rather than paraphrased: it
    is built from the live graph at call time, so it is as true now as it was
    at the join, and one wording means one thing to learn. What it does not
    carry is time — the replies this member has owed since before the reset —
    so the owed line rides after it. Undelivered *incoming* mail needs no
    line at all: the daemon's delivery worker holds a cursor per member and
    retypes the backlog on its own.
    """
    sections = []
    try:
        rows = mesh_mgr.meshes_for_session(name)
    except MeshError:
        return sections
    for row in rows:
        try:
            mesh = mesh_mgr.get(row["mesh"])
            member = mesh_mgr.member_for_session(mesh, name)
            if member is None:
                continue
            block = mesh_mgr.briefing_block(
                mesh, member, inline_stance=inline_stance
            )
            owed = mesh.owed(member.handle)
        except MeshError:
            continue  # deleted between the listing and here
        if owed:
            block += (
                f"\nowed: {len(owed)} delivered message(s) still await your "
                f"reply -- 'claunch mesh history {mesh.name} -n 30' shows "
                "them. Answer or decline them; to the senders, silence since "
                "the reset is still silence."
            )
        sections.append(block)
    return sections


def _cflow_section(sdef) -> str:
    """The run this session drives: its position, and the one rule for resuming.

    Pointer style like the assignment block it echoes (:func:`onboard.arrange`):
    workflow, scope and position orient, and everything else is fetched by the
    ``status`` call the protocol line demands — a read-only call, so composing
    this cannot open a delegated ask as a side effect. An idle slot with no
    pending start says nothing at all: most sessions never drive a run, and a
    section that usually read "no run" would teach agents to skim.
    """
    if not sdef.cwd:
        return ""
    try:
        cwd = cflow_state.resolve_cwd(sdef.cwd)
        payload = cflow_engine.status(cwd, scope=sdef.name)
    except Exception as exc:  # noqa: BLE001 — a broken slot must not sink the rest
        log.warning("rebrief: cflow status failed for %r: %s", sdef.name, exc)
        return ""
    if payload.get("status") == "idle" and not payload.get("pending_start"):
        return ""
    position = str(payload.get("status") or "")
    if payload.get("step_id"):
        position += f" (step {payload['step_id']})"
    lines = [
        "---",
        "# claunch cflow: your run -- machine-generated",
        f"workflow: {payload.get('workflow') or '(requested, not started)'}",
        f"scope: {sdef.name}",
        f"position: {position}",
    ]
    if payload.get("pending_start"):
        lines.append("pending_start: a start request is filed for this slot")
    lines.extend(
        [
            "protocol: this run is yours to drive. Call the cflow 'status' "
            "tool -- what it returns is the current truth, even where it "
            "revisits a step you remember finishing -- then continue per "
            "the /cflow protocol.",
            "---",
        ]
    )
    return "\n".join(lines)


def _asks_section(name: str) -> str:
    """Decisions other runs delegated to this session, still open.

    These live outside the session's own run — another agent's workflow is
    stopped on this session's answer — which is why they get their own line
    instead of riding in the run section. A count and a tool name is enough:
    the ``asks`` tool serves the prompts and ``answer`` closes them.
    """
    try:
        open_asks = cflow_engine.open_asks(name)
    except Exception as exc:  # noqa: BLE001
        log.warning("rebrief: open_asks failed for %r: %s", name, exc)
        return ""
    if not open_asks:
        return ""
    return (
        f"cflow: {len(open_asks)} delegated decision(s) from other runs "
        "await your answer -- their workflows are stopped on it. Call the "
        "cflow 'asks' tool, then 'answer'."
    )


def _children_section(name: str, manager) -> str:
    """The sessions this one spawned that are still running.

    The relationship is in the children's own prompts; what the reset erased
    is this side's list of who they are. They cannot see the reset any more
    than the parent can, so a child holding a finished result keeps holding
    it until this session asks.
    """
    live = manager.live_children(name)
    if not live:
        return ""
    return (
        f"children: you spawned {', '.join(live)} -- still running, still "
        "reporting to you, and unable to see your reset. The 'children' tool "
        "lists them; chase the ones you were waiting on."
    )


def _task_section(task: str, *, issue: Optional[str] = None) -> str:
    """The opening instruction, as recorded at creation.

    Restated last so it sits closest to the agent's next turn. The note draws
    the line the record cannot: this is what was *asked*, and whatever has
    been done toward it since lives in the conversation summary, the mesh
    history and the cflow journal — not here. The board issue the session is
    for is named with it: the one record that *does* hold what has been done
    since, and the one thing a compaction cannot take away.
    """
    task = task.strip()
    if not task and not issue:
        return ""
    if len(task) > TASK_LIMIT:
        task = task[:TASK_LIMIT] + (
            "\n[... task cut for the re-briefing; the full text is in the "
            "session record]"
        )
    issue_line = (
        f"issue: {issue} -- your board record; `claunch beads show {issue} "
        "--json` for its state and comments\n"
        if issue else ""
    )
    return (
        "---\n"
        "# claunch: your opening task, as recorded at creation -- "
        "machine-generated\n"
        f"{issue_line}"
        + (f"{task}\n" if task else "")
        + "note: this is the instruction as first given. What has been done "
        "toward it since is in your conversation summary, the mesh history "
        "and the cflow journal -- not in this block.\n"
        "---"
    )
