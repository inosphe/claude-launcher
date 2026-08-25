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
        PROJECT_OVERRIDES / "improv-worker.yaml",
        PROJECT_OVERRIDES / "improv-leader.yaml",
    ):
        assert _block(path) == reference, (
            f"{path} carries a beads rule block that differs from the "
            f"bundled worker's — the rule drifted"
        )


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
def test_the_worker_is_never_told_to_close_or_sync(layer):
    """Closing is the leader's, after the merge; so is flushing the JSONL.
    The shared block names both as the leader's — outside it, the worker's
    text must not spell either command."""
    path = (
        _bundled("improv-worker") if layer == "bundled"
        else PROJECT_OVERRIDES / "improv-worker.yaml"
    )
    own = _without_block(path.read_text(encoding="utf-8"))
    assert "claunch beads close" not in own
    assert "claunch beads sync" not in own
    assert "워커가 하지 않는 것 셋" in own


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
