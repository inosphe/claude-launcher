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

import pytest
import yaml

import claude_launcher
from claude_launcher.cflow2 import check, layers
from claude_launcher.cflow2.frontend import compile_text

WF_DIR = Path(claude_launcher.__file__).parent / "workflows"
V1 = WF_DIR / "improv-worker.yaml"
V2 = WF_DIR / "v2" / "improv-worker-v2.yaml"
COMMON = WF_DIR / "v2" / "improv-common.yaml"
SELF_IMPROVE = WF_DIR / "v2" / "self-improve-common.yaml"

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
ACCEPTED_WARNINGS = {("par-write-overlap", "round/item/work-group"),
                     # claunch-0b8dx: no cap on the number of rounds
                     ("unbounded-visits", None)}

# Every v1 step id -> the v2 labels that carry it.
V1_TO_V2 = {
    "intake": ["intake", "prior-context", "prior-context-fallback", "intake-issue"],
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
    "landing-review": ["landing-review", "landing-review-leader"],
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
    "end-gate": ["end-decision", "end-notice"],
    "end-hold": ["end-hold"],
}

PROCEDURES = {"resolve-issue", "workspace", "item", "land", "settle",
              "worker-round", "queue-recheck", "confirm-landed", "found-issue"}

# prior-context, targeted-test and merge-ready are the driver's (claunch-onbdf):
# a read-only delegate has no shell on the claude harness.
DELEGATED = {"round-report"}
DRIVER_RUN = {"prior-context", "targeted-test", "merge-ready"}


def _block(text: str) -> str:
    start = text.find(BLOCK_START)
    assert start >= 0
    end = text.find(BLOCK_END, start)
    assert end >= 0
    return text[start:end + len(BLOCK_END)]


#: claunch-0b8dx (user decision 2026-10-07, v2 only): the v2 copy of the
#: beads block drops v1's self cap and settles on "every issue closed, or a
#: leader HOLD". Apart from DIVERGENCES below, the v2 copy stays the v1
#: block word for word.
V1_ONLY = """묻는다. self에는 런 단위 상한이 있다: 이 런의 intake 방문 수
(`claunch cflow status --json`의 `visits.intake`)가 10 이상이면 self
대신 pool로 보낸다 — 엔진의 loop guard(max_visits 25)는 26번째
intake에서 런을 사람의 승인(`loop_limit`)으로 세우는데, 그 전에 범위
판단을 상위에게 넘기기 위해서다. 리더는 거부권을 가진다(`update <id>
--assignee ""` + `UNQUEUED by leader: <이유>`); 거부된 이슈는
pool과 같이 상위의 처분을 따른다. 세션이 만든 활성
이슈가 전부 누군가의 손(살아 있는 세션의 assignee, 또는 리더의
`HOLD:` 코멘트)에 있어야 세션이 끝난다 — 워커의 settle-check가"""
V2_INSTEAD = """묻는다. self에는 회차 수 상한이 없다(claunch-0b8dx). 리더는
거부권을 가진다(`update <id> --assignee ""` + `UNQUEUED by leader:
<이유>`); 거부된 이슈는 pool과 같이 상위의 처분을 따른다. 세션이
만들었거나 배정받은 일감이 전부 closed여야 세션이 끝난다 — 예외는
리더의 `HOLD:` 코멘트뿐이고, 살아 있는 다른 세션에 배정된 것은 그
세션이 닫을 때까지 기다린다(claunch-0b8dx) — 워커의 settle-check가"""


#: claunch-7r1qr (v2 only): a series of related issues (siblings under one
#: parent, children and follow-ups, issues that depend on one already in the
#: batch) lands as one batch -- one PR -- and the batch cap is 10.
V1_ONLY_BATCH = """상한 5개에 닿으면) 배치의 이슈 전부를 in_review로 올려 착지 요청은 한
번이다."""
V2_INSTEAD_BATCH = """상한 10개에 닿으면) 배치의 이슈 전부를 in_review로 올려 착지 요청은 한
번이다. 같은 연속 작업(같은 parent의 형제, 자식·후속, 배치 안 일감에
의존하는 것)은 영역이 달라도 한 배치에 싣고, 배치 안 일감에만 막힌
의존은 blocked로 세지 않는다 — 연속 작업 하나는 PR(착지 요청) 하나다."""

#: Every declared v2 divergence from the v1 block, as (v1 text, v2 text).
DIVERGENCES = [(V1_ONLY, V2_INSTEAD), (V1_ONLY_BATCH, V2_INSTEAD_BATCH)]


def _words(text: str) -> str:
    return " ".join(text.split())


def _as_v2(v1_block: str) -> str:
    """The v1 block with the declared v2 divergences applied, in words."""
    words = _words(v1_block)
    for v1_only, v2_instead in DIVERGENCES:
        assert words.count(_words(v1_only)) == 1
        words = words.replace(_words(v1_only), _words(v2_instead))
    return words


def _chart():
    return layers.load("improv-worker-v2")


def _labels(chart) -> set:
    return {n.label for n in chart.nodes.values() if n.label}


def _coding_item(tmp_path, monkeypatch, approval):
    """Run the real item route, replacing only the implementation tasks."""
    import json

    from claude_launcher.cflow2 import frontend, inputs, runtime

    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    doc = yaml.load(V2.read_text(encoding="utf-8"), Loader=frontend._Loader)
    doc["body"] = [{"call": "item"}, {"call": "item"}]
    doc.pop("spec", None)
    for name in ("work", "test-skipped", "commit"):
        doc["nodes"][name] = {"prompt": name}
    chart = frontend.compile_text(json.dumps(doc, ensure_ascii=False),
                                  resolve_include=layers.resolver())
    chart = inputs.apply(chart, {} if approval is None else {
        "require_coding_approval": approval,
    })
    kw = {"cwd": str(tmp_path), "scope": "coding", "now": 1.0}
    return runtime.start(chart, caps={}, **kw), kw


@pytest.mark.parametrize("approval", [None, False])
def test_coding_approval_disabled_preserves_direct_work(tmp_path, monkeypatch, approval):
    st, _ = _coding_item(tmp_path, monkeypatch, approval)
    assert [t["label"] for t in st["tasks"]] == ["work"]
    assert not st.get("asks")


def test_coding_approval_requires_user_each_item_visit(tmp_path, monkeypatch):
    from claude_launcher.cflow2 import runtime

    st, kw = _coding_item(tmp_path, monkeypatch, True)
    for visit in (1, 2):
        [plan] = st["tasks"]
        assert plan["label"] == "coding-plan"
        brief = f"item {visit}: edit workflow and test true/false routing"
        st = runtime.report(plan["instance"], "ok", {"brief": brief}, **kw)
        assert not st["tasks"]
        [ask] = st["asks"]
        assert ask["label"] == "coding-approval"
        assert brief in ask["prompt"]
        # urgent: the dashboard raises it on its notification channel (claunch-x76l8.1)
        assert ask["urgent"] is True and ask["resolution"] == "human"
        with pytest.raises(runtime.FlowError, match="not yours to answer"):
            runtime.approve(None, caller="agent", **kw)
        with pytest.raises(runtime.FlowError):
            runtime.report(ask["instance"], "ok", {}, **kw)
        # Time alone must not release a human gate.
        later = runtime.status(**{**kw, "now": 100000.0})
        assert not later["tasks"] and later["asks"]
        runtime.approve(None, reason="plan accepted", **kw)
        st = runtime.status(**kw)
        [gate] = st["tasks"]
        assert gate["label"] == "coding-approval"
        st = runtime.report(gate["instance"], "ok", {}, **kw)
        [work] = st["tasks"]
        assert work["label"] == "work"
        st = runtime.report(work["instance"], "ok", {}, **kw)
        st = runtime.select(st["choices"][0]["instance"], "skip", reason="fixture", **kw)
        for label in ("test-skipped", "commit"):
            [task] = st["tasks"]
            assert task["label"] == label
            st = runtime.report(task["instance"], "ok", {}, **kw)
    assert st["status"] == "done"


def test_coding_approval_is_a_false_by_default_boolean_and_human_only():
    doc = yaml.safe_load(V2.read_text(encoding="utf-8"))
    declaration = doc["inputs"]["require_coding_approval"]
    assert declaration["type"] == "bool"  # shared form renders bool as checkbox
    assert declaration["default"] is False
    ask = doc["nodes"]["coding-approval"]["ask"]
    assert ask["otherwise"] == "human"
    assert "from" not in ask and "timeout" not in ask
    assert ask["urgent"] is True


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
                 if n.type == "task" and n.executor == ["session", "subagent", "inline"]}
    assert delegated == DELEGATED
    for n in chart.nodes.values():
        if n.label in DELEGATED:
            assert n.props["returns"], n.label


def test_every_task_declares_effects():
    for n in _chart().nodes.values():
        if n.type == "task":
            # ask-user's ask-pose (ask-common) only waits for the answer; it
            # declares that with an explicit ``effects: []``
            if (n.props.get("ask") or {}).get("brief") and n.props.get("effects_declared"):
                continue
            assert n.props.get("effects"), n.label


# -- user questions go through ask-user (docs/cflow-v2.md 2.10) -------------

USER_QUESTIONS = {
    "issue-decision": ["adopt", "create", "none"],
    "cut-unreachable-choice": ["confirmed", "held"],
    "landed-unclear-choice": ["confirmed", "held"],
    "rework-decision": ["rework", "hold"],
    "settle-stall": ["proceed", "keep-waiting"],
    "end-decision": ["end", "hold", "new-task", "self-improve"],
    "self-improve-align": ["register", "revise", "drop"],
}
#: The two PR-medium questions go through the pr-or-local skeleton
#: (claunch-yq80p.11): the label is the skeleton's call, the ask-user calls
#: are inside it.
PR_QUESTIONS = ("medium-blocked", "publish-blocked")


def _asked(chart, wrapper):
    """A ``call: ask-user`` with ``cases`` is a seq [call, cond]; the choices
    are the cases keys (docs 2.10, cases)."""
    assert wrapper.type == "seq" and wrapper.get("cases"), wrapper.id
    call = chart.node(wrapper.get("children")[0])
    assert call.type == "scope" and call.get("proc") == "ask-user", wrapper.id
    return wrapper.get("cases"), call.get("with_")


def test_no_user_choose_is_left():
    chart = _chart()
    assert [n.label or n.id for n in chart.nodes.values()
            if n.type == "choose" and n.get("chooser") == "user"] == []


