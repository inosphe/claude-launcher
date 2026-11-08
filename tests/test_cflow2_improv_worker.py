"""improv-worker-v2: the v2 port of improv-worker.yaml (claunch-yq80p).

The port lives in ``workflows/v2/improv-worker-v2.yaml`` with its shared
fragments, rules and procedures in ``workflows/v2/improv-common.yaml``.
These tests hold the port to four things: it compiles with no check errors,
every v1 step has a named place in v2, its procedures are callable from
another definition, and the beads rule block is the v1 block byte for byte.
"""

from __future__ import annotations

import re
import textwrap
from pathlib import Path

import pytest
import yaml

import claude_launcher
from claude_launcher.cflow2 import check, frontend, layers
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

# The findings the checker reports. They are accepted, with these reasons:
#   par-write-overlap [round/item/work-group] — the `found` group runs the
#   work task next to found-issue members. The members write the board only
#   through `claunch beads` (one DB, its own lock) and spawn sessions; they do
#   not write the worktree, so there is no path to declare and no overlap
#   with `work`'s fs.write. The checker cannot see that from undeclared paths.
#   par-write-overlap [round/item/slices] — the parallel_fork group
#   (claunch-dm09i). Its members write the checkout, but each one only the
#   paths work hands it at flow_fork (`with.paths`), which are decided per
#   item and cannot be declared in the chart; slice-merge checks the boundary
#   with `git status` before it commits.
ACCEPTED_WARNINGS = {("par-write-overlap", "round/item/work-group"),
                     ("par-write-overlap", "round/item/slices"),
                     # claunch-0b8dx: no cap on the number of rounds
                     ("unbounded-visits", None),
                     # claunch-yzwut: the one place a worker may merge master --
                     # merge-by-agent's apply step, once per attempt (the
                     # self-merge procedure tries three times). The ban is the
                     # conditional rule master-merge-effect, so the check warns
                     # instead of failing; a fourth entry here is a second
                     # merging node and must be argued for, not added.
                     ("never-effect-when", "round/land/self-merge-run.try/self-merge-apply"),
                     ("never-effect-when", "round/land/self-merge-run.try/self-merge-apply#2"),
                     ("never-effect-when", "round/land/self-merge-run.try/self-merge-apply#3")}

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
    "rework-gate": ["rework-budget", "rework-auto", "rework-decision", "rework-gate"],
    "landing-declined-hold": ["landing-declined-hold"],
    "landed": ["landed-wait", "landed"],
    "wrapup": ["wrapup", "round-report"],
    "queue-recheck": ["recheck"],
    "stack-round": ["stack-round"],
    "settle-check": ["settle-check", "settle-poll"],
    "settle-wait": ["settle-wait"],
    "settle-stall": ["settle-stall"],
    "end-gate": ["end-decision"],
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
V1_ONLY = """self에는 런 단위 상한이 있다: 이 런의 intake 방문 수
(`claunch cflow status --json`의 `visits.intake`)가 10 이상이면 self
대신 pool로 보낸다 — 엔진의 loop guard(max_visits 25)는 26번째
intake에서 런을 사람의 승인(`loop_limit`)으로 세우는데, 그 전에 범위
판단을 상위에게 넘기기 위해서다. 리더는 거부권을 가진다(`update <id>
--assignee ""` + `UNQUEUED by leader: <이유>`); 거부된 이슈는
pool과 같이 상위의 처분을 따른다. 세션이 만든 활성
이슈가 전부 누군가의 손(살아 있는 세션의 assignee, 또는 리더의
`HOLD:` 코멘트)에 있어야 세션이 끝난다 — 워커의 settle-check가"""
V2_INSTEAD = """self에는 회차 수 상한이 없다(claunch-0b8dx). 리더는
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

#: claunch-eyz0s.2 gave v2's landing check 4 the tip-carrying marker form, so
#: the shared marker list had to show it -- and it diverged from v1's tip-less
#: spelling. claunch-eyz0s.2.1 ported check 4 to v1, so the two blocks say the
#: same thing again and that divergence is gone: the marker list is no longer
#: in DIVERGENCES, and a re-introduced difference is a test failure, not a
#: silently allowed one.

#: Every declared v2 divergence from the v1 block, as (v1 text, v2 text).
DIVERGENCES = [(V1_ONLY, V2_INSTEAD), (V1_ONLY_BATCH, V2_INSTEAD_BATCH)]


def _words(text: str) -> str:
    return " ".join(text.split())


def _procedure_prose(chart) -> str:
    """``description`` of every procedure the chart's definition can reach.

    A procedure's own ``description`` never becomes a node: the frontend
    keeps it on its internal procedure record and drops it, so no amount of
    walking ``chart.nodes`` reaches it (claunch-qt475). It is still text an
    author writes for a reader, so a guard aimed at it has to read the
    files -- the chart's file and everything it ``include``s, transitively.

    Nodes declared inside a procedure body are already in ``chart.nodes``
    when a step reaches them; this adds only the descriptions, which no
    compiled chart carries at all.
    """
    source = Path(chart.source or "")
    if not source.is_file():
        return ""
    chunks, seen, pending = [], set(), [source.resolve()]
    while pending:
        path = pending.pop()
        if path in seen:
            continue
        seen.add(path)
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(doc, dict):
            continue
        for raw in (doc.get("procedures") or {}).values():
            if isinstance(raw, dict) and isinstance(raw.get("description"), str):
                chunks.append(raw["description"])
        for name in doc.get("include") or []:
            # beside the including file first: a layer's library is looked up
            # next to it, which is what the resolver does for that layer
            beside = path.parent / f"{name}.yaml"
            hit = beside if beside.is_file() else layers.find(str(name))
            if hit is not None:
                pending.append(Path(hit).resolve())
    return _words(" ".join(chunks))


def _chart_prose(chart) -> str:
    """Every piece of prose the chart hands a reader, in words.

    Crediting only labelled nodes misses most of it: of this workflow's 788
    nodes, 498 have no label, and a ``choose`` option's ``description`` --
    the line a session reads when it picks the option -- lives in the
    ``options`` list of just such a node. A guard that reads only labelled
    ``prompt``/``description`` therefore asserts its absence against a text
    that was never collected, and stays green through any revert
    (claunch-f56fz, second review).

    So collect from every node, labelled or not, and read the option list
    too: ``name``/``description`` per option are what the chooser sees. The
    procedures' own descriptions are not in any node and come from the
    files (:func:`_procedure_prose`) -- claunch-qt475.

    ``done_when`` is prose too, and it is the line a step is judged by: 97
    nodes carry one and it is in no ``prompt``, ``description`` or option,
    so an anchor aimed at it used to pass whatever it said
    (claunch-4zz0b).
    """
    chunks = []
    for node in chart.nodes.values():
        for key in ("prompt", "description", "done_when"):
            value = node.get(key)
            if isinstance(value, str):
                chunks.append(value)
        options = node.get("options")
        if isinstance(options, list):
            for option in options:
                if not isinstance(option, dict):
                    continue
                for value in option.values():
                    if isinstance(value, str):
                        chunks.append(value)
    return _words(" ".join(chunks + [_procedure_prose(chart)]))


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
    st = runtime.start(chart, caps={}, **kw)
    return (_start_work(st, kw) if approval is not True else st), kw


def _plan_clear(st, kw):
    """Let the plan stop pass: the probe answers 0, so the plan owes the user
    nothing and the round goes on (claunch-rzepk).

    A test has no probe runner of its own, so the wait would sit forever.
    The helper asserts the run really is at a plan wait, so a route that
    stopped stopping there is red here rather than silently passing the
    assertion that follows."""
    from claude_launcher.cflow2 import runtime

    waits = [w for w in st.get("waits") or []
             if "plan-wait" in str(w.get("node") or "")]
    assert waits, f"no plan wait to clear (tasks {[t['label'] for t in st['tasks']]})"
    return runtime.wait_tick(kw["now"], lambda cmd, cwd: (0, ""),
                             cwd=kw["cwd"], scope=kw["scope"])


def _start_work(st, kw):
    """The gate-less round start: plan-seed, the plan stop, then work.

    Every path that skipped ``coding-plan`` now writes the plan first and
    waits on it (claunch-rzepk 규칙 1·3), so a test that used to find
    ``work`` here crosses both nodes."""
    from claude_launcher.cflow2 import runtime

    if any(t["label"] == "plan-seed" for t in st["tasks"]):
        st = runtime.report(_one(st, "plan-seed"), "ok", {"plan_waiting": 0}, **kw)
    return _plan_clear(st, kw)


@pytest.mark.parametrize("approval", [None, False])
def test_coding_approval_disabled_preserves_direct_work(tmp_path, monkeypatch, approval):
    st, _ = _coding_item(tmp_path, monkeypatch, approval)
    assert [t["label"] for t in st["tasks"]] == ["work"]
    assert not st.get("asks")


def _approve_item(st, kw, visit, decision):
    """One item through the plain gate: plan, a person's answer, work, done."""
    from claude_launcher.cflow2 import runtime

    [plan] = st["tasks"]
    assert plan["label"] == "coding-plan"
    brief = f"item {visit}: edit workflow and test true/false routing"
    st = runtime.report(plan["instance"], "ok", {"brief": brief}, **kw)
    # the plan stop sits between the plan and the gate (claunch-rzepk)
    st = _plan_clear(st, kw)
    assert not st["tasks"]
    [ask] = st["asks"]
    assert ask["label"] == "coding-approval"
    assert brief in ask["prompt"]
    # urgent: the dashboard raises it on its notification channel (claunch-x76l8.1)
    assert ask["urgent"] is True and ask["resolution"] == "human"
    # both approvals and the reject are the person's answers (claunch-sxdnc)
    assert [o["name"] for o in ask["options"]][:3] == [
        "approve-once", "approve-always", "reject"]
    with pytest.raises(runtime.FlowError, match="not yours to answer"):
        runtime.approve(None, decision=decision, caller="agent", **kw)
    with pytest.raises(runtime.FlowError):
        runtime.report(ask["instance"], "ok", {}, **kw)
    # Time alone must not release a human gate.
    later = runtime.status(**{**kw, "now": 100000.0})
    assert not later["tasks"] and later["asks"]
    runtime.approve(None, decision=decision, reason="plan accepted", **kw)
    return _work_item(runtime.status(**kw), kw)


def _approval_data(kw):
    """The run's $data.coding_approval, read off its state file."""
    import json

    from claude_launcher.cflow2.store import RunStore

    state = json.loads(RunStore(kw["cwd"], kw["scope"]).state_path.read_text(encoding="utf-8"))
    return state["data"]["coding_approval"]


def _work_item(st, kw):
    from claude_launcher.cflow2 import runtime

    [work] = st["tasks"]
    assert work["label"] == "work"
    st = runtime.report(work["instance"], "ok", {}, **kw)
    st = runtime.select(st["choices"][0]["instance"], "skip", reason="fixture", **kw)
    for label in ("test-skipped", "commit"):
        [task] = st["tasks"]
        assert task["label"] == label
        st = runtime.report(task["instance"], "ok", {}, **kw)
    return st


def test_coding_approval_once_asks_again_on_the_next_item(tmp_path, monkeypatch):
    st, kw = _coding_item(tmp_path, monkeypatch, True)
    for visit in (1, 2):
        st = _approve_item(st, kw, visit, "approve-once")
    assert st["status"] == "done"
    assert _approval_data(kw)["decision"] == "approve-once"


def test_coding_approval_always_skips_the_gate_for_later_items(tmp_path, monkeypatch):
    from claude_launcher.cflow2 import runtime

    st, kw = _coding_item(tmp_path, monkeypatch, True)
    st = _approve_item(st, kw, 1, "approve-always")
    assert _approval_data(kw)["decision"] == "approve-always"
    # The second item skips the gate, and still stops on the plan: approve-
    # always turns off the gate, not the plan wait (claunch-rzepk 규칙 4).
    assert not st.get("asks")
    st = _plan_clear(runtime.report(_one(st, "plan-seed"), "ok",
                                    {"plan_waiting": 0}, **kw), kw)
    st = _work_item(st, kw)
    assert st["status"] == "done"


def test_coding_approval_always_holds_after_a_reject(tmp_path, monkeypatch):
    """A reject before the approval does not stand in the way of 'always'."""
    from claude_launcher.cflow2 import runtime

    st, kw = _coding_item(tmp_path, monkeypatch, True)
    [plan] = st["tasks"]
    st = runtime.report(plan["instance"], "ok", {"brief": "first"}, **kw)
    st = _plan_clear(st, kw)
    runtime.approve(None, decision="reject", reason="too broad", **kw)
    st = runtime.status(**kw)
    assert _approval_data(kw)["decision"] == "reject"
    st = _approve_item(st, kw, 1, "approve-always")
    assert not st.get("asks")
    # the next item writes its plan first and still stops on it (규칙 4)
    st = _plan_clear(runtime.report(_one(st, "plan-seed"), "ok",
                                    {"plan_waiting": 0}, **kw), kw)
    assert _work_item(st, kw)["status"] == "done"


def test_coding_approval_is_a_false_by_default_boolean_and_human_only():
    doc = yaml.safe_load(V2.read_text(encoding="utf-8"))
    declaration = doc["inputs"]["require_coding_approval"]
    assert declaration["type"] == "bool"  # shared form renders bool as checkbox
    assert declaration["default"] is False
    ask = doc["nodes"]["coding-approval"]["ask"]
    assert ask["otherwise"] == "human"
    assert "from" not in ask and "timeout" not in ask
    assert ask["urgent"] is True


# --------------------------------------------------------------------------- #
# the plan's own decisions stop the round before the gate (claunch-rzepk)
# --------------------------------------------------------------------------- #
def test_the_plan_wait_is_one_node_on_every_path():
    """One wait after the plan step, whether the plan was written by
    ``coding-plan`` (gate path) or ``plan-seed`` (no gate) -- claunch-r3j1o.2.

    It probes ``claunch gate plan-waiting --scope start`` -- the unchosen
    decisions only, claunch-jw1v2 -- (0 clear, 1 still owed, 2 could
    not tell), poll, and cap undecided reads -- but carry no ``seconds``:
    the confirmed rule has no upper bound on "stop until the user decides",
    and a failed read goes to a person instead of releasing the run
    (docs/plan-coding-approval.md 규칙 1)."""
    chart = _chart()
    # settle's closure gate probes the same command (claunch-5sryb); it is
    # tested on its own below
    waits = [n for n in chart.nodes.values() if n.type == "wait"
             and n.get("probe") == ["claunch", "gate", "plan-waiting", "--scope", "start", "--notify"]
             and "closure-wait" not in n.id]
    assert [n.label for n in waits] == ["plan-wait"], [n.id for n in waits]
    for n in waits:
        assert "plan-wait" in n.id
        assert n.get("poll") == 30 and n.get("undecided_max") == 3
        assert n.get("seconds") is None
        assert "undecidable" in n.on


def test_coding_plan_reports_the_count_the_wait_reads():
    """The agent's report and the probe read the same number, so a reader of
    the round can see why it stopped."""
    doc = yaml.safe_load(V2.read_text(encoding="utf-8"))
    plan = doc["nodes"]["coding-plan"]
    assert plan["returns"]["ok"]["plan_waiting"] == "int?"
    assert "plan_waiting" in _words(plan["done_when"])
    assert "summary.waiting" in _words(plan["prompt"])


def test_a_plan_decision_stops_the_round_before_the_gate(tmp_path, monkeypatch):
    from claude_launcher.cflow2 import runtime

    st, kw = _coding_item(tmp_path, monkeypatch, True)
    [plan] = st["tasks"]
    assert plan["label"] == "coding-plan"
    st = runtime.report(plan["instance"], "ok",
                        {"brief": "b", "plan_waiting": 3}, **kw)
    # the gate does not stand while the plan still owes the user a decision
    assert not st.get("asks")
    [wait] = st["waits"]
    assert "plan-wait" in wait["node"]
    # once the plan is clear, the same item reaches the gate
    st = _plan_clear(st, kw)
    [ask] = st["asks"]
    assert ask["label"] == "coding-approval"


def test_a_plan_decision_stops_the_gate_less_round_too(tmp_path, monkeypatch):
    """approve-always and the routing-only path skip ``coding-plan``; the
    plan is written first (``plan-seed``) and the round stops on it there
    too (claunch-rzepk 규칙 1·3)."""
    from claude_launcher.cflow2 import runtime

    st, kw = _routing_item(tmp_path, monkeypatch, {})
    [seed] = st["tasks"]
    assert seed["label"] == "plan-seed"
    st = runtime.report(seed["instance"], "ok", {"plan_waiting": 0}, **kw)
    [wait] = st["waits"]
    assert "plan-wait" in wait["node"]
    st = _plan_clear(st, kw)
    assert [t["label"] for t in st["tasks"]] == ["work"]


def test_pre_approval_summary_thread_opens_whenever_the_gate_stands():
    """승인을 구하는 일감이면 항상 사용자 요약 스레드를 연다(사용자 지시,
    th-275cfa) — 끌 수 있는 스위치가 없고, 거절 뒤 재승인을 구할 때도 매번
    다시 연다. thread_open이 실패하면 done_when은 thread_id 대신 실패 사유를
    요구한다(claunch-vz15z)."""
    doc = yaml.safe_load(V2.read_text(encoding="utf-8"))
    assert "coding_brief_thread" not in doc["inputs"]
    plan = doc["nodes"]["coding-plan"]
    assert "thread_id" in plan["returns"]["ok"]
    prompt = plan["prompt"]
    assert "문제제기" in prompt and "작업 목표" in prompt
    assert "작업 범위" in prompt and "기대효과" in prompt and "리스크" in prompt
    assert "thread_open" in prompt
    assert "항상 연다" in prompt
    assert "coding_brief_thread" not in prompt
    done_when = plan["done_when"]
    assert "thread_open이 실패한 경우에는 thread_id 대신" in done_when


def test_merge_approval_is_a_false_by_default_boolean_and_human_only():
    # claunch-boxug: the switch gates the landing request itself — same shape
    # as coding approval (bool off by default, person-only, urgent card).
    doc = yaml.safe_load(V2.read_text(encoding="utf-8"))
    declaration = doc["inputs"]["require_merge_approval"]
    assert declaration["type"] == "bool"  # shared form renders bool as checkbox
    assert declaration["default"] is False
    assert declaration["set_by"] == ["user"]
    assert declaration["group"] == "착지 승인"
    node = doc["nodes"]["merge-approval"]
    ask = node["ask"]
    assert ask["otherwise"] == "human"
    assert "from" not in ask and "timeout" not in ask
    assert ask["urgent"] is True
    assert set(ask["options"]) == {"approve", "hold"}
    assert ask["options"]["approve"]["outcome"] == "ok"
    assert ask["options"]["hold"]["outcome"] == "hold"
    assert set(node["returns"]) == {"ok", "hold"}


def test_merge_approval_gate_stands_between_peer_review_and_publish():
    # claunch-boxug: with the switch on, the ask sits in land's submit right
    # before publish (the request); a hold ends the round held (freeze), and
    # the off branch is a pass-through so default runs are unchanged.
    # safe_load parses the YAML 1.1 ``on:`` handler key as boolean True —
    # the frontend loader keeps it the string the engine reads.
    from claude_launcher.cflow2 import frontend
    doc = yaml.load(V2.read_text(encoding="utf-8"), Loader=frontend._Loader)
    land = doc["procedures"]["land"]
    submit = next(s for s in land["body"]
                  if isinstance(s, dict) and s.get("id") == "submit")
    do = submit["loop"]["do"]
    gate = next(s for s in do if isinstance(s, dict)
                and s.get("id") == "merge-approval-gate")
    publish = next(s for s in do if isinstance(s, dict)
                   and s.get("id") == "publish")
    assert do.index(publish) == do.index(gate) + 1
    peer = next(s for s in do if isinstance(s, dict)
                and s.get("id") == "peer-review-loop")
    assert do.index(peer) < do.index(gate)
    cond = gate["cond"][0]["if"]
    assert "$data.inputs.require_merge_approval == true" in cond
    assert gate["cond"][1] == {"else": []}
    hold = gate["cond"][0]["then"][0]["on"]["hold"]
    assert hold["return"] == "held"
    assert hold["value"]["reason"] == \
        "$result['merge-approval'].value.reason"
    assert gate["cond"][0]["then"][0]["seq"] == ["merge-approval"]


