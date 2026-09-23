"""The improv workflows and the board: one rule block, and who may do what.

The beads board became the system of record for work items after a shift
in which the leader kept the integration queue in its own context — and
lost a hand-off, a reassignment and a merge order to that. The rule that
fixes it is prose in three workflows (worker, leader, mid) across two
layers, which is exactly the kind of text that drifts one file at a time.
So the shared block is pinned byte-for-byte, and the role-specific halves
are pinned by the commands each role is — and is not — told to run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_launcher.cflow import model, state as state_mod

PROJECT_OVERRIDES = Path(__file__).resolve().parents[1] / ".claunch" / "workflows"

#: The shared block starts at this marker and ends with this sentence; the
#: worker's bundled copy is the reference the others are compared against.
BLOCK_START = "── beads(br) 규칙 — 공통 ──"
BLOCK_END = "것만은 금지다."


def _bundled(name: str) -> Path:
    return Path(dict(state_mod.bundled_workflows())[name])


def _block(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    start = text.find(BLOCK_START)
    assert start >= 0, f"{path.name} has no beads rule block"
    end = text.find(BLOCK_END, start)
    assert end >= 0, f"{path.name}'s beads rule block never ends"
    return text[start : end + len(BLOCK_END)]


def _without_block(text: str) -> str:
    start = text.find(BLOCK_START)
    end = text.find(BLOCK_END, start) + len(BLOCK_END)
    return text[:start] + text[end:]


# --------------------------------------------------------------------------- #
# one block, five files
# --------------------------------------------------------------------------- #
def test_the_improv_workflows_share_one_beads_rule_block():
    """Five files, one rule. A block edited in one place would teach two
    protocols under one name — the leader would wait for a transition the
    worker was never told to make."""
    reference = _block(_bundled("improv-worker"))
    assert "claunch beads" in reference and "close뿐이다" in reference
    for path in (
        _bundled("improv-leader"),
        _bundled("improv-mid"),
        _bundled("improv-pm"),
        PROJECT_OVERRIDES / "improv-worker.yaml",
        PROJECT_OVERRIDES / "improv-leader.yaml",
    ):
        assert _block(path) == reference, (
            f"{path} carries a beads rule block that differs from the "
            f"bundled worker's — the rule drifted"
        )


def test_the_open_pool_criteria_section_is_part_of_the_shared_block():
    """The open-pool triage criteria live in the one shared block, not in a
    workflow's own half — a worker-only pin would teach the leader nothing
    about the pool it is the leader's to tidy."""
    block = _block(_bundled("improv-worker"))
    flat = " ".join(block.split())                     # prose wraps mid-phrase
    assert "── open 풀 판정 기준 ──" in block
    assert "--limit 0" in block                       # full-pool measurement, never the 50-row view
    assert "일괄 변경은 없다" in flat            # per-issue verdicts only, never a batch
    assert "merged 해시가 코멘트에 있는데 open인" in block
    assert "SESSION ENDED 코멘트가 있고 assignee 없이 3일" in block
    assert "재배정이 먼저" in block                 # reassignment before closing


def test_the_shared_block_defines_the_in_ready_transition_owners():
    """The new lane has explicit owners and an exit rule for every path."""
    block = _block(_bundled("improv-worker"))
    flat = " ".join(block.split())
    assert "open→in_ready는 리더" in flat
    assert "open 또는 in_ready→in_progress는 assignee 워커" in flat
    assert "in_ready→open은 리더" in flat
    assert "in_ready는 검토 결과를 유지" in flat
    assert "doc은 참조 전용(`in_ready`로 전이하지 않고 배정하지 않는다)" in flat


def test_the_block_sits_in_every_intake():
    """The block is read where a round begins, in each workflow."""
    for name in ("improv-worker", "improv-leader", "improv-mid"):
        wf = model.load(_bundled(name))
        assert BLOCK_START in wf.steps["intake"].instructions, name


