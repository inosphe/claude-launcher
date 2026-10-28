"""The landing gate's sixth check: is there a session to route the request to?

``improv-worker-v2``'s ``landing`` asked five machine questions, all about
cleanliness, and then filed a request whose routing was attempted only as
the daemon pushed it. Nothing asked whether anybody would take it. On the
measured failure (claunch-od1mx, run ``20261027-192120-21d28c``) the answer
was nobody: the parent held no ``merger`` role, the mesh had no leader, the
board had already moved to ``in_review``, and the worker spent 25 nudges --
about four hours -- reporting ``outcome=ok`` for a request that was never
sent.

What this module pins is the shape of the fix, in the workflow itself:

* a sixth check judged by a command, next to the five;
* its ``1`` goes to ``no-target`` -- a person -- and **not** to
  ``escalate``, whose question goes to the session above, which in this
  arrangement is the very session that cannot merge;
* its ``2`` goes to ``escalate``: "could not tell" is not "nobody";
* the person's three answers map onto ``land``'s existing outcomes, so the
  procedure's returns do not grow (``tests/test_cflow2_improv_worker.py``
  pins ``{"landed", "held", "rework", "blocked"}``);
* the board is not touched on the way -- no ``in_review`` move, no
  ``LANDING REQUEST`` comment;
* the marker this leaves on the board is not one of the three the landing
  gate's fourth check greps for, or the next round would read its own
  "nobody could take it" note as the parent's stop order.

The packaged file is read directly where the compiled chart cannot answer:
procedure params and returns live on the frontend's internal record
(``_Proc``) and never become nodes, the fact ``test_cflow2_improv_worker``
notes for procedure descriptions (claunch-qt475).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import yaml

import claude_launcher
from claude_launcher.cflow2 import check, layers

V2 = Path(claude_launcher.__file__).parent / "workflows" / "v2" / "improv-worker-v2.yaml"

#: The findings the packaged definition has always produced, with their
#: reasons in ``tests/test_cflow2_improv_worker.py``. Imported rather than
#: restated: a check that starts firing here because this file gained a node
#: must fail one place, and widening the accepted set must widen both.
try:
    from test_cflow2_improv_worker import ACCEPTED_WARNINGS
except ImportError:  # pragma: no cover - pytest puts tests/ on sys.path
    ACCEPTED_WARNINGS = {("par-write-overlap", "round/item/work-group"),
                         ("par-write-overlap", "round/item/slices"),
                         ("unbounded-visits", None)}


def _doc() -> Dict[str, Any]:
    return yaml.safe_load(V2.read_text(encoding="utf-8"))


def _chart():
    return layers.load("improv-worker-v2")


def _words(text: str) -> str:
    return " ".join(text.split())


def _landing():
    return _chart().nodes["round/land/landing"]


def _options() -> Dict[str, dict]:
    return {o["name"]: o for o in _landing().get("options") or []}


def _gate_subtree() -> List[Any]:
    """The compiled nodes the ``no-target`` option leads to.

    A procedure body is compiled under ``<id>/...`` while its call site's own
    children are ``<id>.<n>``, so both separators are prefixes here -- the
    alternative is naming each path, which goes stale the moment the body
    gains a node.
    """
    chart = _chart()
    child = _options()["no-target"]["child"]
    prefixes = (child + ".", child + "/")
    return [chart.nodes[n] for n in sorted(chart.nodes)
            if n == child or n.startswith(prefixes)]


def _find(tree: Any, key: str, value: Any) -> Any:
    """The mapping under ``tree`` whose ``key`` is ``value`` (depth first)."""
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


# --------------------------------------------------------------------------- #
# the sixth check
# --------------------------------------------------------------------------- #
def test_the_sixth_check_names_the_command_that_answers_it():
    """Five checks that each name a command, and a sixth that is an adjective,
    would be decided by whoever is in a hurry -- the exact reason the first
    five were written as commands (test_landing_fast_path.py)."""
    prompt = _words(_landing().get("prompt") or "")
    assert "claunch gate landing-target" in prompt
    assert "0 대상 있음" in prompt
    assert "1 대상 없음" in prompt
    assert "2 판정 불가" in prompt
    assert "1이면 request가 아니라 no-target" in prompt
    assert "2면 escalate" in prompt


def test_the_sixth_check_does_not_replace_the_other_five():
    """A round whose tree is dirty must still escalate, whatever the routing
    says. The sixth is an addition, not a gate of its own."""
    prompt = _words(_landing().get("prompt") or "")
    assert "여섯째는 다섯을 대신하지 않고 그 뒤에 붙는 확인" in prompt
    for n in ("1. 작업 트리가 깨끗하다", "2. 그 표적 수치가", "3. 통합 대상 위로",
              "4. 내 이슈에 열린 제동 마커가 없다", "5. 상위가 걸어 둔"):
        assert n in prompt, f"the original check {n!r} is gone"


def test_the_sixth_check_says_why_one_is_not_escalate():
    """The distinction a future editor is most likely to undo.

    ``escalate`` looks like the safe branch -- ask somebody. But the somebody
    is the session above, and in the arrangement that fails, that session is
    the one that cannot merge: the question walks the candidate groups empty
    and lands on a person anyway, after the round has waited. The measured
    cost of that walk is why this branch exists."""
    prompt = _words(_landing().get("prompt") or "")
    assert "escalate는 landing-review로 가서" in prompt
    assert "상위는 바로 그 병합할 수 없는 세션" in prompt
    assert "25회·약 4시간" in prompt


# --------------------------------------------------------------------------- #
# the option and where it goes
# --------------------------------------------------------------------------- #
def test_no_target_is_its_own_option():
    opts = _options()
    assert set(opts) == {"request", "no-target", "escalate"}, sorted(opts)
    assert opts["no-target"]["guard"] == "'no-target' in $params.allow"


def test_both_allow_defaults_carry_no_target():
    """``land`` is called by ``worker-round``, which passes its own ``allow``.
    Extending only the inner one would leave the outer default -- the value
    every packaged run actually carries -- without the branch, and an option
    whose guard is false on every run is not a branch."""
    doc = _doc()
    for name in ("land", "worker-round"):
        value = doc["procedures"][name]["params"]["allow"]["default"]
        assert value == ["request", "escalate", "no-target"], (name, value)


def test_no_target_asks_the_person_and_does_not_route_upward():
    """Its child is the gate procedure, not ``landing-review``: the whole
    point is to skip the question that goes to the session above."""
    chart = _chart()
    child = _options()["no-target"]["child"]
    assert child, "no-target has no tree -- it must lead to the person's gate"
    # the option's subtree is a call of the gate procedure...
    assert chart.nodes[child].type == "scope"
    assert chart.nodes[child].get("proc") == "landing-target-gate"
    # ...which asks a person (ask-user compiles to these)
    labels = {n.label for n in _gate_subtree() if n.label}
    assert "ask-pose" in labels
    assert "landing-review" not in labels
    assert "landing-review-leader" not in labels
    # ...and the sibling that does route upward is still there, unchanged
    assert chart.nodes[_options()["escalate"]["child"]] is not None


# --------------------------------------------------------------------------- #
# the person's gate
# --------------------------------------------------------------------------- #
def test_the_person_gets_three_named_choices():
    """Named here rather than left to the prompt: the ``cases`` keys are the
    answers, so these words are the interface a person answers through."""
    choices = {key for n in _gate_subtree() for key in (n.get("cases") or [])}
    assert choices == {"rebase-and-repost", "merge-by-person", "freeze-and-close"}


def test_the_three_choices_map_onto_lands_declared_outcomes():
    """``land`` declares four results and the packaged tests pin that set
    (``tests/test_cflow2_improv_worker.py``). The gate reuses three of them
    rather than growing the procedure's interface.

    ``ask-user`` returns ``ok`` and ``reframe`` of its own -- its body is
    inside this subtree -- so the set to check is the returns the gate
    *itself* raises with an outcome ``land`` names, plus a check that every
    one of ``land``'s discarded-by-this-gate outcomes is reachable here."""
    declared = set(_doc()["procedures"]["land"]["returns"])
    assert declared == {"landed", "held", "rework", "blocked"}
    raised = {n.get("outcome") for n in _gate_subtree()
              if n.type == "control" and n.get("op") == "return"} - {"ok", "reframe"}
    assert raised <= declared, f"a return names {raised - declared}, which land does not declare"
    assert raised == {"rework", "blocked", "held"}, raised
    # the declaration's shape is what makes these land's answers, not the
    # ask procedure's: each carries the fields land declares for it
    fields = _doc()["procedures"]["land"]["returns"]
    assert set(fields["blocked"]) == {"reason"}
    assert set(fields["held"]) == {"reason", "rework"}
    assert set(fields["rework"]) == {"reason", "rework"}