def test_user_questions_call_ask_user_with_fixed_choices_and_reasons():
    chart = _chart()
    labelled = {n.label: n for n in chart.nodes.values() if n.label}
    for label, choices in USER_QUESTIONS.items():
        cases, w = _asked(chart, labelled[label])
        assert cases == choices, label
        assert w["choices"] == {"lit": choices}, label
        assert w["require_reason"] == {"lit": True}, label
        assert "from" not in w, f"{label}: a user question has no other responder"
    for label in PR_QUESTIONS:
        skeleton = labelled[label]
        assert skeleton.type == "scope" and skeleton.get("proc") == "pr-or-local", label
        inner = [n for n in chart.nodes.values()
                 if n.get("cases") and skeleton.id in chart.ancestors(n.id)]
        assert len(inner) == 2, label  # blocked and failed share the handler
        for wrapper in inner:
            cases, w = _asked(chart, wrapper)
            assert cases == ["retry", "local"] and w["require_reason"] == {"lit": True}
            assert "from" not in w


def test_no_ask_user_is_followed_by_a_hand_written_decision_cond():
    """claunch-yq80p.11: the branch on an answer is cases, so a misspelt
    choice cannot compile."""
    text = V2.read_text(encoding="utf-8")
    assert ".decision ==" not in text


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
    assert _words(_block(COMMON.read_text(encoding="utf-8"))) == \
        _as_v2(_block(V1.read_text(encoding="utf-8")))


def test_beads_common_loaded_value_is_the_v1_block():
    v1 = yaml.safe_load(V1.read_text(encoding="utf-8"))
    v1_block = _block(v1["steps"]["intake"]["instructions"])
    common = yaml.safe_load(COMMON.read_text(encoding="utf-8"))
    assert _words(_block(common["fragments"]["beads-common"])) == _as_v2(v1_block)
    chart = _chart()
    intake_issue = next(n for n in chart.nodes.values() if n.label == "intake-issue")
    assert _words(_block(intake_issue.props["prompt"])) == _as_v2(v1_block)
    assert "${frag:beads-common}" in V2.read_text(encoding="utf-8")


# -- triggers (docs/cflow-v2.md 2.8) -----------------------------------------


def _v1_triggers() -> dict:
    """v1 step id -> [(do, at)] as v1's model reads them (bare name = enter)."""
    v1 = yaml.safe_load(V1.read_text(encoding="utf-8"))
    out = {}
    for sid, step in v1["steps"].items():
        raw = (step or {}).get("triggers")
        if not raw:
            continue
        out[sid] = sorted(
            (e, "enter") if isinstance(e, str) else (e["do"], e.get("at", "enter")) for e in raw
        )
    return out


def test_triggers_mirror_v1_at_the_corresponding_tasks():
    v1 = _v1_triggers()
    assert set(v1) == {"work", "commit", "integration-request", "landed", "wrapup"}
    chart = _chart()
    seen = set()
    for node in chart.nodes.values():
        trig = node.get("triggers")
        if not trig:
            continue
        assert node.label in v1, node.id
        # a push to parent:landing is v1's enqueue-landing with more fields
        # (the PR landing fields, claunch-yq80p.5); relation independent sends
        # the same request to leader:landing instead (claunch-yq80p.9) -- one
        # of the two is recorded, so the pair counts as one
        got = sorted({("enqueue-landing" if t["do"] == "push" and (t.get("push") or {}).get("to")
                       in ("parent:landing", "leader:landing") else t["do"], t["at"])
                      for t in trig})
        assert got == v1[node.label], node.id
        seen.add(node.label)
    assert seen == set(v1)


def test_landing_queue_item_records_who_passed_the_review():
    """claunch-n74fs.1: the leader's landing queue row must carry whether the
    peer-review pass was a real review (the responder's handle) or a 0-answer
    take ("unanswered"), so an unanswered pass is never read as reviewed.
    The expression is safe when peer-review never ran: it evaluates to None.
    The field must also be declared in the landing-queue item schema —
    apply_push rejects unknown fields, so a push-side-only change would fail
    every landing request at the leave trigger."""
    chart = _chart()
    node = next(n for n in chart.nodes.values() if n.label == "integration-request")
    pushes = [t for t in node.get("triggers") or []
              if t.get("do") == "push" and (t.get("push") or {}).get("to")
              in ("parent:landing", "leader:landing")]
    assert len(pushes) == 2
    for t in pushes:
        item = (t.get("push") or {}).get("item") or {}
        assert item.get("review") == "$result['peer-review'].value.by"
    # ...and the declared schema accepts it — apply_push rejects unknown
    # fields, so a push-side-only change would fail every landing request.
    from claude_launcher.cflow2 import collections as col_mod
    head = "cflow: 2\nname: t\ninclude: [improv-common]\nnodes:\n  a: {prompt: a}\nbody: a\n"
    resolve = layers.resolver(None)
    chart2 = compile_text(head + "collections:\n  landing: {type: landing-queue, "
                          "env: {TARGET: master}}\n", source="t.yaml",
                          resolve_include=resolve)
    spec = chart2.meta["collections"]["landing"]
    state = {"kind": "queue", "items": [], "seq": 0}
    col_mod.apply_push(state, spec, {"issue": "x-1", "branch": "b", "tip": "t" * 40,
                                     "requested_by": "w", "review": "unanswered"},
                       principal="child", by="w", now=1.0)
    assert state["items"][0]["review"] == "unanswered"


def test_landing_queue_and_signals_types():
    from claude_launcher.cflow2.frontend import FrontendError
    import pytest

    head = "cflow: 2\nname: t\ninclude: [improv-common]\nnodes:\n  a: {prompt: a}\nbody: a\n"
    resolve = layers.resolver(None)
    chart = compile_text(head + "collections:\n  landing: {type: landing-queue, env: {TARGET: master}}\n"
                         "  signals: {type: signals}\n", source="t.yaml", resolve_include=resolve)
    landing = chart.meta["collections"]["landing"]
    assert landing["kind"] == "queue" and landing["env"] == {"TARGET": "master"}
    assert landing["access"]["set.landed"] == ["daemon"]
    assert landing["watch"]["on"] == {"0": {"set": {"status": "landed"}}}
    assert chart.meta["collections"]["signals"]["kind"] == "log"
    with pytest.raises(FrontendError, match="env.TARGET is required"):
        compile_text(head + "collections:\n  landing: {type: landing-queue}\n",
                     source="t.yaml", resolve_include=resolve)


# --------------------------------------------------------------------------- #
# PR landing (claunch-yq80p.5, docs/cflow-v2.md 10a)
# --------------------------------------------------------------------------- #
def _labels(chart):
    return {n.label for n in chart.nodes.values() if n.label}


def test_the_round_decides_its_landing_medium_and_publishes_a_pr():
    chart = layers.load("improv-worker-v2")
    labels = _labels(chart)
    assert {"remote-setup", "pr-open", "medium-blocked", "publish-blocked"} <= labels
    assert chart.data["landing"]["medium"] == "local"
    prs = chart.meta["collections"]["prs"]
    assert prs["key"] == "number" and prs["lifecycle"]["inherit"] is True
    assert set(prs["item"]) >= {"number", "url", "branch", "base", "issues"}
    # the landing push carries the medium and the PR
    req = next(n for n in chart.nodes.values() if n.label == "integration-request")
    pushes = [t["push"] for t in req.get("triggers") if isinstance(t, dict) and t.get("push")]
    assert [p["to"] for p in pushes] == ["parent:landing", "leader:landing"]
    for push in pushes:
        assert {"medium", "pr_url", "pr_number", "base"} <= set(push["item"])
    # the leader's queue type declares them
    common = yaml.safe_load(COMMON.read_text(encoding="utf-8"))
    assert {"medium", "pr_url", "pr_number", "base"} <= set(
        common["collection_types"]["landing-queue"]["item"])


def test_remote_setup_reads_the_preflight_and_the_force_inputs():
    text = V2.read_text(encoding="utf-8")
    doc = yaml.safe_load(text)
    prompt = doc["nodes"]["remote-setup"]["prompt"]
    assert "claunch cflow2 pr-preflight" in prompt
    assert "${data.inputs.force_pr}" in prompt and "${data.inputs.force_no_pr}" in prompt
    assert set(doc["nodes"]["remote-setup"]["returns"]) == {"ok", "blocked"}
    assert set(doc["nodes"]["pr-open"]["returns"]) == {"ok", "failed"}


# -- the pr-or-local skeleton at run time (claunch-yq80p.11) --------------------


def _medium_flow(tmp_path, *, verified=False):
    """landing-medium and pr-publish as they are in the definition, with their
    skeleton and the two attempt tasks, in a small workflow."""
    doc = yaml.safe_load(V2.read_text(encoding="utf-8"))
    procs = {k: doc["procedures"][k] for k in ("pr-or-local", "local-landing", "landing-medium", "pr-publish")}
    nodes = {k: {kk: vv for kk, vv in doc["nodes"][k].items() if kk in ("title", "prompt", "returns")}
             for k in ("remote-setup", "pr-open", "local-landing-policy")}
    if verified:
        nodes["local-landing-policy"]["verify"] = doc["nodes"]["local-landing-policy"]["verify"]
    nodes["after"] = {"prompt": "after"}
    flow = {"cflow": 2, "name": "medium-flow", "include": ["ask-common"],
            "params": {"force_pr": {"default": False}},
            "nodes": nodes, "procedures": procs,
            "body": [{"call": "landing-medium", "bind": "landing"},
                     {"call": "pr-publish", "bind": "published",
                      "with": {"landing": "$data.landing", "force_pr": "$params.force_pr"}},
                     "after"]}
    from claude_launcher.cflow2 import frontend
    chart = frontend.compile_text(yaml.safe_dump(flow, allow_unicode=True, sort_keys=False),
                                  resolve_include=layers.resolver(str(tmp_path)))
    return chart


BRIEF = {"problem": "p", "options": [
    {"name": "retry", "summary": "s", "risk": "r", "outcome": "o", "reversibility": "reversible"},
    {"name": "local", "summary": "s", "risk": "r", "outcome": "o", "reversibility": "reversible"}],
    "recommendation": "local", "why": "w"}