# --------------------------------------------------------------------------- #
# the worker: creates, claims, hands back — never closes
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("layer", ["bundled", "project"])
def test_the_worker_creates_its_own_issue_and_claims_it(layer):
    path = (
        _bundled("improv-worker") if layer == "bundled"
        else PROJECT_OVERRIDES / "improv-worker.yaml"
    )
    wf = model.load(path)
    intake = wf.steps["intake"].instructions
    # A goal typed into the worker's terminal is the worker's to register...
    assert "--labels user-direct" in intake
    assert "claunch beads show" in intake            # ...or read, if the leader assigned it
    assert "--status in_progress" in intake          # and claimed either way
    assert "DECLINED:" in intake                     # a colliding leader assignment is handed back
    assert "in_progress" in wf.steps["intake"].done_when
    # Out-of-scope findings are filed, not fixed.
    assert "--labels found" in wf.steps["work"].instructions
    # The request is the in_review transition; hand-offs are issues.
    assert "--status in_review" in wf.steps["integration-request"].instructions
    assert "in_review" in wf.steps["integration-request"].done_when
    wrapup = wf.steps["wrapup"].instructions
    assert "--labels handoff" in wrapup
    assert "HOLD:" in wrapup


@pytest.mark.parametrize("layer", ["bundled", "project"])
def test_the_worker_settles_its_issue_before_branch_setup(layer):
    """A round reaches ``branch-setup`` only through an issue verdict.

    ``intake`` alone used to carry the issue rules, and a session that
    arrived with no assignment (no daemon issue, no ``--no-issue`` order)
    slid into work with nothing on the board. The check phase pins the
    four verdicts and who owns each: facts are the agent's (does an issue
    stand? which board answer did the creation request give?), while adopting
    somebody else's record and proceeding with no record at all are a
    person's — ``chooser: user`` records the agent's pick as a proposal
    only, so the ``none`` option confirmed by a person IS the explicit
    consent the round journals.

    The two no-issue verdicts are the fix for the answer that used to be
    one. Both mean "nothing is on the board for you" and they say opposite
    things about what to do with that, so a single verdict left the session
    inferring which was meant — and a session told "no issue" went to the
    board looking for one. ``no-issue-wait`` goes straight to work with no
    search; ``no-issue-auto`` goes to a branch that searches and takes,
    with no user gate, because the person answered at creation time.
    """
    path = (
        _bundled("improv-worker") if layer == "bundled"
        else PROJECT_OVERRIDES / "improv-worker.yaml"
    )
    wf = model.load(path)
    assert wf.steps["intake"].next == "issue-check"
    check = wf.steps["issue-check"].select
    assert check.chooser == "agent"                  # four readable facts
    assert check.options["claimed"].next == "branch-setup"
    assert check.options["no-issue-wait"].next == "branch-setup"
    assert check.options["no-issue-auto"].next == "issue-auto"
    assert check.options["unclaimed"].next == "issue-search"
    # the auto branch is the one that has no user gate, so it says out loud
    # that it takes the issue itself and how
    auto = wf.steps["issue-auto"]
    assert auto.next == "branch-setup", (
        f"{layer}: the auto branch takes its own issue and then joins the "
        "same place issue-adopt does — routing it through issue-adopt "
        "instead would strand the round when the board has nothing worth "
        "taking, and routing it past branch-setup would leave it without a "
        "branch to work on"
    )
    assert "--status in_progress" in auto.instructions
    assert "JOINED" in auto.instructions
    assert "issue-decision" in auto.instructions, (
        f"{layer}: the branch has to say why the user gate is skipped here, "
        "or the next reader adds it back"
    )
    # the search files the candidates the deciding person reads
    assert wf.steps["issue-search"].next == "issue-decision"
    assert "search" in wf.steps["issue-search"].instructions
    decision = wf.steps["issue-decision"].select
    assert decision.chooser == "user", (
        f"{layer}: adopting a record or proceeding without one is a "
        "person's call — an agent chooser would let the round consent "
        "to itself"
    )
    # adopt and create are two transitions, not one edge with two labels
    # (2026-09-21, user). While they shared a `next`, the engine saw a single
    # edge and the difference survived only in the human's `reason` prose --
    # which is not a machine-readable input: `require_reason` guarantees the
    # reason is non-empty, not that an issue id is recoverable from it.
    assert decision.options["adopt"].next == "issue-adopt"
    assert decision.options["create"].next == "issue-create"
    assert decision.options["none"].next == "branch-setup"
    assert decision.options["adopt"].next != decision.options["create"].next, (
        f"{layer}: adopt and create must not collapse into the same edge"
    )
    adopt = wf.steps["issue-adopt"]
    assert adopt.next == "branch-setup"
    assert "--status in_progress" in adopt.instructions
    assert "JOINED" in adopt.instructions            # a live holder is joined, not taken
    assert "in_progress" in adopt.done_when
    mint = wf.steps["issue-create"]
    assert mint.next == "branch-setup"
    assert "--status in_progress" in mint.instructions
    assert "in_progress" in mint.done_when
    assert "JOINED" not in mint.instructions, (
        f"{layer}: a freshly minted issue has no holder to join -- a JOINED "
        "line on this branch describes something that cannot happen"
    )
    assert wf.steps["branch-setup"].next == "work"
    # an assigned-but-empty issue is filled from the opening task in intake,
    # where the issue is first read — not minted a second time
    assert "자리표시" in wf.steps["intake"].instructions


