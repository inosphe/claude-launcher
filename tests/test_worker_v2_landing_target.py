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
* the ``1`` is not acted on at once (claunch-sp0ut.1): the branch re-measures
  it three times, a minute apart, before it asks a person, and a target that
  appears in between takes the normal landing path instead;
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
    """The compiled nodes the gate procedure expands to.

    The ``no-target`` option's tree is a ``seq`` now (claunch-1aj1a.1 put the
    reuse guard in front of the call), so the gate is located by what it is
    -- the scope whose ``proc`` is ``landing-target-gate`` -- rather than by
    being the option's direct child. A procedure body compiles under
    ``<id>/...`` while a call site's own children are ``<id>.<n>``; both
    separators are prefixes here, the alternative being to name each path,
    which goes stale the moment the body gains a node.
    """
    chart = _chart()
    scope = next(n.id for n in chart.nodes.values()
                 if n.get("proc") == "landing-target-gate")
    prefixes = (scope + ".", scope + "/")
    return [chart.nodes[n] for n in sorted(chart.nodes)
            if n == scope or n.startswith(prefixes)]


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
    # the option's subtree reaches the gate procedure...
    procs = {n.get("proc") for n in _gate_subtree()}
    assert "landing-target-gate" in procs, procs
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
def test_the_person_gets_four_named_choices():
    """Named here rather than left to the prompt: the ``cases`` keys are the
    answers, so these words are the interface a person answers through.
    claunch-yzwut added the fourth, ``merge-by-agent``."""
    choices = {key for n in _gate_subtree() for key in (n.get("cases") or [])}
    assert choices == {"rebase-and-repost", "merge-by-person", "freeze-and-close",
                       "merge-by-agent"}


def test_the_three_choices_map_onto_lands_declared_outcomes():
    """``land`` declares five results and the packaged tests pin that set
    (``tests/test_cflow2_improv_worker.py``). The gate reuses three of them
    rather than growing the procedure's interface.

    ``ask-user`` returns ``ok`` and ``reframe`` of its own -- its body is
    inside this subtree -- so the set to check is the returns the gate
    *itself* raises with an outcome ``land`` names, plus a check that every
    one of ``land``'s discarded-by-this-gate outcomes is reachable here."""
    declared = set(_doc()["procedures"]["land"]["returns"])
    # pr-submitted is the fifth (claunch-ijkge): auto_merge=false ends the
    # round at PR submission, so that landing has to name its own outcome --
    # the gate itself never returns it.
    assert declared == {"landed", "held", "rework", "blocked", "pr-submitted"}
    raised = {n.get("outcome") for n in _gate_subtree()
              if n.type == "control" and n.get("op") == "return"} - {"ok", "reframe"}
    assert raised <= declared, f"a return names {raised - declared}, which land does not declare"
    # claunch-1aj1a.1: rebase-and-repost stopped returning rework. A rework
    # re-runs the batch without changing the probe's input (the run's cwd
    # repository), so the same exit 1 stands and the question reopens --
    # measured on s6086 across two rounds (claunch-i8cj4 30083 -> 30727 ->
    # 30737). Every answer the gate gives now ends the round.
    assert raised == {"blocked", "held"}, raised
    # claunch-yzwut: merge-by-agent is the one answer that does not end the
    # round -- the gate returns ok and land carries on to its own landing
    # steps, so land's returns do not grow.
    assert _the_ask()["cases"]["merge-by-agent"]["return"] == "ok"
    # the declaration's shape is what makes these land's answers, not the
    # ask procedure's: each carries the fields land declares for it. The
    # optionals are the recorded decision (claunch-1aj1a.1) -- decision, its
    # place (repo·relation), the receiver the evidence measured (owner) and
    # the issue are what the next round reads to decide whether to ask again.
    fields = _doc()["procedures"]["land"]["returns"]
    route = {"decision", "tip", "repo", "owner", "issue", "relation"}
    assert set(fields["blocked"]) == {"reason"} | route
    assert set(fields["held"]) == {"reason", "rework"} | route
    assert set(fields["rework"]) == {"reason", "rework"}
    assert set(fields["pr-submitted"]) == {"url", "number", "tip"}