# -- session routing (claunch-yl0to) ----------------------------------------


def _routing_item(tmp_path, monkeypatch, values, data=None, items=1):
    """The real item route with the implementation tasks stubbed, as above,
    plus delegate-work: the routing decisions are what is under test."""
    import json

    from claude_launcher.cflow2 import frontend, inputs, runtime

    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    doc = yaml.load(V2.read_text(encoding="utf-8"), Loader=frontend._Loader)
    doc["body"] = [{"call": "item"}] * items
    doc.pop("spec", None)
    for name in ("work", "test-skipped", "commit", "review"):
        doc["nodes"][name] = {"prompt": name}
    doc["nodes"]["targeted-test"] = {"prompt": "measure",
                                     "returns": {"ok": {"failed": "int"}}}
    doc["data"].update(data or {})
    chart = frontend.compile_text(json.dumps(doc, ensure_ascii=False),
                                  resolve_include=layers.resolver())
    chart = inputs.apply(chart, values)
    kw = {"cwd": str(tmp_path), "scope": "routing", "now": 1.0}
    return runtime.start(chart, caps={}, **kw), kw


def _one(st, label):
    [task] = st["tasks"]
    assert task["label"] == label, (task["label"], label)
    return task["instance"]


DELEGATE = {"mode": "delegate", "profile": "ds4", "model": "small", "difficulty": "easy"}
DIRECT = {"mode": "direct", "difficulty": "hard"}
DONE = {"files": ["a.py"], "summary": "s", "tried": [], "child": "c1", "commits": ["abc"]}


def _finish_skip(st, kw):
    from claude_launcher.cflow2 import runtime

    st = runtime.select(st["choices"][0]["instance"], "skip", reason="fixture", **kw)
    for label in ("test-skipped", "commit"):
        st = runtime.report(_one(st, label), "ok", {}, **kw)
    return st


def test_routing_declares_three_human_inputs_off_by_default():
    sch = _chart().meta["inputs"]
    assert sch["session_routing"]["type"] == "bool"
    assert sch["session_routing"]["default"] is False
    for name in ("session_routing", "delegate_profile", "delegate_model"):
        assert sch[name]["set_by"] == ["user"], name
    assert sch["delegate_profile"]["default"] == "" == sch["delegate_model"]["default"]


def test_routing_without_a_gate_follows_the_plans_recommendation(tmp_path, monkeypatch):
    from claude_launcher.cflow2 import runtime

    st, kw = _routing_item(tmp_path, monkeypatch, {"session_routing": True})
    plan = {"brief": "edit a.py", "difficulty": "easy", "recommend": "delegate", "why": "one file"}
    st = _plan_clear(runtime.report(_one(st, "coding-plan"), "ok", plan, **kw), kw)
    assert not st.get("asks")  # no gate: nobody is asked
    st = runtime.report(_one(st, "route-planned"), "ok", DELEGATE, **kw)
    # the delegate does the work in place of `work`
    st = runtime.report(_one(st, "delegate-work"), "ok", DONE, **kw)
    assert _finish_skip(st, kw)["status"] == "done"


def test_routing_hard_plan_keeps_the_work_here(tmp_path, monkeypatch):
    from claude_launcher.cflow2 import runtime

    st, kw = _routing_item(tmp_path, monkeypatch, {"session_routing": True})
    st = _plan_clear(runtime.report(_one(st, "coding-plan"), "ok",
                    {"brief": "b", "difficulty": "hard", "recommend": "direct"}, **kw), kw)
    st = runtime.report(_one(st, "route-planned"), "ok", DIRECT, **kw)
    st = runtime.report(_one(st, "work"), "ok", {}, **kw)
    assert _finish_skip(st, kw)["status"] == "done"


def test_routing_gate_keeps_approve_always_out_of_the_per_item_promise(
        tmp_path, monkeypatch):
    """The routing prompt offers four approvals and then says which of them
    last one item. `approve-always` is the exception — the line describing it
    says the run asks nobody again — so a count over all four ("위의 네 승인은
    이번 일감만의 승인이다") states the opposite of the line right above it
    (claunch-ns4kd). The paragraph has to name the exception."""
    from claude_launcher.cflow2 import runtime

    st, kw = _routing_item(tmp_path, monkeypatch,
                           {"session_routing": True, "require_coding_approval": True})
    st = _plan_clear(runtime.report(_one(st, "coding-plan"), "ok",
                    {"brief": "b", "difficulty": "easy", "recommend": "delegate"}, **kw), kw)
    [ask] = st["asks"]
    approvals = [o["name"] for o in ask["options"] if o["name"].startswith("approve-")]
    assert len(approvals) == 4
    blocks = [b for b in ask["prompt"].split("\n\n") if "이번 일감만의 승인" in b]
    assert blocks, "the routing prompt still says which approvals last one item"
    block = " ".join(blocks[0].split())
    assert "approve-always" in block, (
        "the paragraph about per-item approvals has to name approve-always: it "
        "is the one answer the run keeps for the items after this one")
    assert "네 승인" not in block, (
        "counting all four approvals as per-item is the contradiction this "
        "test exists for")


@pytest.mark.parametrize("answer, route, worker", [
    ("approve-as-planned", "route-planned", "delegate-work"),
    ("approve-no-delegate", "route-direct", "work"),
    ("approve-down-delegate", "route-delegate", "delegate-work"),
    ("approve-always", "route-planned", "delegate-work"),
])
def test_routing_gate_offers_five_answers(tmp_path, monkeypatch, answer, route, worker):
    from claude_launcher.cflow2 import runtime

    st, kw = _routing_item(tmp_path, monkeypatch,
                           {"session_routing": True, "require_coding_approval": True})
    st = _plan_clear(runtime.report(_one(st, "coding-plan"), "ok",
                    {"brief": "b", "difficulty": "easy", "recommend": "delegate"}, **kw), kw)
    [ask] = st["asks"]
    assert ask["label"] == "coding-approval-routing" and ask["urgent"] is True
    assert [o["name"] for o in ask["options"]][:5] == [
        "approve-as-planned", "approve-no-delegate", "approve-down-delegate",
        "approve-always", "reject"]
    with pytest.raises(runtime.FlowError, match="not yours to answer"):
        runtime.approve(None, decision=answer, caller="agent", **kw)
    runtime.approve(None, decision=answer, reason="r", **kw)
    st = runtime.status(**kw)
    value = DIRECT if route == "route-direct" else DELEGATE
    st = runtime.report(_one(st, route), "ok", value, **kw)
    st = runtime.report(_one(st, worker), "ok", DONE, **kw)
    assert _finish_skip(st, kw)["status"] == "done"


def test_routing_gate_always_follows_the_plan_without_asking_again(tmp_path, monkeypatch):
    """approve-always on the routing gate: later items go the plan's way and
    nobody is asked (claunch-sxdnc)."""
    from claude_launcher.cflow2 import runtime

    st, kw = _routing_item(tmp_path, monkeypatch,
                           {"session_routing": True, "require_coding_approval": True}, items=2)
    st = _plan_clear(runtime.report(_one(st, "coding-plan"), "ok",
                    {"brief": "b", "difficulty": "hard", "recommend": "direct"}, **kw), kw)
    runtime.approve(None, decision="approve-always", reason="r", **kw)
    st = runtime.report(_one(runtime.status(**kw), "route-planned"), "ok", DIRECT, **kw)
    st = _finish_skip(runtime.report(_one(st, "work"), "ok", {}, **kw), kw)
    # the second item: the plan is still written (routing needs it), the gate is not
    st = _plan_clear(runtime.report(_one(st, "coding-plan"), "ok",
                    {"brief": "b2", "difficulty": "easy", "recommend": "delegate"}, **kw), kw)
    assert not st.get("asks")
    st = runtime.report(_one(st, "route-planned"), "ok", DELEGATE, **kw)
    st = runtime.report(_one(st, "delegate-work"), "ok", DONE, **kw)
    assert _finish_skip(st, kw)["status"] == "done"


@pytest.mark.parametrize("routing", [True, False])
def test_reject_rewrites_the_plan(tmp_path, monkeypatch, routing):
    from claude_launcher.cflow2 import runtime

    st, kw = _routing_item(tmp_path, monkeypatch,
                           {"session_routing": routing, "require_coding_approval": True})
    for visit in (1, 2):
        st = runtime.report(_one(st, "coding-plan"), "ok", {"brief": f"plan {visit}"}, **kw)
        st = _plan_clear(st, kw)
        [ask] = st["asks"]
        assert f"plan {visit}" in ask["prompt"]
        if visit == 1:
            if routing:
                runtime.approve(None, decision="reject", reason="too broad", **kw)
            else:
                # the plain approval's reject is an option too (claunch-sxdnc)
                runtime.approve(None, decision="reject", reason="too broad", **kw)
            st = runtime.status(**kw)
    assert ask["label"] == ("coding-approval-routing" if routing else "coding-approval")


@pytest.mark.parametrize("outcome", ["escalated", "failed"])
def test_a_stopped_delegate_asks_the_person(tmp_path, monkeypatch, outcome):
    from claude_launcher.cflow2 import runtime

    st, kw = _routing_item(tmp_path, monkeypatch, {"session_routing": True})
    st = _plan_clear(runtime.report(_one(st, "coding-plan"), "ok",
                    {"brief": "b", "difficulty": "easy", "recommend": "delegate"}, **kw), kw)
    st = runtime.report(_one(st, "route-planned"), "ok", DELEGATE, **kw)
    stopped = ({"reason": "needs a design call", "commits": [], "remaining": "all"}
               if outcome == "escalated" else {"reason": "spawn refused"})
    st = runtime.report(_one(st, "delegate-work"), outcome, stopped, **kw)
    [ask] = st["asks"]
    assert ask["label"] == "delegated-rework-approval"
    assert ask["resolution"] == "human" and ask["urgent"] is True
    assert [o["name"] for o in ask["options"]][:3] == ["rework-direct", "rework-delegate", "reject"]
    # rework-delegate: delegate again with the same setting
    runtime.approve(None, decision="rework-delegate", reason="try again", **kw)
    st = runtime.report(_one(runtime.status(**kw), "route-delegate"), "ok", DELEGATE, **kw)
    st = runtime.report(_one(st, "delegate-work"), outcome, stopped, **kw)
    # rework-direct: this session takes it over
    runtime.approve(None, decision="rework-direct", reason="mine now", **kw)
    st = runtime.report(_one(runtime.status(**kw), "route-direct"), "ok", DIRECT, **kw)
    st = runtime.report(_one(st, "work"), "ok", {}, **kw)
    assert _finish_skip(st, kw)["status"] == "done"


def test_a_stopped_delegate_rejected_goes_back_to_the_plan(tmp_path, monkeypatch):
    from claude_launcher.cflow2 import runtime

    st, kw = _routing_item(tmp_path, monkeypatch, {"session_routing": True})
    st = _plan_clear(runtime.report(_one(st, "coding-plan"), "ok", {"brief": "b", "recommend": "delegate"}, **kw), kw)
    st = runtime.report(_one(st, "route-planned"), "ok", DELEGATE, **kw)
    st = runtime.report(_one(st, "delegate-work"), "escalated",
                        {"reason": "r", "commits": [], "remaining": "x"}, **kw)
    runtime.approve(None, decision="reject", reason="plan again", **kw)
    # marked direct first, so the plan is rewritten instead of the gate reopening
    st = runtime.report(_one(runtime.status(**kw), "route-direct"), "ok", DIRECT, **kw)
    assert _one(st, "coding-plan")


def test_a_failed_parent_test_on_delegated_work_asks_the_person(tmp_path, monkeypatch):
    from claude_launcher.cflow2 import runtime

    st, kw = _routing_item(tmp_path, monkeypatch, {"session_routing": True})
    st = _plan_clear(runtime.report(_one(st, "coding-plan"), "ok", {"brief": "b", "recommend": "delegate"}, **kw), kw)
    st = runtime.report(_one(st, "route-planned"), "ok", DELEGATE, **kw)
    st = runtime.report(_one(st, "delegate-work"), "ok", DONE, **kw)
    st = runtime.select(st["choices"][0]["instance"], "run", reason="fixture", **kw)
    st = runtime.report(_one(st, "review"), "ok", {}, **kw)
    st = runtime.report(_one(st, "targeted-test"), "ok", {"failed": 2}, **kw)
    [ask] = st["asks"]
    assert ask["label"] == "delegated-rework-approval"
    runtime.approve(None, decision="rework-direct", reason="fix here", **kw)
    st = runtime.report(_one(runtime.status(**kw), "route-direct"), "ok", DIRECT, **kw)
    # the test loop goes on with this session: the next failure asks nobody
    st = runtime.report(_one(st, "review"), "ok", {}, **kw)
    st = runtime.report(_one(st, "targeted-test"), "ok", {"failed": 1}, **kw)
    assert not st.get("asks")
    st = runtime.report(_one(st, "review"), "ok", {}, **kw)
    st = runtime.report(_one(st, "targeted-test"), "ok", {"failed": 0}, **kw)
    st = runtime.report(_one(st, "commit"), "ok", {}, **kw)
    assert st["status"] == "done"


def _failed_parent_test(tmp_path, monkeypatch):
    """Delegated work whose parent targeted test fails once."""
    from claude_launcher.cflow2 import runtime

    st, kw = _routing_item(tmp_path, monkeypatch, {"session_routing": True})
    st = _plan_clear(runtime.report(_one(st, "coding-plan"), "ok", {"brief": "b", "recommend": "delegate"}, **kw), kw)
    st = runtime.report(_one(st, "route-planned"), "ok", DELEGATE, **kw)
    st = runtime.report(_one(st, "delegate-work"), "ok", DONE, **kw)
    st = runtime.select(st["choices"][0]["instance"], "run", reason="fixture", **kw)
    st = runtime.report(_one(st, "review"), "ok", {}, **kw)
    st = runtime.report(_one(st, "targeted-test"), "ok", {"failed": 2}, **kw)
    return st, kw


@pytest.mark.parametrize("outcome", ["escalated", "failed"])
def test_a_redelegated_child_that_stops_asks_again(tmp_path, monkeypatch, outcome):
    """The test-failure path's rework-delegate: a child that stops again is
    caught by the same gate instead of failing the run (review of fb313ba9)."""
    from claude_launcher.cflow2 import runtime

    st, kw = _failed_parent_test(tmp_path, monkeypatch)
    runtime.approve(None, decision="rework-delegate", reason="again", **kw)
    st = runtime.report(_one(runtime.status(**kw), "route-delegate"), "ok", DELEGATE, **kw)
    stopped = ({"reason": "r", "commits": [], "remaining": "x"}
               if outcome == "escalated" else {"reason": "spawn refused"})
    st = runtime.report(_one(st, "delegate-work"), outcome, stopped, **kw)
    assert st["status"] != "failed"
    [ask] = st["asks"]
    assert ask["label"] == "delegated-rework-approval"
    # rework-delegate once more: the loop retries and stops at the gate again
    runtime.approve(None, decision="rework-delegate", reason="again", **kw)
    st = runtime.report(_one(runtime.status(**kw), "route-delegate"), "ok", DELEGATE, **kw)
    st = runtime.report(_one(st, "delegate-work"), outcome, stopped, **kw)
    # then rework-direct: this session fixes it in the test loop's next review
    runtime.approve(None, decision="rework-direct", reason="mine", **kw)
    st = runtime.report(_one(runtime.status(**kw), "route-direct"), "ok", DIRECT, **kw)
    st = runtime.report(_one(st, "review"), "ok", {}, **kw)
    st = runtime.report(_one(st, "targeted-test"), "ok", {"failed": 0}, **kw)
    st = runtime.report(_one(st, "commit"), "ok", {}, **kw)
    assert st["status"] == "done"


def test_a_redelegated_child_that_finishes_goes_on_testing(tmp_path, monkeypatch):
    from claude_launcher.cflow2 import runtime

    st, kw = _failed_parent_test(tmp_path, monkeypatch)
    runtime.approve(None, decision="rework-delegate", reason="again", **kw)
    st = runtime.report(_one(runtime.status(**kw), "route-delegate"), "ok", DELEGATE, **kw)
    st = runtime.report(_one(st, "delegate-work"), "ok", DONE, **kw)
    st = runtime.report(_one(st, "review"), "ok", {}, **kw)
    st = runtime.report(_one(st, "targeted-test"), "ok", {"failed": 0}, **kw)
    st = runtime.report(_one(st, "commit"), "ok", {}, **kw)
    assert st["status"] == "done"


def test_a_failed_parent_test_rejected_rewrites_the_plan(tmp_path, monkeypatch):
    from claude_launcher.cflow2 import runtime

    st, kw = _failed_parent_test(tmp_path, monkeypatch)
    runtime.approve(None, decision="reject", reason="plan again", **kw)
    st = runtime.report(_one(runtime.status(**kw), "route-direct"), "ok", DIRECT, **kw)
    assert _one(st, "coding-plan")


def test_delegated_work_sent_back_by_review_asks_first(tmp_path, monkeypatch):
    from claude_launcher.cflow2 import runtime

    st, kw = _routing_item(
        tmp_path, monkeypatch, {"session_routing": True},
        data={"route": DELEGATE, "land_out": {"reason": "CHANGES REQUESTED", "rework": True}})
    assert not st["tasks"]
    [ask] = st["asks"]
    assert ask["label"] == "delegated-rework-approval"
    assert "CHANGES REQUESTED" in ask["prompt"]
    runtime.approve(None, decision="reject", reason="new plan", **kw)
    st = runtime.report(_one(runtime.status(**kw), "route-direct"), "ok", DIRECT, **kw)
    # rejected: back to the plan, and the gate does not reopen
    st = _plan_clear(runtime.report(_one(st, "coding-plan"), "ok", {"brief": "b2", "recommend": "direct"}, **kw), kw)
    st = runtime.report(_one(st, "route-planned"), "ok", DIRECT, **kw)
    assert _one(st, "work")


def test_routing_off_keeps_todays_path_even_with_a_delegate_route(tmp_path, monkeypatch):
    from claude_launcher.cflow2 import runtime

    st, kw = _routing_item(tmp_path, monkeypatch, {},
                           data={"route": DELEGATE, "land_out": {"rework": True}})
    st = _start_work(st, kw)
    assert [t["label"] for t in st["tasks"]] == ["work"]
    assert not st.get("asks")


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
    # blocked is the fourth (claunch-improver-integration-request-blocked-o6paj):
    # "the landing request was not raised because it is blocked" had no
    # declared outcome, so it could only be reported as the engine's fail.
    # pr-submitted is the fifth (claunch-ijkge): auto_merge=false stops at PR
    # submission and the person merges, so that ending needs its own outcome
    # rather than leaving the round as a merge the workflow did not do.
    assert set(land["returns"]) == {"landed", "held", "rework", "blocked", "pr-submitted"}
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
    "end-decision": ["end", "hold", "new-task", "self-improve", "auto-release"],
    "release-decision": ["end", "hold", "new-task"],
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


#: The one ``chooser: user`` choose this definition keeps (claunch-dsr4o).
#: A person's questions go through ask-user (63d53352, claunch-yq80p.8); that
#: commit already left workspace-gate an approval because "holding is not an
#: answer, so it is not a decision with two or more options". test-skip-gate
#: is the same kind: a stop button open beside the running tests loop, not a
#: decision with a brief -- ask-user would add an ask-frame task next to the
#: tests and hold the run in waiting_approval for as long as they run. Named
#: exactly; no pattern or prefix match.
USER_CHOOSE_ALLOWED = frozenset({"test-skip-gate"})