@pytest.mark.parametrize("layer", ["bundled", "project"])
def test_the_worker_closes_only_its_own_landed_issue(layer):
    """The assignee closes its own issue; the leader still owns the JSONL.

    Closing used to be the leader's for two reasons, and only one of them
    survived: the sweep numbers that made up half the close reason, and the
    single ``.beads/issues.jsonl`` every worker branch would collide on.
    ``landed`` now proves the merge from git in the worker's own hands, so
    the first reason is met where the worker stands — while the second is
    untouched, because ``br close`` writes the DB and committing the JSONL
    is a separate step the leader keeps.

    So the pin moves rather than lifts. The worker may spell ``beads
    close`` in exactly one step — ``wrapup``, where the board is tidied —
    and nowhere else: spelling it in ``landed`` or ``integration-request``
    would close an issue before or without the proof. ``beads sync`` stays
    out of the worker's text entirely.
    """
    path = (
        _bundled("improv-worker") if layer == "bundled"
        else PROJECT_OVERRIDES / "improv-worker.yaml"
    )
    own = _without_block(path.read_text(encoding="utf-8"))
    assert "claunch beads sync" not in own
    assert "워커가 하지 않는 것 둘" in own

    wf = model.load(path)
    # The shared block lives inside `intake`'s instructions and states the
    # rule for every role, so it is stripped here too: what is under test is
    # the worker's own text, step by step.
    spells = sorted(
        sid for sid, step in wf.steps.items()
        if "claunch beads close" in _without_block(
            (step.instructions or "")
            + ((step.select.prompt if step.select else "") or "")
        )
    )
    assert spells == ["wrapup"], f"beads close is spelled in {spells}"

    tidy = wf.steps["wrapup"].instructions
    # The reason has to come from the gate, not from a message: `landed`
    # exists because a notification is not evidence.
    assert "landed" in tidy and "merged <머지 해시>" in tidy
    assert "hold" in tidy  # ...and the frozen branch is still not closed


def test_every_role_can_reach_the_close_gate_the_block_names():
    """The rule may not name a gate a workflow does not have.

    The shared block hands closing to the assignee and names the gate that
    qualifies it: ``landed``, which proves the merge from git. That is only
    a rule a session can obey if its own workflow has that step — and
    ``improv-mid`` does not: a stack worker goes ``handoff ->
    await-landing -> wrapup`` with no mechanical proof of its own landing.

    A live mid worker is therefore neither qualified (no gate) nor an
    orphan (not exited), so the block has to name it as the leader's or its
    stack issue belongs to nobody. It said "orphans only" for one commit;
    this is the pin that keeps the two halves in step.
    """
    block = _block(_bundled("improv-worker"))
    for name in ("improv-worker", "improv-leader", "improv-mid"):
        wf = model.load(_bundled(name))
        if "landed" in wf.steps:
            continue
        assert "중간 워커" in block and "스택 이슈" in block, (
            f"{name} has no 'landed' step, so the block must name who closes "
            f"its own issue — it currently does not"
        )