def test_blocked_carries_a_reason_and_no_rework_flag():
    """``land``'s declaration is ``blocked: {reason: str, ...}``. A ``rework``
    field smuggled in here would be dropped by the engine (fill_optional keeps
    the declared shape) -- and the two consumers of ``land_out.rework`` would
    read null, which is falsy, so the behaviour would be right for the wrong
    reason. Pinned so it stays a conscious choice rather than an accident."""
    body = _doc()["procedures"]["landing-target-gate"]["body"]
    blocked = _find(body, "return", "blocked")
    assert blocked is not None, "no return of blocked in the gate"
    assert set(blocked["value"]) == {"reason", "decision", "tip", "repo",
                                     "owner", "issue", "relation"}, blocked["value"]
    assert "rework" not in blocked["value"], (
        "the two land_out.rework consumers would read a blocked answer as a "
        "rework round")
    # ...and it carries a sentence, which is what the person reads next round
    assert blocked["value"]["reason"].strip()


def test_every_answer_records_its_decision_its_place_and_the_tip_it_was_answered_at():
    """claunch-1aj1a.1: the answer is only reusable if the next round can tell
    *which* answer it was, *where* it was about, and *what state* it was
    answered on.

    The record carries the decision name, the place the question was about
    (``repo`` -- which repository the branch lands in -- and ``relation`` --
    which way the request goes), the receiver the evidence measured and the
    issue, plus the tip as it stood when the person answered. The next round
    reuses the decision when the place is unchanged; the tip is kept as
    evidence and is *not* a reuse condition (ANSWER 30759)."""
    cases = _the_ask().get("cases") or {}
    assert cases, "the gate has no cases"
    for name, case in cases.items():
        value = case["value"]
        assert value["decision"] == name, (name, value)
        assert value["tip"] == "$data.commit.tip", (name, value)
        assert value["repo"] == "$data.landing.repo", (name, value)
        assert value["relation"] == "$params.relation", (name, value)
        assert value["owner"] == "$result['landing-target-evidence'].value.owner", (
            name, value)
        assert value["issue"] == "$data.claim.issue", (name, value)


def test_rebase_and_repost_ends_the_round_instead_of_reworking():
    """The choice this issue is named for. ``rework`` cannot change the
    probe's input, so it can only reproduce the same verdict -- and its
    repetition is not counted by ``landing-declined``'s rework budget
    (``rework-budget`` belongs to ``landing``'s ``declined`` branch). It ends
    the round as blocked
    instead, and the next round re-measures with whatever the person
    changed outside the workflow."""
    case = (_the_ask().get("cases") or {})["rebase-and-repost"]
    assert case["return"] == "blocked", case
    assert "rework" not in case["value"], case["value"]
    assert "다음 회차가 새 명부로 다시 잰다" in case["value"]["reason"]


def _land_no_target() -> dict:
    """The ``then:`` tree of ``land``'s ``no-target`` option, from the yaml."""
    land = _doc()["procedures"]["land"]
    tree = _find(land["body"], "id", "landing-target")
    assert tree is not None, "land's no-target branch moved"
    return tree