def _unlisted_user_chooses(chart):
    return [n.label or n.id for n in chart.nodes.values()
            if n.type == "choose" and n.get("chooser") == "user"
            and (n.label or n.id) not in USER_CHOOSE_ALLOWED]


def test_no_user_choose_is_left():
    chart = _chart()
    assert _unlisted_user_chooses(chart) == []
    # the allowed one is really there (a rename must update the list)
    assert [n.label for n in chart.nodes.values()
            if n.type == "choose" and n.get("chooser") == "user"] == ["test-skip-gate"]


def test_a_user_choose_outside_the_allowed_list_is_still_refused():
    """The negative case: any other ``chooser: user`` choose -- including one
    whose name only starts like the allowed one -- is reported."""
    import json

    from claude_launcher.cflow2 import frontend

    doc = yaml.load(V2.read_text(encoding="utf-8"), Loader=frontend._Loader)
    doc["nodes"]["work"] = {"prompt": "work"}
    doc["body"] = [
        {"call": "item"},
        {"id": "test-skip-gate-2", "choose": {
            "chooser": "user", "prompt": "p",
            "options": {"a": {"description": "a", "then": []},
                        "b": {"description": "b", "then": []}}}},
    ]
    doc.pop("spec", None)
    chart = frontend.compile_text(json.dumps(doc, ensure_ascii=False),
                                  resolve_include=layers.resolver())
    assert _unlisted_user_chooses(chart) == ["test-skip-gate-2"]


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
    # claunch-yzwut: the unconditional never_effect invariant could not admit
    # merge-by-agent's merge, so the static ban moved to the conditional rule
    # master-merge-effect. The text everyone reads stays in no-master-merge.
    assert "no-master-merge" not in (chart.spec.get("invariants") or {})
    ban = chart.rules["master-merge-effect"]
    assert ban["effect"] == {"deny": "git.merge", "target": "master"}
    assert ban["when"] == "$data.self_merge.armed != true"
    assert ban["deliver"] == []
    assert "claunch-yzwut" in rule["why"]
    assert chart.spec["paths"]["review-before-self-merge"]["must_pass"] == ["peer-review"]
    assert chart.spec["paths"]["settle-before-end"]["must_pass"] == ["settle"]


# -- allow_work_on_main (claunch-cetzf.2) / require_plan_closure (claunch-5sryb)

MODE = "$result['branch-setup'].value.mode"

def _procedure_run(tmp_path, monkeypatch, body, values):
    """A run of the real definition with ``body`` replaced and ``values``
    applied as start inputs."""
    import json

    from claude_launcher.cflow2 import frontend, inputs, runtime

    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    doc = yaml.load(V2.read_text(encoding="utf-8"), Loader=frontend._Loader)
    doc["body"] = body
    doc.pop("spec", None)
    chart = frontend.compile_text(json.dumps(doc, ensure_ascii=False),
                                  resolve_include=layers.resolver())
    chart = inputs.apply(chart, values)
    kw = {"cwd": str(tmp_path), "scope": "p", "now": 1.0}
    return runtime.start(chart, caps={}, **kw), kw


def _branch_setup(st, kw, mode):
    from claude_launcher.cflow2 import runtime

    [task] = [t for t in st["tasks"] if t["label"] == "branch-setup"]
    value = {"branch": "master" if mode == "main" else "s-x", "base": "master",
             "base_hash": "0" * 40, "shared": False, "mode": mode}
    return runtime.report(task["instance"], "ok", value, **kw)


def test_allow_work_on_main_is_a_user_input_read_by_branch_setup_only():
    from claude_launcher.cflow2 import inputs

    chart = _chart()
    decl = chart.meta["inputs"]["allow_work_on_main"]
    assert decl["type"] == "bool" and decl["default"] is False
    assert decl["set_by"] == ["user"]
    # the input decides branch-setup; every node after it reads the mode that
    # branch-setup recorded, so a mid-round change cannot desync the landing
    assert chart.rules["branch-on-main"]["when"] == "$data.inputs.allow_work_on_main == true"
    assert chart.rules["branch-on-main"]["deliver"] == ["branch-setup"]
    assert chart.rules["no-master-commit"]["when"] == f"{MODE} != 'main'"
    assert chart.rules["work-on-main"]["when"] == f"{MODE} == 'main'"
    assert "branch-setup" not in chart.rules["no-master-commit"]["deliver"]
    assert "branch-setup" not in chart.rules["work-on-main"]["deliver"]
    # the merge ban's text stays unconditional; its static effect is the
    # conditional rule master-merge-effect (claunch-yzwut), whose condition is
    # the self-merge mark and never the work-on-main mode
    assert "when" not in chart.rules["no-master-merge"]
    assert chart.rules["no-master-merge"].get("effect") is None
    assert chart.rules["master-merge-effect"]["effect"] == {"deny": "git.merge",
                                                           "target": "master"}
    assert MODE not in chart.rules["master-merge-effect"]["when"]
    assert "master 체크아웃 후 커밋" not in chart.rules["no-master-merge"]["text"]
    deps = inputs.dependencies(chart)["allow_work_on_main"]
    assert deps["conditional"] == ["branch-on-main"]
    setup = next(n for n in chart.nodes.values() if n.label == "branch-setup")
    assert "branch-on-main" in setup.get("rules")
    assert not {"no-master-commit", "work-on-main"} & set(setup.get("rules"))
    # neither conditional text is baked into the compiled prompt
    assert "이 회차는 master에서 직접 작업한다" not in setup.get("prompt")
    assert "master 체크아웃에서 커밋하지 않는다" not in setup.get("prompt")
    assert setup.get("returns")["ok"]["mode"] == "str?"


def test_branch_setup_reads_the_rule_matching_the_input():
    from claude_launcher.cflow2 import runtime

    chart = _chart()
    setup = next(n for n in chart.nodes.values() if n.label == "branch-setup")

    class _R:
        pass

    r = _R()
    r.chart = chart
    on = runtime.node_prompt(r, setup, {"data": {"inputs": {"allow_work_on_main": True}}})
    off = runtime.node_prompt(r, setup, {"data": {"inputs": {"allow_work_on_main": False}}})
    assert "이 회차는 master에서 직접 작업한다" in on
    assert "이 회차는 master에서 직접 작업한다" not in off


def _mode_env(mode, flag):
    setup = {"outcome": "ok", "value": {"mode": mode}} if mode else None
    return {"data": {"inputs": {"allow_work_on_main": flag}},
            "result": {"branch-setup": setup} if setup else {}}


@pytest.mark.parametrize("mode,flag,on_main", [
    ("main", True, True), ("main", False, True),
    ("new", True, False), ("reuse", False, False), (None, True, False)])
def test_commit_rules_follow_the_recorded_mode_not_the_input(mode, flag, on_main):
    from claude_launcher.cflow2 import runtime

    chart = _chart()
    commit = next(n for n in chart.nodes.values() if n.label == "commit")

    class _R:
        pass

    r = _R()
    r.chart = chart
    text = runtime.node_prompt(r, commit, _mode_env(mode, flag))
    assert ("이 회차는 master에서 작업 중이다" in text) is on_main
    assert ("master 체크아웃에서 커밋하지 않는다" in text) is (not on_main)


def test_land_skips_to_landed_with_the_master_commit_when_working_on_main(tmp_path, monkeypatch):
    from claude_launcher.cflow2 import runtime

    body = [{"try": ["branch-setup", {"call": "land"}],
             "catch": {"landed": ["wrapup"]}}]
    st, kw = _procedure_run(tmp_path, monkeypatch, body, {"allow_work_on_main": True})
    st = _branch_setup(st, kw, "main")
    [task] = st["tasks"]
    assert task["label"] == "main-landed"
    st = runtime.report(task["instance"], "ok", {"tip": "abc123", "commits": ["abc123"]}, **kw)
    # landed reached the caller: wrapup is next, no landing choice stood
    assert [t["label"] for t in st["tasks"]] == ["wrapup"]
    assert not st.get("choices")


def _past_already_merged(st, kw):
    """already-merged(claunch-yzwut.2) stands right before landing: a round
    not yet in the target answers not-landed and meets the landing it had."""
    from claude_launcher.cflow2 import runtime

    assert [c["label"] for c in st["choices"]] == ["already-merged"]
    return runtime.select(st["choices"][0]["instance"], "not-landed", "not in target", **kw)


def test_land_is_unchanged_when_the_input_is_off(tmp_path, monkeypatch):
    body = [{"try": [{"call": "land"}], "catch": {"landed": ["wrapup"]}}]
    st, kw = _procedure_run(tmp_path, monkeypatch, body, {})
    assert not st["tasks"]
    st = _past_already_merged(st, kw)
    assert [c["label"] for c in st["choices"]] == ["landing"]


@pytest.mark.parametrize("flag,mode,skips", [
    # the input turned on after a feature branch was cut: a normal landing
    (True, "new", False),
    # the input turned off after the round committed on master: no request
    (False, "main", True)])
def test_land_follows_the_mode_branch_setup_recorded(tmp_path, monkeypatch, flag, mode, skips):
    body = [{"try": ["branch-setup", {"call": "land"}], "catch": {"landed": ["wrapup"]}}]
    st, kw = _procedure_run(tmp_path, monkeypatch, body, {"allow_work_on_main": not flag})
    from claude_launcher.cflow2 import runtime

    runtime.set_input("allow_work_on_main", flag, caller=runtime.CALLER_USER, **kw)
    st = _branch_setup(st, kw, mode)
    if skips:
        assert [t["label"] for t in st["tasks"]] == ["main-landed"]
    else:
        assert not st["tasks"]
        st = _past_already_merged(st, kw)
        assert [c["label"] for c in st["choices"]] == ["landing"]


def test_a_stack_only_land_ignores_the_mode_a_past_round_left(tmp_path, monkeypatch):
    """A stack-only round runs no branch-setup, so the mode is a past round's.
    That round lands the children's stack: main-gate stays shut and the stack
    cut is next (review CHANGES REQUESTED @ ea06fa49)."""
    body = [{"try": ["branch-setup", {"call": "land", "with": {"mode": "stack-only"}}],
             "catch": {"landed": ["wrapup"]}}]
    st, kw = _procedure_run(tmp_path, monkeypatch, body, {"allow_work_on_main": True})
    st = _branch_setup(st, kw, "main")
    labels = [t["label"] for t in st["tasks"]]
    assert "main-landed" not in labels
    assert labels == ["stack-cut"]


def test_settle_waits_for_plan_closure_without_a_deadline_when_required(tmp_path, monkeypatch):
    from claude_launcher.cflow2 import runtime

    st, kw = _procedure_run(tmp_path, monkeypatch, [{"call": "settle"}],
                            {"require_plan_closure": True})
    [w] = st["waits"]
    assert w["node"].endswith("closure-wait") and w["seconds"] is None
    assert w["probe"] == ["claunch", "gate", "plan-waiting", "--notify"]
    # a day of "still owed" does not release it
    st = runtime.wait_tick(86_400.0, lambda cmd, cwd: (1, ""), cwd=kw["cwd"], scope=kw["scope"])
    assert [x["node"] for x in st["waits"]] == [w["node"]]
    st = runtime.wait_tick(86_500.0, lambda cmd, cwd: (0, ""), cwd=kw["cwd"], scope=kw["scope"])
    assert not any("closure" in x["node"] for x in st.get("waits") or [])
    assert any(c["label"] == "settle-check" for c in st.get("choices") or [])


def test_settle_goes_on_after_three_minutes_when_closure_is_not_required(tmp_path, monkeypatch):
    from claude_launcher.cflow2 import runtime

    st, kw = _procedure_run(tmp_path, monkeypatch, [{"call": "settle"}], {})
    [w] = st["waits"]
    assert w["node"].endswith("closure-wait-timed") and w["seconds"] == 180
    st = runtime.wait_tick(100.0, lambda cmd, cwd: (1, ""), cwd=kw["cwd"], scope=kw["scope"])
    assert [x["node"] for x in st["waits"]] == [w["node"]]
    st = runtime.wait_tick(200.0, lambda cmd, cwd: (1, ""), cwd=kw["cwd"], scope=kw["scope"])
    assert not any("closure" in x["node"] for x in st.get("waits") or [])
    assert any(c["label"] == "settle-check" for c in st.get("choices") or [])


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


def test_pool_answer_assigning_back_is_named_and_announced():
    # claunch-p641s: a leader answered a pool ask by assigning the issue back
    # to the asking worker; wrapup only knew "HOLD or another session", the
    # marker was undefined, and queue-recheck started the round silently.
    chart = _chart()
    wrapup = _words(next(n for n in chart.nodes.values()
                         if n.label == "wrapup").props["prompt"])
    assert "`HOLD: <이유>` 코멘트, (2) 다른 세션에 배정, (3) 물어 온 이 세션에 배정" in wrapup
    assert "`ASSIGNED by leader: <이 세션> <이유>`" in wrapup
    recheck = next(_words(n.props["prompt"]) for n in chart.nodes.values()
                   if "회차가 마감됐다" in (n.props.get("prompt") or ""))
    assert "`ASSIGNED by leader:` 코멘트로 들어온 것이 있으면" in recheck
    assert "`thread_open`" in recheck
    common = _words(_block(COMMON.read_text(encoding="utf-8")))
    assert "`ASSIGNED by leader: <그 세션> <이유>`" in common
    # v1 wrapup lists the same three answers; the shared block promises no
    # user notice because only v2's queue-recheck sends one.
    v1_wrapup = _words(yaml.safe_load(V1.read_text(encoding="utf-8"))
                       ["steps"]["wrapup"]["instructions"])
    assert "(3) 물어 온 이 세션에 배정" in v1_wrapup
    assert "사용자에게 알린다" not in common


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
    v2_only = []
    for node in chart.nodes.values():
        # plan-round has no v1 counterpart: v1 has no per-round plan
        # (claunch-r3j1o). It is declared once, at intake.
        v2_only += [(node.label, t["do"]) for t in node.get("triggers") or []
                    if t["do"] == "plan-round"]
        trig = [t for t in node.get("triggers") or [] if t["do"] != "plan-round"]
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
    assert v2_only == [("intake", "plan-round")]


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

#: A landing record's ``at``: the wall clock the step ran at, in the store's
#: own ISO-8601 second-precision form (claunch-ijkge.4.1.3). The PR page
#: groups rows by it, so every landing value carries one; a test cannot
#: predict the second it lands in, so the shape is checked and the value is
#: dropped before the record is compared.
_STAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00$")


def _no_stamp(record):
    out = dict(record)
    # Required, not merely tolerated: a landing record without a stamp is the
    # defect this field was added for, so its absence has to fail here.
    at = out.pop("at", None)
    assert isinstance(at, str) and _STAMP.match(at), record
    # An unknown title is null, which the optional field fills in; a known one
    # is the value the page shows and keeps a test's attention.
    if out.get("title") is None:
        out.pop("title", None)
    return out


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
    assert _no_stamp(landing) == {"medium": "pr", "reason": "gh ok", "remote": "origin",
                                  "repo": "o/r", "base": "master", "pr_url": None,
                                  "pr_number": None}
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
                   {"landing": _LANDED,
                    "published": {**_SETUP, "pr_url": "u", "pr_number": 7, "tip": "t"}}),
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
                        "published": {**_SETUP, "pr_url": "u", "pr_number": 7,
                                      "tip": "t"}}),
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
    got = {k: _no_stamp(v) for k, v in state["data"].items()
           if k in ("landing", "published")}
    assert got == data


def test_pr_publish_keeps_the_title_the_pr_was_opened_with(tmp_path, monkeypatch):
    """``pr-open`` reports the title it opened or updated, and the landing
    record keeps it (claunch-ijkge.4.1.3): the PR page shows that value under
    the row, so it has to travel with the record rather than be looked up
    again."""
    from claude_launcher.cflow2 import runtime
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    kw = {"cwd": str(tmp_path), "scope": "m", "now": 1.0}
    st = runtime.start(_medium_flow(tmp_path), caps={}, params={"force_pr": True}, **kw)

    def report(label, outcome="ok", value=None):
        iid = next(t["instance"] for t in st["tasks"]
                   if t["label"].rsplit("/", 1)[-1] == label)
        return runtime.report(iid, outcome, value, **kw)

    st = report("remote-setup", "ok", _SETUP)
    st = report("pr-open", "ok", {**_PR, "title": "Add the PR tab"})
    data = runtime.RunStore(str(tmp_path), "m").load_state()["data"]
    # the medium is chosen before any PR exists: it stamps the time and claims
    # no title (there is none to claim yet)
    assert _STAMP.match(data["landing"]["at"])
    assert data["landing"].get("title") is None
    # the PR step is where the title becomes known, and it carries it
    published = data["published"]
    assert published["title"] == "Add the PR tab"
    assert _STAMP.match(published["at"])


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


def test_landing_leaders_third_group_is_the_mesh_root():
    """claunch-jdre5: a worker parented under another worker sees the leader
    the operator started as collateral — neither above it nor beside it. Both
    leader groups then miss, the question falls to a person with no clock, and
    the run waits indefinitely. ``root`` is the group that names that leader;
    the order stays nearest first, so it is tried last."""
    from claude_launcher.cflow2 import frontend

    doc = yaml.load(V2.read_text(encoding="utf-8"), Loader=frontend._Loader)
    ask = doc["nodes"]["landing-review-leader"]["ask"]
    assert [(c["role"], c["scope"]) for c in ask["from"]] == [
        ("leader", "ancestor"),
        ("leader", "sibling"),
        ("leader", "root"),
    ]
    # claunch-u7nu3: every group may make the edge to the leader it names.
    assert all(c.get("connect") is True for c in ask["from"])
    assert ask["otherwise"] == "human"
    assert all(c["scope"] != "descendant" for c in ask["from"]), (
        "a run must not stand up a session to answer its own landing question"
    )


def test_stacked_landing_review_reaches_the_leader_like_independent():
    """claunch-0ka8c: a stacked round with nobody in its parent chain to merge
    has its request demoted to the leader's queue, so the question about that
    request must reach the same leaders ``landing-review-leader`` asks --
    after the worker parent, which stays first for a real stack."""
    from claude_launcher.cflow2 import frontend

    doc = yaml.load(V2.read_text(encoding="utf-8"), Loader=frontend._Loader)
    stacked = doc["nodes"]["landing-review"]["ask"]["from"]
    leader = doc["nodes"]["landing-review-leader"]["ask"]["from"]
    assert stacked[0] == {"role": "worker", "scope": "ancestor"}
    assert stacked[1:] == leader
    assert doc["nodes"]["landing-review"]["ask"]["otherwise"] == "human"


def test_the_sixth_check_reads_a_demotion_as_request():
    """A stacked gate answer of 0 with ``demoted`` set is the leader route,
    not an error: the landing prompt says so and the bundle carries it."""
    text = V2.read_text(encoding="utf-8")
    assert "`demoted`가 비어 있지 않으면" in text
    assert "강등은 에러가 아니다" in text
    assert "stacked였으나 <demoted의\n        사유>로 리더 큐로 라우팅" in text


def test_landing_leader_ask_wires_an_unwired_root_leader(monkeypatch):
    """claunch-u7nu3: an independent child under another worker is wired to
    its parent only, so the root leader is outside its reachable set. The
    root group's ``connect: true`` makes that one edge and asks the leader
    instead of falling to a person."""
    from claude_launcher.cflow import responders
    from claude_launcher.cflow2 import asks, runtime

    chart = runtime.load_definition("improv-worker-v2")
    node = next(n for n in chart.nodes.values() if n.label == "landing-review-leader")
    spec = runtime._ask_spec(node)
    wired = []
    monkeypatch.setattr(responders, "wire",
                        lambda mesh, me, other: wired.append((mesh, me, other)))
    lead = responders.Responder(session="s29", handle="s29", role="leader",
                                reachability="idle")
    parent = responders.Responder(session="p", handle="p", role="worker",
                                  reachability="idle")
    pool = responders.Pool(mesh="claunch", me="w", members={"s29": lead, "p": parent},
                           reachable={"p"}, ancestors=["p"], roots={"s29", "p"})
    plan = asks.plan_route({"id": "a1", "from": spec["from"]}, pool)
    assert [m["handle"] for m in plan["asked"]] == ["s29"], plan
    assert plan["group"] == 2 and plan["wired"] == ["s29"]
    assert wired == [("claunch", "w", "s29")]


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


