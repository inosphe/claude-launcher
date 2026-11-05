"""A person's choice gate, answered by the parent within a declared allow list
(claunch-vjp4y.2.1).

The rule under test. A ``chooser: user`` choice -- and the driver's move or
report request -- is a gate only a person may pass. It may now declare who
else is asked (``from``), for how long (``timeout``), and which of its options
that answerer may take (``delegate: {allow: [...]}``). The mechanism is the ask
machinery, not a second copy of it: the gate is opened as an ask record, so
the candidate groups, the deadline, ``_ask_escalate`` and ``_delegated_refusal``
are the rules that already exist. The answer applies as the gate's own answer
-- the pick (:func:`runtime.select`) or the move verdict
(:func:`runtime.resolve_move`) -- judged by the same checks a typed answer
passes. A person is never restricted, an option outside the allow list is
recorded and dropped to the person, an unanswered gate falls through to the
next candidate and then to the person, and a gate that declares nothing
behaves exactly as before.
"""

from __future__ import annotations

import textwrap

import pytest

from claude_launcher.cflow import responders
from claude_launcher.cflow2 import asks, frontend, runtime
from claude_launcher.cflow2.store import RunStore

T0 = 1_000_000.0

# A round that ends on a person's decision -- the shape every worker run has
# when it stops on ``end``/``new-task``. The gate declares the parent as its
# candidate and ``end`` as the only option it may take.
GATED = """
cflow: 2
name: t
nodes:
  work: {prompt: W}
body:
  seq:
    - work
    - choose:
        chooser: user
        prompt: the round is over -- take a new task, hold, or end?
        from: [{role: parent}]
        timeout: 60
        delegate: {allow: [end]}
        options:
          new-task: {then: []}
          hold: {then: []}
          end: {then: []}
"""

# The same choice with nothing declared: a person's alone.
PLAIN = GATED.replace("        from: [{role: parent}]\n", "") \
             .replace("        timeout: 60\n", "") \
             .replace("        delegate: {allow: [end]}\n", "")

# A run with a node a request may move it to, and the move gate delegated.
MOVED = """
cflow: 2
name: t
gates:
  move:
    from: [{role: parent}]
    timeout: 300
    delegate: {allow: [approve]}
nodes:
  intake: {prompt: I, force_target: true}
  work: {prompt: do it}
body:
  seq:
    - intake
    - work
"""


def _chart(src):
    return frontend.compile_text(textwrap.dedent(src))


class Drv:
    """A run driven by ``w1``, whose parent is ``boss``."""

    def __init__(self, tmp_path, src=GATED, *, parent="boss", driver="w1"):
        self.cwd = str(tmp_path)
        self.scope = driver
        self.st = runtime.start(_chart(src), cwd=self.cwd, scope=self.scope, caps={},
                                now=T0, driver=driver, driver_parent=parent)

    def kw(self, now=T0):
        return {"cwd": self.cwd, "scope": self.scope, "now": now}

    def state(self):
        return RunStore(self.cwd, self.scope).load_state()

    def asks(self):
        return [rec for rec in (self.state().get("asks") or {}).values()
                if rec.get("status") == "open"]

    def gate(self, target="choice"):
        for rec in self.asks():
            if (rec.get("gate") or {}).get("target") == target:
                return rec
        return None

    def events(self, name):
        return [e for e in runtime.journal(cwd=self.cwd, scope=self.scope)
                if e.get("event") == name]

    def to_choice(self):
        task = self.st["tasks"][0]["instance"]
        self.st = runtime.report(task, "ok", None, **self.kw())


def _member(handle, role, session=None):
    return responders.Responder(session=session or f"s-{handle}", handle=handle, role=role,
                                local=True, reachability="idle")


def _pool(**kw):
    base = dict(mesh="m", me="w1", members={}, reachable=set(), ancestors=[], siblings=set())
    base.update(kw)
    return lambda **_: responders.Pool(**base)


class Nudges:
    def __init__(self):
        self.sent = []

    def __call__(self, session, message, cwd=None):
        self.sent.append((session, message))
        return [session]


def _route(d, *, now=T0, nudges=None):
    nudges = nudges if nudges is not None else Nudges()
    sent = []
    asks.route(pool_fn=_pool(), deliver_fn=lambda rec, ctx: sent.append(rec),
               nudge_fn=nudges, **d.kw(now))
    return nudges, sent


# -- the declaration ----------------------------------------------------------


def test_a_choice_gate_compiles_its_candidates_and_allow_list():
    node = [n for n in _chart(GATED).nodes.values() if n.get("gate")][0]
    gate = node.get("gate")
    assert gate["from"] == [{"role": "parent", "scope": "any", "connect": False}]
    assert gate["timeout"] == 60
    assert gate["delegate"] == {"allow": ["end"]}