def test_pr_or_local_retries_then_falls_back_locally(tmp_path, monkeypatch):
    from claude_launcher.cflow2 import runtime
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    cwd = str(tmp_path)
    kw = {"cwd": cwd, "scope": "m", "now": 1.0}
    st = runtime.start(_medium_flow(tmp_path), caps={}, **kw)

    def report(label, outcome="ok", value=None):
        iid = next(t["instance"] for t in st["tasks"] if t["label"].rsplit("/", 1)[-1] == label)
        return runtime.report(iid, outcome, value, **kw)

    st = report("remote-setup", "blocked", {"reason": "gh not logged in"})
    # landing-medium asks the person: retry re-runs the attempt
    st = report("ask-frame", "ok", {"brief": BRIEF})
    runtime.approve(None, decision="retry", reason="logged in", **kw)
    st = runtime.status(**kw)
    assert [t["label"] for t in st["tasks"]] == ["remote-setup"]
    st = report("remote-setup", "ok", {"medium": "pr", "reason": "gh ok", "remote": "origin",
                                       "repo": "o/r", "base": "master"})
    landing = runtime.RunStore(cwd, "m").load_state()["data"]["landing"]
    # the attempt block's return ended landing-medium; optional fields were filled
    assert landing == {"medium": "pr", "reason": "gh ok", "remote": "origin", "repo": "o/r",
                       "base": "master", "pr_url": None, "pr_number": None}
    # pr-publish without force_pr: a failed pr-open falls back to local, no question
    st = report("pr-open", "failed", {"reason": "push refused: stale info"})
    assert st["tasks"][0]["label"].rsplit("/", 1)[-1] == "local-landing-policy"
    st = report("local-landing-policy")
    published = runtime.RunStore(cwd, "m").load_state()["data"]["published"]
    assert published["medium"] == "local" and published["reason"] == "push refused: stale info"
    assert published["pr_url"] is None
    assert [t["label"] for t in st["tasks"]] == ["after"]


def test_pr_or_local_user_local_answer(tmp_path, monkeypatch):
    from claude_launcher.cflow2 import runtime
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    kw = {"cwd": str(tmp_path), "scope": "m", "now": 1.0}
    st = runtime.start(_medium_flow(tmp_path), caps={}, **kw)
    iid = next(t["instance"] for t in st["tasks"] if t["label"].rsplit("/", 1)[-1] == "remote-setup")
    st = runtime.report(iid, "blocked", {"reason": "no remote"}, **kw)
    iid = next(t["instance"] for t in st["tasks"] if t["label"].rsplit("/", 1)[-1] == "ask-frame")
    assert "no remote" in st["tasks"][0]["prompt"]
    runtime.report(iid, "ok", {"brief": BRIEF}, **kw)
    runtime.approve(None, decision="local", reason="this round only", **kw)
    st = runtime.status(**kw)
    assert st["tasks"][0]["label"].rsplit("/", 1)[-1] == "local-landing-policy"
    runtime.report(st["tasks"][0]["instance"], "ok", {}, **kw)
    landing = runtime.RunStore(str(tmp_path), "m").load_state()["data"]["landing"]
    assert landing["medium"] == "local"
    assert landing["reason"] == "user chose local while PR was blocked"


# The six medium scenarios of REVIEW s88 (claunch-yq80p.11): the pr-or-local
# skeleton drives the same task order and data as the two loops it replaced
# (measured old == new at c28cb8a6 / 577b5cce); pinned here as values.
_SETUP = {"medium": "pr", "reason": "gh ok", "remote": "origin", "repo": "o/r", "base": "master"}
_PR = {"url": "u", "number": 7, "branch": "b", "base": "master", "tip": "t", "issues": []}
_LANDED = {**_SETUP, "pr_url": None, "pr_number": None}
_NONE = {"remote": None, "repo": None, "base": None, "pr_url": None, "pr_number": None}
MEDIUM_SCENARIOS = {
    "landing-ok": (False, [("remote-setup", "ok", _SETUP), ("pr-open", "ok", _PR)],
                   ["remote-setup", "pr-open", "after"],
                   {"landing": _LANDED, "published": {**_SETUP, "pr_url": "u", "pr_number": 7}}),
    "landing-retry-ok": (False, [("remote-setup", "blocked", {"reason": "R1"}),
                                 ("answer", "retry", None), ("remote-setup", "ok", _SETUP),
                                 ("pr-open", "failed", {"reason": "F1"})],
                         ["remote-setup", "ask-frame", "remote-setup", "pr-open", "local-landing-policy", "after"],
                         {"landing": _LANDED,
                          "published": {"medium": "local", "reason": "F1", **_NONE}}),
    "landing-local": (False, [("remote-setup", "blocked", {"reason": "R1"}),
                              ("answer", "local", None)],
                      ["remote-setup", "ask-frame", "local-landing-policy", "pr-open"],
                      {"landing": {"medium": "local",
                                   "reason": "user chose local while PR was blocked", **_NONE}}),
    "force-retry-local": (True, [("remote-setup", "ok", _SETUP),
                                 ("pr-open", "failed", {"reason": "F1"}), ("answer", "retry", None),
                                 ("pr-open", "failed", {"reason": "F2"}), ("answer", "local", None)],
                          ["remote-setup", "pr-open", "ask-frame", "pr-open", "ask-frame", "local-landing-policy", "after"],
                          {"landing": _LANDED,
                           "published": {"medium": "local",
                                         "reason": "user chose local after pr-open failed",
                                         **_NONE}}),
    "force-retry-ok": (True, [("remote-setup", "ok", _SETUP),
                              ("pr-open", "failed", {"reason": "F1"}), ("answer", "retry", None),
                              ("pr-open", "ok", _PR)],
                       ["remote-setup", "pr-open", "ask-frame", "pr-open", "after"],
                       {"landing": _LANDED,
                        "published": {**_SETUP, "pr_url": "u", "pr_number": 7}}),
    # a brief spelling 'Local' answers the definition's 'local' (Z2)
    "case-Local": (False, [("remote-setup", "blocked", {"reason": "R1"}),
                           ("answer", "Local", ("retry", "Local"))],
                   ["remote-setup", "ask-frame", "local-landing-policy", "pr-open"],
                   {"landing": {"medium": "local",
                                "reason": "user chose local while PR was blocked", **_NONE}}),
}


def _brief2(n1="retry", n2="local"):
    def opt(n):
        return {"name": n, "summary": "s", "risk": "r", "outcome": "o",
                "reversibility": "reversible"}
    return {"problem": "p", "options": [opt(n1), opt(n2)], "recommendation": n2, "why": "w"}


@pytest.mark.parametrize("scen", list(MEDIUM_SCENARIOS))
def test_pr_or_local_scenarios(scen, tmp_path, monkeypatch):
    from claude_launcher.cflow2 import frontend, runtime
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    force, steps, order, data = MEDIUM_SCENARIOS[scen]
    chart = _medium_flow(tmp_path)
    kw = {"cwd": str(tmp_path), "scope": "m", "now": 1.0}
    st = runtime.start(chart, caps={}, params={"force_pr": force}, **kw)
    seen = []
    for label, outcome, value in steps:
        seen.append(st["tasks"][0]["label"].rsplit("/", 1)[-1])
        if label == "answer":
            frame = next(t for t in st["tasks"] if t["label"].rsplit("/", 1)[-1] == "ask-frame")
            runtime.report(frame["instance"], "ok", {"brief": _brief2(*(value or ()))}, **kw)
            runtime.approve(None, decision=outcome, reason="why", **kw)
            st = runtime.status(**kw)
        else:
            iid = next(t["instance"] for t in st["tasks"] if t["label"].rsplit("/", 1)[-1] == label)
            st = runtime.report(iid, outcome, value, **kw)
        if st.get("tasks") and st["tasks"][0]["label"].rsplit("/", 1)[-1] == "local-landing-policy":
            seen.append("local-landing-policy")
            st = runtime.report(st["tasks"][0]["instance"], "ok", {}, **kw)
    seen.extend(t["label"].rsplit("/", 1)[-1] for t in st.get("tasks") or [])
    assert seen == order
    state = runtime.RunStore(str(tmp_path), "m").load_state()
    assert {k: v for k, v in state["data"].items() if k in ("landing", "published")} == data


# -- landing-review asks whoever takes the landing request (claunch-yq80p.12) -


def _review_route(tmp_path, relation):
    """The escalate branch's route as it is in the definition, with the two
    question nodes, run for one relation."""
    import json

    from claude_launcher.cflow2 import frontend, inputs as inputs_mod, runtime

    doc = yaml.load(V2.read_text(encoding="utf-8"), Loader=frontend._Loader)

    def find(o):
        if isinstance(o, dict):
            if o.get("id") == "landing-review-route":
                return o
            o = list(o.values())
        if isinstance(o, list):
            for v in o:
                hit = find(v)
                if hit:
                    return hit
        return None

    nodes = {k: doc["nodes"][k] for k in ("landing-review", "landing-review-leader")}
    nodes["after"] = {"prompt": "after"}
    flow = {"cflow": 2, "name": "review-route",
            "inputs": {"relation": doc["inputs"]["relation"]},
            "returns": {"held": {"reason": "str?", "rework": "bool"}},
            "nodes": nodes, "body": [find(doc["body"]) or find(doc["procedures"]), "after"]}
    chart = inputs_mod.apply(frontend.compile_text(json.dumps(flow, ensure_ascii=False)),
                             {"relation": relation} if relation else {})
    return runtime.start(chart, cwd=str(tmp_path), scope="m", caps={}, now=1.0)


@pytest.mark.parametrize("relation,label,first", [
    (None, "landing-review", "worker"),
    ("stacked", "landing-review", "worker"),
    ("independent", "landing-review-leader", "leader"),
])
def test_landing_review_asks_by_relation(tmp_path, monkeypatch, relation, label, first):
    from claude_launcher.cflow2 import runtime

    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    st = _review_route(tmp_path, relation)
    [ask] = st["asks"]
    assert ask["label"] == label
    chart = runtime.load_definition("improv-worker-v2")
    node = next(n for n in chart.nodes.values() if n.label == label)
    assert node.get("ask")["from"][0]["role"] == first
    # the same question either way (shared by anchors)
    other = next(n for n in chart.nodes.values() if n.label == "landing-review")
    assert node.get("ask")["prompt"] == other.get("ask")["prompt"]
    assert node.get("ask")["options"] == other.get("ask")["options"]
    # a hold answer ends the round as held with the reason, from either node
    runtime.approve(None, decision="hold", reason="queue is full",
                    cwd=str(tmp_path), scope="m", now=2.0)
    st = runtime.status(cwd=str(tmp_path), scope="m", now=2.0, tick=False)
    assert st["status"] == "done" and st.get("outcome") == "held", st.get("outcome")


def test_landing_review_question_reads_for_a_person_too():
    """claunch-gbw47: with no upper session the question falls through to a
    person, who asked which worker "너에게 딸린 워커" was and why it came to
    them. The question names where the session and the reason are read,
    says both reasons the door opens, and has a paragraph for a person; the
    landing choice says a quick-fork's merge: line is no escalate reason.
    v1 carries the same wording."""
    from claude_launcher.cflow2 import runtime

    chart = runtime.load_definition("improv-worker-v2")
    prompt = next(n for n in chart.nodes.values()
                  if n.label == "landing-review").get("ask")["prompt"]
    landing = next(n for n in chart.nodes.values() if n.label == "landing").props["prompt"]
    v1 = (WF_DIR / "improv-worker.yaml").read_text(encoding="utf-8")
    for text in (prompt, v1):
        assert "너에게 딸린" not in text and "이 질문이 너에게 온 것 자체가 값이다" not in text
        assert "워커 세션 하나가" in text and "from_session" in text
        assert "착지 경로를 스스로 정할 수 없다고 판단했다" in text
        assert "사람에게 온다" in text and "hold다" in text
    assert "data.claim.issue" in prompt
    for text in (landing, v1):
        assert "quick-fork 세션의 개시 블록에 있는 `merge:` 줄" in text
        assert "escalate 사유가 되지 않는다" in text