def _peer_review_ask(tmp_path, issue, items=None):
    """The peer-review node as it is in the definition, after a stub
    branch-setup, with data.claim set to ``issue``; returns the rendered
    question.

    ``items`` stands for the batch this round carried: ``items-pass`` reports
    the issues ``item-intake`` added beyond the claim issue as
    ``[{issue, n}, ...]`` and ``[]`` for a single-item round. A stub node with
    that id reports the value, so the rendered reference reads exactly what the
    real cond would have written. Left at None no such node runs, which is what
    a round looks like whose items pass never ran.

    The id is ``items-pass``, not ``items``: the loop under ``items`` runs only
    in rounds where the items pass runs, so its slot keeps a prior round's list
    into a ``stack-only`` round (claunch-0adj8). The cond completes every round.
    """
    import json

    from claude_launcher.cflow2 import frontend, runtime

    doc = yaml.load(V2.read_text(encoding="utf-8"), Loader=frontend._Loader)
    setup = {k: v for k, v in doc["nodes"]["branch-setup"].items() if k in ("title", "returns")}
    nodes = {"branch-setup": {**setup, "prompt": "branch-setup"},
             "peer-review": doc["nodes"]["peer-review"],
             "after": {"prompt": "after"}}
    body = ["branch-setup", "peer-review", "after"]
    if items is not None:
        # No ``returns``: an undeclared schema accepts any value, which is how
        # a loop's list of item records can be reported here.
        nodes["items-pass"] = {"prompt": "items-pass"}
        body = ["branch-setup", "items-pass", "peer-review", "after"]
    flow = {"cflow": 2, "name": "peer-review-ask",
            "data": {"claim": {"issue": issue, "how": "claimed" if issue else "no-issue-wait"}},
            "nodes": nodes, "body": body}
    chart = frontend.compile_text(json.dumps(flow, ensure_ascii=False))
    kw = dict(cwd=str(tmp_path), scope="m")
    st = runtime.start(chart, caps={}, now=1.0, **kw)
    st = runtime.report(st["tasks"][0]["instance"], "ok",
                        {"branch": "s1-gist", "base": "master", "base_hash": "0" * 40,
                         "shared": False}, now=2.0, **kw)
    if items is not None:
        st = runtime.report(st["tasks"][0]["instance"], "ok", items, now=3.0, **kw)
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
    assert "주 이슈: claunch-zzz" in prompt
    assert "브랜치: s1-gist" in prompt
    assert "보드 목록에서 고르지 않는다" in prompt
    assert "${data.claim.issue}" not in prompt


def test_peer_review_question_without_an_issue_falls_back_to_both_statuses(tmp_path, monkeypatch):
    """A round with no issue leaves the reference as written; the question
    then says to look in in_progress and in_review and to abstain on two
    candidates."""
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    prompt = _peer_review_ask(tmp_path, None)
    assert "주 이슈: ${data.claim.issue}" in prompt
    assert "--status in_progress --status in_review" in prompt
    assert "후보가 둘 이상이면 추측하지 말고 abstain한다" in prompt


# -- claunch-peer-review-batch-scope-x05sq: the ask names every batched issue -


def test_peer_review_question_lists_every_issue_the_batch_carried(tmp_path, monkeypatch):
    """A batch round commits several issues to one branch (claunch-7r1qr). The
    ask named only ``data.claim.issue``, so on PR #636 the reviewer read two
    of three commits as out of scope and judged them only after the worker
    told it otherwise — the judgement was right, and the question was what
    made it nearly not. The rendered question now carries the loop's own
    report of what else went in, and asks for a verdict comment per issue."""
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    prompt = _peer_review_ask(
        tmp_path, "claunch-aaa",
        [{"issue": "claunch-bbb", "n": 2}, {"issue": "claunch-ccc", "n": 3}],
    )
    assert "주 이슈: claunch-aaa" in prompt
    assert '"issue": "claunch-bbb"' in prompt
    assert '"issue": "claunch-ccc"' in prompt
    assert "그 전부가 이번 심사 범위" in prompt
    assert "판정 코멘트도 이슈마다 남긴다" in prompt
    assert "**이슈마다 남긴다**" in prompt
    assert "batch: <id1,id2,...>" in prompt


def test_peer_review_question_calls_a_single_item_round_single(tmp_path, monkeypatch):
    """The regression half: a round that carried only its claim issue reads
    the empty list and is told in words that the scope is that one issue."""
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    prompt = _peer_review_ask(tmp_path, "claunch-zzz", [])
    assert "브랜치에 함께 실린 이슈: []" in prompt
    assert "단일 일감" in prompt
    assert "주 이슈: claunch-zzz" in prompt


def test_peer_review_question_says_how_to_read_an_unrendered_item_list(tmp_path, monkeypatch):
    """When the items pass never ran the reference stays as written — the
    reader is told what that means rather than being left with `${...}`."""
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    prompt = _peer_review_ask(tmp_path, "claunch-zzz")
    assert "${result['items-pass'].value}" in prompt
    assert "위 줄이 `${...}` 그대로 보이면" in prompt


# -- claunch-0adj8: the slot the ask reads belongs to this round ------------


def _batch_step(doc, step_id):
    """The step ``step_id`` of the batch try in ``worker-round``, by id.

    Looked up by id rather than position: the try also opens with
    ``feasibility`` (claunch-101wi), and a guard pinned to an index breaks
    on every step added in front of the one it means.
    """
    steps = doc["procedures"]["worker-round"]["body"][1]["loop"]["do"]["try"]
    found = [s for s in steps if isinstance(s, dict) and s.get("id") == step_id]
    assert len(found) == 1, [s.get("id") if isinstance(s, dict) else s for s in steps]
    return found[0]


def _items_pass_chart(doc):
    """The real ``items-pass`` node from the canon, with its surroundings
    stubbed, called twice: ``full`` (a batch of one) then ``stack-only``."""
    import json

    from claude_launcher.cflow2 import frontend, runtime

    ip = _batch_step(doc, "items-pass")

    def task_of(name, src):
        keeps = ("title", "returns", "effects")
        return {**{k: v for k, v in src.items() if k in keeps}, "prompt": name}

    flow = {
        "cflow": 2, "name": "items-pass-rounds",
        "nodes": {"item-intake": task_of("item-intake", doc["nodes"]["item-intake"]),
                  "peer-review": task_of("peer-review", doc["nodes"]["peer-review"])},
        "procedures": {
            "item": {"params": {"verify": {"type": "str", "default": ""}},
                     "returns": {"ok": {}}, "body": [{"return": "ok", "value": {}}]},
            "worker-round": {"params": {"mode": {"type": "str", "default": "full"},
                                        "verify": {"type": "str", "default": ""}},
                             "body": [ip, "peer-review"]},
        },
        "body": [{"id": "round-one", "call": "worker-round", "with": {"mode": "full"}},
                 {"id": "round-two", "call": "worker-round", "with": {"mode": "stack-only"}}],
    }
    return frontend.compile_text(json.dumps(flow, ensure_ascii=False)), runtime


def test_the_item_list_the_ask_reads_does_not_survive_into_a_stack_only_round(
        tmp_path, monkeypatch):
    """claunch-0adj8. ``result`` is one run-wide table keyed by node label
    (``runtime.node_key``) and a round boundary does not clear it, so a slot
    written only in the rounds where the items loop ran keeps its old value.
    Measured with this exact chart: ``result['items']`` still held round one's
    ``BATCH-1`` while round two was open, and the ask printed it under 「이
    브랜치에 함께 실린 이슈」. ``items-pass`` completes every round, so its slot
    is the one that says what this round carried."""
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    from claude_launcher.cflow2 import frontend

    doc = yaml.load(V2.read_text(encoding="utf-8"), Loader=frontend._Loader)
    chart, runtime = _items_pass_chart(doc)
    kw = {"cwd": str(tmp_path), "scope": "ip"}
    t = [1.0]

    def nxt():
        t[0] += 1
        return t[0]

    st = runtime.start(chart, caps={}, **kw, now=nxt())
    assert [c["label"].split("/")[-1] for c in st["choices"]] == ["queue-next"]
    [ch] = st["choices"]
    st = runtime.select(ch["instance"], "next", reason="a second issue is queued",
                        **kw, now=nxt())
    st = runtime.report(st["tasks"][0]["instance"], "ok",
                        {"issue": "BATCH-1", "n": 1}, **kw, now=nxt())
    [ch] = st["choices"]
    st = runtime.select(ch["instance"], "drained", reason="queue empty", **kw, now=nxt())

    assert [x["label"].split("/")[-1] for x in st["tasks"]] == ["peer-review"]
    round_one = st["result"]["items-pass"]["value"]
    assert round_one == [{"issue": "BATCH-1", "n": 1}]

    st = runtime.report(st["tasks"][0]["instance"], "ok", {"decision": "pass"},
                        **kw, now=nxt())
    assert [x["label"].split("/")[-1] for x in st["tasks"]] == ["peer-review"]

    # The defect: the loop's own slot is not round-local.
    assert st["result"]["items"]["value"] == [{"issue": "BATCH-1", "n": 1}]
    # The reference the ask reads is.
    assert st["result"]["items-pass"]["value"] == []


def test_items_pass_completes_in_a_stack_only_round_with_an_empty_list():
    """The reason ``items-pass`` can carry the reference: its ``else`` arm is a
    real node that reports ``[]``, so the cond finishes in every round instead
    of skipping and leaving the slot untouched."""
    from claude_launcher.cflow2 import frontend

    doc = yaml.load(V2.read_text(encoding="utf-8"), Loader=frontend._Loader)
    ip = _batch_step(doc, "items-pass")
    arms = ip["cond"]
    assert "if" in arms[0] and "then" in arms[0]
    assert list(arms[1]) == ["else"]
    empty = arms[1]["else"]
    assert "foreach" in empty, empty
    assert empty["foreach"]["in"] == "[]"
    assert empty["foreach"]["do"] == []



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
    # claunch-jx4xx: the second binder is the defensive gate before the landing
    # request, which fills the record when the round reaches it unbound
    assert [n.label for n in bound] == ["resolve", "late-issue"]
    # the record has the procedure's return shape
    assert 'ok: {issue: "str?", how: str}' in V2.read_text(encoding="utf-8")


def _then_nodes(option):
    then = option["then"]
    return then if isinstance(then, list) else [then]


def test_resolve_issue_mints_an_issue_when_none_is_bound():
    """claunch-jx4xx: a round that left resolve-issue without an issue could
    not be queued for landing (NOT queued, claunch-b7bvt). Every branch but
    the user's explicit none now ends with an issue id."""
    doc = yaml.safe_load(V2.read_text(encoding="utf-8"))
    options = doc["procedures"]["resolve-issue"]["body"]["choose"]["options"]
    wait = _then_nodes(options["no-issue-wait"])
    assert wait[0] == "issue-create"
    assert wait[1]["value"]["issue"] == "$result['issue-create'].value.issue"
    claimed = options["claimed"]["then"]["cond"]
    assert claimed[0]["if"] == "$result['intake-issue'].value.issue == null"
    assert "issue-create" in claimed[0]["then"]
    auto = options["no-issue-auto"]["then"]
    assert auto[0] == "issue-auto"
    assert auto[1]["cond"][0]["if"] == "$result['issue-auto'].value.issue == null"
    assert "issue-create" in auto[1]["cond"][0]["then"]
    # the explicit consent path stays issue-less
    none = options["unclaimed"]["then"][-1]["cases"]["none"]
    assert none["value"] == {"how": "none"}


def test_the_landing_request_is_gated_on_a_bound_issue():
    """claunch-jx4xx: the gate runs just before integration-request, only when
    the round has no issue and the user did not say none, and it rebinds claim."""
    doc = yaml.safe_load(V2.read_text(encoding="utf-8"))
    gate = _find_id(doc["procedures"], "issue-gate")
    assert gate["cond"][0]["if"] == "$data.claim.issue == null and $data.claim.how != 'none'"
    late = gate["cond"][0]["then"]
    assert late["call"] == "ensure-issue" and late["bind"] == "claim"
    assert doc["procedures"]["ensure-issue"]["returns"]["ok"] == {"issue": "str", "how": "str"}
    def sequence_with_gate(tree):
        if isinstance(tree, dict):
            tree = list(tree.values())
        if isinstance(tree, list):
            if any(isinstance(n, dict) and n.get("id") == "issue-gate" for n in tree):
                return tree
            for sub in tree:
                got = sequence_with_gate(sub)
                if got is not None:
                    return got
        return None

    seq = sequence_with_gate(doc["procedures"])
    assert seq is not None
    labels = [n.get("id") if isinstance(n, dict) else n for n in seq]
    # claunch-101wi: the landing-channel check stands between the issue gate
    # and the request; nothing else may raise a request there. claunch-ijkge
    # adds pr-submitted-route after the gate: auto_merge=false ends the round
    # at PR submission, so it must not reach the request either — and it runs
    # after the gate so the marker task has the bound issue. claunch-yzwut
    # adds self-merge-route: a round the person sent to merge-by-agent leaves
    # here as landed instead of filing a request, and a round whose receiver
    # appeared meanwhile falls through to the channel check.
    between = labels[labels.index("issue-gate") + 1:labels.index("integration-request")]
    assert between == ["pr-submitted-route", "self-merge-route", "landing-channel"]


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
        dict(BRIEF["options"][1], name="self-improve"),
        dict(BRIEF["options"][1], name="auto-release")])
    # claunch-q4cqy.6: end at the first gate runs the cleanup and finishes the
    # run -- the notice already went out ahead of the gate, and
    # release-decision is not asked.
    for scope, answer, then in (("m", "end", "auto-release"), ("n", "new-task", "end-new-task")):
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


def test_reviewer_is_released_at_auto_release_and_recycled_on_new_task():
    """claunch-phkt1: auto-release records the child reviewer the settle reap
    already ended and asks to kill only one still alive; end-new-task asks to
    resume that reviewer (a person's card, resume_gate) and leaves a
    REVIEWER RECYCLE marker the next run's reviewer-ensure reads -- withdrawing
    a still-pending request before it spawns, so two reviewers never stand."""
    doc = yaml.safe_load(V2.read_text(encoding="utf-8"))
    nodes = doc["nodes"]
    release = _words(nodes["auto-release"]["prompt"])
    assert "settle-reap" in release and "MCP `kill`" in release
    assert "reviewer: <이름> exited" in release
    assert "reviewer: <이름> kill-request <id>" in release
    assert "session.kill" in nodes["auto-release"]["effects"]
    # the children rows carry no role: the reviewer is found by matching the
    # mesh roster's session column, as the daemon's own reap does
    assert "`children`의 자식 행에는 role이 없다" in release
    assert "claunch mesh members <이 런의 메시> --state all" in release
    assert "`cflow_clock._reviewer_children`" in release
    # the roster's third column is <daemon>/<session>: compare the part after '/'
    assert "`/` 뒤의 세션 이름과 비교한다" in release
    assert "`+`로 나눈 조각 가운데 reviewer가 있는지 본다" in release
    new_task = _words(nodes["end-new-task"]["prompt"])
    assert "MCP `resume_session`" in new_task
    assert "REVIEWER RECYCLE: <리뷰어 이름> resume-request <요청 id>" in new_task
    assert "operator의 경로이므로 쓰지 않는다" in new_task
    # filed after the new run stands: a resume approved while this run is
    # still in settle would be reaped again by settle-reap
    assert "새 런이 선 **뒤에** 같은 턴에서 되살리기를 요청한다" in new_task
    assert "보고 전에 요청하지 않는다" in new_task
    # a tie is broken by the name auto-release wrote, read before the new run
    # replaces what cflow2 status shows
    assert "`AUTO-RELEASE` 줄에 `reviewer: <이름>`으로 적은 이름을 고른다" in new_task
    assert "끝난 런의 reviewer-ensure 값을 여기서 얻을 수 없다" in new_task
    assert "session.spawn" in nodes["end-new-task"]["effects"]
    ensure = _words(nodes["reviewer-ensure"]["prompt"])
    assert "REVIEWER RECYCLE:" in ensure
    assert "`withdraw: true`로 요청을 거둔 뒤" in ensure
    assert "rejected·failed·withdrawn·moot" in ensure


#: The nodes the end-watch loop runs, stubbed: every one prints its own name.
#: The self-improve nodes are the library's own (self-improve-common).
#: end-notice is not here: it runs before the gate, in settle (claunch-q4cqy.6).
END_NODES = ("end-hold", "end-new-task", "auto-release")
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
    end_q = _brief("end", "hold", "new-task", "self-improve", "auto-release")
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


def _end_flow(tmp_path):
    """The end-watch loop compiled alone, with the end nodes stubbed."""
    from claude_launcher.cflow2 import frontend
    doc = yaml.safe_load(V2.read_text(encoding="utf-8"))
    flow = {"cflow": 2, "name": "end-flow", "include": ["ask-common", "self-improve-common"],
            "nodes": _end_stubs(), "body": [_find_id(doc["procedures"], "end-watch")]}
    return frontend.compile_text(yaml.safe_dump(flow, allow_unicode=True, sort_keys=False),
                                 resolve_include=layers.resolver(str(tmp_path)))


def _end_labels(st):
    # a finished run carries no 'tasks' key at all
    return [t["label"].rsplit("/", 1)[-1] for t in st.get("tasks", [])]


def _pose(st, brief, **kw):
    from claude_launcher.cflow2 import runtime
    iid = next(t["instance"] for t in st["tasks"]
               if t["label"].rsplit("/", 1)[-1] == "ask-frame")
    return runtime.report(iid, "ok", {"brief": brief}, **kw)


def test_the_end_question_times_out_into_auto_release_then_the_release_gate(
        tmp_path, monkeypatch):
    """claunch-q4cqy: the end question has a five-minute deadline; past it
    the run cleans up (auto-release) and asks again at release-decision,
    which has no deadline of its own. An explicit auto-release answer takes
    the same road."""
    from claude_launcher.cflow2 import runtime
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    chart = _end_flow(tmp_path)
    kw = {"cwd": str(tmp_path), "scope": "m", "now": 1.0}
    st = runtime.start(chart, caps={}, **kw)
    st = _pose(st, _brief("end", "hold", "new-task", "self-improve", "auto-release"), **kw)
    assert st["status"] == "waiting_approval"
    assert [a["node"] for a in st["asks"]]
    # the clock's pass five minutes after the question opened
    st = runtime.status(cwd=kw["cwd"], scope=kw["scope"], now=302.0)
    assert _end_labels(st) == ["auto-release"], _end_labels(st)
    iid = st["tasks"][0]["instance"]
    st = runtime.report(iid, "ok", {}, cwd=kw["cwd"], scope=kw["scope"], now=310.0)
    assert _end_labels(st) == ["ask-frame"]
    st = _pose(st, _brief("end", "hold", "new-task"),
               cwd=kw["cwd"], scope=kw["scope"], now=320.0)
    assert st["status"] == "waiting_approval"
    # release-decision has no deadline: an hour later it is still the question
    st = runtime.status(cwd=kw["cwd"], scope=kw["scope"], now=4000.0)
    assert _end_labels(st) == [] and st["status"] == "waiting_approval"
    runtime.approve(None, decision="end", reason="r",
                    cwd=kw["cwd"], scope=kw["scope"], now=4010.0)
    st = runtime.status(cwd=kw["cwd"], scope=kw["scope"], now=4011.0)
    # claunch-q4cqy.6: the cleanup already ran and the notice already went out
    # before the gate, so end here runs no node -- the run just finishes.
    assert _end_labels(st) == [] and st["status"] == "done", _end_labels(st)