# --------------------------------------------------------------------------- #
# the leader: reconciles, registers, closes, commits the board
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("layer", ["bundled", "project"])
def test_the_leader_owns_the_board_lifecycle(layer):
    path = (
        _bundled("improv-leader") if layer == "bundled"
        else PROJECT_OVERRIDES / "improv-leader.yaml"
    )
    wf = model.load(path)
    intake = wf.steps["intake"].instructions
    assert "ORPHANED" in intake                      # recovery after compaction/restart
    assert "보드 대조" in wf.steps["intake"].done_when
    standby = wf.steps["standby"].instructions
    assert "--status in_review" in standby           # the integration table IS the board
    assert "issue: <id>" in standby                  # spawn carries the id
    assert "BLOCKED:" in standby
    assert "REBASE REQUESTED" in wf.steps["integrate-preflight"].instructions
    # The close reason carries the sweep count, so the board is closed where
    # that number exists: the batch sweep step after the merge, not the
    # merge itself.
    sweep = wf.steps["sweep"].instructions
    assert "claunch beads close" in sweep
    assert "sync --flush-only" in sweep
    assert "chore(beads)" in sweep
    assert "chore(beads)" in wf.steps["sweep"].done_when
    assert "claunch beads close" not in wf.steps["integrate"].instructions
    assert "claunch beads list" in wf.steps["wrapup"].instructions
    assert "claunch beads list" in wf.steps["wrapup"].ask.prompt


def test_the_leader_no_longer_keeps_the_table_in_a_select_reason():
    """The old rule — carry the whole tally table in ``standby``'s select
    reason — was the symptom. The reason now carries ids; the table is
    the board's ``in_review`` list."""
    for path in (_bundled("improv-leader"), PROJECT_OVERRIDES / "improv-leader.yaml"):
        standby = model.load(path).steps["standby"].instructions
        assert "이슈 id 목록" in standby
        assert "떠날 때 그 회차분을 reason에 실어라" not in standby


# --------------------------------------------------------------------------- #
# the mid-worker: parents its children, closes only them
# --------------------------------------------------------------------------- #
def test_the_mid_worker_parents_children_and_closes_only_those():
    wf = model.load(_bundled("improv-mid"))
    assert "--parent <내 이슈>" in wf.steps["intake"].instructions
    assert "claunch beads close <자식 이슈>" in wf.steps["land"].instructions
    assert "--status in_review" in wf.steps["handoff"].instructions
    own = _without_block(_bundled("improv-mid").read_text(encoding="utf-8"))
    # The only close a mid runs is on its own children, in the landing step.
    assert own.count("claunch beads close") == 1
    assert "부모 이슈는 닫지 않는다" in wf.steps["wrapup"].instructions


# --------------------------------------------------------------------------- #
# 기록은 코멘트에, 신호는 메시지에
# --------------------------------------------------------------------------- #
# The board became the system of record for work *items*; the evidence about
# that work stayed on the wire, because the shared block said comments carry
# pointers only while the landing step asked for a whole bundle in the
# message. So both were written and only the message was paid for: in one
# measured hour of a six-worker mesh (mesh-0824, seq 740..859) 438,243
# characters were typed into terminals, and 54% of them were fyi/ack —
# records that by their own intent asked nobody for anything. These pins keep
# the two halves of the rule pointing the same way.
#
# Prose is asserted through _flat: the yaml wraps a sentence wherever the
# column runs out, so a phrase pinned verbatim would break on a re-wrap that
# changed nothing. Markers an agent greps for (LANDING REQUEST, STACK) are
# pinned raw on purpose — those must survive on one line, or the instruction
# teaches a wrapped marker.
def _flat(text: str) -> str:
    return " ".join(text.split())


def test_the_block_sends_the_record_to_the_board_and_keeps_the_message_a_nudge():
    b = _block(_bundled("improv-worker"))
    flat = _flat(b)
    assert "기록은 코멘트에, 신호는 메시지에" in flat
    # the bundle has a home, and a way in that survives a long payload
    assert "claunch beads comments add <id> -f <파일>" in flat
    # ...and is not also spent on the wire
    assert "같은 것을 메시지 본문에 다시 싣지 않는다" in flat
    assert "nudge" in flat
    # a shared convention is not a work item, but is still read back
    assert "--type doc" in flat
    # ...and because it is read back, its current value is the body. Comments
    # carry the history of how that ruling stood and was corrected. A doc
    # whose conventions live only in comments goes stale in the body with no
    # warning and no exit code (claunch-qj03), so pin both halves of the fork.
    assert "규범 내용(규약·정의·판정식)은 본문에 두고, 고칠 때 본문을 고친다" in flat
    assert "그 판정이 어떻게 서고 어떻게 정정됐는지의 이력을 쌓는다" in flat
    # the clause that sent the rule itself into the comments is gone
    assert "갱신은 그 코멘트로 쌓는다" not in flat
    # the one place the split does not stand up
    assert "다른 머신의 멤버는 이 저장소의 보드에 닿지 않는다" in flat
    # the clause that used to force the evidence into the message is gone
    assert "포인터와 판정만" not in flat