# -- claunch-106mn: the peer-review question names the round's issue --------


def _peer_review_ask(tmp_path, issue):
    """The peer-review node as it is in the definition, after a stub
    branch-setup, with data.claim set to ``issue``; returns the rendered
    question."""
    import json

    from claude_launcher.cflow2 import frontend, runtime

    doc = yaml.load(V2.read_text(encoding="utf-8"), Loader=frontend._Loader)
    setup = {k: v for k, v in doc["nodes"]["branch-setup"].items() if k in ("title", "returns")}
    flow = {"cflow": 2, "name": "peer-review-ask",
            "data": {"claim": {"issue": issue, "how": "claimed" if issue else "no-issue-wait"}},
            "nodes": {"branch-setup": {**setup, "prompt": "branch-setup"},
                      "peer-review": doc["nodes"]["peer-review"],
                      "after": {"prompt": "after"}},
            "body": ["branch-setup", "peer-review", "after"]}
    chart = frontend.compile_text(json.dumps(flow, ensure_ascii=False))
    kw = dict(cwd=str(tmp_path), scope="m")
    st = runtime.start(chart, caps={}, now=1.0, **kw)
    st = runtime.report(st["tasks"][0]["instance"], "ok",
                        {"branch": "s1-gist", "base": "master", "base_hash": "0" * 40,
                         "shared": False}, now=2.0, **kw)
    [ask] = st["asks"]
    assert ask["label"] == "peer-review"
    return ask["prompt"]


def test_peer_review_question_names_the_round_issue_and_branch(tmp_path, monkeypatch):
    """claunch-106mn: the reviewer read "the in_progress item the asker is
    assignee of" off the board. On a re-request after a re-measure the round's
    issue was already in_review and the reviewer judged the session's HOLD
    issue instead (ask2-93b0effa). The question now carries data.claim.issue
    and the branch, and tells the reader not to pick from the board list."""
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    prompt = _peer_review_ask(tmp_path, "claunch-zzz")
    assert "이슈: claunch-zzz" in prompt
    assert "브랜치: s1-gist" in prompt
    assert "보드 목록에서 고르지 않는다" in prompt
    assert "${data.claim.issue}" not in prompt


def test_peer_review_question_without_an_issue_falls_back_to_both_statuses(tmp_path, monkeypatch):
    """A round with no issue leaves the reference as written; the question
    then says to look in in_progress and in_review and to abstain on two
    candidates."""
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    prompt = _peer_review_ask(tmp_path, None)
    assert "이슈: ${data.claim.issue}" in prompt
    assert "--status in_progress --status in_review" in prompt
    assert "후보가 둘 이상이면 추측하지 말고 abstain한다" in prompt


# -- claunch-0b8dx: no round cap, end on every issue closed ------------------


def test_the_round_count_is_not_capped():
    """User decision 2026-10-07: a run keeps taking rounds while the queue
    has work. The engine wants a positive loop max, so it is set out of
    reach, and node visits (which grow with rounds) are not capped."""
    chart = _chart()
    assert chart.meta["max_visits"] == 0
    rounds = next(n for n in chart.nodes.values() if n.label == "rounds")
    assert rounds.props["max"] >= 10000
    assert "visits.intake" not in V2.read_text(encoding="utf-8").split("self에는")[-1][:400]
    assert "self 상한" not in V2.read_text(encoding="utf-8")


def test_settle_check_ends_only_on_every_issue_closed_or_a_leader_hold():
    chart = _chart()
    prompt = next(n for n in chart.nodes.values() if n.label == "settle-check").props["prompt"]
    words = _words(prompt)
    # every status but closed/tombstone, created by or assigned to this session
    assert "claunch beads list --all --limit 0 --json" in words
    assert "not in ('closed', 'tombstone')" in words
    assert "i.get('assignee')" in words and "i.get('created_by')" in words
    assert "목록이 비어야 완료다" in words
    assert "살아 있는 다른 세션이 assignee라는 것도 정산이 아니다" in words
    # H1 (s88): only a HOLD another session wrote counts; a self HOLD asks up
    assert "`author`가 이 세션이 아닌" in words
    assert "이 세션이 스스로 쓴 `HOLD:`(landing-declined-hold, wrapup의 hold 동결)는 예외가 아니다" in words
    assert "HOLD CONFIRM REQUESTED:" in words
    assert "in_progress인 항목에는 queue-recheck의 done 규칙(unqueue)을 적용하지 않는다" in words


def test_a_held_round_issue_is_not_taken_for_a_next_round():
    """s88 H3: with no round cap, queue-recheck is the gate that keeps a
    held in_progress issue from being worked again round after round."""
    # the procedure's choose node carries no label; find it by its queue read
    chart = _chart()
    prompts = [_words(str(n.props.get("prompt") or "")) for n in chart.nodes.values()
               if n.type == "choose"]
    recheck = [p for p in prompts if "회차가 마감됐다(이슈 닫힘 또는 HOLD" in p]
    assert recheck
    for p in recheck:
        assert "해소되지 않은 `HOLD:` 코멘트가 붙은 in_progress 이슈는 착수할 수 없다" in p


def test_wrapup_and_settle_wait_say_the_new_settlement():
    """s88 H2: no prose left claiming the old 'someone holds it' settlement."""
    text = _words(V2.read_text(encoding="utf-8"))
    assert "두 자리가 같은 것을 읽어야 정산의" not in text
    assert "생성 이슈 정산 대기" not in text
    assert "일감 완료 대기" in text


# -- claunch-60539: the round's issue is recorded in run data ----------------


def test_the_resolved_issue_is_bound_to_data_claim():
    """User decision 2026-10-07 (option A): what resolve-issue settles is the
    run's record of the round's issue, beside the start input it began with."""
    chart = _chart()
    assert chart.data["claim"] == {"issue": None, "how": None}
    bound = [n for n in chart.nodes.values() if getattr(n, "bind", None) == "claim"]
    assert [n.label for n in bound] == ["resolve"]
    # the record has the procedure's return shape
    assert 'ok: {issue: "str?", how: str}' in V2.read_text(encoding="utf-8")


def test_a_pending_start_issue_is_waited_for_then_recorded(tmp_path):
    """claunch-5p2j4, the workflow text only: a session created with "an
    existing issue" and none picked waits in intake for the id, intake-issue
    takes it (and records the start input under its existing flow_input
    clause, pinned by test_cflow2_input_set), and issue-check reads the line
    as claimed rather than as either "no issue" answer. That the daemon
    leaves inputs.issue empty is test_board_input_issue's."""
    import yaml
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "src/claude_launcher/workflows"
    v2 = yaml.safe_load((root / "v2/improv-worker-v2.yaml").read_text(encoding="utf-8"))
    intake = v2["nodes"]["intake"]["prompt"]
    assert "start issue pending:" in intake
    assert "보드를 뒤지거나" in intake and "intake-issue가 한다" in intake
    # the one node that names the start-input tool stays intake-issue
    assert "flow_input" not in intake
    assert "start issue pending:" in v2["nodes"]["intake-issue"]["prompt"]
    check = v2["procedures"]["resolve-issue"]["body"]["choose"]["prompt"]
    assert "`start issue pending:` 줄은 무이슈 답이 아니다" in check
    v1 = (root / "improv-worker.yaml").read_text(encoding="utf-8")
    assert "start issue pending:" in v1
    assert "`start issue pending:` 줄은 무이슈 답이 아니다" in v1
    # v1 has no start input to write
    assert "flow_input" not in v1.split("start issue pending:")[1].split("다음을 정해")[0]


def _find_id(tree, wanted):
    if isinstance(tree, dict):
        if tree.get("id") == wanted:
            return tree
        tree = list(tree.values())
    if isinstance(tree, list):
        for sub in tree:
            got = _find_id(sub, wanted)
            if got is not None:
                return got
    return None


def _prior_flow(tmp_path):
    """intake -> the prior-search block as it is in the definition ->
    intake-issue, with the three nodes' prompts and returns (claunch-onbdf)."""
    doc = yaml.safe_load(V2.read_text(encoding="utf-8"))
    block = _find_id(doc["procedures"], "prior-search")
    keep = ("title", "prompt", "returns", "executor", "effects")
    nodes = {k: {kk: vv for kk, vv in doc["nodes"][k].items() if kk in keep}
             for k in ("intake", "prior-context", "prior-context-fallback", "intake-issue")}
    flow = {"cflow": 2, "name": "prior-flow", "include": doc["include"], "nodes": nodes,
            "data": {"prior": doc["data"]["prior"]},
            "body": ["intake", block, "intake-issue"]}
    from claude_launcher.cflow2 import frontend
    return frontend.compile_text(yaml.safe_dump(flow, allow_unicode=True, sort_keys=False),
                                 resolve_include=layers.resolver(str(tmp_path)))


INTAKE = {"goal": "g", "scope": "s", "branch_gist": "b", "evidence": "e"}


def test_read_only_steps_that_need_a_shell_are_run_by_the_driver():
    chart = _chart()
    got = {n.label: n.executor for n in chart.nodes.values() if n.label in DRIVER_RUN}
    assert got == {label: ["inline"] for label in DRIVER_RUN}


def test_a_failed_prior_context_does_not_end_the_round(tmp_path, monkeypatch):
    """claunch-onbdf: 8 of 9 trial runs lost their round when the read-only
    search delegation failed (a readonly violation, a refused MCP call) and
    went to settle without work, review or landing. The search is not a gate
    (v1 does it inside intake): the driver searches itself and the round goes on."""
    from claude_launcher.cflow2 import runtime
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    kw = {"cwd": str(tmp_path), "scope": "m", "now": 1.0}
    st = runtime.start(_prior_flow(tmp_path), caps={}, **kw)
    iid = next(t["instance"] for t in st["tasks"] if t["label"].rsplit("/", 1)[-1] == "intake")
    st = runtime.report(iid, "ok", INTAKE, **kw)
    iid = next(t["instance"] for t in st["tasks"] if t["label"].rsplit("/", 1)[-1] == "prior-context")
    st = runtime.report(iid, "fail", {"reason": "MCP search refused"}, **kw)
    task = next(t for t in st["tasks"] if t["label"].rsplit("/", 1)[-1] == "prior-context-fallback")
    assert "MCP search refused" in task["prompt"]
    found = {"query": "q", "summary": "검색 실패로 대체 검색: 없음", "related": [], "duplicate": None}
    st = runtime.report(task["instance"], "ok", found, **kw)
    assert [t["label"] for t in st["tasks"]] == ["intake-issue"]
    assert "검색 실패로 대체 검색: 없음" in st["tasks"][0]["prompt"]
    assert runtime.RunStore(str(tmp_path), "m").load_state()["data"]["prior"] == found


