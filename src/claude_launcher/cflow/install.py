"""The ``/cflow`` skill: the execution protocol, and where it is written.

Registering the MCP server that backs it is :mod:`claude_launcher.install`'s
job — the tools ship in one merged server, so there is no cflow-only
registration to do here. This module owns the skill text (what ``/cflow
<workflow> [context]`` primes the agent with) and the copy of the packaged
workflows into the global layer that ``claunch install --global`` (and the
profile install) performs.

The workflows themselves are files under ``claude_launcher/workflows/``, not
strings in here. They were both once: an ``EXAMPLE_WORKFLOW`` literal that
``cflow example`` wrote, and a ``.claunch/workflows/feature-dev.yaml`` in
this checkout — and the two drifted, one teaching the deprecated ``gate:``
the other teaching ``ask:``. One file, read by everything, is the fix.
"""

from __future__ import annotations

import filecmp
import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Dict, List, Tuple

from . import state

SKILL_MD = """\
---
name: cflow
description: >-
  Run a declared claunch workflow (cflow) step by step via the cflow MCP
  tools. Use when the user types /cflow <workflow> or asks to run/resume a
  cflow workflow in this project.
---

# cflow — run a declared workflow step by step

Argument form: `/cflow <workflow-name> [extra context...]`. With no name:
call the `status` tool first — if a run is active, resume it; if `status`
carries a `pending_start` (a human asked from the dashboard/CLI for a
workflow to be started here), that is the answer; otherwise list candidates
(`claunch cflow ls`) and ask which one to run.

## Protocol

1. Call the MCP tool `start` with `{workflow, context}` (context = the extra
   arguments plus anything relevant the user said about the task).
2. The server feeds ONE step at a time. Execute the returned `instructions`
   completely, then file the step's completion report — `report {summary,
   details}` — and advance with `next {}`. The summary is 2–4 honest
   sentences on what actually happened; put evidence in `details` (commands
   run, test names, failure lines, files touched). Both fields are MARKDOWN
   — see "Reports are markdown" below. Reports are journaled, shown live on
   the daemon web dashboard, and become the PR text. Failures belong in the
   report too. If the payload carries `done_when`, that is the
   step's completion criterion: before calling `next`, check the statement is
   actually TRUE — and if it is not yet, keep working instead of advancing.
   If the payload carries `awaits`, the step declares something it is waiting
   for and the daemon is re-measuring it for you: end your turn rather than
   polling it by hand, and you will be sent a `signal` block the moment it
   changes. Silence there means "nothing new", NOT "still unmet" — and if no
   daemon is running, nothing measures it, so check it yourself before you
   conclude anything from quiet.
3. Act on the returned `status`:
   - `step` — do the work, then `report {summary, details}`, then `next {}`.
   - `report_required` — you called `next` without filing the step's
     report; call `report` first.
   - `verify_failed` — the step's verify command failed; its output is
     included and your report was discarded (the outcome it described did
     not survive). Fix the underlying problem, file a new `report`, and
     call `next` again. Never claim success: the server re-runs the command
     itself.
   - `select` with `chooser: agent` — decide per the prompt's criteria and
     call `select {option, reason}`.
   - `waiting_window` — you chose a PACED option (the workflow gives it an
     `interval`), and the interval since it was last taken has not passed:
     your choice is recorded and held, `opens_at` says until when. STOP your
     turn and do not poll — the daemon releases it at that moment, moves
     the run and nudges you (a "window opened" frame); without a daemon,
     your next `next` after that moment releases it. Until then keep doing
     the step's standing work, and if what the choice rests on changes,
     call `select` again: the same option with an updated reason replaces
     the held reason (the one confirmed at release is the latest), a
     different option cancels the hold. Nobody else is asked anything here.
   - `select` with `chooser: user`, or `waiting_selection` — call `select`
     once to record your RECOMMENDATION with reasoning, then STOP your turn
     and write the decision brief below. Ask them to confirm with `!
     claunch cflow select <option>` or from the daemon web dashboard, and
     say that any option is theirs to take — your call recorded a proposal,
     it did not take the decision.
   - `waiting_approval` — a human gate (or the loop guard, when `reason` is
     `loop_limit`; or a delegated decision that reached a human, when it is
     `ask`, or that nobody refused but nobody could take, when the payload's
     `ask.skipped` is non-empty). STOP your turn and write the decision
     brief below — approve/hold are its two options. For a loop limit, the
     brief must say why the loop keeps repeating and what another pass
     would do differently; for an `ask`, who was meant to decide and why
     they could not (that is what `ask.skipped` is). Then ask them to
     approve with `! claunch cflow approve` or the web dashboard's Approve
     button. You cannot approve and must not simulate approval.
   - `waiting_answer` — the workflow delegated this decision to another
     session, and `ask.asked` says who. STOP your turn: present what you
     have so far and say who is deciding. You cannot answer it — not with
     `select`, not with `answer`, not by asking them over the mesh and
     acting on the reply. They record it themselves; you will be nudged.
     The user is not shut out of it, though, and the payload's `user_door`
     carries the press that settles it (`claunch cflow select <option>`, or
     `claunch cflow approve` for an approval): add ONE line saying who holds
     it and that they can answer it now if they want it — a person's answer
     lands over a responder's and closes the question. That is a door, not a
     gate: do not write them the decision brief, do not wait for them, do not
     raise it again. The run is not stopped on them.
   - `status: step` or `select` where the payload says the decision was
     meant to be somebody else's — the workflow declared `otherwise: self`
     and nobody could be reached, so it is yours by default. Say so plainly
     when you report: nobody approved this, and reporting it as approved
     would be false.
   - `waiting_approval` with `reason: declined` — a responder refused, and
     the workflow declared nowhere for a refusal to go. Relay the refusal
     and its reason (`declined.by`, `declined.reason`) in the responder's
     own words, ahead of any answer of your own, then write the decision
     brief below and stop: a human decides whether to override (`! claunch
     cflow approve`) or send the run elsewhere (`! claunch cflow goto
     <step>`). They are being asked to overrule someone who looked at this,
     so a brief that argues only your side is not enough to decide on.
   - `done` — report the run using the returned journal and finish. If the
     payload carries a `pending_start` filed `by: "recur"`, this workflow is
     a service loop: report this round's journal, then immediately start the
     next round (`start` with exactly the requested workflow and context).
     Never decide to stop the loop yourself — only a human ends it
     (`! claunch cflow request --cancel`, or archiving the run).
4. Resuming after a stop: when nudged (any user message), call `status`
   first to see whether the gate/selection was granted, then continue with
   `next`.
5. `pending_start` in a `status` payload = a request for a workflow to be
   started in this session — filed by a human (from the dashboard or
   `claunch cflow request`), or `by: "recur"` when a recurring workflow's
   previous round finished. You perform the start: check it makes sense for
   what you are doing, tell the user you are starting it, then call `start
   {workflow, context}` with exactly that workflow (its `context` is the
   requester's own words — carry it through, adding anything relevant from
   the chat). If a human request is clearly wrong, do NOT start it: say why
   and stop. A recur request is never wrong to fulfil — it is the loop the
   workflow declared. The request clears once you start.

## Reports are markdown

`summary` and `details` are rendered as markdown on the daemon web
dashboard, so write them as markdown and not as one wall of prose — a
report is read by a person who did not watch you work, and the shape is
half of what makes it readable.

- Line breaks are kept. A single newline is a line break, so one fact per
  line stays one fact per line; a blank line starts a new paragraph.
- Evidence goes in a `- ` list, not a comma-spliced sentence. Nest with two
  spaces. Numbers, paths and test counts are the point — put them on their
  own lines.
- Commands, file paths, test ids and identifiers go in `backticks`.
  Multi-line output — a failure trace, a diff, a test tail — goes in a
  fenced ```` ``` ```` block, which is the only thing that keeps its exact
  spacing.
- `**bold**` for the verdict of a section, `## ` headings only when
  `details` is long enough to have sections. A short report needs neither.
- A pipe table (`| axis | value |` with a `|---|---|` rule) is right for
  (axis, tree, value) evidence and wrong for anything else.
- Underscores in names are left alone, so `test_web_topology.py` is safe to
  write bare — but backtick it anyway and it reads as what it is.

The one rule that is not about markdown: the summary still has to be true,
and the shape must not be used to make a thin result look thorough.

## Asking a person to decide

Every stop above ends with a person being asked something. What you write
there is the whole basis they have for answering — they did not watch you
work, and the payload's own text is a stub. A one-line "I recommend X,
confirm with `! claunch cflow select X`" is not a request; it asks them to
ratify a decision you already made. Write a brief that could change their
mind, and cover:

- **The decision, in one line.** What is actually being chosen — not the
  step id, not the workflow's phrasing repeated back.
- **What it rests on.** Concrete evidence, checkable: commands run, tests,
  commit hashes, files touched, the failure line. Not adjectives. If a
  number is the reason, give the number.
- **What each answer costs, holding off included.** What taking it does,
  what it forecloses, which options can be undone later, and what piles up
  for as long as the gate goes unanswered. Not deciding is one of the
  answers available to them, and it has a price they cannot see from where
  they stand — you can.
- **Your recommendation, and what would overturn it.** Say which you would
  take and why, then name the evidence that would flip you. A
  recommendation with no such condition is a demand wearing a hedge.
- **The weakest part of your own case.** What you could not check, what you
  are guessing at, where the suite is thin. Hiding it buys a confirmation
  that was never really given, and you are the only one positioned to see
  it.
- **How to answer, and that the answer is theirs.** The exact command or
  button, plus the standing fact that they may take an option you did not
  recommend, or ask you for more before deciding.

A worked example of what this catches: "the live server is serving
pre-merge code — restart now?" is a polite, clear, useless request. What
the reader is weighing is none of it — whether a restart cuts the sessions
attached right now, whether it can be undone, and what keeps accruing if
they leave it. Politeness was never the missing part.

Put it to them as a request, not an instruction. No manufactured deadline,
no re-asking a gate that is already waiting, no "just confirm" — pressure
applied to a decision that is not yours is how a run collects a rubber
stamp instead of a judgment. Length is not the goal either: this is a
brief, not a report. Say the deciding things and stop.

## A new task is a new run

A run is one task — the context it started with. When a user message is a
genuinely NEW requirement (a different feature, an unrelated fix, "actually
do X instead") rather than feedback on the current work, do not fold it into
the run: reports filed while moonlighting describe work the workflow never
asked for, and the journal stops being a true account. Refuse explicitly —
say the new task is outside this run and why — then get the old run retired
and a fresh one started:

- Run still ACTIVE: present the choice; the user makes it. Either finish
  the current run first and bring the new task after, or retire the run
  now — the user archives it (`! claunch cflow archive`, or the
  dashboard's Archive button) and you `start` fresh with the new task as
  context, or the user gives the explicit go-ahead for `start` with
  `force: true` (aborts + archives; state and journal are kept). Never
  absorb the task silently, never force on your own initiative.
- Run FINISHED (done/aborted): it is closed — do not stretch it to cover
  new work, and do not do the task bare when it is the kind of work the
  declared workflows exist for. Report the finished run's journal if you
  have not yet, then guide the start: propose the workflow and context
  (`/cflow <name> <context>`), and call `start` once the user confirms.
  `start` archives the finished run automatically; nothing is lost.
- NOT new tasks: clarifications, corrections to the current step's work,
  answers to questions you raised, scope the user adjusts within the same
  goal. Those stay in the run. A `pending_start` is also not this
  section's case — it is already an explicit request; follow the protocol
  above.

## Answering for someone else

Other sessions' runs may delegate a decision to your role — anything you are
wired to in the mesh except your own descendants. That is independent of
whether you are running a workflow yourself. A `decide` message on the mesh
is the doorbell; `asks` is where the question actually lives.

1. Call `asks {}` when a mesh message says a run needs a decision, whenever
   you are nudged, and before going idle. It lists what is waiting on you:
   the question, the options you may answer with, and the deadline.
2. Investigate before deciding. You were asked *because the run does not get
   to decide this one* — so check the actual code, tests and diff, not the
   asking agent's account of them. An approval you granted on the strength
   of the request text is worth nothing.
3. Call `answer {ask, decision, reason}`. The decision must be one of that
   request's options, or `abstain`. Put what you actually checked in
   `reason`; it is journaled and a human reads it.
4. `abstain` when you have no basis to decide — it passes the question to
   whoever is next, which is strictly better than a guess. Do not abstain
   to avoid the work of looking.
5. You never receive the asking step's instructions, and must not do its
   work, take over its run, or tell it what you would have implemented.
   Decide, say why, and stop.

You cannot answer a request that was not put to you, nor one from your own
run. Both are refused; neither is a thing to work around.

## Rules

- One step at a time. Do not skip ahead, merge steps, or invent steps.
- Workflows may loop (a `visit` counter > 1 means you are on another pass).
  Gates and selects apply on EVERY visit — a previous approval does not
  carry over.
- Reports must reflect reality, including what failed or was skipped. They
  are watched live by humans — write them as status updates for a reviewer,
  not as praise for yourself.
- Approvals and user selections happen OUTSIDE your tools (CLI / `!`
  commands / the web dashboard); nothing you can call grants them. The same
  holds for a delegated decision: `answer` acts on OTHER sessions' runs and
  refuses your own, so there is no arrangement of tool calls that unblocks
  a gate on you.
- The human's `claunch cflow ...` commands find the run from wherever their
  shell stands: the CLI walks up from the shell's directory (a chat
  session's `!` shell is often pinned inside a git worktree under the
  project root, while the run is keyed to the root) and, when a session is
  named (`-t` or the session env), falls back to the machine's run
  registry. If the user still gets "no active cflow run", the fix is
  `-t <session-name>` (this session's name — see the payload or
  `$CLAUNCH_SESSION`), or running the command from the run's own directory.
  Relay exactly that; do not invent flags or have them hunt for
  directories.
- Gates and selects apply on every visit, and so do delegated decisions: a
  loop that passes an `ask` twice asks twice, and the second answer may
  differ from the first.
- A human may force the run's position while you are stopped
  (`claunch cflow goto <step>`). Whatever `status` serves after a nudge IS
  the current truth — even if it revisits a step you already finished.
- If a tool returns an error about no active run, `start` one. If `start`
  errors because a run is ALREADY ACTIVE, do not retry and do not force:
  call `status` and resume that run — unless the user explicitly asked for
  a new/different workflow. In that case the active run must be retired
  first: ask the user to archive it (`! claunch cflow archive`, or the
  Archive button on the daemon web dashboard), or — only with the user's
  explicit go-ahead — call `start` with `force: true`, which aborts the
  active run and archives it (state + journal are kept, not lost). Never
  pass `force` on your own initiative.
- Finished (done/aborted) runs never block: `start` archives them
  automatically and begins the new run. That is the mechanics only —
  whether to start one for a task the user just handed you is decided
  per "A new task is a new run" above: close out the old run, confirm
  the workflow with the user, then start.
- A run can be replaced under you (someone archived it and started another,
  or started one from the dashboard). Then a tool answers that the run you
  were driving *is not the run here any more* — nothing was applied. Do not
  retry: call `status`, tell the user the run changed, and continue from
  whatever position `status` reports.
"""


