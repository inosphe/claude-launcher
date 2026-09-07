"""Where a workflow name resolves, and who gets told which file won.

Two layers answer a name — the project's ``.claunch/workflows/`` and the
global ``~/.claude-launcher/workflows/`` — and the nearest one wins. That was
always true and never tested; what is new is that the global layer is
actually populated (by ``claunch install --global``, and by ``cflow add``),
so the same name really can exist twice. These tests pin both halves: the
resolution, and the reporting of what resolution passed over.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_launcher import cli, install as install_mod
from claude_launcher.cflow import (
    engine,
    install as cflow_install,
    model,
    state as state_mod,
)

#: This repository's own project-layer overrides — the files that shadow the
#: bundled improv pair for every run in this checkout.
PROJECT_OVERRIDES = Path(__file__).resolve().parents[1] / ".claunch" / "workflows"

TINY = """
name: {name}
description: {desc}
steps:
  only:
    instructions: do the thing
"""


def _write(path, name="tiny", desc="a workflow"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TINY.format(name=name, desc=desc), encoding="utf-8")
    return path


@pytest.fixture
def project(tmp_path, monkeypatch, home):
    """A project directory, with the global layer pointed somewhere throwaway."""
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.chdir(proj)
    return proj


@pytest.fixture
def monkeypatch_stdin(monkeypatch):
    """Feed a string (or EOF) to ``input``, the way a live prompt would read it."""
    holder = {}

    def feed(text):
        holder["text"] = text

    monkeypatch.setattr("builtins.input", lambda prompt="": holder.get("text", ""))
    return feed


# --------------------------------------------------------------------------- #
# resolution
# --------------------------------------------------------------------------- #
def test_the_global_layer_answers_when_the_project_does_not(project, home):
    _write(home / "workflows" / "tiny.yaml")
    found = state_mod.locate("tiny")
    assert found.path == home / "workflows" / "tiny.yaml"
    assert found.origin == state_mod.LAYER_GLOBAL
    assert found.shadows == ()


def test_the_project_wins_and_says_what_it_beat(project, home):
    shared = _write(home / "workflows" / "tiny.yaml", desc="the shared one")
    mine = _write(project / ".claunch" / "workflows" / "tiny.yaml", desc="mine")

    found = state_mod.locate("tiny")
    assert found.path == mine
    assert found.origin == state_mod.LAYER_PROJECT
    # The point of the whole exercise: the loser is named, not dropped.
    assert found.shadows == (shared,)
    assert found.overrides


def test_listing_carries_the_same_answer_as_resolving(project, home):
    _write(home / "workflows" / "tiny.yaml")
    _write(home / "workflows" / "shared-only.yaml", name="shared-only")
    mine = _write(project / ".claunch" / "workflows" / "tiny.yaml")

    listed = {w.name: w for w in state_mod.resolved_workflows()}
    assert set(listed) == {"tiny", "shared-only"}
    assert listed["tiny"].path == mine
    assert listed["tiny"].shadows == (home / "workflows" / "tiny.yaml",)
    assert listed["shared-only"].shadows == ()
    # the old pair-shaped view still agrees with the rich one
    assert dict(state_mod.list_workflows())["tiny"] == mine


def test_an_explicit_path_belongs_to_no_layer(project, home):
    path = _write(project / "elsewhere" / "tiny.yaml")
    found = state_mod.locate(str(path))
    assert found.path == path
    assert found.origin == state_mod.LAYER_FILE


def test_an_unknown_name_names_both_layers_it_looked_in(project, home):
    with pytest.raises(model.WorkflowError) as exc:
        state_mod.locate("nope")
    message = str(exc.value)
    assert str(project / ".claunch" / "workflows") in message
    assert str(home / "workflows") in message


# --------------------------------------------------------------------------- #
# what ships, and how it gets to the global layer
# --------------------------------------------------------------------------- #
def test_the_packaged_workflows_are_valid_and_current():
    """A shipped default is read as a teaching example; it must not be stale.

    ``feature-dev`` used to exist twice — a string in ``cflow/install.py`` and
    a file in this checkout's ``.claunch/`` — and the two taught different
    syntax. One file now, and it may not teach a deprecated form.
    """
    bundled = dict(state_mod.bundled_workflows())
    assert "feature-dev" in bundled and "delegated-dev" in bundled
    for name, path in bundled.items():
        wf = model.load(path)
        assert wf.name == name
        assert wf.step_count() > 0
        assert not wf.deprecations, f"{name} teaches a deprecated form"


def test_the_worker_workflow_keeps_its_branch_setup_isolation_rules():
    """``improv-worker`` creates a fresh branch after task overview.

    ``intake`` establishes the goal and supplies the branch summary. Every
    path that can start implementation then passes through ``branch-setup``;
    it names the fresh branch and isolates a shared checkout in a worktree.
    """
    bundled = dict(state_mod.bundled_workflows())
    worker = model.load(bundled["improv-worker"])
    intake = worker.steps["intake"]
    branch_setup = worker.steps["branch-setup"]
    assert "브랜치 요지" in intake.instructions
    for anchor in (
        "새 피처 브랜치",                      # new feature -> new branch
        "새 워크트리",                         # shared checkout -> new worktree
        "git rev-parse --show-toplevel",       # primary determination
        "--git-common-dir",                    # fallback when parent cwd is unknown
        "$CLAUNCH_SESSION",                    # how a worker names itself
    ):
        assert anchor in branch_setup.instructions, f"branch-setup lost its {anchor!r} rule"
    assert branch_setup.next == "work"
    options = worker.steps["issue-check"].select.options
    assert options["claimed"].next == "branch-setup"
    # Both halves of the "no issue" answer still reach branch-setup: the one
    # that works the opening task goes straight there, and the one that picks
    # its own issue off the board passes through issue-auto first. Neither may
    # start implementing without a branch of its own.
    assert options["no-issue-wait"].next == "branch-setup"
    assert options["no-issue-auto"].next == "issue-auto"
    assert worker.steps["issue-auto"].next == "branch-setup"
    assert worker.steps["issue-decision"].select.options["none"].next == "branch-setup"
    assert worker.steps["issue-claim"].next == "branch-setup"


def test_the_improv_workflows_carry_no_repo_specific_verify():
    """The improv pair ships to every repository; a verify would not.

    A suite command is a property of one repository, and a canonical
    ``verify`` hard-codes it for all of them — a run in any other repo
    blocks on a command that cannot exit 0 there. The policy is layering:
    the canonical files carry none, and a repository that wants a machine
    check overrides in its project layer (``.claunch/workflows/``).
    """
    bundled = dict(state_mod.bundled_workflows())
    for name in ("improv-worker", "improv-leader", "improv-mid"):
        wf = model.load(bundled[name])
        for step_id, step in wf.steps.items():
            assert step.verify is None, (
                f"{name}:{step_id} carries a verify — repo-specific commands "
                "belong in the project layer"
            )


def test_the_bundled_improv_worker_teaches_the_nested_merge_contract():
    """A worker that spawned sub-workers folds their branches upward as a
    tree: each finished child branch is collected with a ``--no-ff`` merge
    into the worker's own branch, and integration is then *requested* from
    the parent session (leader -> master review, worker -> --no-ff merge
    into its branch) — the worker never merges master itself. The bundled
    file is the teaching copy of that contract; this pins it."""
    bundled = dict(state_mod.bundled_workflows())
    wf = model.load(bundled["improv-worker"])

    collect = wf.steps["commit"].instructions
    assert "자식" in collect and "--no-ff" in collect
    assert "트리" in collect

    landing = wf.steps["landing"].select
    assert set(landing.options) == {"request", "escalate"}
    review = wf.steps["landing-review"].select
    assert "상위" in review.prompt
    assert set(review.options) == {"request", "hold"}

    request = wf.steps["integration-request"].instructions
    assert "상위 세션" in request and "--no-ff" in request
    assert "master를 직접 머지하지 않는다" in request


def test_the_worker_rebases_onto_the_target_before_a_request():
    """A merge request follows a rebase onto the integration target.

    The request path routes through a dedicated ``rebase`` step (a normal
    pull-request flow: align, then request). The step must say how the
    divergence is judged, that a realigned branch re-runs the simplified suite
    before asking, and that rebasing is not an exception to the master
    prohibition.

    How it is judged used to be two git commands written out here in prose,
    and the same two were written out again in the leader's preflight. That
    is the shape this now refuses: the prose named the commands, nothing ran
    them, and the thing actually judging was the leader's rejection — a
    measurement paid for with a turn and a round trip. Both sides now name one
    script, and ``test_both_sides_judge_a_landing_with_the_same_command``
    holds them to naming the same one.

    ``peer-review`` sits between the rebase and the request, and belongs
    there: the reviewer must read the tree that gets merged, not the one
    before the rebase resolved its conflicts. What this pins is that nothing
    between them commits — the review's only other exit is back to ``work``,
    which reaches the request through ``rebase`` again.
    """
    bundled = dict(state_mod.bundled_workflows())
    wf = model.load(bundled["improv-worker"])

    request = wf.steps["landing"].select.options["request"]
    assert request.next == "rebase"

    rebase = wf.steps["rebase"]
    assert rebase.next == "peer-review"
    review = wf.steps["peer-review"].select.options
    assert review["pass"].next == "integration-request"
    assert review["changes"].next == "work"
    for anchor in (
        "pull request",
        "merge_ready.py",  # 판정식은 한 자리에만 있다
        "다시 돌려",  # 재정렬 후 간소화 스위트 재확인
        "master를 직접 머지하지 않는다",
    ):
        assert anchor in rebase.instructions, f"rebase lost its {anchor!r} rule"
    # The split is the step's whole point: only a conflict forces a rebase,
    # and a moved baseline is settled by re-measuring. Fold them back into one
    # and the friction this step was rewritten to remove comes straight back.
    assert "재측정" in rebase.instructions
    assert "merge --no-ff" in rebase.instructions, (
        "the cheaper remedy has to be spelled out where the verdict is read, "
        "or the worker rebases by default and the concession buys nothing"
    )

    for anchor in ("rebase", "재요청"):
        assert anchor in wf.steps["integration-request"].instructions, (
            f"integration-request lost its {anchor!r} rule"
        )


def _peer_review_candidates(path_or_text):
    return model.load(path_or_text).steps["peer-review"].select.delegate.candidates


def test_peer_review_wires_itself_to_the_reviewer_it_cannot_reach():
    """The reviewer group must not be empty by default.

    A spawned session is wired to its parent and to nobody else, so a worker
    cannot reach a sibling reviewer: the first group is skipped for want of an
    edge and the decision falls to the leader — the same session that receives
    the landing request, which is what this door exists to happen before. The
    declaration makes the edge rather than skipping the group.

    The second group must NOT carry it. Landing is an authority decision and
    is held to `scope: ancestor`; building a path out of the run's own chain
    of command is the thing that scope refuses.
    """
    bundled = dict(state_mod.bundled_workflows())
    reviewer, leader = _peer_review_candidates(bundled["improv-worker"])

    assert (reviewer.role, reviewer.connect) == ("reviewer", True)
    assert (leader.role, leader.scope) == ("leader", "ancestor")
    assert leader.connect is False, (
        "a decision reserved for the chain of command must not manufacture a "
        "path to somebody outside it"
    )

    prose = model.load(bundled["improv-worker"]).steps["peer-review"].select.prompt
    assert prose, "the peer-review prompt is what the responder reads"


def test_the_project_override_carries_the_peer_review_wiring():
    """This repository's override shadows the bundled worker, so a run here
    follows the project file — the declaration has to survive the layer or the
    fix is only true for repositories that have no override."""
    reviewer, leader = _peer_review_candidates(PROJECT_OVERRIDES / "improv-worker.yaml")
    assert (reviewer.role, reviewer.connect) == ("reviewer", True)
    assert leader.connect is False


def test_the_project_override_worker_requests_through_a_rebase():
    """This repository's override must carry the rebase-before-request
    routing of the bundled worker it shadows — a run here that follows the
    project file must hit the same regardless of layer."""
    wf = model.load(PROJECT_OVERRIDES / "improv-worker.yaml")

    request = wf.steps["landing"].select.options["request"]
    assert request.next == "rebase"
    assert wf.steps["rebase"].next == "peer-review"
    assert (
        wf.steps["peer-review"].select.options["pass"].next == "integration-request"
    )
    for anchor in ("rebase", "merge_ready.py", "재측정", "다시 돌려"):
        assert anchor in wf.steps["rebase"].instructions, (
            f"project worker rebase lost its {anchor!r} rule"
        )
    assert "재요청" in wf.steps["integration-request"].instructions


def test_a_worker_round_is_not_done_until_the_merge_is_confirmed():
    """"Requested" is not "landed", and the round must not end on the former.

    ``integration-request`` used to route straight to ``wrapup``, which tells
    the session to run ``kill-session`` in the same turn it files its report.
    So the run reached ``done``, and the session ceased to exist, while the
    branch was still queued in the parent -- and a request that got rejected,
    got a rebase asked of it, or was quietly dropped from a batch had nobody
    left to notice. Measured more than once, the last time as a worker
    reporting "통합 대기 중 ... run done. 세션 종료한다".

    Two steps close it in both layers. ``await-landing`` is where the run
    waits (an agent choice, because the two things that arrive -- a merge
    notice or a rebase re-request -- are both real and only the agent sees
    them), and ``landed`` is the machine half. They are separate steps because
    the engine forbids a ``verify`` on a select step, so the gate needs a step
    of its own; ``rebase`` is reachable again from the wait, which is the edge
    the re-request prose always claimed and never had.
    """
    for label, wf in (
        ("bundled", _bundled("improv-worker")),
        ("project", model.load(PROJECT_OVERRIDES / "improv-worker.yaml")),
    ):
        assert wf.steps["integration-request"].next == "await-landing", (
            f"{label}: a filed request goes to the wait, not to wrapup -- "
            "routing it to wrapup is what let a round end at 'requested'"
        )

        wait = wf.steps["await-landing"]
        assert wait.select is not None, f"{label}: await-landing must be a select"
        assert wait.select.chooser == "agent"
        assert wait.select.options["landed"].next == "landed"
        assert wait.select.options["rebase"].next == "rebase", (
            f"{label}: a rebase re-request must be able to reach the rebase "
            "step again, or the prose promising it is a dead end"
        )

        landed = wf.steps["landed"]
        assert landed.select is None, (
            f"{label}: landed carries the machine gate, so it cannot be a "
            "select -- a select routes via its options"
        )
        # The gate moved from a `verify` the agent advanced past to a
        # `checklist:` the daemon advances: same question, and now the answer
        # is a list of exit codes a person can read instead of the agent's
        # account of them. `then` is the step's only exit, so `next` is gone
        # -- an agent exit here would be a way past a condition that is false.
        assert landed.next is None
        assert landed.checklist is not None, (
            f"{label}: landed lost its checklist gate"
        )
        assert landed.checklist.then == "wrapup"
        merged = landed.checklist.item("merged")
        assert merged is not None, (
            f"{label}: landed must still ask whether a merge took this branch in"
        )
        # The rule that survived the move: ask for a MERGE PARENT, never for
        # containment. A child branch stacked on this tip contains it and has
        # integrated nothing, which in a nested formation is the normal shape
        # -- so containment reads as landed when nothing landed. The question
        # now lives in the item's own words and in the script it names, and
        # tools/landed_check.py is where it is actually asked.
        assert "머지 커밋" in merged.describe and "부모" in merged.describe, (
            f"{label}: the merged item must state the merge-parent question, "
            f"not a containment one -- got {merged.describe!r}"
        )
        assert "landed_check.py" in merged.check
        assert "master를 직접 머지하지 않는다" in landed.instructions, (
            f"{label}: landed must forbid turning its own gate green by "
            "merging master, which is the one way to 'pass' it dishonestly"
        )
        # A request is still not a landing. That distinction used to live in
        # done_when because done_when was the only thing standing between the
        # two; it is now the gate itself, so done_when asks for the half no
        # command can answer and the checklist answers the rest.
        assert "머지" in landed.done_when

    for wf in (
        _bundled("improv-worker"),
        model.load(PROJECT_OVERRIDES / "improv-worker.yaml"),
    ):
        assert wf.steps["landing-review"].select.options["hold"].next == "wrapup"


def test_the_worker_end_is_gated_by_a_user_without_a_timeout():
    """Ending a worker session waits for the user's explicit approval.

    The daemon reaps a finished one-shot run's session on sight of ``done``
    (``daemon/cflow_clock.py`` kill-on-end), so ``done`` IS the kill. Before
    this gate the worker took that decision alone and its peers found out by
    sending into a closed terminal. Both layers carry it, or the same name
    runs two policies.

    ``queue-recheck`` sits between wrapup and the gate: it loops the run back
    to ``intake`` while the board still holds assigned work, and its ``done``
    branch leads to ``settle-check`` -- a machine confirmation that every
    issue this session created is in somebody's hands (a live assignee or a
    leader's ``HOLD:``) before ``end-gate`` is reached at all (claunch-380z:
    the queue being empty is not the same fact as the session's own filings
    being seen). The gate therefore still stands between every ending and the
    kill -- a loop is not an ending, and neither is an unsettled filing.
    """
    for label, wf in (
        ("bundled", _bundled("improv-worker")),
        ("project", model.load(PROJECT_OVERRIDES / "improv-worker.yaml")),
    ):
        assert wf.steps["wrapup"].next == "queue-recheck", (
            f"{label}: wrapup must re-read the queue before the ending"
        )
        recheck = wf.steps["queue-recheck"].select
        assert recheck is not None and recheck.options["done"].next == "settle-check", (
            f"{label}: queue-recheck's done branch must reach the settlement check"
        )
        assert recheck.options["next-round"].next == "intake", (
            f"{label}: queue-recheck's loop must start a new round at intake"
        )
        settle = wf.steps["settle-check"].select
        assert settle is not None and settle.options["settled"].next == "end-gate", (
            f"{label}: settle-check's settled branch must reach the user gate"
        )
        assert settle.options["unsettled"].next == "settle-wait", (
            f"{label}: an unsettled filing must wait on the leader, not skip to the gate"
        )
        wait = wf.steps["settle-wait"].select
        assert wait.options["acted"].next == "settle-check", (
            f"{label}: settle-wait must loop back to the mechanical check, not trust the leader's word"
        )
        assert wait.delegate.otherwise == model.OTHERWISE_HUMAN, (
            f"{label}: settle-wait must fall through to a person when the leader times out"
        )
        # No step other than the gate and its hold reaches END: every ending
        # passes the user's approval.
        enders = sorted(
            sid for sid, step in wf.steps.items()
            if step.successors() == [] and sid not in ("end-gate", "end-hold")
        )
        assert enders == [], f"{label}: {enders} reach END around the user gate"
        gate = wf.steps["end-gate"].ask
        assert gate is not None, f"{label}: end-gate carries no ask"
        assert gate.delegate.candidates == [], (
            f"{label}: no session role may approve the user's ending decision"
        )
        assert gate.delegate.otherwise == model.OTHERWISE_HUMAN, (
            f"{label}: the ending must wait for a user"
        )
        assert gate.delegate.timeout is None, (
            f"{label}: the user approval gate must not expire"
        )
        # A refusal has somewhere to go: the session stays up under
        # keep-alive rather than the run ending anyway.
        assert gate.on_decline == "end-hold", (
            f"{label}: a declined ending falls through to the kill it declined"
        )
        hold = wf.steps["end-hold"].instructions
        assert "claunch keep-alive $CLAUNCH_SESSION" in hold, (
            f"{label}: end-hold must set the flag that actually stops the "
            "daemon from ending the session — without it the run reaches done "
            "and the kill happens regardless of the refusal"
        )
        assert wf.steps["end-gate"].next is None    # END
        assert wf.steps["end-hold"].next is None    # END


def test_the_refusal_to_end_is_armed_by_this_repository():
    """``end-hold``'s refusal has to be checkable, and here it is checked.

    The daemon ends a finished one-shot run's session unless ``keep_alive``
    is set, so a refusal that only tells the agent to set the flag is inert
    if the agent does not. The check is an exit code in the project layer
    (``tools/keepalive_check.py``), which is what the layer is for: the
    packaged copy ships to every repository and cannot name a tool that
    lives only in this one. So the two copies differ HERE on purpose, and
    the assertion is written in both directions rather than one — a canon
    that grew its own ``verify`` would collide with the graft.
    """
    project = model.load(PROJECT_OVERRIDES / "improv-worker.yaml")
    verify = project.steps["end-hold"].verify
    assert verify is not None, (
        "the project layer stopped arming the refusal — end-hold is back to "
        "judging its own prose, and a declined ending ends the session anyway"
    )
    assert "tools/keepalive_check.py" in verify.command

    assert _bundled("improv-worker").steps["end-hold"].verify is None, (
        "the packaged copy grew a verify; sync_project_layer grafts the "
        "project layer's on top and two 'verify:' keys in one step is a "
        "parse error, not a merge"
    )



def test_the_worker_wrapup_no_longer_says_landing_does_not_matter():
    """The prose that contradicted the new gate, pinned so it stays gone.

    ``wrapup`` lists the excuses a round uses to put off killing its session
    and answers each. One of them was "I will confirm the merge landed first",
    and the answer was "착지 확인은 이 회차의 완료 조건이 아니다" -- which is
    exactly the rule that has now been reversed. Leaving it would have left
    the file arguing with itself, and the losing half is the one a reader
    obeys because it sits in the step they are actually in.
    """
    for label, wf in (
        ("bundled", _bundled("improv-worker")),
        ("project", model.load(PROJECT_OVERRIDES / "improv-worker.yaml")),
    ):
        wrapup = wf.steps["wrapup"].instructions
        assert "착지 확인은 이 회차의 완료" not in wrapup, (
            f"{label}: wrapup still says landing confirmation is not a "
            "completion condition, which the landed step made false"
        )
        assert "landed 스텝" in wrapup, (
            f"{label}: wrapup should point at the landed step it now sits "
            "behind, so the reader knows the confirmation already happened"
        )
        # The corollary of the mechanical end: the agent's wrapup no longer
        # runs the kill — that is the "가장 흔한 미완료" the daemon-side
        # kill-on-end exists to close, and an instruction that survives here
        # would make the agent race the daemon. The one lever that stays in
        # prose is the keep-alive exception.
        assert "claunch kill-session $CLAUNCH_SESSION" not in wrapup, (
            f"{label}: wrapup still tells the agent to kill its own session — "
            "the daemon ends a finished one-shot run mechanically (record, "
            "then terminate), and this instruction was the most common "
            "incompletion it replaces"
        )
        assert "keep-alive" in wrapup, (
            f"{label}: wrapup should name the keep-alive lever for the one "
            "exception — a user explicitly says not to close the session"
        )


def test_the_leader_requests_a_rebase_when_a_branch_has_drifted():
    """A merge request whose branch has drifted far from master is not merged
    — the leader sends it back for a rebase, like a PR that needs an update.
    The screening is the shared gate rather than two git commands written out
    here (and written out again in the worker), and the refusal names the
    escape instead of silently merging. There are two escapes now, not one:
    a conflict has to be rebased, a moved baseline only has to be re-measured,
    and giving both the same name is what made a worker rebase for a demand
    nobody needed."""
    bundled = dict(state_mod.bundled_workflows())
    leader = model.load(bundled["improv-leader"])

    preflight = leader.steps["integrate-preflight"]
    for anchor in (
        "merge_ready.py",
        "REBASE REQUESTED",
        "REMEASURE REQUESTED",
        "재요청",
    ):
        assert anchor in preflight.instructions, (
            f"integrate-preflight lost its {anchor!r} rule"
        )
    assert "재요청" in leader.steps["standby"].instructions
    assert "재요청" in leader.steps["integrate"].instructions


def test_the_project_override_leader_requests_a_rebase_for_stale_branches():
    """The project-layer leader override screens the same way — a repository
    that shadows the bundled leader must not merge a drifted branch either.

    The screening reads in ``integrate-preflight``, which is where the
    bundled leader keeps it. It did not always: the override was copied
    before that step existed and then edited alongside the canonical file
    without it, so for days this repository merged with no preflight at all
    while this test stayed green against the old copy's ``integrate``. The
    override is a full resync now — see
    ``test_project_layer_override.test_the_leader_override_is_canonical_plus_verify``
    — so the pin follows the rule to the step that holds it.
    """
    wf = model.load(PROJECT_OVERRIDES / "improv-leader.yaml")

    preflight = wf.steps["integrate-preflight"]
    for anchor in ("merge_ready.py", "REBASE REQUESTED", "REMEASURE REQUESTED"):
        assert anchor in preflight.instructions, (
            f"project leader integrate-preflight lost its {anchor!r} rule"
        )
    assert "재요청" in wf.steps["standby"].instructions
    assert "재요청" in wf.steps["integrate"].instructions


@pytest.mark.parametrize("layer", ["bundled", "project"])
def test_the_leader_delegates_every_sweep_and_merge_to_a_subagent(layer):
    """The leader judges; a spawned subagent executes. Both layers say so.

    A leader session's context is the control room — the tally table, the
    rules it has settled, who is waiting on what. A full sweep's output and
    a merge's diff are the two largest things that can land in it, and what
    they evict is exactly that state. So the two executions this workflow
    owns are delegated: the leader decides *what* to merge and *which*
    sweep to run, spawns a subagent to run it, and keeps only the numbers
    that come back.

    Which makes the returned text the weak point — a subagent's report is
    prose, and prose is not evidence of a merge. The rule therefore carries
    its own re-check (``git rev-parse HEAD`` and friends): the leader reads
    the tree itself before writing a hash into a report.

    The route this replaces was a resident ``tester`` session, spawned over
    the mesh to answer sweep requests. Sweeps are one-shot work; a session
    is a slot, a mesh wiring, and an idle-state to manage, all of which came
    back to the leader. Both layers are pinned because this repository runs
    the override, not the bundled file.
    """
    if layer == "bundled":
        wf = model.load(dict(state_mod.bundled_workflows())["improv-leader"])
    else:
        wf = model.load(PROJECT_OVERRIDES / "improv-leader.yaml")

    integrate = wf.steps["integrate"].instructions
    standby = wf.steps["standby"].instructions

    assert "한 subagent = 한 일" in integrate, (
        "the leader's merge step lost the one-job-per-subagent rule"
    )
    assert "git rev-parse HEAD" in integrate, (
        "nothing tells the leader to re-check what the subagent claims it did"
    )
    for step_id, text in (("integrate", integrate), ("standby", standby)):
        assert "subagent" in text, f"{step_id} lost the subagent rule"
        assert "tester에게" not in text, (
            f"{step_id} still routes work to a resident tester session"
        )


def test_the_leader_does_not_hide_a_human_decision_behind_an_agent_chooser():
    """``standby``'s exit must be a fact the agent can see for itself.

    ``chooser: agent`` is read by the engine as *the agent is working*: no
    ``waiting_selection``, no dashboard button, no run event, nothing that
    tells a human a decision is pending — while the reminder clock keeps
    typing "if you are mid-work, keep going". So a human instruction is the
    one thing such a select may not wait on. It did once, and a leader run
    sat on it for ninety minutes with six finished branches behind it.

    The trigger is now evidence, and the master gate no longer routes to a
    person: ``integrate``'s ``ask`` is ``otherwise: self`` with an empty
    ``from`` — nobody is asked and the engine journals the entry as
    *self-decided* (``ask_unanswered_proceeded``, never an ``approval``).
    This is the user-approved reversal of the older pure-user gate: master
    is guarded by the machine full-sweep verify on ``integrate`` (project
    layer, pinned in ``test_project_layer_override``) and by the journaled
    self-decision, not by a waiting human. Both halves are pinned here — the
    evidence trigger, and that the leader genuinely self-decides the merge.
    """
    bundled = dict(state_mod.bundled_workflows())
    wf = model.load(bundled["improv-leader"])

    standby = wf.steps["standby"]
    assert standby.select.chooser == "agent"
    for text in (standby.select.prompt, standby.instructions):
        assert "사용자의 지시" not in text, (
            "standby waits on a user instruction behind an agent chooser — "
            "nothing surfaces that wait to a human"
        )
    # ...and what it waits on instead is the evidence the next step judges.
    assert "증거" in standby.select.options["integrate"].description

    gate = wf.steps["integrate"].ask
    assert gate is not None
    assert not gate.delegate.candidates, "a self-decision must ask nobody"
    assert gate.delegate.otherwise == model.OTHERWISE_SELF, (
        "the merge gate must be the leader's self-decision, not a human gate"
    )
    # The deploy gate is self-decided alongside the merge...
    assert wf.steps["reflect"].ask.delegate.otherwise == model.OTHERWISE_SELF
    # ...and the reversal is scoped: the shift-end gate stays a human's call.
    assert wf.steps["wrapup"].ask.delegate.otherwise == model.OTHERWISE_HUMAN


def test_the_project_override_leader_gates_are_self_decided():
    """The project-layer leader must carry the same gate reversal.

    This repository's override shadows the bundled leader for every run here
    (``PROJECT_OVERRIDES`` wins by layer precedence), so a repository that
    drops the user approval from ``integrate``/``reflect`` only in the bundled
    copy but keeps it in the override would run two different policies from
    the same name. Mirror the contract: merge and deploy are the leader's
    ``otherwise: self``, and the shift-end gate stays a human's call.
    """
    wf = model.load(PROJECT_OVERRIDES / "improv-leader.yaml")

    integrate = wf.steps["integrate"].ask
    assert integrate is not None
    assert not integrate.delegate.candidates, "a self-decision must ask nobody"
    assert integrate.delegate.otherwise == model.OTHERWISE_SELF
    assert wf.steps["reflect"].ask.delegate.otherwise == model.OTHERWISE_SELF
    assert wf.steps["wrapup"].ask.delegate.otherwise == model.OTHERWISE_HUMAN


@pytest.mark.parametrize("layer", ["bundled", "project"])
def test_the_leader_loop_is_started_by_the_daemon_not_by_the_driver(layer):
    """``improv-leader`` recurs with ``auto``, in both copies of the name.

    Plain ``recur: true`` leaves the next round's ``start`` to the driving
    agent. This workflow cannot pay that: its last step, ``reflect``,
    restarts the live daemon, and the daemon takes every attached terminal
    down with it — the turn that would close the round dies with it. What is
    left behind is a run at ``done``, and ``done`` is the one position no
    clock pokes: ``cflow_clock``'s ``_ACTIONABLE`` is ``step`` and
    ``select``, so neither the reminder nor the stall ping reaches it, and
    the ``round-done`` event goes to the driver's overseer rather than to the
    driver — which a leader does not have. The loop then stops until a person
    calls ``start`` by hand, which is what happened between rounds 12 and 13.

    ``auto`` fills both halves: ``RoundStartClock`` performs the start, and
    its ``round_block`` wakes an idle session (its ``_deliver`` has no busy
    check, unlike the reminder's).

    Both layers are pinned for the reason the gate test above states: this
    repository's override shadows the bundled copy for every run here, so the
    two carrying different recurrence would be two policies under one name.
    """
    if layer == "bundled":
        wf = model.load(dict(state_mod.bundled_workflows())["improv-leader"])
    else:
        wf = model.load(PROJECT_OVERRIDES / "improv-leader.yaml")

    assert wf.recur, "the leader is a service loop"
    assert wf.recur_auto, (
        "the leader's rounds must be started by the daemon: its last step "
        "restarts the daemon, so no turn survives to perform the start"
    )


def test_a_global_install_seeds_the_global_layer_and_reinstalls_keep_an_edit(
    project, home
):
    """One install seeds the layer; the next reports it up to date (a line,
    not silence -- silence reads as 'did it skip the workflows?'); an edited
    copy is kept and said so; only ``--force`` replaces it."""
    lines = install_mod.install_into_user()
    assert [line for line in lines if line.startswith("workflow ->")]
    for name, _ in state_mod.bundled_workflows():
        assert (home / "workflows" / f"{name}.yaml").is_file()
    # and they are findable from a project that declares nothing itself
    assert "feature-dev" in dict(state_mod.list_workflows())

    lines = install_mod.install_into_user()
    assert not [line for line in lines if line.startswith("workflow ->")]
    assert any(line.startswith("workflow layer -> up to date") for line in lines)

    edited = home / "workflows" / "feature-dev.yaml"
    edited.write_text("name: mine\nsteps:\n  only:\n    instructions: x\n", "utf-8")
    lines = install_mod.install_into_user()
    assert edited.read_text(encoding="utf-8").startswith("name: mine")
    assert any("kept; yours differs" in line for line in lines)

    cflow_install.seed_global_workflows(force=True)
    assert model.load(edited).name == "feature-dev"


def test_a_project_install_stays_inside_the_project(project, home):
    """An install writes only inside its scope — the machine layer is the
    global (and profile) install's business."""
    lines = install_mod.install_into_project(project)
    assert not [line for line in lines if line.startswith("workflow ->")]
    assert not (home / "workflows").exists()


def test_seeding_carries_workflow_sidecar_assets(project, home):
    """A verify script must land where its workflow's verify command looks:
    the global layer, next to the yaml — a yaml seeded without it is broken."""
    cflow_install.seed_global_workflows()
    assert (home / "workflows" / "e2e-session-roundtrip-verify.mjs").is_file()


def test_seeding_reports_an_untouched_copy_as_unchanged(project, home):
    cflow_install.seed_global_workflows()
    again = cflow_install.seed_global_workflows()
    assert {outcome for _, _, outcome in again} == {cflow_install.UNCHANGED}


# --------------------------------------------------------------------------- #
# claunch cflow add
# --------------------------------------------------------------------------- #
def test_add_promotes_a_project_workflow_by_name(project, home, capsys):
    """The common case: I wrote it here, I want it everywhere.

    By name, not by path — otherwise using the global layer means knowing
    where both layers keep their files, which is the thing nobody knows.
    """
    mine = _write(project / ".claunch" / "workflows" / "tiny.yaml")

    assert cli.main(["cflow", "add", "tiny"]) == 0
    shared = home / "workflows" / "tiny.yaml"
    assert shared.read_bytes() == mine.read_bytes()
    # ...and it says so, because the project copy still wins *here*
    assert "project wins" in capsys.readouterr().out


def test_add_refuses_a_workflow_that_does_not_parse(project, home, capsys):
    bad = project / "bad.yaml"
    bad.write_text("name: bad\nstart: nowhere\nsteps: {}\n", encoding="utf-8")

    assert cli.main(["cflow", "add", str(bad)]) == 1
    assert not (home / "workflows" / "bad.yaml").exists()
    assert "error" in capsys.readouterr().err


def test_add_will_not_quietly_replace_a_different_file(project, home, capsys):
    _write(home / "workflows" / "tiny.yaml", desc="the shared one")
    _write(project / ".claunch" / "workflows" / "tiny.yaml", desc="mine")

    assert cli.main(["cflow", "add", "tiny"]) == 1
    assert "--force" in capsys.readouterr().err
    assert "the shared one" in (home / "workflows" / "tiny.yaml").read_text("utf-8")

    assert cli.main(["cflow", "add", "tiny", "--force"]) == 0
    assert "mine" in (home / "workflows" / "tiny.yaml").read_text("utf-8")


def test_add_refuses_to_copy_a_file_onto_itself(project, home, capsys):
    _write(home / "workflows" / "tiny.yaml")
    assert cli.main(["cflow", "add", "tiny"]) == 1
    assert "already the global copy" in capsys.readouterr().err


def test_add_can_install_into_the_project_instead(project, home):
    _write(home / "workflows" / "tiny.yaml")
    assert cli.main(["cflow", "add", "tiny", "--project", "--name", "forked"]) == 0
    assert (project / ".claunch" / "workflows" / "forked.yaml").is_file()


def test_add_project_takes_a_directory(project, home, tmp_path):
    _write(home / "workflows" / "tiny.yaml")
    other = tmp_path / "other-proj"
    assert cli.main(["cflow", "add", "tiny", "--project", str(other)]) == 0
    assert (other / ".claunch" / "workflows" / "tiny.yaml").is_file()


def test_add_global_is_the_default_and_may_be_spelled_out(project, home):
    mine = _write(project / ".claunch" / "workflows" / "tiny.yaml")
    assert cli.main(["cflow", "add", "tiny", "--global"]) == 0
    assert (home / "workflows" / "tiny.yaml").read_bytes() == mine.read_bytes()


def test_add_refuses_one_name_for_several_workflows(project, home, capsys):
    _write(home / "workflows" / "a.yaml", name="a")
    _write(home / "workflows" / "b.yaml", name="b")
    assert cli.main(["cflow", "add", "a", "b", "--name", "one", "--project"]) == 1
    assert "--name renames one" in capsys.readouterr().err


def test_add_reports_each_of_several_and_fails_on_any(project, home, capsys):
    _write(project / ".claunch" / "workflows" / "good.yaml", name="good")
    (project / "bad.yaml").write_text("steps: {}\n", encoding="utf-8")

    assert cli.main(["cflow", "add", "good", str(project / "bad.yaml")]) == 1
    # the good one still landed — one bad argument is not a reason to skip work
    assert (home / "workflows" / "good.yaml").is_file()


# --------------------------------------------------------------------------- #
# a run remembers which file it was
# --------------------------------------------------------------------------- #
def test_a_run_records_the_file_and_the_layer_it_came_from(project, home):
    shared = _write(home / "workflows" / "tiny.yaml")

    engine.start("tiny")
    status = engine.status()
    assert status["source"] == str(shared)
    assert status["origin"] == state_mod.LAYER_GLOBAL

    started = [e for e in state_mod.read_journal() if e["event"] == "started"][0]
    assert started["source"] == str(shared)
    assert started["shadowed"] == []


def test_a_run_records_what_its_workflow_overrode(project, home):
    shared = _write(home / "workflows" / "tiny.yaml")
    mine = _write(project / ".claunch" / "workflows" / "tiny.yaml")

    engine.start("tiny")
    assert engine.status()["source"] == str(mine)
    assert engine.status()["origin"] == state_mod.LAYER_PROJECT
    started = [e for e in state_mod.read_journal() if e["event"] == "started"][0]
    assert started["shadowed"] == [str(shared)]


def test_the_recorded_layer_survives_the_file_moving_underneath(project, home):
    """The run's answer is the one that was true when it started.

    Deleting the project copy mid-run makes the same name resolve to the
    global layer. The run is driving a snapshot of the project file, so
    reporting it as the global one would be a lie.
    """
    _write(home / "workflows" / "tiny.yaml")
    mine = _write(project / ".claunch" / "workflows" / "tiny.yaml")
    engine.start("tiny")
    mine.unlink()

    assert engine.status()["origin"] == state_mod.LAYER_PROJECT
    assert engine.status()["source"] == str(mine)


# --------------------------------------------------------------------------- #
# what the shipped improv pair teaches about verification
# --------------------------------------------------------------------------- #
def _bundled(name):
    return model.load(dict(state_mod.bundled_workflows())[name])


def _prose(text):
    """A step's prose with its line wrapping taken out.

    The anchors below pin RULES, and a rule does not change when the line it
    sits on is re-wrapped -- but a substring check does. ``3794310`` turned
    the whole suite red for exactly that: it re-flowed ``improv-mid``'s
    ``land`` step, and ``rebase <네 기준>`` became ``rebase\\n<네 기준>``.
    The rule was still there, verbatim; only the column the editor broke at
    had moved, and there is no reading of "the rule was lost" that a newline
    supports. These are ``|`` block scalars, so YAML keeps every one of those
    breaks and hands them to the anchor.

    Collapsing here is what keeps the pin about the phrase instead of about
    the wrap. It is deliberately not applied to the checks that read a
    workflow *file* (``read_text``), where the layering tests are asserting
    about the bytes on disk.
    """
    return " ".join((text or "").split())


def test_the_worker_does_not_fold_into_a_retired_or_frozen_branch():
    """A worktree that runs several rounds outlives the rule written for one.

    "Fold the feature branch into the worktree branch" assumes that branch is
    still this round's. Once it has landed in master, folding revives a strand
    behind master; once it is under integration review, folding moves a tip
    somebody else is judging. Both are one-way, so the rule has to name them
    and say what to hand over instead.
    """
    commit = _bundled("improv-worker").steps["commit"]
    assert "git branch --contains" in commit.instructions
    for anchor in ("은퇴", "동결", "심사", "피처 브랜치"):
        assert anchor in commit.instructions, f"commit lost its {anchor!r} case"
    assert "은퇴" in commit.done_when


def test_the_worker_review_says_what_a_number_is_a_verdict_about():
    """A suite number is not a verdict until it names its tree and window.

    Every anchor here is a rule that cost the fleet a wrong conclusion once:
    which tree ran, which packages, who else was sweeping, and what the check
    cannot see.
    """
    review = _bundled("improv-worker").steps["review"]
    for anchor in (
        "--directory",                     # which tree
        "uv sync --extra test",            # ...and which packages
        "tests/test_tree_isolation.py",    # the tree axis, checked from inside the suite
        "claunch-v5yp",                    # ...and the retired bare probe, named so it stays retired
        "트리 해시",                        # numbers carry their tree
        "--collect-only",                  # baselines cost nothing
        "claunch window status",            # the daemon-owned queue is readable
        "tools/changed_tests.py",           # targeted acquisition point
        "tools/sweep.py run",               # exclusive sweep acquisition point
        "wait_seconds",                     # the receipt records queue delay
        "advisory_n",                       # and the granted xdist width
        "CLAUNCH_WINDOW=off",               # the operator escape hatch is loud
        "파일 잠금",                          # sweep fallback remains exclusive
        "popen-gw",                        # a dead run's numbers are on disk
    ):
        assert anchor in review.instructions, f"review lost its {anchor!r} rule"


def test_the_worker_may_skip_targeted_tests_only_with_a_recorded_reason():
    for label, wf in (
        ("bundled", _bundled("improv-worker")),
        ("project", model.load(PROJECT_OVERRIDES / "improv-worker.yaml")),
    ):
        decision = wf.steps["test-decision"]
        assert decision.is_select, label
        assert decision.select.chooser == "agent"
        assert decision.select.require_reason is True
        assert decision.select.options["run"].next == "review"
        assert decision.select.options["skip"].next == "test-skipped"
        assert "reason" in decision.select.prompt
        assert "검증 범위" in wf.steps["test-skipped"].instructions


def test_the_worker_review_does_not_ship_the_retired_bare_probe():
    """The retired probe is kept out of the prose by a check, not by memory.

    ``claunch-v5yp`` retired a one-line environment probe --
    ``uv run --no-sync python -c "import pytest, xdist; print(...)"`` -- because
    it answers a different question than the suite does: the editable install
    puts the MAIN checkout on a bare interpreter's ``sys.path`` while pytest
    front-inserts ``pythonpath = ["src"]`` from this rootdir, so the probe says
    "contaminated" about a clean run. ``tests/test_tree_isolation.py`` carries
    the whole account, and the cost: two sessions re-synced venvs they did not
    need to, and one green measurement was briefly thrown away.

    The retraction was recorded in prose and the prose kept shipping it -- this
    step's instructions are handed to a new worker every round, so a wrong rule
    written here is re-issued rather than forgotten. That is what this test is
    for: the removal is a rule now, and a rule that costs nothing to re-type is
    a rule nothing enforces.

    Both layers, because the project layer is a whole-file override.
    """
    for label, wf in (
        ("bundled", _bundled("improv-worker")),
        ("project", model.load(PROJECT_OVERRIDES / "improv-worker.yaml")),
    ):
        prose = _prose(wf.steps["review"].instructions)
        assert "import pytest, xdist" not in prose, (
            f"{label}: the retired bare probe is back in the review prose. "
            f"It reports the main checkout for a run pytest is reading out of "
            f"this worktree -- see tests/test_tree_isolation.py."
        )
        assert "tests/test_tree_isolation.py" in prose, (
            f"{label}: the review prose no longer names the check that "
            f"replaced the probe, so nothing tells a worker how the tree axis "
            f"is answered."
        )


def test_the_worker_review_names_the_fallback_limit():
    """A deployment fallback reports the capacity it cannot enforce."""
    review = _bundled("improv-worker").steps["review"]
    assert "데몬 또는 창 API를 사용할 수 없으면" in review.instructions
    assert "targeted는 용량 제한을 적용할 수 없다는" in review.instructions


def test_the_leader_checks_who_else_stands_in_the_tree_before_merging():
    """The merge and the sweep happen in one checkout; the preflight names who
    else is in it, and the existing user gate decides. It is deliberately not
    a wall: a leader cannot move sessions outside its own subtree, and a gate
    that blocks forever is a gate that gets bypassed on day one."""
    leader = _bundled("improv-leader")
    assert "integrate-preflight" in leader.steps
    pre = leader.steps["integrate-preflight"]
    assert "claunch cflow checkout" in pre.instructions
    assert pre.next == "integrate"
    # reachable: standby's integrate option must route through it
    assert leader.steps["standby"].select.options["integrate"].next == "integrate-preflight"
    # and the decision stays with the human gate that already exists
    assert "preflight" in leader.steps["integrate"].entry_prompt


def test_the_leader_gate_points_at_a_record_that_can_exist():
    """The collection table cannot be filed as a report: standby is a select
    step and `report` is refused there. Its home is the select's reason."""
    prompt = _bundled("improv-leader").steps["integrate"].entry_prompt
    assert "reason" in prompt
    assert "select_confirmed" in prompt
    standby = _bundled("improv-leader").steps["standby"]
    assert standby.is_select  # the premise of the rule above
    assert "reason" in standby.instructions


def test_the_leader_treats_a_clean_merge_as_unproven():
    """Text silence is not runtime safety, and the absence of a conflict
    marker is exactly what removes the place a human would look."""
    integrate = _bundled("improv-leader").steps["integrate"]
    for anchor in ("충돌 없음은 안전 판정이 아니다", "같은 모듈", "스텁", "프리뷰"):
        assert anchor in integrate.instructions, f"integrate lost {anchor!r}"


# --------------------------------------------------------------------------- #
# claunch cflow update — bring stale global copies up to the package
# --------------------------------------------------------------------------- #
def _fake_bundle(tmp_path, monkeypatch, files):
    """Point the packaged-workflow source at a throwaway directory.

    ``bundled_workflows()`` resolves through ``bundled_workflows_dir()`` alone
    (the package directory), so seeding and update read the fake copy and the
    tests never touch the real package.
    """
    pkg = tmp_path / "bundle"
    pkg.mkdir()
    for name, body in files.items():
        (pkg / name).write_text(body, encoding="utf-8")
    monkeypatch.setattr(state_mod, "bundled_workflows_dir", lambda: pkg)
    return pkg


def test_update_replaces_a_stale_global_copy(project, home, tmp_path, monkeypatch):
    pkg = _fake_bundle(tmp_path, monkeypatch, {"tiny.yaml": TINY.format(name="tiny", desc="v1")})
    cflow_install.seed_global_workflows()
    # The package moves on; the layer still holds the seeded bytes.
    (pkg / "tiny.yaml").write_text(TINY.format(name="tiny", desc="v2"), encoding="utf-8")

    outcomes = cflow_install.update_global_workflows([], can_ask=False)
    states = {name: outcome for name, outcome, applied, _ in outcomes}
    assert states["tiny"] == cflow_install.STALE
    assert "v2" in (home / "workflows" / "tiny.yaml").read_text("utf-8")


def test_update_seeds_a_missing_global_copy(project, home, tmp_path, monkeypatch):
    _fake_bundle(tmp_path, monkeypatch, {"tiny.yaml": TINY.format(name="tiny", desc="v1")})
    outcomes = cflow_install.update_global_workflows([], can_ask=False)
    states = {name: outcome for name, outcome, applied, _ in outcomes}
    assert states["tiny"] == cflow_install.SEEDED
    assert (home / "workflows" / "tiny.yaml").is_file()


def test_update_leaves_an_unchanged_copy_alone(project, home, tmp_path, monkeypatch):
    _fake_bundle(tmp_path, monkeypatch, {"tiny.yaml": TINY.format(name="tiny", desc="v1")})
    cflow_install.seed_global_workflows()
    outcomes = cflow_install.update_global_workflows([], can_ask=False)
    states = {name: outcome for name, outcome, applied, _ in outcomes}
    assert states["tiny"] == cflow_install.UNCHANGED


def test_update_refuses_an_edited_copy_without_force(project, home, tmp_path, monkeypatch):
    _fake_bundle(tmp_path, monkeypatch, {"tiny.yaml": TINY.format(name="tiny", desc="v1")})
    cflow_install.seed_global_workflows()
    # A human edits the layer copy; the package does not move.
    (home / "workflows" / "tiny.yaml").write_text(
        TINY.format(name="tiny", desc="mine"), encoding="utf-8"
    )

    outcomes = cflow_install.update_global_workflows([], can_ask=False)
    states = {name: (outcome, applied) for name, outcome, applied, _ in outcomes}
    assert states["tiny"] == (cflow_install.EDITED, False)
    # Not applied: no --force, no live terminal.
    assert "mine" in (home / "workflows" / "tiny.yaml").read_text("utf-8")
    assert not (home / "workflows" / "tiny.yaml").with_name("tiny.yaml.bak").exists()


def test_update_with_force_backs_up_then_replaces(project, home, tmp_path, monkeypatch):
    _fake_bundle(tmp_path, monkeypatch, {"tiny.yaml": TINY.format(name="tiny", desc="v1")})
    cflow_install.seed_global_workflows()
    edited = home / "workflows" / "tiny.yaml"
    edited.write_text(TINY.format(name="tiny", desc="mine"), encoding="utf-8")

    outcomes = cflow_install.update_global_workflows([], force=True, can_ask=False)
    states = {name: (outcome, applied) for name, outcome, applied, _ in outcomes}
    assert states["tiny"] == (cflow_install.EDITED, True)
    # The edit is what the .bak keeps; the layer copy is the package again.
    assert (home / "workflows" / "tiny.yaml").is_file()
    assert "v1" in (home / "workflows" / "tiny.yaml").read_text("utf-8")
    bak = home / "workflows" / "tiny.yaml.bak"
    assert bak.is_file()
    assert "mine" in bak.read_text("utf-8")


def test_update_an_unknown_copy_refuses_without_force(project, home, tmp_path, monkeypatch):
    """A file with no seed record is not provably stale — leave it alone."""
    _fake_bundle(tmp_path, monkeypatch, {"tiny.yaml": TINY.format(name="tiny", desc="v1")})
    (home / "workflows").mkdir(parents=True, exist_ok=True)
    (home / "workflows" / "tiny.yaml").write_text(
        TINY.format(name="tiny", desc="pre-sidecar"), encoding="utf-8"
    )
    outcomes = cflow_install.update_global_workflows([], can_ask=False)
    states = {name: (outcome, applied) for name, outcome, applied, _ in outcomes}
    assert states["tiny"] == (cflow_install.UNKNOWN, False)
    assert "pre-sidecar" in (home / "workflows" / "tiny.yaml").read_text("utf-8")


def test_update_can_ask_defers_to_the_person(project, home, tmp_path, monkeypatch, monkeypatch_stdin):
    """A live terminal is asked — a 'no' (or EOF) is a refusal, not assent."""
    _fake_bundle(tmp_path, monkeypatch, {"tiny.yaml": TINY.format(name="tiny", desc="v1")})
    cflow_install.seed_global_workflows()
    (home / "workflows" / "tiny.yaml").write_text(
        TINY.format(name="tiny", desc="mine"), encoding="utf-8"
    )

    # EOF (no answer) must not become a default-yes.
    monkeypatch_stdin("")
    outcomes = cflow_install.update_global_workflows([], can_ask=True)
    states = {name: (outcome, applied) for name, outcome, applied, _ in outcomes}
    assert states["tiny"] == (cflow_install.EDITED, False)
    assert "mine" in (home / "workflows" / "tiny.yaml").read_text("utf-8")

    # An explicit yes replaces it, with the edit preserved in .bak.
    monkeypatch_stdin("y\n")
    outcomes = cflow_install.update_global_workflows([], can_ask=True)
    states = {name: (outcome, applied) for name, outcome, applied, _ in outcomes}
    assert states["tiny"] == (cflow_install.EDITED, True)
    assert "v1" in (home / "workflows" / "tiny.yaml").read_text("utf-8")
    assert "mine" in (home / "workflows" / "tiny.yaml.bak").read_text("utf-8")


def test_seed_writes_a_record_loaded_by_update(project, home, tmp_path, monkeypatch):
    """The sidecar is what lets update tell stale from edited — it must
    survive a re-read."""
    _fake_bundle(tmp_path, monkeypatch, {"tiny.yaml": TINY.format(name="tiny", desc="v1")})
    cflow_install.seed_global_workflows()
    assert (home / "workflows" / ".seeded.json").is_file()
    record = cflow_install.seed_record(home / "workflows")
    assert "tiny.yaml" in record and len(record["tiny.yaml"]) == 64


def test_both_leader_layers_teach_the_topology_skills():
    """The lead re-draws its own team — wires two peers who keep needing each
    other through it, and puts a crowded area under a nested worker it moves
    the area's workers beneath — without waiting to be told. Both layers name
    the skills (``mesh-wire``, ``mesh-delegate``) and the tool (``reparent``)
    so the instruction survives a project-layer resync from the bundle."""
    for wf in (
        _bundled("improv-leader"),
        model.load(PROJECT_OVERRIDES / "improv-leader.yaml"),
    ):
        standby = wf.steps["standby"].instructions
        assert "mesh-wire" in standby
        assert "mesh-delegate" in standby
        assert "mesh-retopology" in standby      # every other reparent
        assert "reparent" in standby
        assert "self-decision" in standby  # not a user gate
        assert "mesh-delegate" in wf.steps["intake"].instructions


# --------------------------------------------------------------------------- #
# the nested worker's own workflow: an area run as a stacked pull request
# --------------------------------------------------------------------------- #
def test_the_bundled_improv_mid_runs_an_area_as_a_stack():
    """A nested worker is still a worker on the mesh but its run is a small
    control loop, not a one-goal round: it opens a stack on its own branch,
    lands its children's branches on it one at a time (``--no-ff``, in
    order, a restack notice to the rest after each), and hands the lead ONE
    branch. The shape below is what ``mesh-delegate`` and both improv
    layers point at, so it is pinned here."""
    wf = _bundled("improv-mid")
    assert wf.filter_roles is not None
    assert wf.filter_roles.type == model.FILTER_WHITELIST
    assert wf.filter_roles.roles == ("worker",)
    # never the wizard's default for a worker — improv-worker keeps that
    assert wf.default_role is None
    assert wf.max_visits > model.DEFAULT_MAX_VISITS  # standby <-> land loops
    assert set(wf.steps) == {
        "intake", "standby", "land", "landing", "landing-review", "handoff",
        "await-landing", "wrapup",
    }

    intake = _prose(wf.steps["intake"].instructions)
    for anchor in ("스택 베이스", "rebase_onto", "스택 표", "기준", "batch send"):
        assert anchor in intake, f"intake lost its {anchor!r} rule"
    assert "머지 커밋만" in intake  # the base takes merges, not feature work

    standby = wf.steps["standby"]
    assert standby.select is not None and standby.select.chooser == "agent"
    assert set(standby.select.options) == {"land", "complete"}
    assert standby.select.options["land"].next == "land"
    assert standby.select.options["complete"].next == "landing"
    standby_rules = _prose(standby.instructions)
    assert "merge-tree" in standby_rules
    assert "master 대비가 아니다" in standby_rules

    land = wf.steps["land"]
    assert land.next == "standby"  # one child per pass, then back on watch
    land_rules = _prose(land.instructions)
    for anchor in ("--no-ff", "merge-tree", "restack", "rebase <네 기준>"):
        assert anchor in land_rules, f"land lost its {anchor!r} rule"
    assert "master는 어떤 경우에도 머지 대상이 아니다" in land_rules

    landing = wf.steps["landing"].select
    # The fast path: a clean stack is offered up without asking the leader.
    # ``hold`` is not reachable from here -- it lives on ``landing-review``,
    # which is where the delegation went. Both are pinned in
    # test_landing_fast_path.py.
    assert landing.chooser == "agent"
    assert set(landing.options) == {"request", "escalate"}
    assert landing.options["request"].next == "handoff"
    assert landing.options["escalate"].next == "landing-review"

    review = wf.steps["landing-review"].select
    # Delegated to the leader, as for every worker -- the route itself is
    # pinned in test_landing_is_decided_by_the_session_above_not_by_a_person.
    assert review.chooser == "delegate"
    assert review.options["request"].next == "handoff"
    assert review.options["hold"].next == "wrapup"

    handoff = wf.steps["handoff"]
    assert handoff.next == "await-landing"
    handoff_rules = _prose(handoff.instructions)
    for anchor in ("--rebase-merges", "git branch --merged", "스택 표",
                   "master를 직접 머지하지 않는다"):
        assert anchor in handoff_rules, f"handoff lost its {anchor!r} rule"

    waiting = wf.steps["await-landing"].select
    assert waiting.chooser == "agent"
    assert waiting.options["landed"].next == "wrapup"
    assert waiting.options["restack"].next == "handoff"

    # The mechanical end: the daemon records and terminates a finished
    # one-shot run's session, so the wrap-up no longer carries the kill
    # instruction (the most common incompletion it replaces); only the
    # keep-alive exception stays in prose.
    wrapup = _prose(wf.steps["wrapup"].instructions)
    assert "claunch kill-session $CLAUNCH_SESSION" not in wrapup
    assert "keep-alive" in wrapup
    assert wf.steps["wrapup"].next is None


def test_a_rule_survives_the_column_its_line_was_broken_at():
    """Re-wrapping a workflow paragraph must not read as losing its rule.

    This is the guard on the test above, not on the workflow: ``3794310``
    re-flowed ``improv-mid``'s ``land`` step, ``rebase <네 기준>`` became
    ``rebase\\n<네 기준>``, and the suite went red for two hours over a rule
    that had not changed by one character -- with two reviewed branches
    frozen behind the gate the whole time (claunch-n5eq).

    So the property is pinned directly: take the shipped rule, break its
    line somewhere else, and the anchor must still find it. A future edit
    that drops ``_prose`` from the checks above turns this red *here*, where
    the failure says what it is, instead of in a workflow test whose message
    is "land lost its rule" -- which is the sentence that sent the last
    reader looking in the wrong file.
    """
    land = _prose(_bundled("improv-mid").steps["land"].instructions)
    anchor = "rebase <네 기준>"
    assert anchor in land
    # Every space the phrase could be broken at, one at a time -- and only the
    # spaces, because that is where a re-wrap breaks. (A break inside a word
    # would not be a re-wrap; it would be a different word, and no amount of
    # collapsing should paper over that.) The continuation is indented, as
    # YAML's own re-indent leaves it.
    breaks = [i for i, ch in enumerate(anchor) if ch == " "]
    assert breaks, "the anchor has no space to break at -- this guard is vacuous"
    for cut in breaks:
        rewrapped = land.replace(
            anchor, anchor[:cut] + "\n          " + anchor[cut + 1:], 1
        )
        assert anchor in _prose(rewrapped), (
            f"a break after {anchor[:cut]!r} hid the rule"
        )
    # And the converse, so this is a pin and not a tautology: a rule that is
    # genuinely gone stays gone, however the prose is folded.
    assert anchor not in _prose(land.replace(anchor, "rebase whenever you like", 1))


def test_both_worker_layers_know_their_place_on_a_stack():
    """A worker under a nested worker measures against its declared base
    branch, requests from that parent, and rebases on a restack notice —
    in both layers, so a project-layer resync cannot un-teach it."""
    for wf in (
        _bundled("improv-worker"),
        model.load(PROJECT_OVERRIDES / "improv-worker.yaml"),
    ):
        intake = wf.steps["intake"].instructions
        assert "improv-mid" in intake and "스택 베이스" in intake
        assert "git rebase" in intake and "<기준>" in intake
        rebase = wf.steps["rebase"].instructions
        assert "restack" in rebase and "upstream에 있으므로" in rebase
        request = wf.steps["integration-request"].instructions
        assert "merge-tree" in request and "improv-mid" in request
        # the general nested-merge rule survives for a worker that spawned
        # helpers of its own; the dedicated nested worker is sent elsewhere
        assert "improv-mid" in wf.steps["commit"].instructions


def test_both_leader_layers_route_a_crowded_area_through_improv_mid():
    """The lead spawns the nested worker with ``workflow: improv-mid``,
    tells moved workers their integration target changed, and merges the
    stack as ONE candidate — re-requests to it mean ``--rebase-merges``."""
    for wf in (
        _bundled("improv-leader"),
        model.load(PROJECT_OVERRIDES / "improv-leader.yaml"),
    ):
        assert "improv-mid" in wf.steps["intake"].instructions
        standby = wf.steps["standby"].instructions
        assert "improv-mid" in standby and "stacked pull request" in standby
        assert "restack" in standby
        integrate = wf.steps["integrate"].instructions
        assert "improv-mid" in integrate
        assert "--rebase-merges" in integrate
        assert "--merged" in integrate


def _landing_route(wf):
    """The landing decision's candidate roles/scopes and its fallback.

    It lives on ``landing-review`` since the clean case stopped asking: the
    delegation moved off the fast path, it did not weaken.
    """
    sel = wf.steps["landing-review"].select
    assert sel.chooser == "delegate", (
        "landing is a delegated decision, not a human gate or a self-decision"
    )
    return (
        [(c.role, c.scope) for c in sel.delegate.candidates],
        sel.delegate.otherwise,
        sel.delegate.timeout,
    )


@pytest.mark.parametrize(
    "name, roles",
    [
        # A worker's parent is a mid worker (role ``worker``) in a stacked
        # formation and the leader in a flat one; the groups are tried in
        # order, so the same file covers both shapes.
        ("improv-worker", [("worker", model.SCOPE_ANCESTOR),
                           ("leader", model.SCOPE_ANCESTOR)]),
        # A mid worker is only ever spawned by the leader.
        ("improv-mid", [("leader", model.SCOPE_ANCESTOR)]),
    ],
)
def test_landing_is_decided_by_the_session_above_not_by_a_person(name, roles):
    """``landing`` must ask the owner of the integration queue, not a human.

    Neither option is irreversible for the run that reaches this step:
    ``request`` ends in ``integration-request``/``handoff``, which forbid a
    master merge outright, so it only puts the branch in the upper session's
    queue — and ``hold`` returns to that same queue through the wrapup
    ``HOLD:`` comment and fyi. So the human gate that used to stand here
    guarded nothing; master is guarded by the leader's exclusive merge and
    the post-merge full sweep. The leader's own ``integrate`` had already
    dropped its user gate for the *heavier* action, which left the lighter
    one — asking to be queued — as the only thing still stopping for a
    person every round.

    It is not ``chooser: agent`` either: ``hold`` skips the rebase and the
    evidence bundle, so chooser and beneficiary would be the same party.
    ``scope: ancestor`` keeps it that way from the other side — a run cannot
    stand up a descendant to approve itself.
    """
    wf = model.load(dict(state_mod.bundled_workflows())[name])
    candidates, otherwise, timeout = _landing_route(wf)
    assert candidates == roles
    # Nobody above, or nobody answering in time, and it is a person's call
    # again -- that is the only place this decision is still the user's.
    assert otherwise == model.OTHERWISE_HUMAN
    assert timeout and timeout > 0, "an unanswered delegation must reach a human"


def test_the_project_override_worker_lands_through_the_same_delegation():
    """The override shadows the bundled worker here, so it must match it.

    Otherwise this repository's own rounds would keep stopping for a person
    at ``landing`` while every other repository's did not -- one name, two
    policies, and the drift invisible until someone waits.
    """
    candidates, otherwise, _ = _landing_route(
        model.load(PROJECT_OVERRIDES / "improv-worker.yaml")
    )
    assert candidates == [
        ("worker", model.SCOPE_ANCESTOR),
        ("leader", model.SCOPE_ANCESTOR),
    ]
    assert otherwise == model.OTHERWISE_HUMAN


@pytest.mark.parametrize("layer", ["bundled", "project"])
def test_the_leader_is_told_that_a_delegated_decision_is_its_own(layer):
    """``standby`` must not read a landing ask as somebody else's gate.

    The same step already says ``approve/select/goto/abort`` are a person's
    doors and never the leader's to walk through. A delegated decision
    arrives at the same session and looks adjacent, but it is the opposite
    case: the workflow named the leader as the one who answers. Without a
    line separating them, the rule against stepping on human gates reads as
    "leave the worker's ask alone" -- and the worker waits out its timeout
    for a person the change was meant to stop calling.
    """
    if layer == "bundled":
        wf = model.load(dict(state_mod.bundled_workflows())["improv-leader"])
    else:
        wf = model.load(PROJECT_OVERRIDES / "improv-leader.yaml")

    standby = wf.steps["standby"].instructions
    assert "answer" in standby, "standby never names the tool that answers an ask"
    assert "asks" in standby
    assert "내 결정이다" in standby, (
        "standby does not distinguish a decision delegated TO the leader "
        "from a human gate it must not touch"
    )


def test_both_sides_judge_a_landing_with_the_same_command():
    """One predicate, two callers -- the invariant the round trip rested on.

    The worker used to decide "am I aligned?" from prose in its own step, and
    the leader used to decide the same thing from prose in its preflight. Two
    copies of a rule drift, and this pair drifting is not a hypothetical
    inconvenience: it is a branch the worker's gate passed and the leader's
    screening sent back, which is the exact round trip the script was written
    to delete. So the name of the script is pinned on both sides, in both
    layers, rather than left to whoever edits one file next.
    """
    bundled = dict(state_mod.bundled_workflows())
    pairs = [
        (model.load(bundled["improv-worker"]), model.load(bundled["improv-leader"])),
        (
            model.load(PROJECT_OVERRIDES / "improv-worker.yaml"),
            model.load(PROJECT_OVERRIDES / "improv-leader.yaml"),
        ),
    ]
    for worker, leader in pairs:
        assert "merge_ready.py" in worker.steps["rebase"].instructions
        assert "merge_ready.py" in leader.steps["integrate-preflight"].instructions