def test_an_ok_prior_context_feeds_intake_issue(tmp_path, monkeypatch):
    from claude_launcher.cflow2 import runtime
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    kw = {"cwd": str(tmp_path), "scope": "m", "now": 1.0}
    st = runtime.start(_prior_flow(tmp_path), caps={}, **kw)
    iid = next(t["instance"] for t in st["tasks"] if t["label"].rsplit("/", 1)[-1] == "intake")
    st = runtime.report(iid, "ok", INTAKE, **kw)
    iid = next(t["instance"] for t in st["tasks"] if t["label"].rsplit("/", 1)[-1] == "prior-context")
    found = {"query": "q", "summary": "선행 맥락 없음(쿼리: q)", "related": [], "duplicate": "claunch-x"}
    st = runtime.report(iid, "ok", found, **kw)
    assert [t["label"] for t in st["tasks"]] == ["intake-issue"]
    assert "선행 맥락 없음(쿼리: q)" in st["tasks"][0]["prompt"]
    assert "claunch-x" in st["tasks"][0]["prompt"]


def test_the_end_question_can_be_posed(tmp_path, monkeypatch):
    """claunch-gpccp: with choices {approve, hold} no brief passed -- the
    question validator refuses an option named approve and the ask refuses
    any other name -- so every finished v2 run failed at end-decision. With
    end/hold the brief is accepted and the question reaches the person.
    claunch-kgfr5 adds new-task: a person's answer that hands the session a
    new task leads to end-new-task, not to work done outside the run."""
    from claude_launcher.cflow2 import runtime
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    doc = yaml.safe_load(V2.read_text(encoding="utf-8"))
    decision = _find_id(doc["procedures"], "end-watch")
    flow = {"cflow": 2, "name": "end-flow", "include": ["ask-common", "self-improve-common"],
            "nodes": _end_stubs(), "body": [decision]}
    from claude_launcher.cflow2 import frontend
    chart = frontend.compile_text(yaml.safe_dump(flow, allow_unicode=True, sort_keys=False),
                                  resolve_include=layers.resolver(str(tmp_path)))
    brief = dict(BRIEF, recommendation="end", options=[
        dict(BRIEF["options"][0], name="end"), dict(BRIEF["options"][1], name="hold"),
        dict(BRIEF["options"][1], name="new-task"),
        dict(BRIEF["options"][1], name="self-improve")])
    for scope, answer, then in (("m", "end", "end-notice"), ("n", "new-task", "end-new-task")):
        kw = {"cwd": str(tmp_path), "scope": scope, "now": 1.0}
        st = runtime.start(chart, caps={}, **kw)
        iid = next(t["instance"] for t in st["tasks"] if t["label"].rsplit("/", 1)[-1] == "ask-frame")
        st = runtime.report(iid, "ok", {"brief": brief}, **kw)
        assert st["status"] == "waiting_approval" and [a["node"] for a in st["asks"]]
        runtime.approve(None, decision=answer, reason="done", **kw)
        assert [t["label"] for t in runtime.status(**kw)["tasks"]] == [then]


def test_end_new_task_starts_the_next_run_and_keeps_the_session():
    """claunch-kgfr5: the new-task branch records the task as an issue the
    next run's intake takes and starts that run in the same turn. It no
    longer raises keep-alive around the run's end (claunch-5aysr): lowering
    it again was the step that let a pending kill-on-end land on the new
    run; the daemon re-reads the slot beside the kill instead."""
    doc = yaml.safe_load(V2.read_text(encoding="utf-8"))
    node = doc["nodes"]["end-new-task"]
    text = node["prompt"]
    assert "claunch keep-alive" not in text
    assert "keep-alive는 건드리지 않는다" in text
    assert "flow_start" in text and "inputs: {issue: <그 id>}" in text
    assert "--labels user-direct" in text and "--parent" in text
    assert node["returns"] == {"ok": {"issue": "str"}}


#: The nodes the end-watch loop runs, stubbed: every one prints its own name.
#: The self-improve nodes are the library's own (self-improve-common).
END_NODES = ("end-notice", "end-hold", "end-new-task")
#: A value each self-improve node accepts.
SI_VALUES = {"self-improve-review": {"where": "c", "general": 1, "cflow": 1},
             "self-improve-discuss": {"items": [], "agreed": 0},
             "self-improve-register": {"issues": ["claunch-x"]}}


def _end_stubs():
    return {name: {"prompt": name} for name in END_NODES}


def _brief(*names):
    return dict(BRIEF, recommendation=names[0],
                options=[dict(BRIEF["options"][0], name=n) for n in names])


def test_self_improve_reviews_aligns_registers_and_asks_again(tmp_path, monkeypatch):
    """claunch-c4e6f: self-improve is a fourth answer to the end question.
    It runs the review, the discussion and the person's alignment gate --
    revise goes back to the discussion, register registers -- and then the
    end question is asked again, where new-task can start a registered
    issue. Nothing ends the run until one of end/hold/new-task is answered."""
    from claude_launcher.cflow2 import frontend, runtime
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    doc = yaml.safe_load(V2.read_text(encoding="utf-8"))
    flow = {"cflow": 2, "name": "self-improve-flow",
            "include": ["ask-common", "self-improve-common"],
            "nodes": _end_stubs(), "body": [_find_id(doc["procedures"], "end-watch")]}
    chart = frontend.compile_text(yaml.safe_dump(flow, allow_unicode=True, sort_keys=False),
                                  resolve_include=layers.resolver(str(tmp_path)))
    kw = {"cwd": str(tmp_path), "scope": "m", "now": 1.0}
    st = runtime.start(chart, caps={}, **kw)
    end_q = _brief("end", "hold", "new-task", "self-improve")
    align_q = _brief("register", "revise", "drop")

    def labels(st):
        return [t["label"].rsplit("/", 1)[-1] for t in st["tasks"]]

    def report(st, label, value=None):
        iid = next(t["instance"] for t in st["tasks"] if t["label"].rsplit("/", 1)[-1] == label)
        return runtime.report(iid, "ok", SI_VALUES.get(label, value), **kw)

    def answer(st, brief, decision):
        st = report(st, "ask-frame", {"brief": brief})
        assert st["status"] == "waiting_approval"
        runtime.approve(None, decision=decision, reason="r", **kw)
        return runtime.status(**kw)

    st = answer(st, end_q, "self-improve")
    assert labels(st) == ["self-improve-review"]
    st = report(st, "self-improve-review")
    assert labels(st) == ["self-improve-discuss"]
    st = report(st, "self-improve-discuss")
    st = answer(st, align_q, "revise")          # back to the discussion
    assert labels(st) == ["self-improve-discuss"]
    st = report(st, "self-improve-discuss")
    st = answer(st, align_q, "register")
    assert labels(st) == ["self-improve-register"]
    st = report(st, "self-improve-register")
    # the end question again; drop on a second pass registers nothing
    st = answer(st, end_q, "self-improve")
    st = report(st, "self-improve-review")
    st = report(st, "self-improve-discuss")
    st = answer(st, align_q, "drop")
    assert labels(st) == ["ask-frame"]
    st = answer(st, end_q, "new-task")
    assert labels(st) == ["end-new-task"]
    st = report(st, "end-new-task")
    assert st["status"] == "done"


def test_self_improve_nodes_keep_registration_behind_the_person():
    """claunch-c4e6f: the review and the discussion create no issue; only
    register (after the person's register answer) does, with label
    self-improve, no assignee, and the REGISTERED marker settle-check reads."""
    doc = yaml.safe_load(V2.read_text(encoding="utf-8"))
    lib = yaml.safe_load(SELF_IMPROVE.read_text(encoding="utf-8"))
    assert lib["kind"] == "library" and lib["include"] == ["ask-common"]
    nodes = dict(lib["procedures"]["self-improve"]["nodes"], **doc["nodes"])
    for name in ("self-improve-review", "self-improve-discuss", "self-improve-register"):
        assert name not in doc["nodes"], f"{name} lives in self-improve-common only"
    review = _words(nodes["self-improve-review"]["prompt"])
    assert "RETRO @ <런 id>" in review and "SELF-IMPROVE @ <런 id>" in review
    assert "**general**" in review and "**cflow**" in review
    assert "이슈를 만들지 않는다" in review
    assert nodes["self-improve-review"]["executor"] == ["inline"]
    assert nodes["self-improve-review"]["returns"] == {
        "ok": {"where": "str", "general": "int", "cflow": "int"}}
    discuss = _words(nodes["self-improve-discuss"]["prompt"])
    assert "턴을 끝낸다" in discuss and "이슈를 만들지 않는다" in discuss
    assert "board.write" not in nodes["self-improve-discuss"]["effects"]
    register = _words(nodes["self-improve-register"]["prompt"])
    assert "--labels self-improve" in register
    assert "--assignee" not in register.split("**assignee는 비워 둔다**")[0]
    assert "SELF-IMPROVE REGISTERED @ <런 id>: <id,...>" in register
    assert nodes["self-improve-register"]["returns"] == {"ok": {"issues": "list"}}
    # the person, not the worker, opens registration
    chart = _chart()
    align = next(n for n in chart.nodes.values() if n.label == "self-improve-align")
    _, w = _asked(chart, align)
    assert "from" not in w
    # end-new-task takes a registered issue by assigning it to itself
    new_task = _words(nodes["end-new-task"]["prompt"])
    assert "label `self-improve`" in new_task
    assert "claunch beads update <id> --assignee $CLAUNCH_SESSION" in new_task