def write_skill(skills_dir: Path) -> Path:
    path = skills_dir / "cflow" / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(SKILL_MD, encoding="utf-8")
    return path


#: Outcomes of seeding one packaged workflow into the global layer.
SEEDED = "seeded"  #: nothing was there; the packaged copy is now
UNCHANGED = "unchanged"  #: what is there is byte-for-byte the packaged copy
KEPT = "kept"  #: something different is there, and it was left alone

#: What ``claunch cflow update`` distinguishes beyond the seed itself. Two of
#: them are not contradictory to ``KEPT`` — KEPT is what seeding *did* (left it
#: alone), these are why, so KEPT is refined into one of them before a human
#: sees it.
STALE = "stale"  #: exactly the bytes we seeded; the packaged copy has moved on
EDITED = "edited"  #: differs from both the packaged copy and what we seeded
UNKNOWN = "unknown"  #: no seed record, so stale and edited are indistinguishable

#: The sidecar that remembers, per global workflow file, the sha256 of the
#: packaged bytes it was seeded from. File *names*, not workflow names — a
#: sidecar of stems would collide the moment two workflows share a stem.
SEED_RECORD_NAME = ".seeded.json"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def seed_record(workflows_dir: Path) -> Dict[str, str]:
    """The file-name -> sha256 map recording what was last seeded.

    Absent or unreadable means there is no record, not that there is nothing
    to remember: a layer seeded before the sidecar existed comes back empty,
    and every file in it is then ``UNKNOWN`` — the honest answer, because
    nothing can tell stale from edited without the memory.
    """
    path = workflows_dir / SEED_RECORD_NAME
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_seed_record(workflows_dir: Path, record: Dict[str, str]) -> None:
    """Write the sidecar atomically — a torn half-JSON is a trashed memory
    that turns every future file ``UNKNOWN`` for no good reason."""
    path = workflows_dir / SEED_RECORD_NAME
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(tmp, path)