def test_a_choice_gate_needs_a_person_as_its_chooser():
    with pytest.raises(frontend.FrontendError, match="chooser: user"):
        _chart(GATED.replace("chooser: user", "chooser: agent"))


def test_a_choice_gate_allow_name_must_be_one_of_its_options():
    with pytest.raises(frontend.FrontendError, match="do not have"):
        _chart(GATED.replace("allow: [end]", "allow: [done]"))


def test_a_choice_gate_must_name_a_candidate():
    with pytest.raises(frontend.FrontendError, match="at least one candidate"):
        _chart(GATED.replace("from: [{role: parent}]", "from: []"))


def test_a_choice_gate_falls_back_to_the_person_only():
    with pytest.raises(frontend.FrontendError, match="fallback is the person"):
        _chart(GATED.replace("timeout: 60", "timeout: 60\n        otherwise: self"))


def test_a_run_level_move_gate_compiles():
    gates = _chart(MOVED).meta.get("gates")
    assert gates["move"]["from"][0]["role"] == "parent"
    assert gates["move"]["timeout"] == 300
    assert gates["move"]["delegate"] == {"allow": ["approve"]}


def test_a_run_level_move_gate_allow_name_must_be_a_verdict():
    with pytest.raises(frontend.FrontendError, match="does not answer"):
        _chart(MOVED.replace("allow: [approve]", "allow: [land]"))


def test_a_run_level_gate_refuses_an_unknown_target():
    with pytest.raises(frontend.FrontendError, match="gates"):
        _chart(MOVED.replace("gates:\n", "gates:\n  rollback:\n    from: [{role: parent}]\n"))


def test_a_gate_less_choice_keeps_no_gate():
    assert not [n for n in _chart(PLAIN).nodes.values() if n.get("gate")]


# -- the gate is opened and routed --------------------------------------------


def test_a_person_choice_gate_opens_a_gate_ask(tmp_path):
    d = Drv(tmp_path)
    d.to_choice()
    rec = d.gate("choice")
    assert rec is not None
    assert rec["resolution"] == runtime.RES_ROUTING
    assert rec["instance"] == d.st["choices"][0]["instance"]
    assert [o["name"] for o in rec["options"]] == ["new-task", "hold", "end"]
    assert rec["delegate"] == {"allow": ["end"]}
    assert d.events("gate_delegated")[-1]["allow"] == ["end"]


def test_the_parent_is_asked_the_gate_question_directly(tmp_path):
    d = Drv(tmp_path)
    d.to_choice()
    nudges, _ = _route(d)
    assert [n[0] for n in nudges.sent] == ["boss"]
    rec = d.gate("choice")
    assert rec["resolution"] == runtime.RES_ASKED
    assert [e["session"] for e in rec["asked"]] == ["boss"]
    assert rec["deadline"] == T0 + 60


def test_the_allow_list_is_shown_to_the_answerer(tmp_path):
    d = Drv(tmp_path)
    d.to_choice()
    _route(d)
    pending = asks.pending_for("boss")
    rec = [p for p in pending if p["ask_id"] == d.gate("choice")["id"]][0]
    assert rec["delegate"] == {"allow": ["end"]}
    assert "new-task" in rec["options"] and "end" in rec["options"]


def test_no_parent_means_no_gate_ask_and_an_unchanged_choice(tmp_path):
    d = Drv(tmp_path, parent="")
    d.to_choice()
    assert d.asks() == []
    assert d.st["choices"][0]["chooser"] == "user"
    assert not d.events("gate_delegated")


# -- the parent's answer ------------------------------------------------------


def test_the_parents_answer_takes_the_option(tmp_path):
    d = Drv(tmp_path)
    d.to_choice()
    _route(d)
    receipt = runtime.answer(d.gate("choice")["id"], "end", by_session="boss", **d.kw())
    assert receipt["decision"] == "end"
    event = d.events("selected")[-1]
    assert event["by"] == "boss" and event["delegated"] is True
    assert d.state()["person_picks"][-1]["by"] == "boss"
    assert d.gate("choice") is None


def test_an_option_outside_the_allow_list_is_recorded_and_dropped_to_the_person(tmp_path):
    d = Drv(tmp_path)
    d.to_choice()
    _route(d)
    inst = d.st["choices"][0]["instance"]
    receipt = runtime.answer(d.gate("choice")["id"], "hold", reason="it can wait",
                             by_session="boss", **d.kw())
    assert receipt["refused"] == "hold"
    refused = d.events("ask_delegated_refused")[-1]
    assert refused["decision"] == "hold" and refused["allow"] == ["end"]
    # nothing was applied: the choice still stands, for a person
    st = runtime.status(**d.kw())
    assert [c["instance"] for c in st["choices"]] == [inst]
    assert d.events("selected") == []
    # every candidate group is spent, so the gate is a person's again
    assert d.gate("choice")["resolution"] == runtime.RES_HUMAN