def test_self_improve_is_a_library_procedure_any_v2_definition_can_call(tmp_path, monkeypatch):
    """claunch-c4e6f.1: the flow lives in self-improve-common, so a
    definition other than improv-worker-v2 includes it and calls it. It
    returns ok with the registered ids on register and without on drop,
    and the caller goes on to its own next node."""
    from claude_launcher.cflow2 import frontend, runtime
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    flow = {"cflow": 2, "name": "other-flow", "include": ["self-improve-common"],
            "nodes": {"after": {"prompt": "after: ${result['improve'].value.issues}"}},
            "body": [{"id": "improve", "call": "self-improve",
                      "with": {"issue": "claunch-y", "workflow": "other-flow"}},
                     "after"]}
    chart = frontend.compile_text(yaml.safe_dump(flow, allow_unicode=True, sort_keys=False),
                                  resolve_include=layers.resolver(str(tmp_path)))
    align_q = _brief("register", "revise", "drop")
    for scope, decision, issues in (("r", "register", ["claunch-x"]), ("d", "drop", None)):
        kw = {"cwd": str(tmp_path), "scope": scope, "now": 1.0}
        st = runtime.start(chart, caps={}, **kw)
        review = next(t for t in st["tasks"] if t["label"].endswith("self-improve-review"))
        assert "claunch-y" in review["prompt"] and "other-flow" in review["prompt"]
        for label in ("self-improve-review", "self-improve-discuss"):
            iid = next(t["instance"] for t in st["tasks"] if t["label"].endswith(label))
            st = runtime.report(iid, "ok", SI_VALUES[label], **kw)
        iid = next(t["instance"] for t in st["tasks"] if t["label"].endswith("ask-frame"))
        st = runtime.report(iid, "ok", {"brief": align_q}, **kw)
        runtime.approve(None, decision=decision, reason="r", **kw)
        st = runtime.status(**kw)
        if decision == "register":
            iid = next(t["instance"] for t in st["tasks"]
                       if t["label"].endswith("self-improve-register"))
            st = runtime.report(iid, "ok", SI_VALUES["self-improve-register"], **kw)
        assert [t["label"] for t in st["tasks"]] == ["after"]
        assert ("claunch-x" in st["tasks"][0]["prompt"]) == bool(issues)


def test_self_improve_without_an_issue_renders_the_issue_as_empty(tmp_path, monkeypatch):
    """claunch-8dzu2: a round with no claimed issue (no-issue-wait) leaves
    ``$data.claim.issue`` null. The engine turns a null argument into the
    param's default and leaves a reference to a null value unrendered, so
    without a default the review prompt carried the literal
    ``${params.issue}``. The param defaults to the empty string the
    description promises."""
    from claude_launcher.cflow2 import frontend, runtime
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    flow = {"cflow": 2, "name": "other-flow", "include": ["self-improve-common"],
            "data": {"claim": {"issue": None}},
            "body": [{"id": "improve", "call": "self-improve",
                      "with": {"issue": "$data.claim.issue", "workflow": "other-flow"}}]}
    chart = frontend.compile_text(yaml.safe_dump(flow, allow_unicode=True, sort_keys=False),
                                  resolve_include=layers.resolver(str(tmp_path)))
    st = runtime.start(chart, caps={}, cwd=str(tmp_path), now=1.0)
    review = next(t for t in st["tasks"] if t["label"].endswith("self-improve-review"))
    assert "${params.issue}" not in review["prompt"]
    assert "None" not in review["prompt"]
    assert "주 이슈: \n" in review["prompt"]


def test_improv_worker_calls_self_improve_with_its_own_issue_and_definition():
    """claunch-c4e6f.1: the worker hands the library its claimed issue, its
    canonical definition and the retrospective's RETRO comment as the basis."""
    doc = yaml.safe_load(V2.read_text(encoding="utf-8"))
    assert "self-improve-common" in doc["include"]
    call = _find_id(doc["procedures"], "self-improve")
    assert call["call"] == "self-improve"
    assert call["with"]["issue"] == "$data.claim.issue"
    assert "improv-worker-v2.yaml" in call["with"]["workflow"]
    assert "RETRO @ <런 id>" in call["with"]["basis"]


def test_settle_check_excepts_only_a_user_approved_self_improve_issue():
    """claunch-c4e6f (leader s29, msg-74aacd2fcfd6): an issue the person
    agreed to leave on the board is not unsettled -- only with all three of
    label, no assignee and the REGISTERED marker."""
    chart = _chart()
    words = _words(next(n for n in chart.nodes.values()
                        if n.label == "settle-check").props["prompt"])
    assert "세 조건을 모두 만족할 때만이다" in words
    assert "label에 `self-improve`가 있다" in words
    assert "assignee가 비어 있다" in words
    assert "`SELF-IMPROVE REGISTERED @ <런 id>: <id,...>` 코멘트" in words
    # the beads-common block is untouched: still byte-compared with v1
    assert "self-improve" not in COMMON.read_text(encoding="utf-8")


# -- claunch-jbk5w: the landing wait paces the nudge, not the whole choice ----


def _await_flow():
    """The ``await`` loop as it is in the definition, with landing-nudge and
    every other option's branch stubbed by one task named after it."""
    import json

    from claude_launcher.cflow2 import frontend

    doc = yaml.load(V2.read_text(encoding="utf-8"), Loader=frontend._Loader)
    loop = _find_id(doc["procedures"], "await")
    opts = loop["loop"]["do"]["choose"]["options"]
    nodes = {"landing-nudge": {k: v for k, v in doc["nodes"]["landing-nudge"].items()
                               if k in ("title", "prompt", "done_when", "effects")}}
    for name, opt in opts.items():
        if name != "nudge":
            nodes[f"stub-{name}"] = {"prompt": name}
            opt["then"] = [f"stub-{name}", {"return": "ok"}]
    flow = {"cflow": 2, "name": "await-flow",
            "inputs": {"relation": doc["inputs"]["relation"]},
            "returns": {"ok": {}}, "nodes": nodes, "body": [loop]}
    return loop, frontend.compile_text(json.dumps(flow, ensure_ascii=False))


def test_the_landing_wait_paces_only_the_nudge():
    loop, _chart_ = _await_flow()
    assert "every" not in loop["loop"] and "between" not in loop["loop"]
    nudge = loop["loop"]["do"]["choose"]["options"]["nudge"]
    assert nudge["interval"] == 600 and nudge["then"] == ["landing-nudge"]


def test_landed_can_be_chosen_while_a_nudge_is_held(tmp_path, monkeypatch):
    from claude_launcher.cflow2 import runtime

    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    t0 = 1_000_000.0
    kw = {"cwd": str(tmp_path), "scope": "m"}
    _loop, chart = _await_flow()
    runtime.start(chart, caps={}, now=t0, **kw)
    # right after the request: the first nudge has no window record yet
    st = runtime.select(None, "nudge", "no answer yet", now=t0, **kw)
    assert [t["label"] for t in st["tasks"]] == ["landing-nudge"]
    runtime.report(st["tasks"][0]["instance"], now=t0 + 5, **kw)
    # the next nudge is held for the rest of the 10 minutes ...
    st = runtime.select(None, "nudge", "still nothing", now=t0 + 10, **kw)
    assert st["held"]["option"] == "nudge" and st["held"]["opens_at"] == t0 + 600
    # ... and the choice stays open: the parent merged at +26s (s910)
    st = runtime.status(now=t0 + 40, tick=False, **kw)
    assert st["choices"], "the wait closed the choice while the nudge was held"
    st = runtime.select(None, "landed", "merged e3fa3155", now=t0 + 40, **kw)
    assert [t["label"] for t in st["tasks"]] == ["stub-landed"]
    events = [e["event"] for e in runtime.journal(**kw)]
    assert "hold_cancelled" in events


def _settle_flow():
    """settle-poll and its catch as they are in the definition; settled and
    the stall question are stubbed."""
    import json

    from claude_launcher.cflow2 import frontend

    doc = yaml.load(V2.read_text(encoding="utf-8"), Loader=frontend._Loader)
    watch = _find_id(doc["procedures"], "settle-watch")["loop"]["do"]
    poll = watch["try"]
    opts = poll["retry"]["do"]["choose"]["options"]
    opts["settled"]["then"] = ["stub-settled", {"return": "ok"}]
    keep = ("title", "prompt", "done_when", "effects")
    nodes = {"settle-wait": {k: v for k, v in doc["nodes"]["settle-wait"].items() if k in keep},
             "stub-settled": {"prompt": "settled"}, "stub-stall": {"prompt": "stall"}}
    body = {"try": poll, "catch": {"unsettled": ["stub-stall", {"return": "ok"}]}}
    flow = {"cflow": 2, "name": "settle-flow", "returns": {"ok": {}},
            "nodes": nodes, "body": [body]}
    return poll, frontend.compile_text(json.dumps(flow, ensure_ascii=False))


def test_the_settle_wait_paces_only_unsettled():
    poll, _chart_ = _settle_flow()
    assert "every" not in poll["retry"] and poll["retry"]["max"] == 7
    assert poll["retry"]["do"]["choose"]["options"]["unsettled"]["interval"] == 600


def _settle_step(st, kw, now):
    from claude_launcher.cflow2 import runtime

    for t in st["tasks"]:
        assert t["label"] == "settle-wait", t["label"]
        st = runtime.report(t["instance"], now=now, **kw)
    return runtime.status(now=now, tick=False, **kw)


def test_settled_can_be_chosen_while_unsettled_is_held(tmp_path, monkeypatch):
    from claude_launcher.cflow2 import runtime

    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    t0 = 1_000_000.0
    kw = {"cwd": str(tmp_path), "scope": "m"}
    _poll, chart = _settle_flow()
    runtime.start(chart, caps={}, now=t0, **kw)
    st = runtime.select(None, "unsettled", "s1 open", now=t0, **kw)
    st = _settle_step(st, kw, t0 + 1)
    st = runtime.select(None, "unsettled", "s1 open", now=t0 + 2, **kw)
    assert st["held"]["option"] == "unsettled" and st["held"]["opens_at"] == t0 + 600
    # the last issue closes inside the window: settled is still selectable
    st = runtime.select(None, "settled", "all closed", now=t0 + 60, **kw)
    assert [t["label"] for t in st["tasks"]] == ["stub-settled"]
    assert "hold_cancelled" in [e["event"] for e in runtime.journal(**kw)]


def test_unsettled_reaches_the_stall_after_the_same_hour(tmp_path, monkeypatch):
    """Seven checks, ten minutes apart, as with the former ``every: 600``."""
    from claude_launcher.cflow2 import runtime

    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    t0 = 1_000_000.0
    kw = {"cwd": str(tmp_path), "scope": "m"}
    _poll, chart = _settle_flow()
    runtime.start(chart, caps={}, now=t0, **kw)
    st = runtime.select(None, "unsettled", "open", now=t0, **kw)
    for k in range(1, 7):
        st = _settle_step(st, kw, t0 + (k - 1) * 600 + 1)
        runtime.select(None, "unsettled", "open", now=t0 + (k - 1) * 600 + 2, **kw)
        st = runtime.wait_tick(t0 + k * 600 - 1, **kw)
        assert st["ticked"] == 0
        st = runtime.wait_tick(t0 + k * 600, **kw)
        assert st["ticked"] == 1
    assert [t["label"] for t in st["tasks"]] == ["stub-stall"]


# -- claunch-ygbsd: the session retrospective before the end decision ---------