def worktree_state(src: Path, dest: Path, record: Dict[str, str]) -> str:
    """How a file in a layer relates to the packaged copy, given the record.

    ``STALE`` is the case ``cflow update`` exists for: the layer copy is
    exactly what we seeded (it equals the recorded hash) and the packaged
    copy has since moved — replacing it loses nothing a human wrote. ``EDITED``
    is the dangerous one: it matches neither the package nor the record, so a
    person changed it since seeding, and a refresh must not be silent about
    it. ``UNKNOWN`` is EDITED's twin without proof — no record at all.
    """
    if dest.is_file() and filecmp.cmp(src, dest, shallow=False):
        return UNCHANGED
    recorded = record.get(dest.name)
    if recorded is None:
        return UNKNOWN
    return STALE if _sha256(dest) == recorded else EDITED


def example_workflow() -> Path:
    """The packaged workflow ``claunch cflow example`` scaffolds from."""
    return state.bundled_workflows_dir() / "feature-dev.yaml"


def install_workflow(src: Path, dest: Path, force: bool = False) -> str:
    """Copy one workflow file into a layer; report what became of it.

    Bytes, not text: a workflow that arrives with different line endings than
    it left with is a workflow that will look modified forever after.
    """
    if dest.exists() and not force:
        return UNCHANGED if filecmp.cmp(src, dest, shallow=False) else KEPT
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    return SEEDED