def test_the_same_question_is_not_asked_twice_for_the_same_place():
    """Branch 1 of the three the approved answer names: the same question is
    not opened twice.

    Reaching this branch already means this round's sixth check just said
    「no target」. If a decision is recorded and the place it was made about
    (``repo`` and ``relation``) is unchanged, asking again repeats a question
    the person already answered -- so the tree replays the recorded answer
    first and calls the gate only when the replay answers ``ask-again``.

    The replay returns the choice *name*, and the branch on that name is a
    ``cases`` map: the repo forbids a hand-written decision comparison
    (test_cflow2_improv_worker.py::
    test_no_ask_user_is_followed_by_a_handwritten_decision_cond), because a
    misspelt choice in a cond is never compiled or reported.
    """
    tree = _land_no_target()
    replay = _find(tree, "id", "landing-target-replay")
    assert replay is not None, "no replay before the gate"
    assert replay["call"] == "landing-route-replay", replay
    # the key is (stored route, this round's place) -- never the tip
    assert replay["with"] == {"previous": "$data.landing_route",
                              "repo": "$data.landing.repo",
                              "relation": "$data.inputs.relation"}, replay["with"]
    cases = replay["cases"]
    # every answer the gate can give has a case, and the unanswered key asks
    assert set(cases) == {"merge-by-person", "rebase-and-repost",
                          "freeze-and-close", "merge-by-agent", "ask-again"}, cases
    # claunch-yzwut: a replayed merge-by-agent does not end the round; it marks
    # the round as a self-landing and the seq goes on (re-measure, then on)
    arm = cases["merge-by-agent"]
    assert arm["call"] == "self-merge-arm" and arm["bind"] == "self_merge", arm
    assert arm["with"]["armed"] is True, arm
    # the replayed answers go to the same outcomes the gate returns for them
    assert cases["merge-by-person"]["return"] == "blocked"
    assert cases["rebase-and-repost"]["return"] == "blocked"
    assert cases["freeze-and-close"]["return"] == "held"
    assert "rework" not in cases["merge-by-person"]["value"]
    assert "rework" not in cases["rebase-and-repost"]["value"]
    assert cases["freeze-and-close"]["value"]["rework"] is False
    # the replayed answer keeps the decision and the place it was made about
    for key in ("merge-by-person", "rebase-and-repost", "freeze-and-close"):
        value = cases[key]["value"]
        assert value["decision"] == key, value
        assert value["repo"] == "$data.landing_route.repo", value
        assert value["relation"] == "$data.landing_route.relation", value
        assert value["owner"] == "$data.landing_route.owner", value
        assert value["issue"] == "$data.landing_route.issue", value
    # ask-again does nothing: the gate call after it poses the question
    assert cases["ask-again"] == []


def test_a_moved_tip_keeps_the_decision_and_re_runs_the_action():
    """Branch 2: a moved tip keeps the decision and re-runs only the action
    (ANSWER 30759 -- 「tip이 바뀌면 결정은 유지하고 행동만 다시 한다」).

    The tip is deliberately absent from the replay's inputs and guard; the
    condition is the place, not the state. What moves with the tip is the
    *value* the replayed answer carries: ``tip`` is this round's
    ``$data.commit.tip``, not the tip stored in the record. That is the point
    of reusing the decision -- a round that committed more work lands that
    new tip under the person's original answer.
    """
    replay = _find(_land_no_target(), "id", "landing-target-replay")
    assert set(replay["with"]) == {"previous", "repo", "relation"}, replay["with"]
    assert "tip" not in replay["with"], replay["with"]
    for key in ("merge-by-person", "rebase-and-repost", "freeze-and-close"):
        value = replay["cases"][key]["value"]
        assert value["tip"] == "$data.commit.tip", (key, value)
        assert value["tip"] != "$data.landing_route.tip", (key, value)


def test_the_replay_cases_name_the_same_choices_the_gate_offers():
    """The replay's case keys and the gate's ask options are two lists of the
    same three names, and a mismatch fails at runtime rather than at compile
    time: a name the replay emits with no case makes the ``cases`` cond fail
    ("no cond branch matched"), which would leave ``land`` as a failure and
    skip ``queue-recheck`` -- the accident ``blocked`` exists to prevent."""
    offered = set(_the_ask()["cases"])
    replayed = set(_find(_land_no_target(), "id", "landing-target-replay")["cases"])
    assert replayed - {"ask-again"} == offered, (replayed, offered)


def test_the_replay_guard_compares_the_place_and_asks_again_when_it_moved():
    """Branch 3: a changed receiver or repository asks again.

    The guard is the whole reuse condition, so this pins it exactly: a
    decision exists, the repository is the same, and the request direction is
    the same. A miss goes to ``ask-again`` rather than to a choice, so a
    missing record cannot be mistaken for an answer.

    ``repo`` and ``relation`` are strings whose default is ``""``, so the
    truthiness half keeps two empty places from matching each other.

    A change in *who can receive* the request is not compared here because
    the record cannot decide it: reaching this branch already means this
    round's sixth check found no receiver, and a round in which one exists
    takes ``request`` and never arrives here. The receiver the evidence
    measured is stored as ``owner`` for the record and for the person
    reading the reused answer, not as a reuse condition.
    """
    body = _doc()["procedures"]["landing-route-replay"]["body"]
    [cond] = [node for node in body if isinstance(node, dict) and "cond" in node]
    guard = cond["cond"][0]["if"]
    assert "$params.previous.decision" in guard, guard
    assert "$params.previous.repo == $params.repo" in guard, guard
    assert "$params.previous.relation == $params.relation" in guard, guard
    # the tip is not a reuse condition (ANSWER 30759)
    assert "tip" not in guard, guard
    assert cond["cond"][0]["then"]["return"] == "ok"
    assert cond["cond"][1]["else"]["value"] == {"decision": "ask-again"}
    # and the hit hands back what was stored, not a re-worded copy
    value = cond["cond"][0]["then"]["value"]
    assert value["decision"] == "$params.previous.decision"
    assert value["reason"] == "$params.previous.reason"
    assert value["relation"] == "$params.previous.relation"