def test_the_retrospective_reads_the_pack_and_runs_inline():
    chart = _chart()
    node = next(n for n in chart.nodes.values() if n.label == "retrospective")
    words = _words(node.props["prompt"])
    # The instructions live in the on-demand pack, the same text a person
    # sends with `claunch retro <session>`; the node only has it read.
    assert "`claunch retro --print`" in words
    assert "RETRO @ <런 id>" in words
    doc = yaml.safe_load(V2.read_text(encoding="utf-8"))
    assert doc["nodes"]["retrospective"]["executor"] == ["inline"]
    # Placed after the settlement and before the end decision.
    body = doc["procedures"]["settle"]["body"]
    ids = [b.get("id") if isinstance(b, dict) else b for b in body]
    retro = next(i for i, b in enumerate(body)
                 if isinstance(b, dict) and b.get("try") == {"seq": ["retrospective"]})
    assert ids.index("settle-watch") < retro < ids.index("end-watch")


@pytest.mark.parametrize("outcome", ["ok", "fail"])
def test_a_failed_retrospective_still_reaches_the_end_decision(tmp_path, monkeypatch, outcome):
    from claude_launcher.cflow2 import frontend, runtime

    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    doc = yaml.safe_load(V2.read_text(encoding="utf-8"))
    body = doc["procedures"]["settle"]["body"]
    retro = next(b for b in body
                 if isinstance(b, dict) and b.get("try") == {"seq": ["retrospective"]})
    flow = {"cflow": 2, "name": "retro-flow",
            "nodes": {"retrospective": doc["nodes"]["retrospective"],
                      "end-stub": {"prompt": "end"}},
            "body": [retro, "end-stub"]}
    chart = frontend.compile_text(yaml.safe_dump(flow, allow_unicode=True, sort_keys=False),
                                  resolve_include=layers.resolver(str(tmp_path)))
    kw = {"cwd": str(tmp_path), "scope": "m", "now": 1.0}
    st = runtime.start(chart, caps={}, **kw)
    task = next(t for t in st["tasks"] if t["label"].rsplit("/", 1)[-1] == "retrospective")
    value = {"where": "claunch-x", "proposals": 2, "problems": 1} if outcome == "ok" else {}
    st = runtime.report(task["instance"], outcome, value, **kw)
    assert [t["label"] for t in st["tasks"]] == ["end-stub"]


# -- the round record (claunch-qs4l9) ----------------------------------------

#: The nodes where a round's exploration happens; each reports its tries and
#: gets the rule that says to leave them on the issue as they happen.
TRIES = ("work", "review", "targeted-test", "rebase")


def test_the_tries_rule_reaches_the_exploring_nodes_and_their_returns_require_it():
    from claude_launcher.cflow2 import runtime
    chart = _chart()
    assert "record-tries" in chart.rules
    doc = yaml.safe_load(V2.read_text(encoding="utf-8"))
    assert doc["rules"]["record-tries"]["deliver"] == list(TRIES)
    by_label = {n.label: n for n in chart.nodes.values() if n.type == "task"}
    for label in TRIES:
        authored, _, rules = by_label[label].props["prompt"].partition("── 규칙 ──")
        # delivered by the compiler, not typed into each prompt
        assert "TRIED:" in rules, label
        assert "TRIED:" not in authored, label
        ok = doc["nodes"][label]["returns"]["ok"]
        assert ok["tried"] == "list", label
        # required (an empty list is the answer for a round that went straight)
        without = {k: None for k in ok if k != "tried"}
        assert any("tried" in p for p in runtime.validate_result({"ok": ok}, "ok", without)), label
    work_ok = doc["nodes"]["work"]["returns"]["ok"]
    assert runtime.validate_result({"ok": work_ok}, "ok",
                                   {"files": [], "summary": "s", "tried": []}) == []


def test_wrapup_leaves_the_html_report_to_round_report():
    doc = yaml.safe_load(V2.read_text(encoding="utf-8"))
    words = _words(doc["nodes"]["wrapup"]["prompt"])
    # s906/s910/s934/s935: the driver wrote the page in wrapup and the
    # round-report delegation wrote it again
    assert ("이 스텝이 내는 것은 셋: **깨끗한 트리**, **이 세션이 만든 테스트 "
            "부산물의 정리**, **넘길 것마다 handoff 이슈**.") in words
    assert "**회차 보고서 HTML은 이 스텝에서 쓰지 않는다.**" in words
    assert "claunch report path" not in words
    assert doc["nodes"]["wrapup"]["returns"]["ok"]["gaps"] == "list?"


# claunch-4zc51.1: the wrapup used to keep basetemps "as forensic records" and
# nothing removed them afterwards -- C:/t reached 28.06 GB of exited sessions'
# trees and C: fell to 264 MB free. Both canonicals now have the session that
# made them remove them, after moving the four values into the report.
_OWN_ARTIFACT_LINES = (
    "**이 세션이 직접 만든 테스트 부산물은 이 세션이 전부 지운다**",
    "── 테스트 부산물 정리 (이 세션이 만든 것만, 전부) ──",
    "`ls -d C:/t/${CLAUNCH_SESSION}c[0-9]* C:/t/${CLAUNCH_SESSION}-* 2>/dev/null`",
    "접두사만으로 고르지 않는다: `C:/t/s98*`은 s987의 것까지 잡는다.",
    "`git worktree remove --force <경로>`",
    "다른 세션의 항목은 그 세션이 이미 종료됐어도 여기서 지우지 않는다",
)


@pytest.mark.parametrize("which", ["v1"])
def test_wrapup_removes_the_test_artifacts_its_own_session_made(which):
    if which == "v1":
        step = yaml.safe_load(V1.read_text(encoding="utf-8"))["steps"]["wrapup"]
        words, done = _words(step["instructions"]), _words(step["done_when"])
    else:
        node = yaml.safe_load(V2.read_text(encoding="utf-8"))["nodes"]["wrapup"]
        words, done = _words(node["prompt"]), _words(node["done_when"])
    assert "basetemp는 지우지 않는다" not in words
    for line in _OWN_ARTIFACT_LINES:
        assert _words(line) in words, line
    assert "(시작 · 소요 · 워커 수 · 실행량)" in done
    assert "`C:/t/<세션>c<10자리>`·`C:/t/<세션>-*`" in done


@pytest.mark.parametrize("which", ["v1"])
def test_wrapup_keeps_a_held_rounds_preview_ref(which):
    # tools/merge_ready.py reads refs/claunch/preview/<branch> as the landing
    # evidence of a frozen round; deleting it in a hold wrapup sends the
    # leader's integrate back to re-measure (rc=1)
    if which == "v1":
        step = yaml.safe_load(V1.read_text(encoding="utf-8"))["steps"]["wrapup"]
        words, done = _words(step["instructions"]), _words(step["done_when"])
    else:
        node = yaml.safe_load(V2.read_text(encoding="utf-8"))["nodes"]["wrapup"]
        words, done = _words(node["prompt"]), _words(node["done_when"])
    assert "`refs/claunch/preview/<세션>-*`" not in words
    assert ("`refs/claunch/preview/<브랜치>`는 회차가 착지했을 때만 지운다. "
            "hold로 동결한 회차는 그 ref를 남긴다") in words
    assert "「착지 근거라 남김」" in words
    assert ("hold 회차의 `refs/claunch/preview/<브랜치>`는 착지 근거라 "
            "남기고") in done


def test_v2_wrapup_uses_bounded_cleanup_and_records_deferred_items():
    node = yaml.safe_load(V2.read_text(encoding="utf-8"))["nodes"]["wrapup"]
    words, done = _words(node["prompt"]), _words(node["done_when"])
    assert "artifact_cleanup {}" in words and "plan_id" in words
    assert "cleanup_pending" in words and "cleanup_pending" in done
    assert "거절 뒤 셸/Python/다른 도구로 삭제를 다시 시도" in words
    assert "preview ref는 착지 여부와 관계없이 보존" in words
    assert "보드 마감은 별도 호출" in words
    assert "git worktree remove --force" not in words


def test_v1_wrapup_cleanup_stays_inside_the_reminder_window():
    # the reminder re-fire carries only the first _INSTRUCTIONS_LIMIT chars
    from claude_launcher.daemon.cflow_clock import _INSTRUCTIONS_LIMIT
    step = yaml.safe_load(V1.read_text(encoding="utf-8"))["steps"]["wrapup"]
    window = step["instructions"][:_INSTRUCTIONS_LIMIT]
    assert "부산물의 정리**" in _words(window)
    assert "이 세션이 직접 만든 테스트 부산물은 이 세션이 전부 지운다" in window


def test_v2_wrapup_artifacts_reach_the_round_report():
    doc = yaml.safe_load(V2.read_text(encoding="utf-8"))
    assert doc["nodes"]["wrapup"]["returns"]["ok"]["artifacts"] == "list?"
    words = _words(doc["nodes"]["round-report"]["prompt"])
    assert "정리한 테스트 부산물: ${result.wrapup.value.artifacts}" in words


def test_round_report_reads_the_round_records_before_writing():
    doc = yaml.safe_load(V2.read_text(encoding="utf-8"))
    node = doc["nodes"]["round-report"]
    words = _words(node["prompt"])
    assert "${result.wrapup.value.gaps}" in words
    for source in ("`claunch cflow2 journal`", "`TRIED:`", "`claunch commits`"):
        assert source in words, source
    # a spawned child first (claunch-xjbrh); the records are what lets it run
    # without the chat, and it names the run's session since its own differs
    assert node["executor"][0] == "session"
    for flag in ("-t <scope>", "--session <scope>", "report path --session <scope>"):
        assert flag in words, flag


# -- claunch-5itbf: an unroutable peer review is the agent's recorded choice ---


def _peer_review_loop(tmp_path):
    """The definition's peer-review loop (the review question and its
    unrouted handler) run on its own, followed by a marker task."""
    import json

    from claude_launcher.cflow2 import frontend, runtime

    doc = yaml.load(V2.read_text(encoding="utf-8"), Loader=frontend._Loader)

    def find(o):
        if isinstance(o, dict):
            if o.get("id") == "peer-review-loop":
                return o
            o = list(o.values())
        if isinstance(o, list):
            for v in o:
                hit = find(v)
                if hit:
                    return hit
        return None

    flow = {"cflow": 2, "name": "peer-review-route",
            "returns": {"rework": {"reason": "str?", "rework": "bool"}},
            "nodes": {"peer-review": doc["nodes"]["peer-review"], "after": {"prompt": "after"}},
            "body": [find(doc["procedures"]) or find(doc["body"]), "after"]}
    chart = frontend.compile_text(json.dumps(flow, ensure_ascii=False))
    runtime.start(chart, cwd=str(tmp_path), scope="m", caps={}, now=1.0)
    return {"cwd": str(tmp_path), "scope": "m", "now": 1.0}