def test_end_at_the_first_gate_cleans_up_then_finishes_the_run(
        tmp_path, monkeypatch):
    """claunch-q4cqy.6: end chosen at end-decision has not passed auto-release
    yet, so it runs the cleanup and then the run finishes -- release-decision
    is not asked again."""
    from claude_launcher.cflow2 import runtime
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    chart = _end_flow(tmp_path)
    kw = {"cwd": str(tmp_path), "scope": "m", "now": 1.0}
    st = runtime.start(chart, caps={}, **kw)
    st = _pose(st, _brief("end", "hold", "new-task", "self-improve", "auto-release"), **kw)
    assert st["status"] == "waiting_approval"
    runtime.approve(None, decision="end", reason="r", **kw)
    st = runtime.status(**kw)
    assert _end_labels(st) == ["auto-release"], _end_labels(st)
    iid = st["tasks"][0]["instance"]
    st = runtime.report(iid, "ok", {}, **kw)
    assert st["status"] == "done"
    assert _end_labels(st) == []
    # no release-decision behind the cleanup: the person already answered end
    state = runtime.RunStore(kw["cwd"], kw["scope"]).load_state()
    assert [a for a in state["asks"].values() if a["status"] == "open"] == []


def test_the_notice_runs_before_the_end_gate():
    """claunch-q4cqy.6: the notice is written on the turn the round just
    ended, while the context cache is still warm -- a session standing at the
    gate has an invalidated cache, so the one-line notice must not cost a
    prefill of that context. In settle the notice therefore sits after the
    retrospective and before end-watch, not inside the loop."""
    chart = _chart()
    settle = chart.nodes["settle.body"]
    children = settle.props["children"]

    def try_task(node_id):
        """The label of the single task a try{seq:[...]} wrapper runs."""
        node = chart.nodes[node_id]
        if node.type != "try":
            return None
        seq = chart.nodes[node.props["child"]]
        return chart.nodes[seq.props["children"][0]].label

    # after the retrospective's try, before the gate loop. The closure gate
    # (claunch-5sryb) stands first, ahead of settle-watch.
    assert chart.nodes[children[0]].label == "closure-gate"
    assert chart.nodes[children[1]].label == "settle-watch"
    assert try_task(children[2]) == "retrospective"
    assert try_task(children[3]) == "end-notice"
    gate = next(c for c in children if chart.nodes[c].label == "end-watch")
    assert children.index(children[3]) < children.index(gate)
    # the notice is not a member of either gate's cases any more, and each
    # gate's end does its own part: the first cleans up, the second is a bare
    # break (the cleanup already ran before it).
    first_end = chart.nodes["settle/end-decision.cases.case.end"]
    assert [chart.nodes[c].label for c in first_end.props["children"]][0] == "auto-release"
    second_end = chart.nodes["settle/release-decision.cases.case.end"]
    assert len(second_end.props["children"]) == 1
    assert chart.nodes[second_end.props["children"][0]].props["op"] == "break"
    for case in (first_end, second_end):
        assert "settle/end-notice" not in str(chart.nodes[case.id].props)


def test_ending_the_session_at_an_end_gate_finishes_the_run(tmp_path, monkeypatch):
    """claunch-q4cqy: a person may answer the round's last question by ending
    the session instead of picking an option. The daemon's exit hook then
    leaves the state ``end`` would have left: the open ask closed, the run
    done -- not a run stranded at waiting_approval with no terminal left."""
    from claude_launcher.cflow2 import runtime
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    chart = _end_flow(tmp_path)
    kw = {"cwd": str(tmp_path), "scope": "m", "now": 1.0}
    st = runtime.start(chart, caps={}, **kw)
    st = _pose(st, _brief("end", "hold", "new-task", "self-improve", "auto-release"), **kw)
    assert st["status"] == "waiting_approval"
    ask = st["asks"][0]["ask"]
    done = runtime.end_on_session_exit(cwd=kw["cwd"], scope=kw["scope"], now=5.0)
    assert done["finished"] is True and done["ask"] == ask, done
    st = runtime.status(cwd=kw["cwd"], scope=kw["scope"], now=6.0)
    assert st["status"] == "done"
    state = runtime.RunStore(kw["cwd"], kw["scope"]).load_state()
    assert state["asks"][ask]["status"] == "cancelled"
    # and it is not done twice
    again = runtime.end_on_session_exit(cwd=kw["cwd"], scope=kw["scope"], now=7.0)
    assert again["finished"] is False


def test_hold_keeps_the_run_open_and_asks_again_under_a_new_ask(
        tmp_path, monkeypatch):
    """claunch-llfac: hold no longer ends the run. From end-decision it cleans
    up first (as end does), runs end-hold and poses release-decision; from
    release-decision it runs end-hold and poses the same question again
    under a new ask id. The run stays waiting on a person -- so kill-on-end
    never applies and no keep-alive is needed -- until end or new-task."""
    from claude_launcher.cflow2 import runtime
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    chart = _end_flow(tmp_path)
    kw = {"cwd": str(tmp_path), "scope": "m", "now": 1.0}
    st = runtime.start(chart, caps={}, **kw)
    st = _pose(st, _brief("end", "hold", "new-task", "self-improve", "auto-release"), **kw)
    runtime.approve(None, decision="hold", reason="r", **kw)
    st = runtime.status(**kw)
    assert _end_labels(st) == ["auto-release"], _end_labels(st)
    st = runtime.report(st["tasks"][0]["instance"], "ok", {}, **kw)
    assert _end_labels(st) == ["end-hold"], _end_labels(st)
    st = runtime.report(st["tasks"][0]["instance"], "ok", {}, **kw)
    assert _end_labels(st) == ["ask-frame"], _end_labels(st)
    st = _pose(st, _brief("end", "hold", "new-task"), **kw)
    assert st["status"] == "waiting_approval"
    first = st["asks"][0]["ask"]
    # hold again at release-decision: end-hold, then the question anew
    runtime.approve(None, decision="hold", reason="r", **kw)
    st = runtime.status(**kw)
    assert _end_labels(st) == ["end-hold"], _end_labels(st)
    st = runtime.report(st["tasks"][0]["instance"], "ok", {}, **kw)
    assert _end_labels(st) == ["ask-frame"], _end_labels(st)
    st = _pose(st, _brief("end", "hold", "new-task"), **kw)
    assert st["status"] == "waiting_approval"
    # the driver reads the hold command off the waiting task, session included
    assert "`claunch hold $CLAUNCH_SESSION`" in st["asks"][0]["task_prompt"]
    second = st["asks"][0]["ask"]
    assert second != first
    # the reposed question is still an end gate: ending the session answers it
    done = runtime.end_on_session_exit(cwd=kw["cwd"], scope=kw["scope"], now=5.0)
    assert done["finished"] is True and done["ask"] == second, done
    assert runtime.status(**kw)["status"] == "done"


def test_end_hold_sends_the_session_to_hold_without_keep_alive():
    """claunch-llfac: the hold answer means the session hold state (held_at,
    daemon/hold.py). keep-alive would keep the session out of the idle hold
    and delivery-hold would leave a woken session unable to be held again, so
    neither is set; `claunch hold` runs after release-decision is posed."""
    doc = yaml.safe_load(V2.read_text(encoding="utf-8"))
    text = doc["nodes"]["end-hold"]["prompt"]
    assert "claunch keep-alive $CLAUNCH_SESSION" not in text
    assert "delivery-hold $CLAUNCH_SESSION --on" not in text
    assert "keep-alive와 delivery-hold는 세우지 않는다" in text
    watch = _find_id(doc["procedures"], "release-watch")
    release = watch["loop"]["do"][0]
    assert release["id"] == "release-decision"
    assert release["cases"]["hold"] == ["end-hold", {"continue": "release-watch"}]
    # `lit` keeps $CLAUNCH_SESSION as text; a bare with: string reads $ as an expression
    nudge = release["with"]["nudge"]["lit"]
    # every hold command the driver is told to run must name the session:
    # `claunch hold` has no CLAUNCH_SESSION fallback (error: no session given)
    import re
    from claude_launcher.cli import build_parser
    from claude_launcher.cli_sessions import _resolve_target
    commands = re.findall(r"`(claunch hold[^`]*)`", text + nudge)
    assert commands, "no hold command in end-hold / release-decision"
    for cmd in commands:
        if cmd == "claunch hold":
            continue  # quoted as the failing form, not as an instruction
        argv = cmd.replace("$CLAUNCH_SESSION", "s1").split()[1:]
        args = build_parser().parse_args(argv)
        assert _resolve_target(args) and args.session == "s1", cmd
    assert "`claunch hold $CLAUNCH_SESSION`을 실행한다" in nudge
    assert "`claunch hold $CLAUNCH_SESSION`은 여기서 실행하지 않는다" in text
    for topic in (release["with"]["topic"],
                  _find_id(doc["procedures"], "end-decision")["with"]["topic"]):
        flat = " ".join(topic.split())
        assert "세션 hold 상태" in flat and "keep-alive" not in flat


def test_ending_the_session_away_from_an_end_gate_leaves_the_run_alone(
        tmp_path, monkeypatch):
    """A question that is not the end gate is somebody's real decision; a
    session dying does not answer it."""
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    from claude_launcher.cflow2 import frontend, runtime
    flow = {"cflow": 2, "name": "ask-flow", "include": ["ask-common"], "nodes": {},
            "body": [{"id": "cost-gate", "call": "ask-user",
                      "with": {"topic": "t"},
                      "cases": {"spend": [], "skip": []}}]}
    chart = frontend.compile_text(yaml.safe_dump(flow, allow_unicode=True, sort_keys=False),
                                  resolve_include=layers.resolver(str(tmp_path)))
    kw = {"cwd": str(tmp_path), "scope": "n", "now": 1.0}
    st = runtime.start(chart, caps={}, **kw)
    st = _pose(st, dict(BRIEF, recommendation="spend", options=[
        dict(BRIEF["options"][0], name="spend"),
        dict(BRIEF["options"][1], name="skip")]), **kw)
    assert st["status"] == "waiting_approval"
    done = runtime.end_on_session_exit(cwd=kw["cwd"], scope=kw["scope"], now=5.0)
    assert done["finished"] is False and "not an end gate" in done["why"], done
    assert runtime.status(cwd=kw["cwd"], scope=kw["scope"], now=6.0)["status"] == \
        "waiting_approval"


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


def test_a_long_landing_wait_alerts_the_user_by_thread_before_the_loop_limit():
    """s29-qf1 sat in the await/landing-nudge loop for 25 rounds (about four
    hours) with the request NOT queued: terminal text and issue comments raise
    no notification, and flow_nudge is refused while the run owes a person
    nothing. landing-nudge tells the worker to open or answer a thread at fixed
    points; the shared pack carries the general rule.

    The ceiling is 5 rounds now (about 50 minutes, claunch-gpgw1), so the
    re-alert point moved from the sixth nudge to the fourth: a sixth nudge
    cannot happen before the loop stops."""
    from claude_launcher.cflow2 import frontend

    doc = yaml.load(V2.read_text(encoding="utf-8"), Loader=frontend._Loader)
    node = doc["nodes"]["landing-nudge"]
    nudge = _flat(node["prompt"])
    for anchor in ("사용자 알림", "`thread_reply`", "`thread_open`",
                   "3번째 재촉", "4번째 재촉", "`flow_nudge`", "loop_limit",
                   "`owed`"):
        assert anchor in nudge, f"landing-nudge lost {anchor!r}"
    assert "6번째 재촉" not in nudge, "a sixth nudge cannot happen inside a 5-round wait"
    assert "사용자 알림」의 스레드로" in nudge, "NOT queued must point at the thread notice"
    assert "알린 스레드 id" in _flat(node["done_when"])
    common = _flat((V2.parents[2] / "prompt_packs" / "common.md").read_text(encoding="utf-8"))
    for anchor in ("같은 대기 단계를 반복할 때", "`thread_reply`", "`thread_open`",
                   "`flow_nudge`", "대기 한도에 닿기 전에"):
        assert anchor in common, f"common.md lost {anchor!r}"


def test_the_landing_wait_stops_at_five_rounds():
    """The wait was 25 rounds of ten minutes (about four hours); a round whose
    parent never answers sat there while nobody was told. User decision
    2026-10-30 (claunch-gpgw1): 5 rounds, about 50 minutes, so a fault reaches
    a person quickly. The gate that stands before the wait quotes the same
    ceiling, and the two statements of it must not drift."""
    from claude_launcher.cflow2 import frontend

    doc = yaml.load(V2.read_text(encoding="utf-8"), Loader=frontend._Loader)
    loop, _chart_ = _await_flow()
    assert loop["loop"]["max"] == 5
    gate = _flat(doc["nodes"]["landing-channel-gate"]["ask"]["prompt"])
    assert "상한(5회, 10분 간격)" in gate
    assert "25회" not in gate


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


# -- claunch-dsr4o: a person may skip the targeted tests while they run --------


def _skip_gate(chart):
    [gate] = [n for n in chart.nodes.values() if n.label == "test-skip-gate"]
    return gate


def test_the_skip_gate_races_the_tests_loop():
    """Static shape: the gate is a person's choose in a race par beside the
    tests loop; its one option raises user-skipped, which the enclosing try
    sends to test-skipped."""
    chart = _chart()
    gate = _skip_gate(chart)
    assert gate.type == "choose" and gate.get("chooser") == "user"
    assert gate.get("require_reason") is True
    race = chart.node(gate.parent)
    assert race.type == "par" and race.get("settle") == "race"
    branches = race.get("branches")
    assert set(branches) == {"tests", "skip-gate"}
    tests = chart.node(branches["tests"])
    assert tests.type == "loop" and tests.label == "tests"
    [skip] = gate.get("options")
    assert skip["name"] == "skip"
    raised = chart.node(skip["child"])
    assert raised.type == "control" and raised.get("op") == "raise" \
        and raised.get("outcome") == "user-skipped"
    guard = chart.node(race.parent)
    while guard.type != "try":
        guard = chart.node(guard.parent)
    handler = chart.node(guard.get("catch")["user-skipped"])
    assert handler.label == "test-skipped"
    assert "replan" in guard.get("catch")


def _to_review(tmp_path, monkeypatch):
    from claude_launcher.cflow2 import runtime

    st, kw = _coding_item(tmp_path, monkeypatch, None)
    st = runtime.report(_one(st, "work"), "ok", {}, **kw)
    [choice] = st["choices"]
    st = runtime.select(choice["instance"], "run", reason="code change", **kw)
    return st, kw


def _gates(st):
    return [c for c in st["choices"] if c["label"] == "test-skip-gate"]


_REVIEWED = {"modules": ["tests/test_mesh.py"], "surfaces": "관련 표면 없음", "tried": []}


def test_the_skip_gate_is_open_while_the_tests_run(tmp_path, monkeypatch):
    from claude_launcher.cflow2 import runtime

    st, kw = _to_review(tmp_path, monkeypatch)
    assert [t["label"] for t in st["tasks"]] == ["review"]
    [gate] = _gates(st)
    assert gate["chooser"] == "user"
    # the run is not held: the agent keeps reporting the test tasks
    assert st["status"] == "running"
    st = runtime.report(_one(st, "review"), "ok", _REVIEWED, **kw)
    assert [t["label"] for t in st["tasks"]] == ["targeted-test"]
    assert [g["instance"] for g in _gates(st)] == [gate["instance"]]
    # a new failure sends the loop round again: the same gate stays open
    st = runtime.report(_one(st, "targeted-test"), "ok",
                        {**_MEASURED, "failed": 1, "new_failed": 1}, **kw)
    assert [t["label"] for t in st["tasks"]] == ["review"]
    assert [g["instance"] for g in _gates(st)] == [gate["instance"]]


def test_the_skip_gate_goes_away_when_the_tests_finish(tmp_path, monkeypatch):
    """Nobody presses it: the tests loop ends, the race drops the open
    choice, and it does not come back later in the item."""
    from claude_launcher.cflow2 import runtime

    st, kw = _to_review(tmp_path, monkeypatch)
    [gate] = _gates(st)
    st = runtime.report(_one(st, "review"), "ok", _REVIEWED, **kw)
    st = runtime.report(_one(st, "targeted-test"), "ok", {**_MEASURED, "failed": 0}, **kw)
    assert [t["label"] for t in st["tasks"]] == ["commit"]
    assert _gates(st) == []
    with pytest.raises(runtime.FlowError):
        runtime.select(gate["instance"], "skip", reason="too late", caller="user", **kw)
    seen = []
    while st["tasks"]:
        [task] = st["tasks"]
        if task["label"] == "work":
            break
        seen.append(task["label"])
        assert _gates(st) == [], task["label"]
        st = runtime.report(task["instance"], "ok", {}, **kw)
    assert "test-skipped" not in seen
    journal = runtime.journal(cwd=kw["cwd"], scope=kw["scope"])
    left = [e for e in journal if e.get("event") == "left"
            and e.get("instance") == gate["instance"]]
    assert left and left[-1]["outcome"] == "cancelled"


@pytest.mark.parametrize("at", ["review", "targeted-test"])
def test_a_person_skip_stops_the_tests_loop(tmp_path, monkeypatch, at):
    from claude_launcher.cflow2 import runtime

    st, kw = _to_review(tmp_path, monkeypatch)
    if at == "targeted-test":
        st = runtime.report(_one(st, "review"), "ok", _REVIEWED, **kw)
    running = _one(st, at)
    [gate] = _gates(st)
    # the agent cannot press it
    with pytest.raises(runtime.FlowError):
        runtime.select(gate["instance"], "skip", reason="mine", **kw)
    st = runtime.select(gate["instance"], "skip", reason="tests take 20 min; sweep covers it",
                        caller="user", **kw)
    assert [t["label"] for t in st["tasks"]] == ["test-skipped"]
    assert _gates(st) == []
    assert st["person_picks"][-1]["label"] == "test-skip-gate"
    assert st["person_picks"][-1]["reason"] == "tests take 20 min; sweep covers it"
    # the running test task is gone: its report is refused
    with pytest.raises(runtime.FlowError):
        runtime.report(running, "ok", _REVIEWED, **kw)
    st = runtime.report(_one(st, "test-skipped"), "ok", {}, **kw)
    assert [t["label"] for t in st["tasks"]] == ["commit"]


def test_test_skipped_says_how_a_person_skip_is_marked():
    from claude_launcher.cflow2 import frontend

    doc = yaml.load(V2.read_text(encoding="utf-8"), Loader=frontend._Loader)
    nodes = doc["nodes"]
    skipped = _flat(nodes["test-skipped"]["prompt"])
    assert "TESTS SKIPPED @ <HEAD 해시>: user — <사람의 reason>" in skipped
    assert "person_picks" in skipped
    landing = _flat(V2.read_text(encoding="utf-8"))
    assert "마커의 사유가 `user — `로 시작하면" in landing
    stack = _flat((V2.parent / "stack-v2.yaml").read_text(encoding="utf-8"))
    assert "`user — `로 시작하면" in stack


# -- claunch-wuaup: the tests loop ends on new failures, not on preexisting ones


def _to_targeted_test(tmp_path, monkeypatch):
    from claude_launcher.cflow2 import runtime

    st, kw = _coding_item(tmp_path, monkeypatch, None)
    [work] = st["tasks"]
    st = runtime.report(work["instance"], "ok", {}, **kw)
    [choice] = st["choices"]
    st = runtime.select(choice["instance"], "run", reason="code change", **kw)
    [review] = st["tasks"]
    assert review["label"] == "review"
    st = runtime.report(review["instance"], "ok",
                        {"modules": ["tests/test_mesh.py"], "surfaces": "관련 표면 없음",
                         "tried": []}, **kw)
    [measure] = st["tasks"]
    assert measure["label"] == "targeted-test"
    return measure, kw