def seed_global_workflows(force: bool = False) -> List[Tuple[str, Path, str]]:
    """Copy the packaged workflows into the global layer, for ``install``.

    A seeded file is an ordinary file from that moment on — a human edits it,
    a project overrides it, ``claunch cflow add`` joins more to it. So a
    re-install must not undo an edit: a destination that differs from the
    package is reported and left alone unless ``force``. That is the whole
    price of copying rather than reading the package at resolve time, and it
    is the deliberate one: the global layer is meant to be yours.

    Each seed also writes the sha256 of the *packaged* bytes into
    `.seeded.json` — the memory ``cflow update`` needs to tell a stale copy
    (still byte-for-byte what we seeded, package moved on) from an edited one
    (a person changed it since). A file left alone gets no record entry: an
    edit is not a seed.
    """
    dest_dir = state.global_workflows_dir()
    sources = list(state.bundled_workflows())
    # Sidecar assets (verify scripts) ride along under the same edit-respecting
    # rules — a workflow whose verify command looks them up in this layer is
    # broken without them.
    sources += [(src.stem, src) for src in state.bundled_workflow_assets()]

    record = seed_record(dest_dir)
    outcomes = []
    for name, src in sources:
        dest = dest_dir / src.name
        outcome = install_workflow(src, dest, force)
        # A seed (or an identical re-seed) is the moment the record is made:
        # the packaged bytes are what the layer is now seeded from. An edit is
        # deliberately not recorded, so it stays "edited" to update.
        if outcome in (SEEDED, UNCHANGED):
            record[dest.name] = _sha256(src)
        outcomes.append((name, dest, outcome))
    write_seed_record(dest_dir, record)
    return outcomes