@pytest.mark.parametrize("layer", ["bundled", "project"])
def test_the_worker_puts_its_evidence_in_a_comment_not_in_the_message(layer):
    path = (
        _bundled("improv-worker") if layer == "bundled"
        else PROJECT_OVERRIDES / "improv-worker.yaml"
    )
    wf = model.load(path)
    req = wf.steps["integration-request"].instructions
    assert "LANDING REQUEST @ <tip>" in req      # grepped verbatim: keep it inline
    assert "claunch beads comments add <id> -f <파일>" in _flat(req)
    assert "메시지 본문에 다시 싣지 않는다" in _flat(req)
    # done_when has to agree, or the step keeps passing on the old behaviour
    dw = _flat(wf.steps["integration-request"].done_when)
    assert "LANDING REQUEST" in dw and "nudge" in dw
    # the completion report is what the parent decides landing on, so it goes
    # to the board as well
    commit = _flat(wf.steps["commit"].instructions)
    assert "claunch beads comments add <id> -f <파일>" in commit
    assert "nudge" in commit


@pytest.mark.parametrize("layer", ["bundled", "project"])
def test_the_leader_reads_the_queue_from_the_board_and_pulls_selectively(layer):
    """Moving the bundle to the board is only half the saving — a leader that
    then opens every comment has bought nothing. The nudge's value line is
    what decides which one to open."""
    path = (
        _bundled("improv-leader") if layer == "bundled"
        else PROJECT_OVERRIDES / "improv-leader.yaml"
    )
    whole = _flat(path.read_text(encoding="utf-8"))
    standby = model.load(path).steps["standby"].instructions
    assert "LANDING REQUEST @ <tip>" in standby
    flat = _flat(standby)
    assert "claunch beads list --status in_review --json" in flat
    assert "골라서 당긴다" in flat
    # a certified rule is a doc issue with an id to broadcast, not prose
    assert "--type doc" in flat
    # and the table is the board's: the journal cross-check in `integrate` no
    # longer claims mesh fyi is what builds it
    assert "표는 mesh fyi로만 쌓이므로" not in whole


@pytest.mark.parametrize("layer", ["bundled", "project"])
def test_the_leader_moves_triaged_work_into_in_ready_before_assignment(layer):
    path = (
        _bundled("improv-leader") if layer == "bundled"
        else PROJECT_OVERRIDES / "improv-leader.yaml"
    )
    wf = model.load(path)
    intake = _flat(wf.steps["intake"].instructions)
    standby = _flat(wf.steps["standby"].instructions)
    assert "--status in_ready --status in_progress --status in_review" in intake
    assert "update <id> --status in_ready" in standby
    assert standby.index("update <id> --status in_ready") < standby.index(
        "update <id> --assignee <세션>"
    )


def test_the_mid_worker_keeps_the_stack_table_on_the_board():
    """The stack table changes on every landing. Sent as a message it is the
    same table re-typed into the leader's terminal once per child."""
    wf = model.load(_bundled("improv-mid"))
    assert "STACK @ <베이스 tip>" in wf.steps["intake"].instructions
    land = wf.steps["land"].instructions
    assert "STACK @ <새 tip>" in land
    assert "STACK @ <새 tip>" in wf.steps["land"].done_when
    handoff = wf.steps["handoff"].instructions
    assert "LANDING REQUEST @ <tip>" in handoff
    assert "묶음을 메시지 본문에 다시 싣지 않는다" in _flat(handoff)
    # restack is an event, not a record, so it stays on the wire — and the step
    # says why, or the next edit moves it to the board along with the rest
    assert "restack 공지" in _flat(land)
    assert "사건이라 메시가 맞는 자리다" in _flat(land)


# --------------------------------------------------------------------------- #
# the mesh layer learns the same split
# --------------------------------------------------------------------------- #
# The board was wired into the daemon (daemon/beads.py) and into the three
# workflows, but not into the layer that teaches an agent what to put in a
# message — so the payload rule had nowhere to live and members sent whatever
# they had in hand. These two pin the other end of it.
def test_the_mesh_skill_teaches_where_a_record_goes():
    from claude_launcher import mesh_install

    md = mesh_install.SKILL_MD
    assert "claunch beads" in md
    assert "Records go to the board, not the wire." in md
    assert "Then send the nudge." in md
    assert "Events stay on the wire." in md
    assert "another machine" in md               # the cross-machine exception
    assert "claunch beads list --status in_review --json" in md