_MEASURED = {"modules": ["tests/test_mesh.py"], "passed": 40, "tree": "abc",
             "env": "worktree venv", "receipt": "r.json", "tried": []}


@pytest.mark.parametrize("value, next_label", [
    # every failure also fails at the merge base: the loop ends
    ({"failed": 21, "new_failed": 0,
      "preexisting": ["tests/test_mesh.py::test_old"]}, "commit"),
    # one of them is this change's: back to review
    ({"failed": 22, "new_failed": 1,
      "preexisting": ["tests/test_mesh.py::test_old"]}, "review"),
    # a report without the split keeps the old condition (failed == 0)
    ({"failed": 2}, "review"),
    ({"failed": 0}, "commit"),
])
def test_the_tests_loop_judges_new_failures_only(tmp_path, monkeypatch, value, next_label):
    from claude_launcher.cflow2 import runtime

    measure, kw = _to_targeted_test(tmp_path, monkeypatch)
    st = runtime.report(measure["instance"], "ok", {**_MEASURED, **value}, **kw)
    assert [t["label"] for t in st["tasks"]] == [next_label]


def test_the_targeted_test_prompt_says_how_preexisting_failures_are_reported():
    from claude_launcher.cflow2 import frontend

    doc = yaml.load(V2.read_text(encoding="utf-8"), Loader=frontend._Loader)
    nodes = doc["nodes"]
    measure = _flat(nodes["targeted-test"]["prompt"])
    for anchor in ("PREEXISTING FAILURES @ <base_commit>", "--known-failures <파일>",
                   "--deselect <nodeid>", "--no-compare-base", "--labels found",
                   "가르지 못한 실패는 새 실패로 센다"):
        assert anchor in measure, f"targeted-test lost {anchor!r}"
    returns = nodes["targeted-test"]["returns"]["ok"]
    assert returns["new_failed"] == "int?" and returns["preexisting"] == "list?"
    assert "새 실패부터 고친다" in _flat(nodes["review"]["prompt"])
    assert "기존 실패: <건수> @ <base_commit>" in _flat(
        nodes["integration-request"]["prompt"])


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
    # step children carry the same provenance label as a follow-up (claunch-dr9cu)
    assert "--parent <주 이슈> --labels user-direct --assignee $CLAUNCH_SESSION" in intake
    pr = _words(labelled["pr-open"].get("prompt"))
    assert "PR 하나가 연속 작업 전체다" in pr
    assert "공통 parent(없으면 주 이슈)" in pr


def test_every_issue_a_run_starts_gets_its_own_external_ref():
    """The board keeps external-ref unique per issue, and one run starts
    several issues -- a revisit round, the second item of a batch. With the
    bare run id the second `update --external-ref` was refused (run
    20261017-185520-cfc785, claunch-dr9cu) and the session made up a suffix.
    Every start command now writes `<run id>/<id>` (claunch-7r1qr.1)."""
    for path in (V2, COMMON, V1):
        text = path.read_text(encoding="utf-8")
        assert "--external-ref <run id>`" not in text, path.name
        assert "external-ref에 run id" not in text, path.name
    chart = _chart()
    labelled = {n.label: n for n in chart.nodes.values() if n.label}
    for label in ("intake-issue", "issue-adopt", "issue-create", "item-intake"):
        prompt = _words(labelled[label].get("prompt"))
        assert "--external-ref <run id>/<id>`" in prompt, label
    intake = _words(labelled["intake-issue"].get("prompt"))
    assert "run id만 쓰면 같은 런의 둘째 이슈에서 보드가 거절한다" in intake


def test_a_child_spawn_waits_for_the_spawn_request_block():
    """An agent's non-reviewer child spawn answers 202 pending since
    f9fccdd2 (claunch-iw5i8.16): the name arrives later in a
    [claunch spawn-request <id>] block. Every node that spawns a child
    carries the spawn-pending rule; a node that does not spawn does not
    (claunch-fuw9o)."""
    spawning = {
        "improv-worker-v2": ("work", "wrapup", "found-handoff"),
        "stack-v2": ("open",),
        "improv-leader-v2": ("handle-regression",),
    }
    for name, labels in spawning.items():
        labelled = {n.label: n for n in layers.load(name).nodes.values() if n.label}
        for label in labels:
            prompt = _words(labelled[label].get("prompt"))
            assert "자식 spawn은 승인 대기로 돌아온다" in prompt, (name, label)
            assert "[claunch spawn-request <id>]" in prompt, (name, label)
    leader = {n.label: n for n in layers.load("improv-leader-v2").nodes.values() if n.label}
    assert "자식 spawn은 승인 대기로 돌아온다" not in _words(leader["wrapup"].get("prompt"))
    # the definitions and texts outside improv-common say the same inline
    pkg = Path(claude_launcher.__file__).parent
    inline = [
        "workflows/v2/improv-coordinator-v2.yaml", "workflows/v2/perf-monitor-v2.yaml",
        "workflows/v2/reposync-merge-push-v2.yaml", "workflows/improv-worker.yaml",
        "workflows/improv-leader.yaml", "workflows/improv-pm.yaml", "workflows/stack.yaml",
        "workflows/e2e-session-roundtrip.yaml", "prompt_packs/child-agents.md",
        "mesh_install.py",
    ]
    for rel in inline:
        assert "[claunch spawn-request <id>]" in (pkg / rel).read_text(encoding="utf-8"), rel


def test_the_targeted_run_starts_in_the_background_whenever_possible():
    """claunch-4s5d0: the user asked that the test step start its run in the
    background whenever the harness allows it. The earlier baseline made it
    conditional on the expected run time ("예상 시간이 길면"), which left short
    runs in the foreground; the waiting rules around it are unchanged."""
    chart = _chart()
    node = next(n for n in chart.nodes.values() if n.label == "targeted-test")
    prompt = _words(node.get("prompt"))
    assert "예상 시간과 관계없이 가능하면 항상 백그라운드로 시작한다" in prompt
    assert "예상 시간이 길면 백그라운드" not in prompt
    assert "백그라운드 실행의 끝은 그 프로세스로 기다린다" in prompt


# -- the branch decision: reuse before cutting (claunch-f56fz) ---------------


def test_branch_setup_reuses_a_branch_before_it_cuts_a_new_one():
    """A round used to cut a fresh feature branch every time, so a session
    reused across rounds -- or a worktree shared with another session --
    split one line of work into a fresh branch per round (s1762/s1803 both
    ran in the same worktree and left one branch each). The rule is now a
    decision: keep the branch you are on when it is fit to carry the round,
    and cut only when it is not.

    What still must hold: the name of a NEW branch is session-prefixed, an
    existing branch is never overwritten, and the report says which of the
    two happened and why -- a done_when that only accepts a "new" branch
    would push every round back to cutting one.
    """
    chart = _chart()
    setup = next(n for n in chart.nodes.values() if n.label == "branch-setup")
    prompt = _words(setup.get("prompt"))
    done_when = _words(setup.get("done_when") or "")

    # the decision comes first, and it names all three ways to keep a branch
    assert prompt.index("지금 있는 브랜치를 쓸 수 있는가") < prompt.index("새로 팔 때 이름은")
    for anchor in (
        "통합 대상이 아니다",             # the target itself is not a work branch
        "이미 착지하지 않았다",            # git branch --contains ... --list <기준>
        "동결·심사 중이 아니다",           # a frozen branch must not move
        "이 세션의 이번 회차 브랜치가 이미 있다",   # restart / re-entry
        "지금 체크아웃을 다른 세션이 함께 쓴다",      # the shared worktree case
        "위 둘이 아니다",                  # and only then: cut a new one
    ):
        assert anchor in prompt, f"branch-setup lost its reuse rule {anchor!r}"

    # the shared-worktree case cites the incidents it exists for
    assert "s1762" in prompt and "s1803" in prompt

    # a new branch keeps its session prefix and never overwrites a name
    assert "<세션>-<요지>" in prompt
    assert "$CLAUNCH_SESSION" in prompt
    assert "이미 있는 브랜치를 덮어쓰지 않는다" in prompt

    # a reused branch is switched to, not cut; its uncommitted work is not ours
    assert "재사용을 골랐으면 브랜치를 만들지 않고" in prompt
    assert "그 브랜치의 미커밋 변경은 내 것이 아니다" in prompt

    # done_when accepts a reuse, and asks for the reason either way
    assert "재사용한 경우 1의 네 조건을 만족한다는 근거가 보고에 있다" in done_when
    assert "새 세션 접두사 피처 브랜치가 존재하고" not in done_when


def test_the_commit_step_folds_only_a_branch_this_round_cut():
    """The --no-ff fold into the worktree branch is how a round tidies its own
    fresh branch away. On a reused branch there is no second line to fold --
    the round's commits are already on that branch -- so the fold is scoped
    to ``branch-setup`` having cut one."""
    chart = _chart()
    commit = next(n for n in chart.nodes.values() if n.label == "commit")
    prompt = _words(commit.get("prompt"))
    assert "branch-setup이 이 세션의 브랜치를 새로 판 경우에만 이 절이 적용된다" in prompt
    assert "접을 갈래가 없으므로 머지하지 않는다" in prompt


def test_intake_no_longer_promises_a_fresh_branch_per_round():
    """``intake`` used to tell the reader a reused session still gets a new
    branch ("이전 회차의 브랜치를 새 목표에 재사용하지 않는다"), and the
    re-entry paragraph said ``branch-setup`` cuts one. Both contradicted the
    reuse decision; the branch is now settled at ``branch-setup``."""
    chart = _chart()
    intake = _words(next(n for n in chart.nodes.values() if n.label == "intake").get("prompt"))
    assert "이전 회차의 브랜치를 새 목표에 재사용하지 않는다" not in intake
    assert "실제 브랜치 이름과 생성 여부는" in intake
    assert "쓸 수 없을 때만 이 요지로 새로 판다" in intake


def test_intake_leaves_code_reading_and_measuring_to_later_steps():
    """deepseek-v4.1-flash sessions spent 30-90 tool calls in ``intake``
    reading code, tests and data to fill scope and evidence, against 4-21
    for Claude on the same prompt (claunch-igkbo). The prompt now says what
    intake reads, asks for feature/kind level, and gives a question task
    its goal form. Nothing reads intake's evidence; its scope is read by
    ``prior-context`` (as a search query) and by ``sim-check``, which runs
    before ``coding-plan`` and used to pick its sources "from the scope" --
    so sim-check now finds the paths itself when the scope names none, and
    the ``scope`` input's help (shown in the web form and CLI) no longer
    promises that intake fills in paths. Review of 422aa158 caught both."""
    chart = _chart()
    node = next(n for n in chart.nodes.values() if n.label == "intake")
    intake = _words(node.get("prompt"))
    done = _words(node.get("done_when"))
    sim_check = next(n for n in chart.nodes.values() if n.label == "sim-check")
    sim = _words(sim_check.get("prompt"))
    doc = yaml.safe_load(V2.read_text(encoding="utf-8"))
    scope_help = _words(doc["inputs"]["scope"]["help"])
    assert "intake는 범위를 기능 수준으로 적고, 바꿀 파일은 coding-plan이 고른다" in scope_help
    assert "intake가 목표에서 정한다" not in scope_help
    assert "없으면(intake는 범위를 기능 수준으로 적는다) 목표와 지금 코드에서 이 단계가 찾는다" in sim
    assert "(범위에서 고른다)" not in sim
    assert "스택 기준의 git 참조 확인 — 기준 tip 비교와 rebase — 은 예외다" in intake
    assert "이 단계가 읽는 것은 오프닝·이슈 본문·보드(이슈와 코멘트)까지다" in intake
    assert "바꿀 파일을 고르는 일은 coding-plan이 하고" in intake
    assert "질문에 답하는 일은 work가 한다" in intake
    assert "「질문 <요지>에 근거와 함께 답한다」" in intake
    assert "기능·동작 수준으로 적는다" in intake
    assert "종류만 적는다" in intake
    assert "work·wrapup이 잰 뒤 채운다" in intake
    assert "시작 입력 범위(고쳐도 되는 경로)" in intake
    assert "기능·종류 수준이면 충분하다" in done


def test_no_step_still_promises_a_brand_new_branch_for_every_round():
    """The reuse decision has to reach the places that cite it, not just the
    step that makes it. Leftovers survived the first pass and reviewers
    found them (claunch-f56fz):

    * ``stack-round`` said "(intake의 branch-setup과 같은 규칙)" next to
      "cut a new ``<세션>-stack-round-<n>`` from master" -- citing the rule
      while instructing its opposite.
    * ``next-round`` described the next round as "새 목표·새 브랜치·새 착지".
    * ``workspace``'s occupancy axis excluded the main checkout because
      "branch-setup이 워크트리를 파서 그 경우를 스스로 푼다" -- but the rule
      now keeps the shared branch instead of always cutting a worktree, so
      that stated grounds no longer holds (the axis value itself does not
      change: the main checkout is still excluded).

    The collection this reads covers every node and every choose option, so
    all four anchors below fire on a revert. The second review caught the
    earlier version reading only labelled nodes -- 498 of 788 have no label,
    and ``next-round``'s line is in one of them, which made that anchor true
    no matter what the yaml said.
    """
    chart = _chart()
    everything = _chart_prose(chart)

    # What this guard reads has to include an unlabelled choose option's
    # description, or the ``next-round`` anchor below is vacuous. This line
    # lives in exactly such an option; if the collection ever narrows again,
    # this fails here rather than letting the anchors pass silently.
    assert "배정 큐가 남았다" in everything

    assert "intake의 branch-setup과 같은 규칙" not in everything
    assert "새 목표·새 브랜치·새 착지" not in everything
    assert "branch-setup이 워크트리를 파서" not in everything
    assert "브랜치를 파기 전에" not in everything

    # the shipped text: stack-round hands the decision over instead of
    # prescribing a cut, and the occupancy axis names the reuse rule
    assert "branch-setup`의 판정을 그대로 쓴다" in everything
    assert "재사용 판정이 그 공유를 조건부로 승인한다" in everything
    # and next-round, in the option description the collection now reaches
    assert "새 목표·브랜치 판정·새 착지" in everything


def test_chart_prose_reaches_a_procedure_description():
    """A procedure's ``description`` is prose an author writes for a reader,
    and it reaches no compiled node -- the frontend keeps it on its own
    record (claunch-qt475). A guard written against the procedure text used
    to pass no matter what that text said, because the collection only ever
    walked ``chart.nodes``.

    The first anchor lives in ``land``'s description and nowhere else in any
    layer file, so it fails the moment the collection stops reading the
    procedures -- which is the revert this test exists to catch. The second
    lives in a procedure of an *included* file (``improv-common``), so it
    fails if the collection reads only the definition's own file.
    """
    chart = _chart()
    # claunch-ijkge added the fifth outcome to this sentence; the anchor is
    # the sentence as it stands, so the test keeps failing when the prose
    # collection stops reading procedures.
    anchor = ("결과는 landed(머지 해시), held(동결로 마감), rework(작업으로 "
              "되돌아감), blocked(차단으로 요청을 세우지 않음), pr-submitted(PR만 제출, "
              "머지는 사용자가 함 — auto_merge=false) 다섯 중 하나다")
    included = "회차 도중 발견한 범위 밖 결함 하나를 이슈로 남긴다"

    # not reachable from nodes: this is the gap the collection had to close,
    # and asserting it keeps the anchor from being satisfied by some node
    # that happens to say the same thing
    nodes_only = _words(" ".join(
        str(value)
        for node in chart.nodes.values()
        for key in ("prompt", "description", "options")
        for value in [node.get(key)]
        if isinstance(value, str)
    ))
    assert anchor not in nodes_only
    assert included not in nodes_only

    assert anchor in _chart_prose(chart)
    assert included in _chart_prose(chart)


# -- 재작업 예산: 3회까지 자율, 그 뒤는 사람 승인 (사용자 지시 2026-10-26) -----
#
# 「rework를 최대 3회까지 자율적으로 진행할 수 있도록 해줘. 이후에는 사용자
# 허가하에 추가 1회, 진행.」 예산의 계수는 ``rework-gate``의 방문 수다 — 그
# 노드는 재작업 한 바퀴마다 정확히 한 번 들어가고, 방문 수는 회차가 바뀌어도
# 런 전체에서 이어진다.

REWORK_BUDGET = 3


def _declined_tree(doc):
    """``land``의 declined 갈래(await-landing의 선택지 ``then``) 그대로."""
    def find(o):
        if isinstance(o, dict):
            branch = o.get("declined")
            if isinstance(branch, dict) and "then" in branch:
                return branch["then"]
            o = list(o.values())
        if isinstance(o, list):
            for item in o:
                hit = find(item)
                if hit:
                    return hit
        return None

    tree = find(doc["procedures"]["land"]["body"])
    assert tree, "land의 declined 갈래를 찾지 못했다"
    return tree


def _rework_budget_chart(tmp_path):
    """정의의 declined 갈래를, 그 갈래가 실제로 사는 모양(``land`` 프로시저 +
    batch 루프의 try/catch)으로 감싼 작은 차트."""
    from claude_launcher.cflow2 import frontend

    doc = yaml.safe_load(V2.read_text(encoding="utf-8"))
    nodes = {k: doc["nodes"][k] for k in
             ("landing-declined", "rework-auto", "rework-gate", "landing-declined-hold")}
    flow = {
        "cflow": 2, "name": "rework-budget", "include": ["ask-common"],
        "procedures": {"land": {
            "params": {"mode": {"type": "str", "default": "full"}},
            "returns": {"rework": {"reason": "str", "rework": "bool"},
                        "held": {"reason": "str", "rework": "bool"}},
            "body": _declined_tree(doc)}},
        "nodes": nodes,
        "body": [{"id": "batch", "loop": {
            "while": "true", "max": 20,
            "do": [{"try": {"seq": [{"id": "land", "call": "land", "bind": "land_out",
                                     "with": {"mode": "'full'"}}]},
                    "catch": {"rework": {"continue": "batch"},
                              "held": {"break": "batch"}}}]}}],
    }
    return frontend.compile_text(yaml.safe_dump(flow, allow_unicode=True, sort_keys=False),
                                 resolve_include=layers.resolver(str(tmp_path)))


BRIEF_REWORK = {"problem": "재작업을 재개할지 정한다", "recommendation": "rework", "why": "w",
                "options": [{"name": name, "summary": "s", "risk": "r", "outcome": "o",
                             "reversibility": "reversible"}
                            for name in ("rework", "hold")]}


def _drive_rework(tmp_path, monkeypatch, refusals):
    """착지 거절을 ``refusals`` 번 지나며 지나간 노드 라벨을 순서대로 낸다.

    지나간 노드는 짧은 라벨이고 ``ASK:<label>``은 사람 게이트에 선 자리다.
    게이트에 서면 ``rework``로 답하고 계속한다. 예산 안(3회)을 넘는 거절은
    게이트를 지나므로, 그 수만큼 게이트가 서는 것을 보고 멈춘다.
    """
    from claude_launcher.cflow2 import runtime

    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    kw = {"cwd": str(tmp_path), "scope": "m", "now": 1.0}
    st = runtime.start(_rework_budget_chart(tmp_path), caps={}, **kw)
    gates_expected = max(0, refusals - REWORK_BUDGET)
    path = []
    for _ in range(200):
        if st.get("asks"):
            ask = st["asks"][0]
            path.append("ASK:" + ask["label"].rsplit("/", 1)[-1])
            if path.count("ASK:ask-pose") == gates_expected:
                break
            runtime.approve(ask["instance"], decision="rework", reason="go", **kw)
            st = runtime.status(**kw)
            continue
        if not st.get("tasks"):
            break
        task = st["tasks"][0]
        label = task["label"].rsplit("/", 1)[-1]
        if label == "ask-frame":
            runtime.report(task["instance"], "ok", {"brief": BRIEF_REWORK}, **kw)
        else:
            path.append(label)
            runtime.report(task["instance"], "ok",
                           {"reason": "declined", "issues": [], "msg": "x"}, **kw)
            if label == "landing-declined" and path.count("landing-declined") == refusals \
                    and gates_expected == 0:
                break
        st = runtime.status(**kw)
    return path