def test_a_person_is_never_restricted_by_the_allow_list(tmp_path):
    d = Drv(tmp_path)
    d.to_choice()
    _route(d)
    inst = d.st["choices"][0]["instance"]
    d.st = runtime.select(inst, "hold", caller=runtime.CALLER_USER, **d.kw())
    assert d.events("selected")[-1]["by"] == runtime.CALLER_USER
    assert d.gate("choice") is None


def test_a_persons_pick_closes_the_gate_ask_still_with_the_parent(tmp_path):
    d = Drv(tmp_path)
    d.to_choice()
    _route(d)
    inst = d.st["choices"][0]["instance"]
    runtime.select(inst, "end", caller=runtime.CALLER_USER, **d.kw())
    rec = [r for r in (d.state().get("asks") or {}).values()
           if (r.get("gate") or {}).get("target") == "choice"][0]
    assert rec["status"] == "closed"
    closed = d.events("ask_closed")[-1]
    assert "user took 'end'" in (closed.get("why") or "")


def test_an_unanswered_gate_falls_through_to_the_person_at_its_deadline(tmp_path):
    d = Drv(tmp_path)
    d.to_choice()
    _route(d)
    runtime.status(**d.kw(now=T0 + 61))
    rec = [r for r in (d.state().get("asks") or {}).values()
           if (r.get("gate") or {}).get("target") == "choice"][0]
    assert rec["resolution"] == runtime.RES_HUMAN
    assert d.events("ask_timeout")
    assert runtime.status(**d.kw(now=T0 + 61))["choices"]


def test_the_driver_cannot_answer_its_own_gate(tmp_path):
    d = Drv(tmp_path)
    d.to_choice()
    _route(d)
    with pytest.raises(runtime.FlowError, match="your own run"):
        runtime.answer(d.gate("choice")["id"], "end", by_session="w1", **d.kw())


def test_a_stale_gate_ask_is_closed_when_the_choice_is_gone(tmp_path):
    d = Drv(tmp_path)
    d.to_choice()
    _route(d)
    rec = d.gate("choice")
    runtime.select(d.st["choices"][0]["instance"], "end", caller=runtime.CALLER_PARENT,
                   by="boss", **d.kw())
    assert (d.state()["asks"][rec["id"]])["status"] == "closed"


# -- the move gate ------------------------------------------------------------


def test_a_move_request_gate_is_answered_by_the_parent(tmp_path):
    d = Drv(tmp_path, src=MOVED)
    runtime.report(d.st["tasks"][0]["instance"], "ok", None, **d.kw())
    d.st = runtime.request_move("intake", "the definition has no route for this",
                                by="w1", **d.kw())
    rec = d.gate("move")
    assert rec is not None
    assert rec["instance"] == d.st["move_request"]["id"]
    _route(d)
    receipt = runtime.answer(d.gate("move")["id"], "approve", reason="go ahead",
                             by_session="boss", **d.kw())
    assert receipt["decision"] == "approve"
    assert d.events("move_approved")[-1]["by"] == "boss"
    assert d.events("forced")[-1]["node"] == "intake"
    assert d.gate("move") is None


def test_a_move_gate_refuses_a_verdict_outside_its_allow_list(tmp_path):
    d = Drv(tmp_path, src=MOVED)
    runtime.report(d.st["tasks"][0]["instance"], "ok", None, **d.kw())
    d.st = runtime.request_move("intake", "the definition has no route for this",
                                by="w1", **d.kw())
    _route(d)
    receipt = runtime.answer(d.gate("move")["id"], "deny", by_session="boss", **d.kw())
    assert receipt["refused"] == "deny"
    assert not d.events("move_denied")
    assert runtime.status(**d.kw())["move_request"]["id"] == d.st["move_request"]["id"]


def test_a_person_still_answers_the_move_gate(tmp_path):
    d = Drv(tmp_path, src=MOVED)
    runtime.report(d.st["tasks"][0]["instance"], "ok", None, **d.kw())
    d.st = runtime.request_move("intake", "the definition has no route for this",
                                by="w1", **d.kw())
    _route(d)
    runtime.resolve_move("deny", reason="no", **d.kw())
    assert d.events("move_denied")[-1]["by"] == runtime.CALLER_USER
    assert d.gate("move") is None


def test_a_withdrawn_request_closes_its_gate_ask(tmp_path):
    d = Drv(tmp_path, src=MOVED)
    runtime.report(d.st["tasks"][0]["instance"], "ok", None, **d.kw())
    d.st = runtime.request_move("intake", "the definition has no route for this",
                                by="w1", **d.kw())
    _route(d)
    runtime.cancel_move_request(by="w1", **d.kw())
    rec = [r for r in (d.state().get("asks") or {}).values()
           if (r.get("gate") or {}).get("target") == "move"][0]
    assert rec["status"] == "closed"