@pytest.mark.parametrize("layer", ["bundled", "project"])
def test_every_layer_forbids_editing_the_export_file(layer):
    """`.beads/issues.jsonl` is an export of `.beads/beads.db`, and a line
    written into it by hand reaches no database and survives no flush. The
    sentence sits in the shared block, so it reaches every role in both
    layers; the installer plants the matching deny rule
    (tests/test_board_guard.py) for the sessions that read no workflow."""
    for name in ("improv-worker", "improv-leader", "improv-mid", "improv-pm"):
        path = _bundled(name) if layer == "bundled" else PROJECT_OVERRIDES / f"{name}.yaml"
        if layer == "project" and not path.exists():
            continue
        block = _flat(_block(path))
        assert "`.beads/issues.jsonl`을 직접 고치는 것은 금지다" in block
        assert "sync --flush-only" in block          # what overwrites it
        assert "stale 가드" in block                 # what the reverse breaks
        assert "sed -i" in block                     # the shell spelling too


def test_the_mesh_skill_forbids_it_in_the_layer_every_session_reads():
    """The profile layer is where a session with no cflow run and no assigned
    issue gets it — or does not. Measured empty in
    `claunch-beads-guidance-missing-from-profile-g77zs`."""
    from claude_launcher import mesh_install

    md = mesh_install.SKILL_MD
    assert "Never write to `.beads/issues.jsonl` yourself." in md
    assert "claunch beads sync --flush-only" in md
    assert "stale guard" in md


def test_the_packaged_stances_know_a_record_has_a_home():
    """A stance that never names the board leaves `mesh send` the only channel
    an agent knows it has."""
    from claude_launcher.daemon import mesh_roles

    rs = mesh_roles.resolve()
    assert "board" in rs.get("leader").stance
    assert "board" in rs.get("worker").stance


# --------------------------------------------------------------------------- #
# queues and batches: many issues per session, one landing per branch
# --------------------------------------------------------------------------- #
def test_the_shared_block_defines_the_queue_and_the_batch():
    """A session's queue is a reading of the board (assignee), the dashboard
    writes only that, and a queue lands as one batch: every role reads the
    same paragraph, so the leader's table and the worker's transitions agree
    on what a batch row is."""
    block = _block(_bundled("improv-worker"))
    flat = " ".join(block.split())
    assert "── 큐(queue)·배치(batch) 규칙 ──" in block
    assert "assignee가 그 세션인 활성 이슈" in flat
    assert "--assignee <세션> --status open --status in_ready --status in_progress --limit 0 --json" in flat
    assert "priority 오름차순" in flat
    assert "`QUEUED`/`UNQUEUED` 코멘트" in flat
    assert "상태(status)는 손대지 않는다" in flat        # the dashboard writes no status
    assert "착지 요청은 한 번이다" in flat
    assert "`batch: <id,...>`" in flat
    assert "assignee가 이슈마다 `merged <해시>`로 한다" in flat
    assert "UNQUEUED: <세션> landed without it" in flat
    # leftover rows are re-read by queue-recheck before any return to pool,
    # and the worker may not fill its own queue except through the narrow,
    # leader-confirmed self exception (claunch-qac7, Q1: A -> B)
    assert "워커의 queue-recheck가 다시 읽는다" in flat
    assert "워커가 자기 후속 이슈를 스스로 배정해 큐를 채우는 것은 원칙적으로 금지다" in flat
    assert "claunch-qac7 Q1: A→B" in flat
    assert "회차당 최대 2건" in flat