def test_blocked_carries_only_a_reason():
    """``land``'s declaration is ``blocked: {reason: str}``. A ``rework`` field
    smuggled in here would be dropped by the engine (fill_optional keeps the
    declared shape) -- and the two consumers of ``land_out.rework`` would read
    null, which is falsy, so the behaviour would be right for the wrong
    reason. Pinned so it stays a conscious choice rather than an accident."""
    body = _doc()["procedures"]["landing-target-gate"]["body"]
    blocked = _find(body, "return", "blocked")
    assert blocked is not None, "no return of blocked in the gate"
    assert set(blocked["value"]) == {"reason"}, blocked["value"]
    # ...and it carries a sentence, which is what the person reads next round
    assert blocked["value"]["reason"].strip()


def test_the_two_rework_consumers_read_a_blocked_result_as_no_rework():
    """``land_out.rework`` is consulted in two places (``round/item``'s
    delegated rework route, and ``items-pass``'s "work again" test). Both
    read a missing field as false, which is what a ``blocked`` result should
    do: it neither re-does the work nor reopens the delegated-review gate.
    Pinned because a later change that starts writing ``rework: true`` into
    ``blocked`` would quietly turn a merge-by-person answer into a rework
    loop."""
    text = V2.read_text(encoding="utf-8")
    consumers = [line for line in text.splitlines()
                 if "land_out.rework == true" in line or "$data.land_out.rework" in line]
    # the two the parent named, still spelled the same way
    assert len(consumers) >= 2, consumers
    assert any("$data.land_out.rework == true" in line for line in consumers)
    blocked = _find(_doc()["procedures"]["landing-target-gate"]["body"], "return", "blocked")
    assert "rework" not in blocked["value"], (
        "blocked now writes rework; the two land_out.rework consumers would "
        "read it as a rework round")


