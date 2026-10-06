"""improv-worker-v2: the v2 port of improv-worker.yaml (claunch-yq80p).

The port lives in ``workflows/v2/improv-worker-v2.yaml`` with its shared
fragments, rules and procedures in ``workflows/v2/improv-common.yaml``.
These tests hold the port to four things: it compiles with no check errors,
every v1 step has a named place in v2, its procedures are callable from
another definition, and the beads rule block is the v1 block byte for byte.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import yaml

import claude_launcher
from claude_launcher.cflow2 import check, layers
from claude_launcher.cflow2.frontend import compile_text

WF_DIR = Path(claude_launcher.__file__).parent / "workflows"
V1 = WF_DIR / "improv-worker.yaml"
V2 = WF_DIR / "v2" / "improv-worker-v2.yaml"
COMMON = WF_DIR / "v2" / "improv-common.yaml"

# copied from tests/test_beads_protocol.py (not imported: that module is the
# v1 suite and is not edited by this port)
BLOCK_START = "── beads(br) 규칙 — 공통 ──"
BLOCK_END = "것만은 금지다."

# The only finding the checker reports. It is accepted, with this reason:
#   par-write-overlap [round/item/work-group] — the `found` group runs the
#   work task next to found-issue members. The members write the board only
#   through `claunch beads` (one DB, its own lock) and spawn sessions; they do
#   not write the worktree, so there is no path to declare and no overlap
#   with `work`'s fs.write. The checker cannot see that from undeclared paths.
ACCEPTED_WARNINGS = {("par-write-overlap", "round/item/work-group")}

# Every v1 step id -> the v2 labels that carry it.
V1_TO_V2 = {
    "intake": ["intake", "prior-context", "intake-issue"],
    "issue-check": ["issue-check"],
    "issue-auto": ["issue-auto"],
    "issue-search": ["issue-search"],
    "issue-decision": ["issue-decision"],
    "issue-adopt": ["issue-adopt"],
    "issue-create": ["issue-create"],
    "workspace-check": ["workspace-check"],
    "workspace-ok": ["workspace-ok"],
    "workspace-gate": ["workspace-gate"],
    "branch-setup": ["branch-setup"],
    "work": ["work", "work-group"],
    "test-decision": ["test-decision"],
    "test-skipped": ["test-skipped"],
    "review": ["review", "targeted-test", "tests"],
    "commit": ["commit"],
    "queue-next": ["queue-next"],
    "item-intake": ["item-intake"],
    "stack-cut": ["stack-cut", "stack-landing"],
    "stack-merge": ["cut-wait", "stack-merge"],
    "landing": ["landing"],
    "landing-review": ["landing-review"],
    "rebase": ["merge-ready", "rebase", "align"],
    "reviewer-ensure": ["reviewer-ensure"],
    "peer-review": ["peer-review"],
    "integration-request": ["integration-request"],
    "await-landing": ["await-landing", "await"],
    "landing-withdraw": ["landing-withdraw"],
    "landing-nudge": ["landing-nudge"],
    "landing-declined": ["landing-declined"],
    "rework-gate": ["rework-decision", "rework-gate"],
    "landing-declined-hold": ["landing-declined-hold"],
    "landed": ["landed-wait", "landed"],
    "wrapup": ["wrapup", "round-report"],
    "queue-recheck": ["recheck"],
    "stack-round": ["stack-round"],
    "settle-check": ["settle-check", "settle-poll"],
    "settle-wait": ["settle-wait"],
    "settle-stall": ["settle-stall"],
    "end-gate": ["end-decision", "end-gate"],
    "end-hold": ["end-hold"],
}

PROCEDURES = {"resolve-issue", "workspace", "item", "land", "settle",
              "worker-round", "queue-recheck", "confirm-landed", "found-issue"}

DELEGATED = {"prior-context", "targeted-test", "merge-ready", "round-report"}


def _block(text: str) -> str:
    start = text.find(BLOCK_START)
    assert start >= 0
    end = text.find(BLOCK_END, start)
    assert end >= 0
    return text[start:end + len(BLOCK_END)]


def _chart():
    return layers.load("improv-worker-v2")


def _labels(chart) -> set:
    return {n.label for n in chart.nodes.values() if n.label}


# -- compile and check -------------------------------------------------------


def test_compiles_with_no_check_errors_and_only_accepted_warnings():
    findings = check.check(_chart())
    assert [str(f) for f in findings if f.level == "error"] == []
    warned = {(f.code, f.node) for f in findings if f.level == "warning"}
    assert warned <= ACCEPTED_WARNINGS, warned


def test_meta():
    meta = _chart().meta
    assert meta["default_role"] is None
    assert meta["filter_roles"] == {"type": "whitelist", "roles": ["worker"]}
    assert meta["default_child_cflow"] == "improv-worker-v2"


def test_common_compiles_on_its_own():
    chart = layers.load("improv-common")
    assert [str(f) for f in check.check(chart) if f.level == "error"] == []


# -- v1 coverage -------------------------------------------------------------


def test_every_v1_step_is_mapped():
    v1 = yaml.safe_load(V1.read_text(encoding="utf-8"))
    assert set(v1["steps"]) == set(V1_TO_V2)
    assert len(V1_TO_V2) == 41


def test_every_mapped_label_exists_in_the_ir():
    labels = _labels(_chart())
    missing = {k: [l for l in v if l not in labels] for k, v in V1_TO_V2.items()}
    assert {k: v for k, v in missing.items() if v} == {}


def test_the_mapping_table_in_the_header_names_every_v1_step():
    head = V2.read_text(encoding="utf-8").split("\nparams:", 1)[0]
    for step in V1_TO_V2:
        assert f"#   {step} " in head, step


# -- procedures --------------------------------------------------------------


def test_procedures_are_present_in_the_ir():
    procs = {n.props.get("proc") for n in _chart().nodes.values() if n.type == "scope"}
    assert PROCEDURES <= procs


def test_worker_round_is_callable_from_an_including_definition():
    pm = textwrap.dedent("""\
        cflow: 2
        name: pm-like
        include: [improv-worker-v2]
        body:
          - id: wait-board
            wait:
              probe: "claunch beads list --assignee $CLAUNCH_SESSION --json"
              poll: 300
              describe: 보드에 배정이 생길 때까지 기다린다
          - id: one
            call: worker-round
            with: {verify: "uv run --no-sync python tools/changed_tests.py"}
          - call: settle
        """)
    chart = compile_text(pm, source="pm-like.yaml", resolve_include=layers.resolver())
    assert [str(f) for f in check.check(chart) if f.level == "error"] == []
    procs = {n.props.get("proc") for n in chart.nodes.values() if n.type == "scope"}
    assert {"worker-round", "resolve-issue", "workspace", "item", "land", "settle"} <= procs


def test_land_returns_landed_held_rework():
    doc = yaml.safe_load(V2.read_text(encoding="utf-8"))
    land = doc["procedures"]["land"]
    assert set(land["returns"]) == {"landed", "held", "rework"}
    assert set(land["params"]) == {"mode", "allow"}


# -- delegation, effects, rules, spec ---------------------------------------


def test_delegation_points():
    chart = _chart()
    delegated = {n.label for n in chart.nodes.values()
                 if n.type == "task" and n.executor == ["headless", "subagent", "inline"]}
    assert delegated == DELEGATED
    for n in chart.nodes.values():
        if n.label in DELEGATED:
            assert n.props["returns"], n.label


def test_every_task_declares_effects():
    for n in _chart().nodes.values():
        if n.type == "task":
            assert n.props.get("effects"), n.label


def test_rules_and_master_merge_invariant():
    chart = _chart()
    assert {"no-master-merge", "no-premature-rebase", "scope-discipline"} <= set(chart.rules)
    doc = yaml.safe_load(V2.read_text(encoding="utf-8"))
    rule = doc["rules"]["no-master-merge"]
    assert "claunch-97knp" in rule["why"]
    assert chart.spec["invariants"]["no-master-merge"] == {"never_effect": "git.merge",
                                                           "target": "master"}
    assert chart.spec["paths"]["settle-before-end"]["must_pass"] == ["settle"]


def test_moved_rule_prose_is_not_repeated_in_prompts():
    # the compiler appends delivered rule texts under "── 규칙 ──"; the authored
    # part above it must not repeat them
    chart = _chart()
    phrase = "워커는 master를 직접 머지하지 않는다"
    delivered = 0
    for n in chart.nodes.values():
        if n.type == "task":
            prompt = n.props.get("prompt") or ""
            authored, _, rules = prompt.partition("── 규칙 ──")
            assert phrase not in authored, n.label
            delivered += phrase in rules
    assert delivered >= 5


# -- beads-common ------------------------------------------------------------


def test_beads_common_raw_text_is_the_v1_block():
    assert _block(COMMON.read_text(encoding="utf-8")) == _block(V1.read_text(encoding="utf-8"))


def test_beads_common_loaded_value_is_the_v1_block():
    v1 = yaml.safe_load(V1.read_text(encoding="utf-8"))
    v1_block = _block(v1["steps"]["intake"]["instructions"])
    common = yaml.safe_load(COMMON.read_text(encoding="utf-8"))
    assert _block(common["fragments"]["beads-common"]) == v1_block
    chart = _chart()
    intake_issue = next(n for n in chart.nodes.values() if n.label == "intake-issue")
    assert _block(intake_issue.props["prompt"]) == v1_block
    assert "${frag:beads-common}" in V2.read_text(encoding="utf-8")
