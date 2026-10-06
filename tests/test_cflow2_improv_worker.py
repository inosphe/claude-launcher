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
    "rework-decision": ["approve", "hold"],
    "settle-stall": ["proceed", "keep-waiting"],
    "end-decision": ["approve", "hold"],
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
        # (the PR landing fields, claunch-yq80p.5)
        got = sorted(("enqueue-landing" if t["do"] == "push" and (t.get("push") or {}).get("to")
                      == "parent:landing" else t["do"], t["at"]) for t in trig)
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
    (push,) = [t["push"] for t in req.get("triggers") if isinstance(t, dict) and t.get("push")]
    assert push["to"] == "parent:landing"
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
    chart = frontend.compile_text(yaml.safe_dump(flow, allow_unicode=True),
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