def test_the_commit_report_is_bound_so_the_reused_answer_carries_this_rounds_tip():
    """Every reused answer writes ``tip: "$data.commit.tip"`` (the action
    re-run of ANSWER 30759), so that key has to be written by something. The
    ``commit`` task returns ``{commits, tip}``; ``item`` binds it. Pinned
    because the bind is one word in a long procedure body and its absence is
    silent: an undeclared chain reads as null, and the reused answer would
    then carry a tip that describes nothing."""
    assert "commit" in _doc()["data"], "data.commit is not declared"
    item = _doc()["procedures"]["item"]["body"]
    bound = [node for node in item
             if isinstance(node, dict) and node.get("bind") == "commit"]
    assert bound, "nothing binds the commit report"
    assert bound[-1]["seq"] == ["commit"], bound[-1]


def test_the_gate_call_binds_the_answer_for_the_next_round():
    tree = _land_no_target()
    call = _find(tree, "call", "landing-target-gate")
    assert call is not None, "the gate is no longer called here"
    assert call.get("bind") == "landing_route", call
    # and the data key is declared, so a reader sees it in flow_status
    assert "landing_route" in _doc()["data"]


def test_the_gate_is_still_asked_when_nothing_is_recorded():
    """The replay must not swallow the first question: the branch runs it
    first, its ``ask-again`` case is an empty tree, and the re-measure and the
    person's gate come after it in the same seq."""
    tree = _land_no_target()
    seq = tree.get("seq") or []
    ids = [n.get("id") for n in seq if isinstance(n, dict)]
    assert ids == ["landing-target-replay", "landing-target-retry",
                   "landing-target-reroute"], ids
    # ...and the gate is still reached on that seq: the ask is the else of the
    # re-route, so a run that never sees a target still ends at the person
    # (the middle branch is the replayed merge-by-agent, claunch-yzwut)
    assert _find(seq[-1]["cond"][-1]["else"], "call", "landing-target-gate") is not None


def _land_no_target_retry() -> dict:
    retry = _find(_land_no_target(), "id", "landing-target-retry")
    assert retry is not None, "the no-target branch lost its re-measure"
    return retry


def test_the_no_target_branch_re_measures_the_gate_before_a_person_is_asked():
    """claunch-sp0ut.1: the sixth check's ``1`` is a momentary value.

    The measured failure (claunch-sp0ut, 2026-11-03): the gate answered 1 at
    21:51 while the leader was between rounds, this workflow opened the person
    question, and at 22:13 the leader's round 583 came up and the same command
    answered 0 -- the question then stayed open for a day on a premise that no
    longer held. So the branch spends three more measurements before it asks.

    The probe is the sixth check's own command: two different commands would
    re-measure something the verdict is not about.
    """
    retry = _land_no_target_retry()
    assert "wait" in retry, retry
    wait = retry["wait"]
    assert wait["probe"] == ["claunch", "gate", "landing-target", "--json"], wait
    # exit 0 holds, exit 1 keeps polling, anything else is undecided
    assert wait["poll"] == 60, wait
    assert wait["undecided_max"] == 3, wait
    # the engine polls at t=0/60/120 (three re-measurements) and the clock runs
    # out at 180 -- pin the arithmetic, not just the two numbers
    assert wait["seconds"] == 3 * wait["poll"], wait