@pytest.mark.parametrize("layer", ["bundled", "project"])
def test_the_worker_loops_over_its_queue_before_it_lands(layer):
    """commit -> queue-next -> (item-intake -> work ...)* -> landing.

    The loop is closed by a select whose material is the board (one listing),
    whose ``next`` branch requires an issue to transition, and whose
    ``drained`` branch is the whole landing procedure -- there is no lazy
    exit. The batch cap is counted here and the ambiguous case drains."""
    path = (
        _bundled("improv-worker") if layer == "bundled"
        else PROJECT_OVERRIDES / "improv-worker.yaml"
    )
    wf = model.load(path)
    assert wf.steps["commit"].next == "queue-next"
    gate = wf.steps["queue-next"]
    assert gate.select is not None and gate.select.chooser == "agent"
    assert gate.select.require_reason is True
    assert {k: o.next for k, o in gate.select.options.items()} == {
        "next": "item-intake", "drained": "landing",
    }
    prompt = " ".join(gate.select.prompt.split())
    assert "--assignee $CLAUNCH_SESSION --status open --status in_ready --limit 0 --json" in prompt
    assert "5개" in prompt                          # the batch cap
    assert "모호하면 drained다" in prompt             # the tie-break
    item = wf.steps["item-intake"]
    assert item.next == "work"
    text = " ".join(item.instructions.split())
    assert "--status in_progress --external-ref <run id>" in text
    assert "새 브랜치를 만들지 않고" in text            # one branch per batch
    # intake reads the queue but transitions only the primary issue
    intake = " ".join(wf.steps["intake"].instructions.split())
    assert "--assignee $CLAUNCH_SESSION --status open --status in_ready --status in_progress --limit 0 --json" in intake
    assert "전이하는 것은 주 이슈 하나다" in intake
    assert "큐의 나머지 일감" in " ".join(wf.steps["intake"].done_when.split())


@pytest.mark.parametrize("layer", ["bundled", "project"])
def test_the_worker_lands_a_batch_once_and_settles_every_issue(layer):
    """One nudge per batch, every issue to in_review, a COMMIT comment on the
    issue the commit belongs to, at wrapup a close per landed issue, and the
    return-to-pool for what was queued but never taken up lives one step
    later, in ``queue-recheck`` -- wrapup no longer touches those rows,
    because the next step may turn them into the next round."""
    path = (
        _bundled("improv-worker") if layer == "bundled"
        else PROJECT_OVERRIDES / "improv-worker.yaml"
    )
    wf = model.load(path)
    commit = " ".join(wf.steps["commit"].instructions.split())
    assert "그 커밋이 속한 일감의 이슈" in commit
    assert "큐에 일감이 남아 있으면 이 nudge는 보내지 않는다" in commit
    req = " ".join(wf.steps["integration-request"].instructions.split())
    assert "── 배치 회차(이 브랜치에 실린 일감이 둘 이상) ──" in req
    assert "실린 이슈 **전부**를 in_review로 전이한다" in req
    assert "`LANDING REQUEST @ <tip> batch: <id1,id2,...>`" in req
    assert "`LANDING REQUEST @ <tip> (batch of <주 이슈 id>)`" in req
    assert "배치면 실린 이슈 전부" in " ".join(wf.steps["integration-request"].done_when.split())
    # landing is a select: its text is the prompt, not instructions
    landing = " ".join((wf.steps["landing"].select.prompt or "").split())
    assert "배치 회차면 실린 이슈마다 본다" in landing   # a brake on any issue escalates
    wrapup = " ".join(wf.steps["wrapup"].instructions.split())
    assert "배치 회차면 **실린 이슈마다** 같은 사유로 닫는다" in wrapup
    assert "여기서 손대지 않는다 — 상태도 assignee도 그대로다" in wrapup
    assert "UNQUEUED: $CLAUNCH_SESSION landed without it" not in wrapup
    assert "배치 회차면 주 이슈 id 하나로 한 장" in wrapup   # one report per landing
    recheck = " ".join((wf.steps["queue-recheck"].select.prompt or "").split())
    assert '--assignee ""' in recheck
    assert "UNQUEUED: $CLAUNCH_SESSION landed without it" in recheck


