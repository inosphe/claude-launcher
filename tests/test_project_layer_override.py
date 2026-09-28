"""This repository's own project-layer overrides stay loadable and armed.

The canonical improv pair ships verify-free (a suite command is a property
of one repository), and THIS repository's machine checks live in its
``.claunch/workflows/`` overrides instead. These tests pin that arrangement:
the two override files must parse, and each must carry exactly the one
verify its layer exists to add. A later edit that breaks the yaml or drops a
verify would otherwise only be discovered by a run blocking on it.

**Neither of those verifies is a test suite any more, and that is the rule
these pin hardest.** The engine runs a ``verify`` synchronously as the run
leaves the step (``cflow/engine.py`` ``_run_verify``), so a suite in that
field is a sweep that blocks the round and that nobody typed — the worker
workflow's own review step calls this out: "the least visible sweep is the
least recorded sweep." It had both:

* ``review`` ran ``pytest tests -m "not worktree" -n 8`` — 1450 of 1558
  tests. The step's prose said "the full sweep is not run here" while the
  command ran 93% of it, and six sessions ran it concurrently.
* ``sweep`` ran the whole suite outright, which quietly made a liar of the
  leader workflow's "the leader does not run sweeps or merges in its turn".

So the suites moved out to ``tools/``: ``changed_tests.py`` runs only what a
branch's change can affect, and ``sweep.py`` splits the sweep (a subagent
runs it) from the verdict (this gate reads its receipt in milliseconds).
:func:`test_no_override_verify_runs_a_test_suite` is what keeps a future
edit from putting a suite back.

The measurements that used to justify ``-n 8`` and a short basetemp did not
go away — they moved with the commands, and are pinned where those commands
now live (``tests/test_sweep.py``, ``tests/test_changed_tests.py``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_launcher.cflow import model, state as state_mod

OVERRIDES = Path(__file__).resolve().parents[1] / ".claunch" / "workflows"
SYNC = Path(__file__).resolve().parents[1] / "tools" / "sync_project_layer.py"


def _graft_fields() -> tuple:
    """The field names the project layer owns, from the tool that grafts them.

    Loaded by path rather than imported: pytest's ``pythonpath`` is ``src``,
    so ``tools`` is not on it — the same reason the sync tool's own tests load
    it this way.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("sync_project_layer", SYNC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.GRAFT_FIELDS

#: Which steps each override arms.
#:
#: The leader arms two, and by now they are the same *kind* of check: each
#: reads a fact some other actor already established, rather than
#: establishing it here. ``sweep`` asks whether a subagent's sweep of the
#: batch's merge result was green (it used to sit on ``integrate`` itself,
#: when every merge was its own sweep; the 5-minute integration window split
#: the two, and moving the suite into a subagent split run from verdict).
#: ``reflect`` asks whether the live daemon was actually restarted onto that
#: merge. Both exist because their step used to be prose alone -- a round
#: could be filed as swept, or as deployed, with nothing having happened.
#:
#: ``workspace-ok`` is the newest, and it is armed for a reason none of the
#: others share: it is what keeps the ``chooser: agent`` on ``workspace-check``
#: from being a decision the driver profits by. That select's ``match`` option
#: skips a human approval, so the branch it routes to re-runs the same command
#: on the way out -- a false ``match`` stops the run at ``workspace-ok``, and
#: the only way on from there is ``request_goto`` to the approval the agent
#: was avoiding. Disarm this one and the select becomes an agent choosing
#: whether to stand a person up. ``tests/test_workspace_gate.py`` holds the
#: pair together.
#:
#: The worker arms four more. ``review`` runs the tests its own change can affect;
#: ``wrapup`` asks whether the round left its HTML report behind. The second
#: is armed for the same reason the leader's two are -- the step was prose
#: alone, and it sits in the last thing a session does before killing itself,
#: which is the line a busy round drops first: skipping it costs nothing,
#: because the turn ends either way.
#:
#: ``landed`` is the third and it is the same kind of check as the leader's:
#: it reads a fact another actor established -- whether the parent actually
#: merged this branch. It exists because the round used to end at "requested".
#: ``integration-request`` froze the branch, filed the request and went
#: straight to ``wrapup``, which tells the session to kill itself in that same
#: turn; the run reached ``done`` with the branch still sitting in somebody
#: else's queue. Measured, repeatedly: a worker filed "통합 대기 중 ... run
#: done. 세션 종료한다" and exited, and a request that was rejected, or asked
#: for a rebase, or quietly dropped from a batch, had nobody left to notice.
ARMED = {
    # stack-merge: the cut must be new to leave (tools/published.py)
    "improv-worker": ("workspace-ok", "review", "stack-merge", "rebase", "wrapup", "end-hold"),
    "improv-leader": ("sweep",),
}

#: Steps whose gate is a ``checklist:`` — a list of item commands, measured by
#: the daemon, and the run leaves only when every one of them exits 0. A third
#: table rather than a third column in :data:`ARMED` because the field is run
#: by a third thing: the engine runs a ``verify`` on the way out, the reminder
#: clock samples an ``awaits`` while a run stands still, and the checklist
#: clock both samples these AND performs the transition.
#:
#: Both entries were a ``verify`` until the gates they arm stopped being
#: carried by prose. That is the change: the two decisions a person most needs
#: to see — did the parent merge this branch, did the live daemon pick the
#: merge up — used to be advanced by the driving agent reading its own step
#: text, and are now advanced by exit codes a person can read in
#: ``claunch cflow status``.
CHECKED = {
    "improv-worker": ("landed",),
    "improv-leader": ("reflect",),
}

#: Steps whose project-layer field is an ``awaits`` probe rather than (or as
#: well as) a ``verify``. Kept apart from :data:`ARMED` because the two are
#: run by different things -- the engine on the way out of a step, the daemon
#: on a run standing still -- and only ``awaits`` may sit on a select step,
#: which is the shape ``await-landing`` has.
WATCHED = {
    # `work` waits on the found-issue sub runs it opened (tools/sub_done.py,
    # this repository's spelling of `awaits: {sub: all}`, minus the stack);
    # `stack-merge` on the stack sub run's cut (tools/published.py).
    "improv-worker": ("work", "stack-merge", "await-landing"),
    "improv-leader": (),
}

# Every gate runs against a venv that is already there. A worker's worktree
# builds its venv once during the work ('uv sync --extra test'); after that,
# re-syncing inside a verify is a side effect that really did block the gate
# — sync cannot replace the claunch.exe a running daemon holds open (os error
# 5). So every one of them is --no-sync.
NO_SYNC = "uv run --no-sync python"

#: Commands that reach nothing of this project's own — the outside tools a
#: gate is allowed to call directly. Anything else is presumed to be this
#: tree, and has to be reached the way :data:`NO_SYNC` says.
_OUTSIDE_TOOLS = ("git",)


def _runs_project_code(command: str) -> bool:
    head = command.strip().split()
    return not (head and head[0] in _OUTSIDE_TOOLS)


def gate_commands(step) -> list:
    """Every command a step arms, whichever field holds it.

    Written once because the properties the tests below protect — reach into
    this checkout, never run a suite — are properties of *a command a gate
    runs*, and which field it was written in is not part of them. Reading
    ``.verify.command`` directly was what made the census blind the moment a
    gate moved to a ``checklist:``.
    """
    out = []
    if step.verify is not None:
        out.append(step.verify.command)
    if step.awaits is not None:
        command = step.awaits.command(step)
        if command:
            out.append(command)
    if step.checklist is not None:
        out.extend(item.check for item in step.checklist.items)
    return out


#: What each armed step's gate must invoke. The point of the table is that
#: none of these is a suite: the worker's picks the tests its own change can
#: affect, and the leader's two read a fact somebody else already established
#: (a sweep receipt, a daemon's boot time).
GATES = {
    # The one gate here that judges *where* the round is running rather than
    # what it produced, and the only one that runs before anything is made.
    # It is also the one that exists to keep another control point honest:
    # `workspace-check` lets the driving agent choose `match` and skip a human
    # approval, so the branch that choice routes to re-runs the same command
    # on the way out. Three axes, no suite -- two `git rev-parse` answers and
    # three daemon reads: the repository the issue's board and workspace name
    # point at, the repository the session was spawned into, and whether
    # another live session is standing in this same linked worktree.
    ("improv-worker", "workspace-ok"): "tools/workspace_check.py",
    ("improv-worker", "review"): "tools/changed_tests.py",
    # This one was twice wrong before it was a path like the other three.
    # First `claunch report check`: the PATH entry is whichever copy happens
    # to be installed, and it answered "invalid choice: 'report'" -- exit 2 in
    # every session until the branch landed and was reinstalled. Then
    # `-m claude_launcher.cli report check`, which looks like it runs this
    # tree and does not: a src layout cannot resolve `-m` from the working
    # directory, so it comes from site-packages, and --no-sync is a promise
    # that the worktree's .venv stays empty (measured: ModuleNotFoundError).
    # A path into this checkout is the only form that does not depend on what
    # is installed. tests/test_gates_run_this_checkout.py holds all six to it.
    ("improv-worker", "wrapup"): "tools/report_check.py",
    # Two git calls, no suite: "is there a merge commit on another branch with
    # my frozen tip as a parent?". Asked that way on purpose -- "does any
    # branch contain my tip" answers yes for a child stacked on it, which in a
    # nested formation is the normal shape, so containment reads as landed
    # when nothing landed.
    ("improv-worker", "landed"): "tools/landed_check.py",
    # The measurement the leader's rejection used to be. A worker files a
    # landing request, waits out a 300s batch window plus a sweep, and the
    # target moves while it sits there; the leader then measures the
    # divergence by hand and sends it back. Two git commands, spent as a
    # leader turn, a mesh round trip and two board transitions. The same
    # script answers it here and in the leader's preflight, so the two sides
    # cannot disagree about the same branch.
    ("improv-worker", "rebase"): "tools/merge_ready.py",
    # The only gate here that reads the daemon rather than the tree. The
    # worker's ending is put to the session above it, and a refusal routes to
    # end-hold -- but refusing is not what keeps the session: the daemon ends
    # a finished one-shot run's session unless keep_alive is set. So the step
    # tells the agent to set it, and this asks whether that happened. It has
    # to read, and `claunch keep-alive <session>` without `off` SETS the flag,
    # so the CLI cannot be the probe; GET /api/sessions/{name} carries it and
    # leaves it alone.
    ("improv-worker", "end-hold"): "tools/keepalive_check.py",
    ("improv-leader", "sweep"): "tools/sweep.py",
    ("improv-leader", "reflect"): "tools/deploy_check.py",
}


@pytest.mark.parametrize("stem", sorted(WATCHED))
def test_the_override_adds_no_other_awaits(stem):
    """The same census as ``ARMED``, for the field the daemon runs.

    An ``awaits`` is cheaper to add than a ``verify`` and easier to forget:
    nothing fails when one appears where it does not belong, it just puts a
    command on a 60-second clock in every session that reaches the step.
    """
    wf = model.load(OVERRIDES / f"{stem}.yaml")
    watched = [s.id for s in wf.steps.values() if s.awaits is not None]
    assert watched == list(WATCHED[stem])


@pytest.mark.parametrize("stem", sorted(CHECKED))
def test_the_override_adds_no_other_checklist(stem):
    """The same census for the field that also MOVES the run.

    A checklist is the most consequential of the three to add by accident: it
    takes the transition away from the agent entirely, so a step that grows
    one silently stops being a step anybody can advance.
    """
    wf = model.load(OVERRIDES / f"{stem}.yaml")
    checked = [s.id for s in wf.steps.values() if s.checklist is not None]
    assert checked == list(CHECKED[stem])


@pytest.mark.parametrize("stem", sorted(CHECKED))
def test_a_checklist_gate_has_no_other_way_out(stem):
    """The property that makes these two gates worth the change.

    A ``next:`` on a checklist step is refused by the parser, so this is not
    re-checking the schema: it pins that the two steps kept the shape after
    an edit, and that each item says in words what it is asserting — a
    checklist a person cannot read is the prose problem again with boxes
    drawn round it.
    """
    wf = model.load(OVERRIDES / f"{stem}.yaml")
    for step_id in CHECKED[stem]:
        step = wf.steps[step_id]
        assert step.next is None
        assert step.checklist.then != step_id
        assert step.checklist.items
        for item in step.checklist.items:
            assert item.describe.strip(), f"{stem}:{step_id}:{item.id} has no describe"


def test_the_waiting_probe_is_the_gate_the_worker_already_passed():
    """``await-landing`` re-measures what ``rebase`` gated, and that is the point.

    The worker leaves ``rebase`` green and then waits, frozen, while the batch
    window fills and the sweep runs. The same command on the same branch is
    what turns "it was ready when I asked" into "it is ready now" -- and the
    daemon speaks only when the exit code changes, so a branch that stays
    ready costs nothing at all.
    """
    wf = model.load(OVERRIDES / "improv-worker.yaml")
    step = wf.steps["await-landing"]
    assert step.awaits is not None and step.awaits.probe is not None, (
        "a select step takes no verify, so `awaits: verify` has nothing to "
        "re-measure here -- the probe has to be named"
    )
    assert step.awaits.command(step) == wf.steps["rebase"].verify.command
    assert step.awaits.describe, (
        "without it the daemon's signal shows the command, which is true and "
        "does not say what the waiting was about"
    )


@pytest.mark.parametrize("key, script", sorted(GATES.items()))
def test_each_armed_step_runs_its_gate_without_touching_the_environment(
    key, script
):
    stem, step_id = key
    wf = model.load(OVERRIDES / f"{stem}.yaml")
    assert wf.name == stem
    commands = gate_commands(wf.steps[step_id])
    assert commands, f"{stem}:{step_id} lost its gate"
    armed = [c for c in commands if script in c]
    assert armed, f"{stem}:{step_id} no longer runs {script}"
    for command in armed:
        assert command.startswith(NO_SYNC), (
            f"{stem}:{step_id} must run --no-sync: a gate that re-resolves the "
            f"venv fails on the claunch.exe a live daemon holds open"
        )


def test_the_worker_gate_targets_the_change_rather_than_the_suite():
    """The exact command, and the two arguments that make it targeted.

    ``--base`` is what turns "run tests" into "run the tests this branch's
    change can affect": without it there is no diff to select from and the
    script has nothing to narrow to.

    ``auto`` rather than ``master`` is the second half, and it is what the
    argument is worth on this formation's branches. A worker here is cut from
    an integration branch, and against master such a branch reads the whole
    batch as its own change -- 93 of 119 modules, 95 of 119, and 1945 tests in
    221s were all measured on one day, for rounds that had touched four to six
    files. The step's own prose forbids running the suite; with the base fixed
    at master, this line was how the suite got run (``claunch-eghh``).
    ``auto`` reads the branch's upstream, which is the ref
    ``tools/merge_ready.py`` already resolves to answer "what does this
    integrate into".
    """
    verify = model.load(OVERRIDES / "improv-worker.yaml").steps["review"].verify
    assert verify.command == (
        "uv run --no-sync python tools/changed_tests.py --base auto"
    )


def test_the_leader_sweep_gate_reads_a_receipt_rather_than_sweeping():
    """``check``, not ``run`` — the distinction the whole split rests on.

    ``tools/sweep.py`` has both halves in one file so the receipt format has
    one home, which means the gate is one word away from being the very
    blocking sweep it replaced. Pin the word.
    """
    verify = model.load(OVERRIDES / "improv-leader.yaml").steps["sweep"].verify
    assert verify.command == (
        "uv run --no-sync python tools/sweep.py check --branch master"
    )
    # Anchored to the script: a bare `" run "` also matches `uv run`, which
    # every one of these commands starts with.
    assert "sweep.py run" not in verify.command, (
        "the sweep gate must read a receipt, not run the suite in the "
        "leader's turn — that is the whole reason it stopped being pytest"
    )


def test_the_leader_override_gates_the_deploy_on_a_real_restart():
    """``reflect`` closes a round, so something has to check it happened.

    The step says "restart the live server and confirm it is serving the
    merge", and for as long as that was only a sentence the run could not
    tell a restart from no restart: no signal reaches a leader when the
    daemon comes back, so the round sat there collecting its 300-second
    reminders while a human eventually thought to check by hand.

    ``tools/deploy_check.py`` compares the daemon's recorded boot time with
    the tip it is meant to serve; ``tests/test_deploy_check.py`` pins its
    behaviour. What this test keeps is the wiring -- that the gate is armed
    on the step that ends the round, and reads the branch the leader merges
    to.
    """
    step = model.load(OVERRIDES / "improv-leader.yaml").steps["reflect"]
    assert step.checklist is not None, "the deploy gate is gone from reflect"
    commands = gate_commands(step)
    assert any("tools/deploy_check.py" in c for c in commands), commands
    assert any("--branch master" in c for c in commands), commands
    # And now it does more than refuse a green nobody earned: the daemon ends
    # the round on it, so the leader no longer sits collecting reminders while
    # the restart it is waiting for has already happened. The move goes by
    # landed-notice, which tells the landed sessions before the end
    # transition drops them from the queue (claunch-w9dvz).
    assert step.checklist.then == "landed-notice"
    notice = model.load(OVERRIDES / "improv-leader.yaml").steps["landed-notice"]
    assert notice.next is None  # `end`


def test_no_gate_calls_a_binary_off_PATH():
    """Every gate must run the tree it is checking, not whatever is installed.

    This is a rule the other three gates already followed without anyone
    writing it down: they name a path into this checkout and reach it through
    ``uv run``. The first gate that did not follow it proved why. It was
    ``claunch report check`` -- the obvious spelling -- and the ``claunch`` on
    PATH is an installed copy, not this tree, so it answered
    ``invalid choice: 'report'`` and exited 2. That gate could not have passed
    in any session until the branch landed and was reinstalled.

    ``ARMED``/``GATES`` almost cover this already, but only for steps somebody
    remembered to list there. This walks the files instead, so a gate added to
    the layer without touching either table is still held to the rule.

    A prefix is all this checks, and a prefix turned out not to be enough:
    ``uv run --no-sync python -m claude_launcher.cli ...`` starts with the
    right words and still resolves out of site-packages, and so does an
    absolute path into a different checkout. What the command has to *reach*
    is pinned next door, in tests/test_gates_run_this_checkout.py.
    """
    offenders = []
    for path in sorted(OVERRIDES.glob("*.yaml")):
        # A layer over the project copy of its base (improv-worker-remote)
        # is read as what it composes to, the way a run here reads it.
        wf = model.compose(
            path, resolve=state_mod.base_resolver(str(OVERRIDES.parents[1]))
        ).workflow
        for step_id, step in wf.steps.items():
            for command in gate_commands(step):
                if not _runs_project_code(command):
                    # `git diff --quiet` is not this rule's business: the
                    # failure it protects against is a command resolving to
                    # ANOTHER COPY OF THIS TREE, and a tool that is not this
                    # tree has no other copy to be confused with. Narrowed
                    # here rather than at the gate that hit it, because the
                    # alternative is writing `uv run --no-sync python -c
                    # "subprocess.run(['git', ...])"` to satisfy a check about
                    # something else entirely.
                    continue
                if not command.startswith(NO_SYNC):
                    offenders.append(f"{path.name}:{step_id} -> {command}")
    assert not offenders, (
        "a gate must run this checkout, not a binary off PATH. Reach it "
        f"with '{NO_SYNC} ...'. Measured: 'claunch report check' exited 2 "
        "with \"invalid choice: 'report'\" because PATH held an older "
        "install. Offending gates: " + "; ".join(offenders)
    )


@pytest.mark.parametrize("stem", sorted(ARMED))
def test_the_override_adds_no_other_verify(stem):
    """The override's whole diff against canonical is its machine checks —
    every other step stays verify-free exactly like the file it shadows."""
    wf = model.load(OVERRIDES / f"{stem}.yaml")
    armed = [s.id for s in wf.steps.values() if s.verify is not None]
    assert armed == list(ARMED[stem])


def test_the_leader_override_is_canonical_plus_its_grafted_fields():
    """Field for field the bundled leader, the grafted fields excluded.

    The override is a whole copy with a verify grafted on, so it goes stale
    silently the moment the canonical file is edited alone. It did: commit
    6dc8602 added the ``integrate-preflight`` step — the "who else is
    standing in this tree" check, and the rebase screening for drifted
    branches — to the canonical leader only. Every merge in THIS repository
    runs the override, so every merge for the days after it ran with no
    preflight step at all, and nothing was red: the two files simply said
    different things under one name. Comparing them is the only check that
    sees that, because each file on its own is valid.

    The worker pair drifted the same way and is deliberately NOT covered
    here yet — resyncing it would rewrite a workflow other sessions are
    mid-run on, so it is reported rather than fixed in passing.

    The excluded fields are the ones the project layer owns, and they are
    read from the tool that grafts them rather than restated here. Both answer
    "what does THIS repository check, with which tool" — a question the
    packaged copy, which ships everywhere, cannot answer — and the exclusion
    has to stay exactly that set: widen it and this stops being a drift check.
    A second hand-kept list would agree until somebody widened one, and that
    is the kind of disagreement no machine would have been watching.
    """
    from dataclasses import replace

    grafted = dict.fromkeys(_graft_fields())

    canonical = model.load(dict(state_mod.bundled_workflows())["improv-leader"])
    override = model.load(OVERRIDES / "improv-leader.yaml")

    assert override.name == canonical.name
    assert override.description == canonical.description
    assert override.start == canonical.start
    assert override.recur == canonical.recur
    assert override.default_role == canonical.default_role
    assert override.filter_roles == canonical.filter_roles
    assert list(override.steps) == list(canonical.steps), (
        "the override gained or lost a step against the canonical leader"
    )
    for step_id, canonical_step in canonical.steps.items():
        assert replace(override.steps[step_id], **grafted) == replace(
            canonical_step, **grafted
        ), (
            f"{step_id!r} differs from the canonical leader by more than its "
            f"grafted fields — the override drifted, and a run here would "
            f"follow the override's version of the rule"
        )


@pytest.mark.parametrize("key", sorted(GATES))
def test_no_override_verify_runs_a_test_suite(key):
    """The rule the rest of this file exists to protect.

    A ``verify`` is run by the engine, synchronously, as the run leaves the
    step. Put a suite there and you have a sweep that blocks the round,
    that six sessions start at once, and that nobody typed — so nobody
    records it either. Both of this repository's suites lived there once,
    and both prose halves said they did not: the worker step said "the full
    sweep is not run here" over a command running 93% of the suite, and the
    leader workflow said "the leader does not run sweeps in its turn" over a
    verify that ran the whole thing in exactly that turn.

    The prose is fixed now, but prose is what was already wrong. This is the
    machine half: no verify in either override may invoke pytest.
    """
    stem, step_id = key
    commands = gate_commands(model.load(OVERRIDES / f"{stem}.yaml").steps[step_id])
    command = "; ".join(commands)
    assert "pytest" not in command, (
        f"{stem}:{step_id} verify runs pytest ({command!r}). The engine runs "
        f"this synchronously on leaving the step, so a suite here is a "
        f"blocking sweep nobody typed. Targeted selection belongs in "
        f"tools/changed_tests.py; a full sweep belongs in a spawned subagent "
        f"via 'tools/sweep.py run', with this gate reading its receipt"
    )
    assert " -n " not in command, (
        f"{stem}:{step_id} verify passes -n ({command!r}): xdist width is a "
        f"property of running a suite, and these gates do not run one"
    )


def test_the_prose_forbids_the_suite_too_so_the_next_editor_reads_it():
    """Both halves say it, because only one of them said it last time.

    The commands are fixed above; this pins that a reader of either workflow
    is told *why* before they reach for pytest again. The worker's review
    step and the leader's sweep step are the two places the temptation
    lands.
    """
    worker = model.load(OVERRIDES / "improv-worker.yaml").steps["review"]
    assert "전체 스위트는 워커가 어떤 경로로도 돌리지 않는다" in worker.instructions
    assert "verify" in worker.instructions  # and that the ban covers the field

    sweep = model.load(OVERRIDES / "improv-leader.yaml").steps["sweep"]
    assert "spawn한 subagent 안에서 돈다" in sweep.instructions
    assert "subagent" in sweep.done_when


# --------------------------------------------------------------------------- #
# The naming rule an agent reads: a worker prefixes its own worktree/branch
# names with its session. The wizard path already does (``default_name``
# falls back to $CLAUNCH_SESSION), so the convention is prompting — it has to
# live where an agent learns it when it names a checkout itself: branch-setup
# of the worker workflow. A bare name like ``worktree-session-click-cache``
# minted inside a fleet of sessions is the accident these pin.
# --------------------------------------------------------------------------- #
def test_worker_override_branch_setup_insists_on_session_prefixed_names():
    """The override this repo runs tells a worker to prefix its own names."""
    text = model.load(OVERRIDES / "improv-worker.yaml").steps["branch-setup"].instructions
    assert "$CLAUNCH_SESSION" in text    # the prefix source is named
    assert "<세션>-<요지>" in text         # and its shape is spelled out


def test_bundled_worker_workflow_branch_setup_insists_on_session_prefixed_names():
    """A repo with no project override runs the bundled copy — same rule."""
    bundled = dict(state_mod.bundled_workflows())["improv-worker"]
    text = model.load(bundled).steps["branch-setup"].instructions
    assert "$CLAUNCH_SESSION" in text
    assert "<세션>-<요지>" in text