def test_the_autonomous_rework_budget_is_three_refusals(tmp_path, monkeypatch):
    """예산 안의 거절 세 번은 사람을 세우지 않는다 — ``rework-auto``가 예고하고
    ``rework-gate``가 예산을 센다. 네 번째 거절부터 게이트가 선다.

    s1895 실측: 이 게이트 하나가 회차를 20:28Z~03:29Z(약 7시간) 세웠다.
    """
    path = _drive_rework(tmp_path, monkeypatch, refusals=4)
    before_gate = path[:path.index("ASK:ask-pose")]
    assert before_gate.count("rework-gate") == REWORK_BUDGET, before_gate
    assert before_gate.count("rework-auto") == REWORK_BUDGET, before_gate
    assert "ASK:ask-pose" in path


def test_the_budget_gate_reads_the_gate_visits():
    """게이트의 조건은 ``$visits['rework-gate']``이고, 아직 방문이 없을 때(null)를
    먼저 본다 — ``null < 3``은 식 오류라 순서가 뒤집히면 첫 거절이 런을 죽인다."""
    from claude_launcher.cflow2 import runtime

    guard = runtime.load_definition("improv-worker-v2").node("round/land/rework-budget") \
        .props["branches"][0]["guard"]
    assert "$visits['rework-gate']" in guard
    assert guard.index("== null") < guard.index("<")
    assert f"< {REWORK_BUDGET}" in guard


def test_the_budget_does_not_reset_between_rounds(tmp_path, monkeypatch):
    """방문 수는 런 전체에서 세므로 배치 패스가 바뀌어도 예산이 되살아나지
    않는다 — 예산을 쓴 뒤의 거절은 모두 게이트를 지난다."""
    path = _drive_rework(tmp_path, monkeypatch, refusals=5)
    assert path.count("ASK:ask-pose") == 2, path
    assert path.count("rework-auto") == REWORK_BUDGET, path


def test_hold_at_the_gate_ends_without_rework(tmp_path, monkeypatch):
    """예산을 다 쓴 뒤 사람이 hold를 고르면 되감지 않고 마감한다 — 승인 1회가
    「추가 1회」이고, 그 답이 hold면 그 1회는 쓰이지 않는다."""
    from claude_launcher.cflow2 import runtime

    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    kw = {"cwd": str(tmp_path), "scope": "m", "now": 1.0}
    st = runtime.start(_rework_budget_chart(tmp_path), caps={}, **kw)
    answered = False
    for _ in range(60):
        if st.get("asks"):
            ask = st["asks"][0]
            if ask["label"].endswith("ask-pose"):
                runtime.approve(ask["instance"], decision="hold", reason="별건으로 넘긴다", **kw)
                answered = True
            else:
                runtime.approve(ask["instance"], decision="rework", reason="go", **kw)
            st = runtime.status(**kw)
            continue
        if not st.get("tasks"):
            break
        task = st["tasks"][0]
        if task["label"].endswith("ask-frame"):
            runtime.report(task["instance"], "ok", {"brief": BRIEF_REWORK}, **kw)
        else:
            runtime.report(task["instance"], "ok",
                           {"reason": "declined", "issues": [], "msg": "x"}, **kw)
        st = runtime.status(**kw)
    assert answered

    state = runtime.RunStore(str(tmp_path), "m").load_state()
    assert state["status"] == "done"
    assert state["visits"]["rework-gate"] == REWORK_BUDGET
    # hold 갈래는 사람 게이트를 지나 곧바로 마감 절차로 간다 (rework-gate 없이)
    entered = [e["node"] for e in runtime.journal(cwd=str(tmp_path), scope="m")
               if e["event"] == "entered"]
    assert "land/landing-declined-hold" in entered
    assert entered.count("land/rework-gate") == REWORK_BUDGET
    assert state["data"]["land_out"] == {"reason": "declined", "rework": False}


def test_chart_prose_reaches_a_done_when():
    """``done_when`` is the line a step is actually judged by, and it is in
    no ``prompt``, ``description`` or option: 97 nodes carry one of their
    own, and none of those 97 strings appears among the prose those other
    keys hold (claunch-4zz0b).

    So a guard written against ``done_when`` used to pass no matter what it
    said -- the same blind spot the procedure descriptions had before
    claunch-qt475. The anchor below lives in ``done_when`` (in ``commit``'s
    and ``integration-request``'s) and nowhere in ``prompt``,
    ``description`` or an option, which is the assertion the body makes
    below; the collection stopping its ``done_when`` key fails this test.
    """
    chart = _chart()
    anchor = "master는 움직이지 않았다"

    # unreachable from the keys the collection used to read: asserting it
    # keeps the anchor from being satisfied by some prompt that happens to
    # say the same thing, which would make this test vacuous again
    without_done_when = []
    for node in chart.nodes.values():
        for key in ("prompt", "description"):
            value = node.get(key)
            if isinstance(value, str):
                without_done_when.append(value)
        options = node.get("options")
        if isinstance(options, list):
            for option in options:
                if isinstance(option, dict):
                    for value in option.values():
                        if isinstance(value, str):
                            without_done_when.append(value)
    assert anchor not in _words(" ".join(without_done_when))

    assert anchor in _chart_prose(chart)


# -- parallel_fork (claunch-dm09i) --------------------------------------------------


def _fork_item(tmp_path, monkeypatch, parallel):
    """The real item route with parallel_fork set, implementation tasks stubbed."""
    import json

    from claude_launcher.cflow2 import frontend, inputs, runtime

    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    doc = yaml.load(V2.read_text(encoding="utf-8"), Loader=frontend._Loader)
    doc["body"] = [{"call": "item"}]
    doc.pop("spec", None)
    for name in ("test-skipped", "commit"):
        doc["nodes"][name] = {"prompt": name}
    chart = frontend.compile_text(json.dumps(doc, ensure_ascii=False),
                                  resolve_include=layers.resolver())
    chart = inputs.apply(chart, {} if parallel is None else {"parallel_fork": parallel})
    kw = {"cwd": str(tmp_path), "scope": "forking", "now": 1.0}
    st = runtime.start(chart, caps={}, **kw)
    return _start_work(st, kw), kw


WORK_OK = {"files": ["a.py"], "summary": "s", "tried": []}


def test_parallel_fork_is_a_user_settable_bool_off_by_default():
    decl = _chart().meta["inputs"]["parallel_fork"]
    assert decl["type"] == "bool" and decl["default"] is False
    assert decl.get("set_by") == ["user"] and decl.get("group") == "병렬 작업"


@pytest.mark.parametrize("parallel", [None, False])
def test_parallel_fork_off_keeps_work_alone_and_skips_slice_merge(tmp_path, monkeypatch,
                                                                  parallel):
    from claude_launcher.cflow2 import runtime

    st, kw = _fork_item(tmp_path, monkeypatch, parallel)
    assert [t["label"] for t in st["tasks"]] == ["work"]
    slices = next(g for g in st["groups"] if g["name"] == "slices")
    assert slices["can_fork"] is False and slices["limit"] == 5
    with pytest.raises(runtime.FlowError, match="does not take members"):
        runtime.fork("slices", {"goal": "g", "paths": ["a.py"]}, **kw)
    st = runtime.report(st["tasks"][0]["instance"], "ok", WORK_OK, **kw)
    assert not st["tasks"]
    assert [c["label"] for c in st["choices"]] == ["test-decision"]


def test_parallel_fork_on_joins_every_slice_before_slice_merge(tmp_path, monkeypatch):
    from claude_launcher.cflow2 import runtime

    st, kw = _fork_item(tmp_path, monkeypatch, True)
    slices = next(g for g in st["groups"] if g["name"] == "slices")
    assert slices["can_fork"] is True and slices["member_executor"] == ["fork", "inline"]
    for i in range(2):
        runtime.fork("slices", {"goal": f"g{i}", "paths": [f"p{i}/"]}, **kw)
    st = runtime.status(**kw)
    # no daemon in a test: fork falls back inline and the slices come back here
    assert sorted(t["label"] for t in st["tasks"]) == ["slice-work", "slice-work", "work"]
    assert "p0/" in " ".join(t["prompt"] for t in st["tasks"])
    work = next(t for t in st["tasks"] if t["label"] == "work")
    st = runtime.report(work["instance"], "ok", WORK_OK, **kw)
    # the group waits for both slices
    assert [t["label"] for t in st["tasks"]] == ["slice-work", "slice-work"]
    for t in st["tasks"]:
        st = runtime.report(t["instance"], "ok",
                            {"files": ["p.py"], "summary": "x", "tests": "none"}, **kw)
    [merge] = st["tasks"]
    assert merge["label"] == "slice-merge" and "g0" in merge["prompt"]
    st = runtime.report(merge["instance"], "ok", WORK_OK, **kw)
    assert [c["label"] for c in st["choices"]] == ["test-decision"]


def test_parallel_fork_limit_is_five_slices(tmp_path, monkeypatch):
    from claude_launcher.cflow2 import runtime

    _, kw = _fork_item(tmp_path, monkeypatch, True)
    for i in range(5):
        runtime.fork("slices", {"goal": f"g{i}", "paths": [f"p{i}/"]}, **kw)
    with pytest.raises(runtime.FlowError, match="limit of 5"):
        runtime.fork("slices", {"goal": "g5", "paths": ["p5/"]}, **kw)


def test_the_work_prompt_holds_the_parallel_fork_rules():
    prose = _chart_prose(_chart())
    for anchor in ("group: slices", "조각을 하나라도 세웠으면 이 작업에서 커밋하지 않는다",
                   "auto-compact까지 100k 토큰 미만"):
        assert _words(anchor) in prose, anchor


# --------------------------------------------------------------------------- #
# A blocked landing request is a declared outcome, not fail
# (claunch-improver-integration-request-blocked-o6paj)
# --------------------------------------------------------------------------- #
def test_a_blocked_landing_request_is_a_declared_outcome():
    """The request node can say "I raised no request" without ``fail``.

    ``integration-request`` declared only ``ok``. ``validate_result`` always
    accepts ``fail`` (the engine's own failure result), so a session told to
    stand down had no other outcome to report. Nothing between that node and
    the round loop caught ``fail``, so ``rounds.do``'s next statement --
    ``recheck`` (queue-recheck) -- was skipped whole: run
    20261027-164135-3ad031, journal 02:33:08, five ``fail`` lines down to
    ``rounds@2 -> finally: settle``.
    """
    doc = yaml.safe_load(V2.read_text(encoding="utf-8"))
    req = doc["nodes"]["integration-request"]
    assert set(req["returns"]) == {"ok", "blocked"}
    assert req["returns"]["blocked"] == {"reason": "str"}


def test_land_declares_and_routes_blocked_out_of_the_batch():
    """A declaration alone is dead: an uncaught ``blocked`` leaves the
    ``batch`` try as a non-ok result, and ``_loop_body_done`` completes it
    upward (``if outcome != OUTCOME_OK: _complete(...)``) -- the same escape
    as ``fail``."""
    doc = yaml.safe_load(V2.read_text(encoding="utf-8"))
    land = doc["procedures"]["land"]
    # pr-submitted (claunch-ijkge) is the auto_merge=false ending: the round
    # stops at PR submission and the person merges, so it is a declared
    # outcome too and has to leave the batch the same way.
    assert set(land["returns"]) == {"landed", "held", "rework", "blocked", "pr-submitted"}
    # reason is required; the route fields are what a person's answer is
    # recorded with so the next round need not ask again (claunch-1aj1a.1),
    # and they are optional because the ``reframe`` answer carries none. The
    # place is ``repo`` + ``relation`` (the reuse condition), ``owner`` is
    # the receiver the evidence measured, and ``issue`` says what was
    # answered about.
    assert land["returns"]["blocked"] == {
        "reason": "str", "decision": "str?", "tip": "str?", "repo": "str?",
        "owner": "str?", "issue": "str?", "relation": "str?"}

    batch = _find_id(doc["procedures"]["worker-round"]["body"], "batch")
    # ``do: {try: [...], catch: {...}}`` -- a bare-key mapping, not a list
    [try_node] = [batch["loop"]["do"]] if isinstance(batch["loop"]["do"], dict) \
        else [c for c in batch["loop"]["do"] if isinstance(c, dict) and "try" in c]
    catch = try_node["catch"]
    assert set(catch) == {"rework", "landed", "held", "blocked", "pr-submitted"}
    # a blocked round is over: continuing would re-run the work that was just
    # refused a request, so it leaves the batch loop like held does.
    assert catch["blocked"] == {"break": "batch"}
    assert catch["rework"] == {"continue": "batch"}
    # a submitted round is over too: the PR is open and the merge is the
    # person's, so the batch must not re-run the work under another request.
    assert catch["pr-submitted"] == {"break": "batch"}


# --------------------------------------------------------------------------- #
# PR 제출에서 끝나는 회차: auto_merge (claunch-ijkge)
# --------------------------------------------------------------------------- #
def test_auto_merge_is_a_pr_input_defaulting_to_the_current_behaviour():
    """The user's shape for the variable: the default keeps today's path
    (landing request, parent's merge, landed), and the manual-merge path is
    the opt-in. A default of ``false`` would have changed every existing run
    into a run that stops at the PR."""
    doc = yaml.safe_load(V2.read_text(encoding="utf-8"))
    spec = doc["inputs"]["auto_merge"]
    assert spec["type"] == "bool"
    assert spec["default"] is True
    assert spec["group"] == "PR"
    assert spec["set_by"] == ["user"]
    # the two switches it qualifies read the same inputs dict, so the help
    # text has to say it applies to the PR path only
    assert "로컬 착지에는 적용되지 않는다" in _words(spec["help"])


def test_auto_merge_false_ends_the_round_at_pr_submission():
    """``land``'s submit loop publishes the PR and, when auto_merge is off,
    ends the landing right there with its own outcome -- not by continuing to
    the landing request, which would put the parent back in the merge seat
    the person just took."""
    doc = yaml.safe_load(V2.read_text(encoding="utf-8"))
    submit = _find_id(doc["procedures"]["land"]["body"], "submit")
    ids = [n.get("id") for n in submit["loop"]["do"] if isinstance(n, dict)]
    assert ids.index("publish") < ids.index("pr-submitted-route")
    route = _find_id(submit, "pr-submitted-route")
    branch = route["cond"][0]           # the if-branch; the second is the else
    assert "$data.landing.medium == 'pr'" in branch["if"]
    assert "$data.inputs.auto_merge == false" in branch["if"]
    # a stack-only round carries no work branch and no claim; the marker there
    # would have no issue to write to
    assert "$params.mode == 'full'" in branch["if"]
    mark, stop = branch["then"]
    assert mark == "pr-submitted-mark"
    assert stop["return"] == "pr-submitted"
    # the value is the PR the person merges, not a merge hash
    assert set(stop["value"]) == {"url", "number", "tip"}
    assert "pr_url" in stop["value"]["url"]
    assert "pr_number" in stop["value"]["number"]


def test_the_submitted_pr_marker_is_what_leaves_the_issue_in_review():
    """The board is the record the workflow and the person both read, so the
    marker has to be written by the round that submitted, and the round must
    not close the issue itself -- the merge has not happened yet."""
    doc = yaml.safe_load(V2.read_text(encoding="utf-8"))
    task = doc["nodes"]["pr-submitted-mark"]
    prose = _words(task["prompt"])
    for anchor in ("PR SUBMITTED @ <tip> <url>", "--status in_review",
                   "PR SUBMITTED @ <tip> (batch of", "하지 않는 것"):
        assert anchor in prose, anchor
    # the work the round does not do is named, so a session does not infer it
    for anchor in ("gh pr merge", "이슈 close"):
        assert anchor in prose, anchor
    assert set(task["returns"]["ok"]) == {"url", "number", "tip"}


def test_wrapup_does_not_close_a_submitted_pr_issue():
    doc = yaml.safe_load(V2.read_text(encoding="utf-8"))
    wrap = _words(doc["nodes"]["wrapup"]["prompt"])
    assert "PR만 제출하고 끝난 회차로 왔으면" in wrap
    assert "닫지 않는다" in wrap
    assert "PR SUBMITTED @ <tip> <url>" in wrap


def test_settle_reads_a_submitted_pr_as_settled_for_this_session():
    """A session whose PR waits on a person's merge must still be able to
    end: the branch is pushed, the PR is the durable record, and the merge is
    not this session's act. The exception is narrow -- in_review plus this
    session's own marker -- so a stale in_review from another cause still
    blocks."""
    doc = yaml.safe_load(V2.read_text(encoding="utf-8"))
    check = _find_id(doc["procedures"]["settle"]["body"], "settle-check")
    prose = _words(check["choose"]["prompt"])
    assert "PR만 제출하고 머지를 남긴 이슈" in prose
    assert "PR SUBMITTED @" in prose
    assert "author`가 이 세션" in prose
    assert "in_review" in prose


def test_a_local_fallback_warns_the_person_before_the_branch_can_be_lost():
    """When PR submission is impossible (gh missing or logged out) the round
    falls back to a local branch. That branch lives only in this machine's
    checkout, so the person has to see the warning -- a board comment alone
    does not reach them."""
    doc = yaml.safe_load(V2.read_text(encoding="utf-8"))
    submit = _find_id(doc["procedures"]["land"]["body"], "submit")
    warn = _find_id(submit, "local-warning")
    branch = warn["cond"][0]
    assert "$data.landing.medium == 'local'" in branch["if"]
    assert "$params.mode == 'full'" in branch["if"]
    # a person who chose local already knows; this warning is for the
    # automatic fallback only
    assert "force_pr != true" in branch["if"]
    assert "force_no_pr != true" in branch["if"]
    assert branch["then"] == ["pr-local-notice"]
    prose = _words(doc["nodes"]["pr-local-notice"]["prompt"])
    for anchor in ("thread_open", "thread_reply", "유실"):
        assert anchor in prose, anchor
    # remote-setup keeps the board marker and defers the visible warning
    setup = _words(doc["nodes"]["remote-setup"]["prompt"])
    assert "LANDING MEDIUM: local (PR 불가: <reason>)" in setup
    assert "local-warning" in setup


def _local_warning_flow(landing, tmp_path):
    """`land`'s local-warning branch as a workflow of its own (claunch-ijkge.2).

    The medium is decided by the round's `remote-setup` task -- an agent reads
    the gh preflight -- so a test cannot make gh logged out. What it can do is
    seed the decision that follows, which is where this warning lives. The
    condition and the node come from the definition, so what is measured is
    the engine's routing plus the reason reaching the warning text.
    """
    doc = yaml.safe_load(V2.read_text(encoding="utf-8"))
    submit = _find_id(doc["procedures"]["land"]["body"], "submit")
    warn = _find_id(submit, "local-warning")
    flow = {
        "cflow": 2,
        "name": "local-warning-flow",
        "params": {"mode": {"default": "full"}},
        "inputs": {"force_pr": {"type": "bool", "default": False},
                   "force_no_pr": {"type": "bool", "default": False}},
        "nodes": {"pr-local-notice": doc["nodes"]["pr-local-notice"],
                  "after": {"prompt": "after"}},
        "data": {"landing": landing, "claim": {"issue": "claunch-x"}},
        "body": [warn, "after"],
    }
    return compile_text(yaml.safe_dump(flow, allow_unicode=True, sort_keys=False),
                        resolve_include=layers.resolver(str(tmp_path)))