def _the_ask() -> dict:
    """The ``ask-user`` call in the gate's body, wherever it sits.

    It is wrapped in a ``try`` whose ``catch`` handles ``reframe`` (an answer
    that the three choices do not fit the problem), so the call is not a
    direct child of the body any more. Located by what it is rather than by
    its depth, so restructuring the wrapper does not silently make these
    tests read nothing.
    """
    ask = _find(_doc()["procedures"]["landing-target-gate"]["body"], "call", "ask-user")
    assert ask is not None, "the gate no longer asks anybody"
    return ask


def test_the_question_carries_the_routing_reason_verbatim():
    """The evidence field is the Router's own sentence, not the agent's
    summary of it: ``landing-target-evidence`` reports the gate's ``why``
    and the person's question takes it from there."""
    doc = _doc()
    assert _the_ask()["with"]["context"] == \
        "$result['landing-target-evidence'].value.why"
    evidence = _words(doc["nodes"]["landing-target-evidence"]["prompt"])
    assert "claunch gate landing-target --json" in evidence
    assert "그대로" in evidence


def test_the_gate_asks_nobody_but_a_person():
    """The three answers all belong to a person. No ``from:`` list and no
    ``otherwise`` other than the human default: a worker that could answer
    this itself would be choosing its own landing path, which is the rule
    this workflow keeps everywhere else."""
    ask = _the_ask()
    assert "from" not in ask["with"], "the gate may not delegate this to a session"
    assert "otherwise" not in ask["with"]


def test_a_reframed_question_ends_as_blocked_not_as_a_failed_round():
    """``ask-user`` can come back ``reframe`` -- the person says the choices
    do not fit the problem, three times running.

    Left undeclared, that outcome makes ``land`` *fail*, and ``land``'s
    caller catches only rework/landed/held/blocked: the failure escapes the
    round loop and skips ``queue-recheck`` -- the same accident ``blocked``
    was introduced for (claunch-improver-integration-request-blocked-o6paj,
    journal 02:33:08 on run 20261027-164135-3ad031). So this gate catches it
    and ends the landing as ``blocked``: the person is deciding the path,
    which is what the answer says.
    """
    proc = _doc()["procedures"]["landing-target-gate"]
    # the outcome is declared, so the engine has a value shape for it...
    assert set(proc["returns"]) == {"rework", "blocked", "held"}
    # ...and the try's catch handles it
    choice = _find(proc["body"], "id", "landing-target-choice")
    assert choice is not None and "try" in choice, choice
    caught = choice.get("catch") or {}
    assert "reframe" in caught, f"reframe escapes: {sorted(caught)}"
    assert caught["reframe"]["return"] == "blocked"
    # a blocked round carries a reason and no rework flag (land's shape)
    assert set(caught["reframe"]["value"]) == {"reason"}


# --------------------------------------------------------------------------- #
# the board is not touched
# --------------------------------------------------------------------------- #
def test_the_round_files_no_request_on_this_path():
    """The defective sequence moved the issue to ``in_review`` and left a
    ``LANDING REQUEST`` comment, and the failure came after. This branch
    leaves before ``integration-request`` runs at all."""
    labels = {n.label for n in _gate_subtree() if n.label}
    assert "integration-request" not in labels
    prompts = _words(" ".join(n.get("prompt") or "" for n in _gate_subtree()))
    assert "in_review로 전이하지 않" in prompts
    assert "LANDING REQUEST" in prompts


def test_the_board_marker_is_not_a_brake_marker():
    """The landing gate's fourth check greps the issue's comments for
    ``CHANGES REQUESTED`` / ``HOLD`` / ``BLOCKED``. A marker containing any of
    them would make the next round read this round's own note as the parent's
    stop order."""
    evidence = _doc()["nodes"]["landing-target-evidence"]
    marker = next(line for line in evidence["prompt"].splitlines()
                  if "NO LANDING TARGET:" in line and line.strip().startswith("NO LANDING TARGET:"))
    for brake in ("CHANGES REQUESTED", "HOLD", "BLOCKED"):
        assert brake not in marker, (brake, marker)
    # ...and the task tells its reader why, so the next editor does not
    # "improve" the marker into one of them
    assert "BLOCKED·HOLD·CHANGES REQUESTED를 넣지 않는다" in _words(evidence["prompt"])


# --------------------------------------------------------------------------- #
# the workflow still compiles with nothing new wrong
# --------------------------------------------------------------------------- #
def test_the_chart_still_compiles_to_the_same_findings():
    """A new procedure and a new option must not introduce a check finding."""
    findings = {(f.code, f.node) for f in check.check(_chart())}
    assert findings == ACCEPTED_WARNINGS, findings - ACCEPTED_WARNINGS
