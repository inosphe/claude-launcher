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
ARMED = {"improv-worker": ("review",), "improv-leader": ("sweep", "reflect")}

# Every gate runs against a venv that is already there. A worker's worktree
# builds its venv once during the work ('uv sync --extra test'); after that,
# re-syncing inside a verify is a side effect that really did block the gate
# — sync cannot replace the claunch.exe a running daemon holds open (os error
# 5). So every one of them is --no-sync.
NO_SYNC = "uv run --no-sync python"

#: What each armed step's verify must invoke. The point of the table is that
#: none of these is a suite: the worker's picks the tests its own change can
#: affect, and the leader's two read a fact somebody else already established
#: (a sweep receipt, a daemon's boot time).
GATES = {
    ("improv-worker", "review"): "tools/changed_tests.py",
    ("improv-leader", "sweep"): "tools/sweep.py",
    ("improv-leader", "reflect"): "tools/deploy_check.py",
}


@pytest.mark.parametrize("key, script", sorted(GATES.items()))
def test_each_armed_step_runs_its_gate_without_touching_the_environment(
    key, script
):
    stem, step_id = key
    wf = model.load(OVERRIDES / f"{stem}.yaml")
    assert wf.name == stem
    verify = wf.steps[step_id].verify
    assert verify is not None, f"{stem}:{step_id} lost its verify"
    assert verify.command.startswith(NO_SYNC), (
        f"{stem}:{step_id} must run --no-sync: a gate that re-resolves the "
        f"venv fails on the claunch.exe a live daemon holds open"
    )
    assert script in verify.command


def test_the_worker_gate_targets_the_change_rather_than_the_suite():
    """The exact command, and the argument that makes it targeted.

    ``--base`` is what turns "run tests" into "run the tests this branch's
    change can affect": without it there is no diff to select from and the
    script has nothing to narrow to.
    """
    verify = model.load(OVERRIDES / "improv-worker.yaml").steps["review"].verify
    assert verify.command == (
        "uv run --no-sync python tools/changed_tests.py --base master"
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
    verify = model.load(OVERRIDES / "improv-leader.yaml").steps["reflect"].verify
    assert verify is not None, "the deploy gate is gone from reflect"
    assert "tools/deploy_check.py" in verify.command
    assert "--branch master" in verify.command


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
    command = model.load(OVERRIDES / f"{stem}.yaml").steps[step_id].verify.command
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
# live where an agent learns it when it names a checkout itself: the intake
# of the worker workflow. A bare name like ``worktree-session-click-cache``
# minted inside a fleet of sessions is the accident these pin.
# --------------------------------------------------------------------------- #
def test_worker_override_intake_insists_on_session_prefixed_names():
    """The override this repo runs tells a worker to prefix its own names."""
    text = model.load(OVERRIDES / "improv-worker.yaml").steps["intake"].instructions
    assert "$CLAUNCH_SESSION" in text    # the prefix source is named
    assert "<세션>-<요지>" in text         # and its shape is spelled out


def test_bundled_worker_workflow_intake_insists_on_session_prefixed_names():
    """A repo with no project override runs the bundled copy — same rule."""
    bundled = dict(state_mod.bundled_workflows())["improv-worker"]
    text = model.load(bundled).steps["intake"].instructions
    assert "$CLAUNCH_SESSION" in text
    assert "<세션>-<요지>" in text