@pytest.mark.parametrize("layer", ["bundled", "project"])
def test_the_worker_rechecks_its_queue_after_landing_and_loops(layer):
    """wrapup -> queue-recheck -> (intake ... | settle-check -> end-gate).

    A round used to be a session: landing drained the queue back to the pool
    and the session ended, so a second assignment meant a second spawn. Now
    the step after wrapup re-reads the board (the same queue command every
    role uses); an assignable row loops the run back to ``intake`` (a new
    round, new branch, new landing -- the run's visit counter is the loop
    guard), and only when nothing is assignable does the run reach
    ``settle-check`` -- and only then are the leftover rows unqueued. The
    queue is filled by the leader/operator, or by the narrow self exception a
    worker may only propose and a leader must confirm (claunch-qac7, Q1:
    A -> B, 2026-09-04) -- the worker never assigns its own handoff issues
    outright to manufacture a next round.
    """
    path = (
        _bundled("improv-worker") if layer == "bundled"
        else PROJECT_OVERRIDES / "improv-worker.yaml"
    )
    wf = model.load(path)
    assert wf.start == "intake"
    assert wf.steps["wrapup"].next == "queue-recheck"
    gate = wf.steps["queue-recheck"]
    assert gate.select is not None and gate.select.chooser == "agent"
    assert gate.select.require_reason is True
    assert {k: o.next for k, o in gate.select.options.items()} == {
        "next-round": "intake", "done": "settle-check",
    }
    prompt = " ".join(gate.select.prompt.split())
    assert "--assignee $CLAUNCH_SESSION --status open --status in_ready --status in_progress --limit 0 --json" in prompt
    assert "답을 기다리지 않는다 — 보드가 답이다" in prompt   # the board, not the ask, decides
    assert "모호하면 done이다" in prompt                       # the tie-break
    assert "여기서 스스로 자기 배정을 치지 않는다" in prompt     # Q1: A -> B, read-only here
    # end-gate is reached only through settle-check's `settled`
    assert not any(
        s.next == "end-gate" for s in wf.steps.values() if s.next
    )
    settle = wf.steps["settle-check"]
    assert {k: o.next for k, o in settle.select.options.items()} == {
        "settled": "end-gate", "unsettled": "settle-wait",
    }
    # The wait is the clock's now, and asks nobody (2026-09-21, user). It used
    # to be a delegated select put to the leader with a single option, `acted`,
    # whose answer could not move the run -- the next settle-check re-reads the
    # same board regardless -- and which fell to a person whenever no leader
    # was routable, asking them to confirm a board write they had not made.
    wait = wf.steps["settle-wait"]
    assert wait.timer is not None and wait.select is None
    assert wait.timer.then == "settle-check"
    assert wait.timer.after == "settle-stall"          # bounded, and a person decides at the end
    # intake knows a revisit: the primary issue is the head of the queue
    intake = " ".join(wf.steps["intake"].instructions.split())
    assert "재방문 회차(queue-recheck가 next-round로 보낸 것" in intake
    assert "주 이슈는 큐의 첫 일감(priority 오름차순 → created_at)" in intake
    # wrapup narrows self-registered follow-ups, proposes self/spawn/pool,
    # and settle-check/settle-wait are the machine backstop before the session
    # can end (claunch-380z: a session that only filed a follow-up must not
    # go quiet without it being seen)
    wrapup = " ".join(wf.steps["wrapup"].instructions.split())
    assert "협소하게 해석한다" in wrapup
    assert "예외는 아래 「생성 이슈 정산」 절의 self뿐" in wrapup
    assert "── 생성 이슈 정산 — self/spawn/pool 세 갈래로 제안 ──" in wrapup
    assert "`br`에는 생성자로 거르는 옵션이 없다" in wrapup
    assert "settle-check가 기계로 다시 확인한다" in wrapup
    # queue-next hands drained rows forward instead of to wrapup
    qn = " ".join(wf.steps["queue-next"].select.prompt.split())
    assert "착지 뒤 queue-recheck가 다시 읽는다" in qn
    assert "wrapup이 풀에 되돌린다" not in qn


@pytest.mark.parametrize("layer", ["bundled", "project"])
def test_the_leader_reads_a_batch_as_one_candidate(layer):
    """N in_review rows at one tip are one branch: reviewed once, merged
    once, and closed by the assignee per issue -- never by the leader."""
    path = (
        _bundled("improv-leader") if layer == "bundled"
        else PROJECT_OVERRIDES / "improv-leader.yaml"
    )
    text = " ".join(path.read_text(encoding="utf-8").split())
    assert "**배치 행**" in text
    assert "**브랜치 하나 = 후보 하나**" in text
    assert "행 수로 후보 수를 세지 않고" in text
    assert "리더가 대신 닫지 않는다" in text
    assert "--no-ff 한 번으로 머지하고" in text
    assert "실린 이슈 id를 전부 적는다" in text