def test_a_local_landing_without_a_pr_leaves_a_warning_the_person_can_see(
        tmp_path, monkeypatch):
    """claunch-ijkge.2: a round that cannot publish a PR lands on a branch that
    exists only in this machine's checkout, so it has to say so -- reason
    included -- in a task the round actually runs. The structure test above
    pins the branch; this one pins that the branch fires and carries the
    reason, which is the part a person depends on."""
    from claude_launcher.cflow2 import runtime
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    reason = "gh not logged in"
    chart = _local_warning_flow({"medium": "local", "reason": reason}, tmp_path)
    st = runtime.start(chart, caps={}, cwd=str(tmp_path), scope="m", now=1.0)
    assert [t["label"].rsplit("/", 1)[-1] for t in st["tasks"]] == ["pr-local-notice"]
    prompt = _words(st["tasks"][0]["prompt"])
    assert reason in prompt            # the failure reaches the person
    assert "유실" in prompt            # next to what the local branch can lose
    assert "gh auth login" in prompt   # and how to make the copy durable
    assert "thread_open" in prompt     # through the channel they watch


def test_a_person_who_chose_local_is_not_warned_about_the_branch(
        tmp_path, monkeypatch):
    """`force_no_pr` is the person's own answer. Warning them about the risk
    of the option they picked would be the round arguing with that answer, so
    the branch is gated on the switch too (not only on `medium == 'local'`)."""
    from claude_launcher.cflow2 import runtime
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    chart = _local_warning_flow({"medium": "local", "reason": "user chose local"},
                                tmp_path)
    st = runtime.start(chart, caps={}, cwd=str(tmp_path), scope="m", now=1.0,
                       inputs={"force_no_pr": True})
    assert [t["label"].rsplit("/", 1)[-1] for t in st["tasks"]] == ["after"]


def test_a_pr_landing_raises_no_local_warning(tmp_path, monkeypatch):
    """The warning belongs to the fallback, not to every round: a round whose
    landing record already says `pr` must not tell the person to run
    `gh auth login`."""
    from claude_launcher.cflow2 import runtime
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    chart = _local_warning_flow({"medium": "pr", "reason": "gh ok"}, tmp_path)
    st = runtime.start(chart, caps={}, cwd=str(tmp_path), scope="m", now=1.0)
    assert [t["label"].rsplit("/", 1)[-1] for t in st["tasks"]] == ["after"]


def test_a_blocked_request_does_not_push_a_landing_row():
    """``_queue_triggers`` runs on every outcome (runtime._complete's
    ``-> triggers (at leave)``), so a ``when`` reading only ``relation``
    queues the push for a blocked round too -- "I raised no request" would
    raise one, re-adding a refused tip to an issue already in_review. The
    ``frozen`` push beside them always had this guard."""
    chart = _chart()
    node = next(n for n in chart.nodes.values() if n.label == "integration-request")
    land_pushes = [t for t in node.get("triggers") or []
                   if t.get("do") == "push" and (t.get("push") or {}).get("to")
                   in ("parent:landing", "leader:landing")]
    assert len(land_pushes) == 2
    for t in land_pushes:
        assert "$outcome == 'ok'" in t["when"], t["when"]
    # the pair still splits on relation -- the guard is added, not swapped in
    assert {t["when"].replace(" and $outcome == 'ok'", "") for t in land_pushes} == {
        "$data.inputs.relation != 'independent'",
        "$data.inputs.relation == 'independent'",
    }
    for d in node.get("do") or []:
        if (d.get("item") or {}).get("name") == "'frozen'":
            assert "$outcome == 'ok'" in d["when"]


def test_the_request_prompt_states_what_a_blocked_round_does_not_do():
    """A declared outcome the prompt never mentions stays unused: the session
    holding the node reads its prompt, and the three "do not"s are the whole
    difference between a blocked round and a request.

    Asserted against ``prompt`` alone, not the chart's whole prose: every one
    of these also appears in ``done_when``, so a union-wide guard stays green
    when the prompt loses the sentence -- it measures the field it is not
    supposed to be protecting (the vacuous-assertion class this issue family
    has paid for four times).
    """
    doc = yaml.safe_load(V2.read_text(encoding="utf-8"))
    prompt = _words(doc["nodes"]["integration-request"]["prompt"])
    for anchor in ("in_review로 전이하지 않는다",
                   "`LANDING REQUEST` 코멘트를 남기지 않는다",
                   "BLOCKED: 착지 요청 없음"):
        assert _words(anchor) in prompt, anchor
    # ...and the outcome is named, or the three "do not"s hang off no label
    assert "`blocked`" in prompt


def test_the_prompt_narrows_the_trigger_to_a_round_that_arrives_blocked():
    """The trigger the prompt gives has to be one that reaches this node.

    ``86a8a77b`` wrote the first trigger as "``land``'s landing decision ended
    in escalation and the answer was *do not raise it this time*". That round
    never arrives here: a ``hold`` answer makes ``landing-review-route``'s
    ``on.hold`` return ``held`` from ``land`` itself, so everything under
    ``submit`` -- including this node -- is skipped. The prompt was telling the
    session holding this node to report a disposition it could only be given
    by a path that cannot reach it.

    Read from ``prompt`` alone. The same idea also appears in ``done_when``,
    so a union-wide guard would stay green with the prompt unchanged -- the
    vacuous-assertion class this issue family has paid for four times
    (claunch-improver-blocked-prompt-fixes-jcjai).

    Two halves, because either one alone can be satisfied by the wrong text:
    the unreachable trigger is gone, **and** the prompt states why a hold
    never reaches here, with the field that carries the answer away.
    """
    doc = yaml.safe_load(V2.read_text(encoding="utf-8"))
    prompt = _words(doc["nodes"]["integration-request"]["prompt"])

    # The unreachable trigger, as 86a8a77b wrote it.
    assert _words("`land`의 착지 결정이 escalate로 끝나 그 답이") not in prompt

    # The remaining trigger is the state this node can observe on entry.
    assert _words("이 스텝에 들어설 때 착지가 막혀 있으면") in prompt

    # ...and the hold answer is named as the one that never arrives, with the
    # mechanism (return: held from land) and the consequence (submit is skipped
    # and this step does not run). Naming the mechanism is what makes the
    # sentence checkable by a later reader; the consequence is what tells the
    # session that a held round is not its outcome to report.
    assert _words("상위의 `hold` 답") in prompt
    assert _words("`return: held`") in prompt
    assert _words("`submit` 아래의 이 스텝이 실행되지 않는다") in prompt


def test_the_prompt_tells_the_next_round_how_to_clear_the_blocked_marker():
    """``BLOCKED:`` is a brake marker, and the prompt must say how it is lifted.

    ``land``'s landing check 4 reads ``claunch beads comments`` for
    ``CHANGES REQUESTED`` / ``HOLD`` / ``BLOCKED`` that are not resolved, and
    escalates when one is. ``claunch beads comments`` has ``add`` and ``list``
    with no delete, so a ``BLOCKED:`` this node writes can only ever be lifted
    by a later comment -- and without that instruction the marker this node
    was just told to write becomes a latch: the same issue, picked up next
    round, escalates at check 4 forever.

    The two precedents in this file state the resolution alongside the marker
    (``sim-gate`` at :1329, ``remote-setup`` at :1153). This asserts the same
    for the marker this prompt introduces, and asserts the three properties
    that make the instruction usable rather than decorative:

    * the resolution comment has a fixed spelling a later reader can look for,
      carrying the marker's own ``@ <tip>`` so one round's resolution cannot
      silently clear another round's marker;
    * who may write it is stated (the marker's author or the ancestor that
      set it) -- a resolution anyone can write is not a gate;
    * the reason is required to be a checkable condition, because check 4
      escalates when it cannot decide whether a marker is resolved.

    The lift spelling is ``CLEARED``, not ``UNBLOCKED``: check 4 looks for
    ``BLOCKED``, and ``UNBLOCKED`` contains it, so the resolution comment
    would read as a live brake to anyone scanning for the marker. This test
    pins that, because it is a property of the spelling rather than of any
    one sentence and the obvious word for "lift a block" is the broken one.

    Read from ``prompt`` alone, for the reason given in the sibling tests.
    """
    doc = yaml.safe_load(V2.read_text(encoding="utf-8"))
    prompt = _words(doc["nodes"]["integration-request"]["prompt"])

    assert _words("CLEARED: 착지 요청 없음 @ <tip>") in prompt
    # The prompt must carry the reason the lift word is CLEARED and not the
    # obvious UNBLOCKED: check 4 looks for `BLOCKED`, which sits inside
    # `UNBLOCKED`. Asserting "BLOCKED" not in "CLEARED" would measure this
    # test's own literals and could never fail -- so the guard is that the
    # prompt states the trap, which is what stops a later author putting the
    # word back.
    assert _words("낱말을 `UNBLOCKED`로 쓰지 않는 이유") in prompt
    # who may write it, named: the marker's author or the ancestor that set it.
    # Asserted as the named parties, not as the word "자격" -- a survivor of a
    # half-applied edit kept that word while losing the parties, and the first
    # version of this test stayed green on it.
    assert _words("남긴 세션 자신(다음 회차의 같은 세션이다)과, 마커를 상위가 건 경우 그 상위다") in prompt
    assert _words("확인 가능한 조건") in prompt
    # the marker it resolves is the one this same prompt has the node write,
    # and the check that reads it is named -- otherwise "clear the marker"
    # points at nothing the session can find
    assert _words("BLOCKED: 착지 요청 없음") in prompt
    assert _words("착지 확인 4") in prompt


def test_a_blocked_request_reaches_queue_recheck(tmp_path, monkeypatch):
    """The wiring, driven: a ``blocked`` request keeps the round loop going.

    Fails on the pre-fix definition. ``land``'s ``submit`` loop leaves
    ``integration-request`` with ``blocked``; ``_loop_body_done`` completes
    that upward, and the ``batch`` try's catch turns it into ``break``, so
    ``worker-round`` ends ``ok`` and ``rounds.do`` reaches its next statement
    ``recheck``. Before the fix the node could only report ``fail``, which no
    layer caught: ``round`` and ``rounds`` failed and the run fell to
    ``finally: settle`` with ``queue-recheck`` never entered (run
    20261027-164135-3ad031, journal 02:33:08).

    What is real: the packaged ``rounds`` loop and the ``round`` → ``recheck``
    sequencing, ``worker-round``'s ``batch`` try **and its catch**, ``land``'s
    ``submit`` loop, and ``integration-request``'s declared returns (the
    engine validates the reported ``blocked`` against them). What is trimmed:
    ``submit``'s other steps (align, review, PR publish) and the work inside
    the batch -- those need git and a board, and none of them is the path
    under test.
    """
    import json

    from claude_launcher.cflow2 import frontend, runtime

    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    doc = yaml.load(V2.read_text(encoding="utf-8"), Loader=frontend._Loader)
    doc.pop("spec", None)

    # land: keep the submit loop (and the packaged integration-request call
    # out of it), drop the stack cut, the upstream ask and the align/review/
    # publish steps around it.
    submit = [c for c in doc["procedures"]["land"]["body"]
              if isinstance(c, dict) and c.get("id") == "submit"][0]
    # a bare string is a node reference (the form the packaged definition uses)
    submit["loop"]["do"] = ["integration-request"]
    doc["procedures"]["land"]["body"] = [submit]

    # worker-round: keep the batch loop's try/catch, drop the work before land
    batch = _find_id(doc["procedures"]["worker-round"]["body"], "batch")
    batch["loop"]["do"]["try"] = [
        {"id": "land", "call": "land", "with": {"mode": "full",
                                                "allow": ["request", "escalate"]}}
    ]
    doc["procedures"]["worker-round"]["body"] = [batch]

    # the daemon performs triggers; the run under test has none
    doc["nodes"]["integration-request"].pop("triggers", None)

    chart = frontend.compile_text(json.dumps(doc, ensure_ascii=False),
                                  resolve_include=layers.resolver())
    kw = {"cwd": str(tmp_path), "scope": "blocked", "now": 1.0}
    st = runtime.start(chart, caps={}, **kw)

    [first] = st["tasks"]
    assert first["label"] == "integration-request", first["label"]
    st = runtime.report(first["instance"], "blocked",
                        {"reason": "reviewer brake is open"}, **kw)

    # queue-recheck's body is a choose, so reaching it shows up as an open
    # choice, not a task -- and the journal names the node either way.
    entered = [e for e in runtime.journal(cwd=kw["cwd"], scope=kw["scope"])
               if e.get("event") == "entered"
               and str(e.get("node", "")).startswith("recheck")]
    assert entered, [e.get("event") for e in
                     runtime.journal(cwd=kw["cwd"], scope=kw["scope"])[-12:]]
    assert st["status"] == "running"
    # ...and the round itself finished ok rather than failing the loop
    round_left = [e for e in runtime.journal(cwd=kw["cwd"], scope=kw["scope"])
                  if e.get("event") == "left" and e.get("node") == "round"]
    assert round_left and round_left[-1]["outcome"] == "ok", round_left


# -- claunch-d8scd: landing entry (start_at=landing) ------------------------


def test_landing_entry_seeds_the_first_round_and_the_recheck_is_the_invalidation():
    """``start_at=landing`` is a round kind for the FIRST round only.

    The seed step sits in front of the rounds loop, so it runs once. Every
    later round takes its kind from ``data.recheck.mode``, and queue-recheck
    rewrites that with ``full`` on both of its continuing answers — that
    rewrite is the invalidation, so the definition needs no reset node."""
    doc = yaml.load(V2.read_text(encoding="utf-8"), Loader=frontend._Loader)

    steps = doc["body"]["try"]
    assert [s["id"] for s in steps] == ["entry-mode", "rounds"]

    arms = steps[0]["cond"]
    assert arms[0]["if"] == "$data.inputs.start_at == 'landing'"
    assert arms[0]["then"] == {"call": "landing-entry", "bind": "recheck"}
    assert list(arms[1]) == ["else"] and arms[1]["else"] == []

    entry = doc["procedures"]["landing-entry"]
    assert entry["body"] == [{"return": "ok",
                              "value": {"next": "next-round", "mode": "land-only"}}]

    # the switch is a start input with a default that keeps every existing run
    # on the full path
    assert doc["inputs"]["start_at"]["choices"] == ["full", "landing"]
    assert doc["inputs"]["start_at"]["default"] == "full"

    call = steps[1]["loop"]["do"][0]
    assert call["id"] == "round" and call["call"] == "worker-round"
    assert call["with"]["mode"] == "$data.recheck.mode"

    opts = doc["procedures"]["queue-recheck"]["body"]["choose"]["options"]
    assert opts["next-round"]["then"]["value"] == {"next": "next-round", "mode": "full"}
    assert opts["done"]["then"]["value"] == {"next": "done", "mode": "full"}


def _entry_mode_chart(doc, rounds):
    """The canon's ``entry-mode`` step and ``landing-entry`` procedure inside a
    skeleton whose rounds report back the ``mode`` they were called with.

    The queue-recheck stand-in answers ``second`` rather than ``full`` so a
    later round's kind is distinguishable from the seed's."""
    import json

    from claude_launcher.cflow2 import frontend, runtime

    entry = doc["body"]["try"][0]
    assert entry["id"] == "entry-mode"
    flow = {
        "cflow": 2, "name": "landing-entry",
        "inputs": {"start_at": {"type": "enum", "choices": ["full", "landing"],
                                "default": "full"}},
        "data": {"recheck": {"next": "next-round", "mode": "full"}},
        # a task after the loop keeps the run open, so start() answers with the
        # payload that carries `result` (a run that finishes inside start()
        # comes back as a summary without it)
        "nodes": {"after": {"title": "after", "prompt": "after",
                            "returns": {"ok": {}}}},
        "procedures": {
            "landing-entry": doc["procedures"]["landing-entry"],
            "worker-round": {"params": {"mode": {"type": "str", "default": ""}},
                             "returns": {"ok": {"mode": "str"}},
                             "body": [{"return": "ok",
                                       "value": {"mode": "$params.mode"}}]},
            "queue-recheck": {"returns": {"ok": {"next": "str", "mode": "str"}},
                              "body": [{"return": "ok",
                                        "value": {"next": "next-round",
                                                  "mode": "second"}}]},
        },
        "body": [entry, {"id": "rounds", "loop": {
            "while": f"$visits.round == null or $visits.round < {rounds}",
            "max": rounds,
            "do": [{"id": "round", "call": "worker-round", "bind": "round",
                    "with": {"mode": "$data.recheck.mode"}},
                   {"id": "recheck", "call": "queue-recheck", "bind": "recheck"}]}},
                 "after"],
    }
    return frontend.compile_text(json.dumps(flow, ensure_ascii=False)), runtime


def test_start_at_landing_calls_the_first_round_land_only(tmp_path):
    doc = yaml.load(V2.read_text(encoding="utf-8"), Loader=frontend._Loader)
    chart, runtime = _entry_mode_chart(doc, rounds=1)
    kw = {"cwd": str(tmp_path), "scope": "landing", "now": 1.0}
    st = runtime.start(chart, caps={}, inputs={"start_at": "landing"}, **kw)
    assert st["result"]["round"]["value"] == {"mode": "land-only"}


def test_a_full_start_calls_the_first_round_full(tmp_path):
    """The default path is unchanged: a run that says nothing about its entry
    still hands ``full`` to the first round."""
    doc = yaml.load(V2.read_text(encoding="utf-8"), Loader=frontend._Loader)
    chart, runtime = _entry_mode_chart(doc, rounds=1)
    kw = {"cwd": str(tmp_path), "scope": "full", "now": 1.0}
    st = runtime.start(chart, caps={}, inputs={"start_at": "full"}, **kw)
    assert st["result"]["round"]["value"] == {"mode": "full"}


def test_the_second_round_takes_the_recheck_kind_not_the_seed(tmp_path):
    """The seed runs once, in front of the loop. Round two takes whatever
    queue-recheck wrote, which in the canon is ``full``."""
    doc = yaml.load(V2.read_text(encoding="utf-8"), Loader=frontend._Loader)
    chart, runtime = _entry_mode_chart(doc, rounds=2)
    kw = {"cwd": str(tmp_path), "scope": "landing-two", "now": 1.0}
    st = runtime.start(chart, caps={}, inputs={"start_at": "landing"}, **kw)
    assert st["result"]["round"]["value"] == {"mode": "second"}



# --------------------------------------------------------------------------- #
# the round's plan: opened at intake, written after the consult (claunch-r3j1o)
# --------------------------------------------------------------------------- #
def test_intake_opens_the_rounds_plan():
    """Entering intake rolls the plan to this round (trigger plan-round)."""
    doc = yaml.safe_load(V2.read_text(encoding="utf-8"))
    assert {"do": "plan-round", "at": "enter"} in doc["nodes"]["intake"]["triggers"]


def test_the_plan_is_first_written_in_the_item_after_the_consult():
    """Both nodes that write the round's first plan -- coding-plan on the gate
    path, plan-seed without a gate -- sit in the item's plan step, so they run
    after consult-check and can take its reason's branch decision
    (claunch-tcczr). Neither stands before the consult."""
    chart = _chart()
    assert [n.id for n in chart.nodes.values() if n.label == "plan-seed"] == ["round/item/plan-seed"]
    assert [n.id for n in chart.nodes.values() if n.label == "coding-plan"] == ["round/item/coding-plan"]
    doc = yaml.safe_load(V2.read_text(encoding="utf-8"))
    body = doc["procedures"]["worker-round"]["body"]
    head = [s.get("id") if isinstance(s, dict) else s for s in body[0]["cond"][1]["else"]]
    assert "plan-open" not in head and "plan-seed" not in head


def test_every_plan_writer_is_told_to_open_this_round_first():
    """The trigger is asynchronous; the rule delivered to each node that
    writes the plan says to call plan_new_round when the plan is another
    round's, and plan-seed writes only when this round's plan has no steps."""
    doc = yaml.safe_load(V2.read_text(encoding="utf-8"))
    rule = doc["rules"]["plan-panel"]
    assert {"coding-plan", "plan-seed", "work"} <= set(rule["deliver"])
    text = _words(rule["text"])
    assert "plan_new_round" in text and "round_now" in text
    assert "note와 보류(deferred)한 verify는 넘어오지 않는다" in text
    seed = _words(doc["nodes"]["plan-seed"]["prompt"])
    assert "plan_new_round" in seed and "steps가 없으면" in seed
