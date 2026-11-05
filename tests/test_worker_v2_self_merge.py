"""merge-by-agent: the worker lands its own branch when nobody can take it.

claunch-yzwut (user decision, thread th-f8d4c3). ``landing-target-gate``
asks a person what to do when the sixth landing check finds no session to
route the request to. Its fourth answer, ``merge-by-agent``, keeps the
round going: alignment, peer review and PR publication run as usual, and
``land``'s ``submit`` then takes ``self-merge-route`` instead of the request
and the wait.

``self-merge`` is "rebase, then merge", made robust against a moving target
and against conflicts:

* each attempt fixes one target tip ``T`` (``self-merge-prepare``: fetch,
  always ``git rebase T``, ``range-diff`` against the reviewed patches, the
  targeted tests on the new tip, the receiver re-check);
* ``self-merge-apply`` publishes only while the target is still ``T``
  (compare-and-swap) and answers ``moved`` otherwise;
* three attempts, written as a three-tier ``try`` -- a ``loop`` would stop at
  its ``max`` for a person (``loop_limit``), and the third ``moved`` has to
  end as ``blocked``.

What these tests drive is the real procedure body and the real
``self-merge-route`` from the packaged file, wrapped in the smallest chart
that has the shape they live in (``land``'s ``submit`` loop). The stand-ins
are the steps around it whose own behaviour other modules pin: peer review,
the landing request, and ``confirm-landed`` (a daemon probe in the real file).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import yaml

import claude_launcher
from claude_launcher.cflow2 import frontend, layers, runtime

V2 = Path(claude_launcher.__file__).parent / "workflows" / "v2" / "improv-worker-v2.yaml"


def _doc() -> Dict[str, Any]:
    # plain mappings: the drive chart is re-dumped with safe_dump. None of the
    # pieces read here carries a bare ``on:`` key (YAML 1.1 reads it as True).
    return yaml.safe_load(V2.read_text(encoding="utf-8"))


def _find(tree: Any, key: str, value: Any) -> Any:
    if isinstance(tree, dict):
        if tree.get(key) == value:
            return tree
        for item in tree.values():
            found = _find(item, key, value)
            if found is not None:
                return found
    elif isinstance(tree, list):
        for item in tree:
            found = _find(item, key, value)
            if found is not None:
                return found
    return None


def _stub(title: str, returns: Dict[str, Any]) -> Dict[str, Any]:
    return {"title": title, "prompt": title, "done_when": title,
            "returns": returns, "effects": []}


def _chart(tmp_path):
    """``self-merge`` and ``self-merge-route`` from the packaged file inside a
    ``land`` whose ``submit`` is peer review -> self-merge-route -> request."""
    doc = _doc()
    procs = doc["procedures"]
    route = _find(procs["land"]["body"], "id", "self-merge-route")
    assert route is not None, "land lost self-merge-route"
    nodes = {k: doc["nodes"][k] for k in
             ("self-merge-prepare", "self-merge-apply", "self-merge-record")}
    nodes["peer-review"] = _stub("peer review", {"ok": {}})
    nodes["integration-request"] = _stub("request", {"ok": {}})
    nodes["landed"] = _stub("landed", {"ok": {"tip": "str", "merge": "str",
                                               "target": "str"}})
    flow = {
        "cflow": 2, "name": "self-merge-drive",
        "data": {"self_merge": {"armed": False, "reason": None},
                 "landing": {"medium": "pr", "remote": "origin", "repo": "h/o/r",
                             "base": "master", "pr_url": None, "pr_number": 7},
                 "claim": {"issue": "x-1", "how": "claimed"}},
        "procedures": {
            "self-merge": procs["self-merge"],
            "self-merge-arm": procs["self-merge-arm"],
            "confirm-landed": {"body": ["landed"]},
            "land": {
                "returns": procs["land"]["returns"],
                "body": [
                    {"id": "arm", "call": "self-merge-arm",
                     "with": {"armed": True, "reason": "person picked merge-by-agent"},
                     "bind": "self_merge"},
                    {"id": "submit", "loop": {"while": "true", "max": 5, "do": [
                        "peer-review", route, "integration-request",
                        {"return": "blocked", "value": {"reason": "requested"}}]}}],
            },
        },
        "nodes": nodes,
        # land's outcome is the run's: a finished run's status carries it as
        # ``outcome``/``value`` (its data and results are no longer shown)
        "body": [{"id": "land-call", "call": "land", "bind": "land_out"}],
    }
    return frontend.compile_text(yaml.safe_dump(flow, allow_unicode=True, sort_keys=False),
                                 resolve_include=layers.resolver(str(tmp_path)))


PREPARED = {"target": "master", "target_tip": "t0", "tip": "b1", "reviewed_tip": "b0",
            "tests": "12 passed"}


def _drive(tmp_path, monkeypatch, script: Dict[str, List[Any]]):
    """Run the chart. ``script`` maps a short label to the (outcome, value)
    answers it gives in turn; a label not in it answers ok with a stub value.
    Returns the labels reached in order and the final status."""
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    kw = {"cwd": str(tmp_path), "scope": "m", "now": 1.0}
    st = runtime.start(_chart(tmp_path), caps={}, **kw)
    path: List[str] = []
    answers = {k: list(v) for k, v in script.items()}
    for _ in range(100):
        if not st.get("tasks"):
            break
        task = st["tasks"][0]
        label = task["label"].rsplit("/", 1)[-1]
        path.append(label)
        if answers.get(label):
            outcome, value = answers[label].pop(0)
        elif label == "self-merge-prepare":
            outcome, value = "ok", PREPARED
        elif label == "self-merge-apply":
            outcome, value = "ok", {"merge": "m1", "tip": "b1", "target": "master"}
        elif label == "landed":
            outcome, value = "ok", {"tip": "b1", "merge": "m1", "target": "master"}
        elif label == "self-merge-record":
            outcome, value = "ok", {"merge": "m1"}
        else:
            outcome, value = "ok", {}
        st = runtime.report(task["instance"], outcome, value, **kw)
    return path, runtime.status(**kw)


def _land(st) -> Dict[str, Any]:
    # a non-ok outcome at the top ends the run "failed" and keeps the outcome
    assert st["status"] in ("done", "failed"), st
    return {"outcome": st["outcome"], "value": st["value"] or {}}


def test_a_clean_attempt_lands_and_records_the_unrun_sweep(tmp_path, monkeypatch):
    path, st = _drive(tmp_path, monkeypatch, {})
    assert path == ["peer-review", "self-merge-prepare", "self-merge-apply",
                    "landed", "self-merge-record"], path
    assert _land(st)["outcome"] == "landed", _land(st)
    assert _land(st)["value"]["merge"] == "m1"
    # the request is never filed on this path
    assert "integration-request" not in path


def test_a_moved_target_starts_the_attempt_over_from_the_fetch(tmp_path, monkeypatch):
    """compare-and-swap refused once: the second attempt fixes a new T and
    rebases again before it publishes."""
    path, st = _drive(tmp_path, monkeypatch, {
        "self-merge-apply": [("moved", {"reason": "master moved t0 -> t1"})]})
    assert path.count("self-merge-prepare") == 2, path
    assert path.count("self-merge-apply") == 2, path
    assert _land(st)["outcome"] == "landed", _land(st)


def test_three_moved_targets_end_blocked_instead_of_waiting_on_a_loop_limit(
        tmp_path, monkeypatch):
    moved = ("moved", {"reason": "moved"})
    path, st = _drive(tmp_path, monkeypatch, {"self-merge-apply": [moved, moved, moved]})
    assert path.count("self-merge-apply") == 3, path
    assert "landed" not in path, path
    assert _land(st)["outcome"] == "blocked", _land(st)
    assert "세 번" in _land(st)["value"]["reason"]
    assert not st.get("loop_limits"), st.get("loop_limits")


def test_a_rebase_that_changed_the_patches_goes_back_to_review(tmp_path, monkeypatch):
    """range-diff found a changed patch: submit runs again from the top, so the
    new tip is reviewed before anything is merged."""
    path, st = _drive(tmp_path, monkeypatch, {
        "self-merge-prepare": [("review", {"reason": "range-diff: 1 patch changed"})]})
    assert path[:3] == ["peer-review", "self-merge-prepare", "peer-review"], path
    assert path.index("self-merge-apply") > 2, path
    assert _land(st)["outcome"] == "landed", _land(st)


def test_a_receiver_that_appeared_hands_the_round_back_to_the_request(
        tmp_path, monkeypatch):
    path, st = _drive(tmp_path, monkeypatch, {
        "self-merge-prepare": [("target", {"reason": "owner s29"})]})
    assert "self-merge-apply" not in path, path
    assert "integration-request" in path, path
    # the round lands through the request (the stand-in ends land blocked)
    assert _land(st)["value"]["reason"] == "requested", _land(st)


def test_an_unresolved_conflict_or_red_targets_is_rework(tmp_path, monkeypatch):
    path, st = _drive(tmp_path, monkeypatch, {
        "self-merge-prepare": [("rework", {"reason": "conflict in a.py", "rework": True})]})
    assert "self-merge-apply" not in path, path
    assert _land(st)["outcome"] == "rework", _land(st)


def test_a_dirty_target_checkout_is_blocked_and_not_retried(tmp_path, monkeypatch):
    path, st = _drive(tmp_path, monkeypatch, {
        "self-merge-apply": [("blocked", {"reason": "root checkout has changes"})]})
    assert path.count("self-merge-apply") == 1, path
    assert _land(st)["outcome"] == "blocked", _land(st)


# --------------------------------------------------------------------------- #
# the packaged definition's own wiring
# --------------------------------------------------------------------------- #
def test_only_an_armed_round_reaches_the_self_merge():
    route = _find(_doc()["procedures"]["land"]["body"], "id", "self-merge-route")
    [armed, other] = route["cond"]
    assert armed["if"] == "$data.self_merge.armed == true", armed
    assert other["else"] == [], other


def test_every_land_starts_unarmed():
    """The mark is per round: the person's decision (landing_route) outlives
    the round, the mark does not -- or a later round with a receiver would
    merge on its own."""
    body = _doc()["procedures"]["land"]["body"]
    first = body[0]
    assert first["id"] == "self-merge-reset", first
    assert first["call"] == "self-merge-arm" and first["with"] == {"armed": False}, first
    assert first["bind"] == "self_merge", first


def test_the_gate_answer_arms_the_round():
    """The gate returns ok only for merge-by-agent (every other answer ends
    land), so the node after the call is that answer's."""
    doc = _doc()
    tree = _find(doc["procedures"]["land"]["body"], "id", "landing-target")
    miss = _find(tree, "id", "landing-target-reroute")["cond"][-1]["else"]
    ids = [n.get("id") for n in miss]
    assert ids == ["landing-target-ask", "landing-target-arm"], ids
    assert miss[1]["with"]["armed"] is True and miss[1]["bind"] == "self_merge"
    gate = doc["procedures"]["landing-target-gate"]
    ok_returns = [c for c in _find(gate["body"], "call", "ask-user")["cases"].values()
                  if c.get("return") == "ok"]
    assert [c["value"]["decision"] for c in ok_returns] == ["merge-by-agent"]


def test_the_merge_step_is_compare_and_swap():
    prompt = " ".join(_doc()["nodes"]["self-merge-apply"]["prompt"].split())
    assert "--match-head-commit" in prompt
    assert "merge --ff-only M" in prompt
    assert "git update-ref refs/heads/<기준> M T" in prompt
    assert "moved" in prompt
    prep = " ".join(_doc()["nodes"]["self-merge-prepare"]["prompt"].split())
    assert "항상 `git rebase T`" in prep
    assert "git range-diff" in prep
    assert "claunch gate landing-target --json" in prep
