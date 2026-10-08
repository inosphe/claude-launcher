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
    "end-gate": ["end-decision", "end-gate"],
    "end-hold": ["end-hold"],
}

PROCEDURES = {"resolve-issue", "workspace", "item", "land", "settle",
              "worker-round", "queue-recheck", "confirm-landed", "found-issue"}

# prior-context, targeted-test and merge-ready are the driver's (claunch-onbdf):
# a read-only headless delegate has no shell or MCP on the claude harness.
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
#: leader HOLD". Everything else stays the v1 block word for word.
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


def _words(text: str) -> str:
    return " ".join(text.split())


def _as_v2(v1_block: str) -> str:
    """The v1 block with the one claunch-0b8dx divergence applied, in words."""
    words = _words(v1_block)
    assert words.count(_words(V1_ONLY)) == 1
    return words.replace(_words(V1_ONLY), _words(V2_INSTEAD))


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
    "end-decision": ["end", "hold"],
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


def _medium_flow(tmp_path):
    """landing-medium and pr-publish as they are in the definition, with their
    skeleton and the two attempt tasks, in a small workflow."""
    doc = yaml.safe_load(V2.read_text(encoding="utf-8"))
    procs = {k: doc["procedures"][k] for k in ("pr-or-local", "landing-medium", "pr-publish")}
    nodes = {k: {kk: vv for kk, vv in doc["nodes"][k].items() if kk in ("title", "prompt", "returns")}
             for k in ("remote-setup", "pr-open")}
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
        iid = next(t["instance"] for t in st["tasks"] if t["label"] == label)
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
    published = runtime.RunStore(cwd, "m").load_state()["data"]["published"]
    assert published["medium"] == "local" and published["reason"] == "push refused: stale info"
    assert published["pr_url"] is None
    assert [t["label"] for t in st["tasks"]] == ["after"]


def test_pr_or_local_user_local_answer(tmp_path, monkeypatch):
    from claude_launcher.cflow2 import runtime
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    kw = {"cwd": str(tmp_path), "scope": "m", "now": 1.0}
    st = runtime.start(_medium_flow(tmp_path), caps={}, **kw)
    iid = next(t["instance"] for t in st["tasks"] if t["label"] == "remote-setup")
    st = runtime.report(iid, "blocked", {"reason": "no remote"}, **kw)
    iid = next(t["instance"] for t in st["tasks"] if t["label"] == "ask-frame")
    assert "no remote" in st["tasks"][0]["prompt"]
    runtime.report(iid, "ok", {"brief": BRIEF}, **kw)
    runtime.approve(None, decision="local", reason="this round only", **kw)
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
                         ["remote-setup", "ask-frame", "remote-setup", "pr-open", "after"],
                         {"landing": _LANDED,
                          "published": {"medium": "local", "reason": "F1", **_NONE}}),
    "landing-local": (False, [("remote-setup", "blocked", {"reason": "R1"}),
                              ("answer", "local", None)],
                      ["remote-setup", "ask-frame", "pr-open"],
                      {"landing": {"medium": "local",
                                   "reason": "user chose local while PR was blocked", **_NONE}}),
    "force-retry-local": (True, [("remote-setup", "ok", _SETUP),
                                 ("pr-open", "failed", {"reason": "F1"}), ("answer", "retry", None),
                                 ("pr-open", "failed", {"reason": "F2"}), ("answer", "local", None)],
                          ["remote-setup", "pr-open", "ask-frame", "pr-open", "ask-frame", "after"],
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
                   ["remote-setup", "ask-frame", "pr-open"],
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
        seen.append(st["tasks"][0]["label"])
        if label == "answer":
            frame = next(t for t in st["tasks"] if t["label"] == "ask-frame")
            runtime.report(frame["instance"], "ok", {"brief": _brief2(*(value or ()))}, **kw)
            runtime.approve(None, decision=outcome, reason="why", **kw)
            st = runtime.status(**kw)
        else:
            iid = next(t["instance"] for t in st["tasks"] if t["label"] == label)
            st = runtime.report(iid, outcome, value, **kw)
    seen.extend(t["label"] for t in st.get("tasks") or [])
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
    iid = next(t["instance"] for t in st["tasks"] if t["label"] == "intake")
    st = runtime.report(iid, "ok", INTAKE, **kw)
    iid = next(t["instance"] for t in st["tasks"] if t["label"] == "prior-context")
    st = runtime.report(iid, "fail", {"reason": "MCP search refused"}, **kw)
    task = next(t for t in st["tasks"] if t["label"] == "prior-context-fallback")
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
    iid = next(t["instance"] for t in st["tasks"] if t["label"] == "intake")
    st = runtime.report(iid, "ok", INTAKE, **kw)
    iid = next(t["instance"] for t in st["tasks"] if t["label"] == "prior-context")
    found = {"query": "q", "summary": "선행 맥락 없음(쿼리: q)", "related": [], "duplicate": "claunch-x"}
    st = runtime.report(iid, "ok", found, **kw)
    assert [t["label"] for t in st["tasks"]] == ["intake-issue"]
    assert "선행 맥락 없음(쿼리: q)" in st["tasks"][0]["prompt"]
    assert "claunch-x" in st["tasks"][0]["prompt"]


def test_the_end_question_can_be_posed(tmp_path, monkeypatch):
    """claunch-gpccp: with choices {approve, hold} no brief passed -- the
    question validator refuses an option named approve and the ask refuses
    any other name -- so every finished v2 run failed at end-decision. With
    end/hold the brief is accepted and the question reaches the person."""
    from claude_launcher.cflow2 import runtime
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    doc = yaml.safe_load(V2.read_text(encoding="utf-8"))
    decision = _find_id(doc["procedures"], "end-decision")
    flow = {"cflow": 2, "name": "end-flow", "include": ["ask-common"],
            "nodes": {"end-gate": {"prompt": "end"}, "end-hold": {"prompt": "hold"}},
            "body": [decision]}
    from claude_launcher.cflow2 import frontend
    chart = frontend.compile_text(yaml.safe_dump(flow, allow_unicode=True, sort_keys=False),
                                  resolve_include=layers.resolver(str(tmp_path)))
    kw = {"cwd": str(tmp_path), "scope": "m", "now": 1.0}
    st = runtime.start(chart, caps={}, **kw)
    brief = dict(BRIEF, recommendation="end", options=[
        dict(BRIEF["options"][0], name="end"), dict(BRIEF["options"][1], name="hold")])
    iid = next(t["instance"] for t in st["tasks"] if t["label"] == "ask-frame")
    st = runtime.report(iid, "ok", {"brief": brief}, **kw)
    assert st["status"] == "waiting_approval" and [a["node"] for a in st["asks"]]
    runtime.approve(None, decision="end", reason="done", **kw)
    assert [t["label"] for t in runtime.status(**kw)["tasks"]] == ["end-gate"]