def _ambiguous_pool(**_):
    from claude_launcher.cflow import responders

    return responders.Pool(problem="session 'm' is in several meshes (gds6, temp)",
                           ambiguous=("gds6", "temp"))


def _reviewer_pool(**kw):
    from claude_launcher.cflow import responders

    if kw.get("mesh") != "gds6":
        return _ambiguous_pool()
    rev = responders.Responder(session="s-rev", handle="rev", role="reviewer", local=True,
                               reachability="idle")
    return responders.Pool(mesh="gds6", me="m", members={"rev": rev}, reachable={"rev"},
                           descendants={"rev"}, generation={"rev": 1})


def test_peer_review_declares_on_unroutable():
    chart = _chart()
    node = next(n for n in chart.nodes.values() if n.label == "peer-review")
    assert node.get("ask")["on_unroutable"] == "unrouted"
    assert node.get("ask")["otherwise"] == "self"  # self:pass still covers "nobody"
    assert "unrouted" in node.get("returns")
    choice = next(n for n in chart.nodes.values() if n.label == "review-unrouted")
    assert choice.type == "choose" and choice.get("chooser") == "agent"
    assert choice.get("require_reason") is True
    assert sorted(o["name"] for o in choice.get("options")) == [
        "direct-changes", "direct-pass", "retry", "skip"]


def test_unroutable_review_goes_to_the_agent_and_retry_asks_again(tmp_path, monkeypatch):
    from claude_launcher.cflow2 import asks, runtime

    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    kw = _peer_review_loop(tmp_path)
    sent = []
    deliver = lambda rec, ctx: sent.append(ctx) or None  # noqa: E731
    asks.route(pool_fn=_ambiguous_pool, deliver_fn=deliver, **kw)
    st = runtime.status(tick=False, **kw)
    assert sent == [] and st["tasks"] == []
    (choice,) = st["choices"]
    assert choice["label"] == "review-unrouted" and "several meshes" in choice["prompt"]
    with pytest.raises(runtime.FlowError):
        runtime.select(None, "retry", **kw)  # require_reason
    runtime.set_mesh("gds6", caller=runtime.CALLER_USER, **kw)
    runtime.select(None, "retry", "named gds6 for the run", **kw)
    asks.route(pool_fn=_reviewer_pool, deliver_fn=deliver, **kw)
    (a,) = runtime.status(tick=False, **kw)["asks"]
    assert a["label"] == "peer-review" and a["asked"] == ["rev"]
    assert sent[-1]["mesh"] == "gds6"
    runtime.answer(a["ask"], "pass", "ok", by_session="s-rev", **kw)
    assert [t["label"] for t in runtime.status(tick=False, **kw)["tasks"]] == ["after"]
    picked = [e for e in runtime.journal(cwd=kw["cwd"], scope="m") if e["event"] == "selected"]
    assert picked and picked[-1].get("reason") == "named gds6 for the run"


@pytest.mark.parametrize("option,end", [("skip", "after"), ("direct-pass", "after"),
                                        ("direct-changes", "rework")])
def test_unroutable_review_options(tmp_path, monkeypatch, option, end):
    from claude_launcher.cflow2 import asks, runtime

    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    kw = _peer_review_loop(tmp_path)
    asks.route(pool_fn=_ambiguous_pool, deliver_fn=lambda *a: None, **kw)
    runtime.select("review-unrouted", option, "checked the board", **kw)
    st = runtime.status(tick=False, **kw)
    if end == "after":
        assert [t["label"] for t in st["tasks"]] == ["after"]
    else:
        assert st["status"] == "done" and st.get("outcome") == "rework"


# -- claunch-g0ttr.1: the request nudge waits for the daemon's routing --------


def test_the_request_nudge_is_sent_after_the_routing_result():
    """The landing push is routed when integration-request is left, so a
    nudge sent inside that step cannot know who holds the queue (s332 nudged
    its parent 41s before the push was skipped). The first landing-nudge
    sends it, from the daemon's ``landing request routed`` block."""
    from claude_launcher.cflow2 import frontend

    doc = yaml.load(V2.read_text(encoding="utf-8"), Loader=frontend._Loader)
    req = " ".join(doc["nodes"]["integration-request"]["prompt"].split())
    assert "이 스텝에서는 요청 nudge를 보내지 않는다" in req
    assert "landing request routed" in req
    assert "[ask]" not in req, "the request nudge template moved to landing-nudge"
    nudge = " ".join(doc["nodes"]["landing-nudge"]["prompt"].split())
    for anchor in ("landing request routed", "NOT queued", "라우팅 대기",
                   "trigger_skipped", "[ask]", "몇 번째 재촉",
                   "동결은 그대로다", "보드에 코멘트를 새로 쌓지 않는다"):
        assert anchor in nudge, f"landing-nudge lost {anchor!r}"


# -- claunch-77cu6: the targeted-test self-gate and a skip that reaches the request


def _flat(text) -> str:
    return " ".join(str(text).split())


def test_the_test_decision_is_a_self_gate_with_measured_skip_conditions():
    """``skip`` is the agent's own call, made on command output: every file is
    non-behavioural, no test names it, and no full-sweep reason applies.
    Anything unclear is ``run``."""
    from claude_launcher.cflow2 import frontend

    doc = yaml.load(V2.read_text(encoding="utf-8"), Loader=frontend._Loader)
    decision = _find_id(doc, "test-decision")["choose"]
    assert decision["chooser"] == "agent" and decision["require_reason"] is True
    assert set(decision["options"]) == {"run", "skip"}
    assert decision["options"]["skip"]["then"] == "test-skipped"
    prompt = _flat(decision["prompt"])
    for anchor in ("셀프 게이트", "**전부** 참일 때만", "git diff --name-only",
                   "tools/changed_tests.py --list", 'grep -rl "<파일 이름>" tests/',
                   "src/claude_launcher/workflows/", "판단이 서지 않으면 `run`",
                   "통합 후 전체 스윕"):
        assert anchor in prompt, f"test-decision lost {anchor!r}"


def test_a_skipped_test_leaves_a_board_marker_the_landing_steps_read():
    """The skip is recorded once, as ``TESTS SKIPPED @ <hash>:`` on the issue,
    and every later step that used to ask for targeted numbers reads that
    marker instead of escalating or re-measuring."""
    from claude_launcher.cflow2 import frontend

    doc = yaml.load(V2.read_text(encoding="utf-8"), Loader=frontend._Loader)
    nodes = doc["nodes"]
    skipped = _flat(nodes["test-skipped"]["prompt"])
    assert "TESTS SKIPPED @ <HEAD 해시>:" in skipped
    assert "0/0 green" in skipped  # named as the forbidden stand-in
    assert "TESTS SKIPPED @" in nodes["test-skipped"]["done_when"]
    assert "board.write" in nodes["test-skipped"]["effects"]

    landing = _flat(_find_id(doc, "landing")["choose"]["prompt"])
    assert "<TESTS SKIPPED 마커의 해시> HEAD" in landing
    assert "수치가 없다는 이유로 escalate하지 않는다" in landing

    rebase = _flat(nodes["rebase"]["prompt"])
    assert "표적 테스트를 생략한 일감 (TESTS SKIPPED 마커)" in rebase
    assert "머지와 `git update-ref` 줄만 실행한다" in rebase

    review_ask = _flat(nodes["peer-review"]["ask"]["prompt"])
    assert "TESTS SKIPPED @" in review_ask and "표적 테스트 필요" in review_ask

    request = _flat(nodes["integration-request"]["prompt"])
    assert "tests: skipped — TESTS SKIPPED @ <해시>: <사유>" in request
    assert "tests: skipped (TESTS SKIPPED @ <해시>)" in _flat(nodes["commit"]["prompt"])

    # a stacked child lands on its parent's stack, whose review reads the same marker
    stack = _flat((WF_DIR / "v2" / "stack-v2.yaml").read_text(encoding="utf-8"))
    assert "`TESTS SKIPPED @ <해시>:` 코멘트와 그 판정이 diff에 맞는지" in stack


def test_a_skip_goes_from_the_choice_to_commit_without_a_test_task(tmp_path, monkeypatch):
    """Routing: ``skip`` -> test-skipped -> commit; review and targeted-test
    are never entered."""
    from claude_launcher.cflow2 import runtime

    st, kw = _coding_item(tmp_path, monkeypatch, None)
    [work] = st["tasks"]
    st = runtime.report(work["instance"], "ok", {}, **kw)
    [choice] = st["choices"]
    assert choice["label"] == "test-decision"
    st = runtime.select(choice["instance"], "skip", reason="css only, no test names it", **kw)
    seen = []
    while st["tasks"]:
        [task] = st["tasks"]
        seen.append(task["label"])
        if task["label"] == "work":
            break
        st = runtime.report(task["instance"], "ok", {}, **kw)
    assert seen[:2] == ["test-skipped", "commit"]
    assert "review" not in seen and "targeted-test" not in seen


# -- series batching (claunch-7r1qr) -------------------------------------------


def test_a_series_of_related_issues_lands_as_one_batch():
    """s791 landed claunch-iw5i8.1-.5 as five one-commit PRs: each queue-next
    read the next sibling as blocked (it depended on the issue just committed,
    still in_progress) or as another area, and drained. A series -- siblings
    under one parent, children and follow-ups, issues depending on one already
    in the batch -- now stays on one branch and lands as one PR."""
    chart = _chart()
    labelled = {n.label: n for n in chart.nodes.values() if n.label}
    gate = _words(labelled["queue-next"].get("prompt"))
    assert "같은 연속 작업(series)" in gate
    assert "달라도 next다" in gate                      # area does not cut a series
    assert "blocked 판정은 배치 밖 의존만 센다" in gate  # in-batch deps are met
    assert "연속 작업의 일감이면 모호해도 next다" in gate
    assert "모호하면 drained다" in gate                 # the tie-break for the rest
    # a follow-up the user gives before the landing request joins the batch
    assert "--parent <주 이슈> --labels user-direct --assignee $CLAUNCH_SESSION" in gate
    # the cap the prompt counts is the cap the loop enforces
    assert "10개 미만" in gate and labelled["items"].get("max") == 10
    intake = _words(labelled["intake-issue"].get("prompt"))
    assert "영역(daemon·web UI·워크플로)이 달라도 이 배치의 후보다" in intake
    assert "배치 안 일감에만 막힌 의존은 blocked로 세지 않는다" in intake
    pr = _words(labelled["pr-open"].get("prompt"))
    assert "PR 하나가 연속 작업 전체다" in pr
    assert "공통 parent(없으면 주 이슈)" in pr