def update_global_workflows(
    names: List[str],
    force: bool = False,
    can_ask: bool = False,
) -> List[Tuple[str, str, bool, str]]:
    """Bring stale global workflows up to the packaged copy — ``cflow update``.

    Returns ``(name, outcome, applied, detail)`` per bundled workflow (or only
    the requested ``names``). The states are the seed outcomes plus
    ``worktree_state``: an ``UNCHANGED`` file is left alone, a ``STALE`` one is
    overwritten (it is still the bytes we seeded, so nothing a human wrote is
    lost), and an ``EDITED`` or ``UNKNOWN`` one is copied aside to a single
    ``.bak`` slot and then overwritten — but only with ``--force``, or after a
    live person accepted the prompt. ``applied`` records which actually
    happened, so the caller reports the real outcome rather than assuming.

    ``can_ask`` is the isatty guard, not a silent yes: when a real person is
    at a terminal, the overwrite of an edited file is offered as a question
    and only proceeds on an explicit answer. A script or an agent session
    (which sets ``$CLAUNCH_SESSION``) cannot be asked, and must pass
    ``--force`` explicitly. That is the whole distinction — an agent's "yes"
    is not the operator's.
    """
    dest_dir = state.global_workflows_dir()
    sources = list(state.bundled_workflows()) + [
        (src.stem, src) for src in state.bundled_workflow_assets()
    ]

    record = seed_record(dest_dir)
    outcomes = []
    for name, src in sources:
        if names and name not in names:
            continue
        dest = dest_dir / src.name

        if not dest.is_file():
            outcome = SEEDED
            detail = "installed from the packaged copy"
        elif filecmp.cmp(src, dest, shallow=False):
            outcome = UNCHANGED
            detail = "already current"
        else:
            recorded = record.get(dest.name)
            if recorded is None:
                outcome = UNKNOWN
                detail = "no seed record — cannot prove stale from edited"
            elif _sha256(dest) == recorded:
                outcome = STALE
                detail = "replacing the previously seeded copy"
            else:
                outcome = EDITED
                detail = "differs from both the package and the seed record"

        applied = False
        if outcome in (STALE, SEEDED):
            install_workflow(src, dest, force=True)
            record[dest.name] = _sha256(src)
            applied = True
        elif outcome in (EDITED, UNKNOWN):
            if force or (can_ask and _confirm_replace(name)):
                _backup_one(dest)
                install_workflow(src, dest, force=True)
                record[dest.name] = _sha256(src)
                detail = "replaced from the package (previous kept as .bak)"
                applied = True
            elif not force:
                detail = f"{detail}; pass --force to replace (kept as .bak)"
        outcomes.append((name, outcome, applied, detail))

    write_seed_record(dest_dir, record)
    return outcomes


def _confirm_replace(name: str) -> bool:
    """Ask the person at the terminal whether an edited copy may be replaced.

    ``can_ask`` only means a person might be reached; this is where they are
    actually reached. A refused answer (or EOF) is a refusal — never a
    default-yes.
    """
    try:
        answer = input(f"overwrite edited workflow {name!r}? [y/N]: ").strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


def _backup_one(dest: Path) -> Path:
    """Copy ``dest`` to a single ``.bak`` slot, replacing any earlier one.

    One slot, not a numbered sequence: the previous backup is a stale snapshot
    of an already-replaced file, and keeping it would make a pile of versions
    nobody can tell apart. The one before the current overwrite is the one
    that matters.
    """
    bak = dest.with_name(dest.name + ".bak")
    shutil.copyfile(dest, bak)
    return bak