def test_both_retry_failures_continue_to_the_person_instead_of_escaping_the_gate():
    """``timeout``/``undecidable`` have to be handled at the wait.

    Left unhandled they leave ``land`` as outcomes it does not declare, and the
    round loop is left through a ``fail`` -- the accident ``blocked`` was
    introduced for (claunch-improver-integration-request-blocked-o6paj). The
    handler is an empty tree because the branch judges the result itself, in
    the cond below: the question still opens, the outcome does not leak.
    """
    retry = _land_no_target_retry()
    # read the compiled node, not the yaml mapping: a bare ``on:`` key loads as
    # ``True`` under YAML 1.1 (``yaml.safe_load``), while the workflow's own
    # loader reads 1.2 booleans. ``Node.on`` is normalised either way.
    assert "wait" in retry, retry
    node = _chart().nodes["round/land/landing-target-retry"]
    assert set(node.on) == {"timeout", "undecidable"}, node.on
    for outcome, handler in node.on.items():
        assert _chart().nodes[handler].type == "seq", (outcome, handler)
        assert not _chart().nodes[handler].get("children"), (outcome, handler)


def test_a_target_that_appears_during_the_retries_takes_the_normal_landing_path():
    """One 0 among the three re-measurements ends the branch, not the round.

    The re-route's ``then`` is an empty tree, so it completes ``ok``; because
    the cond is the last node of the option's tree, the ``no-target`` option
    completes ``ok`` too and the ``landing`` choose finishes ``ok``. ``land``'s
    seq then goes on to ``submit`` (align -> peer review -> landing request) --
    the same path a ``request`` answer takes. The person's question sits only
    in the ``else``.
    """
    reroute = _find(_land_no_target(), "id", "landing-target-reroute")
    assert reroute is not None, "no branch between the re-measure and the gate"
    [hit, replayed, miss] = reroute["cond"]
    assert hit["if"] == "$result['landing-target-retry'].outcome == 'ok'", hit
    # claunch-yzwut: the only work on that path is to clear the self-landing
    # mark a replayed merge-by-agent may have set -- a round with a receiver
    # lands through the request, never through the agent's own merge
    assert hit["then"]["call"] == "self-merge-arm", hit
    assert hit["then"]["with"]["armed"] is False, hit
    assert hit["then"]["bind"] == "self_merge", hit
    # a replayed merge-by-agent (still no receiver) goes on without asking
    assert replayed["if"] == "$data.self_merge.armed == true", replayed
    assert replayed["then"] == [], replayed
    assert "if" not in miss, miss
    ask = _find(miss["else"], "id", "landing-target-ask")
    assert ask is not None and ask.get("call") == "landing-target-gate", miss
    # the ask appears nowhere else: a question on the ok path would mean the
    # branch still asks after a target appeared
    elsewhere = [n for n in _land_no_target()["seq"]
                 if isinstance(n, dict) and n.get("id") == "landing-target-ask"]
    assert elsewhere == [], elsewhere


def test_the_re_measure_comes_after_the_replayed_decision():
    """A recorded answer is not delayed by three minutes of re-measuring.

    claunch-1aj1a.1's replay answers for the same place and ends the round; the
    re-measure only serves the path that would otherwise ask a person.
    """
    ids = [n.get("id") for n in _land_no_target().get("seq") or []
           if isinstance(n, dict)]
    assert ids.index("landing-target-replay") < ids.index("landing-target-retry") \
        < ids.index("landing-target-reroute"), ids


def test_the_sixth_check_tells_the_agent_the_answer_is_re_measured():
    """The prompt is the agent's instruction. Told 「1이면 곧바로 사람」 the
    agent waits on a question that the branch first spends three minutes
    re-measuring."""
    prompt = _words(_landing().get("prompt") or "")
    assert "landing-target-retry" in prompt
    assert "재측정 세 번이 끝난 뒤" in prompt
    assert "1은 순간값이다" in prompt


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
    # the outcome is declared, so the engine has a value shape for it. There
    # is no ``rework`` any more: claunch-1aj1a.1 made every choice end the
    # round (rework could only re-run the batch), so the declaration would
    # name an outcome nothing raises.
    # claunch-yzwut: ok is merge-by-agent's, the one answer that goes on.
    assert set(proc["returns"]) == {"blocked", "held", "ok"}
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
