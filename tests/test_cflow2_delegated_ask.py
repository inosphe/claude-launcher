"""A delegated answerer: the driver's parent answers an ask within a declared
allow list (claunch-vjp4y.2).

The rule under test. An ``ask`` may name ``{role: parent}`` among its ``from``
candidates. That group is the driving session's own parent -- a session
relationship, not a mesh role -- so it is resolved from ``driver_parent`` and
the question is typed into its terminal instead of being posted as a mesh
``decide`` message. Its answer comes back through ``flow_answer`` like any
responder's. When the ask also declares ``delegate: {allow: [...]}`` the
parent sees every option but may only land the listed ones: anything else is
recorded (``ask_delegated_refused``) and counted as that answerer's
abstention, so the question goes on to the rest of its group and then to a
person. A person is never restricted, and an ask with no ``delegate`` behaves
exactly as before.
"""

from __future__ import annotations

import textwrap

import pytest

from claude_launcher.cflow import responders
from claude_launcher.cflow2 import asks, frontend, runtime

T0 = 1_000_000.0
SCOPE = "w1"

ASKED = """
nodes:
  a:
    prompt: a
    ask:
      prompt: "how should this land?"
      from: [{role: parent}]
      otherwise: human
      timeout: 60
      delegate: {allow: [ship]}
      options:
        ship: {description: land it}
        leave: {description: leave it to a person}
        freeze: {description: freeze it}
  b: {prompt: b}
body:
  try: [a, b]
  catch: {declined: b}
"""

# A mesh-routed group after the parent: the refusal's fallback reaches it.
ASKED_THEN_REVIEWER = ASKED.replace("from: [{role: parent}]",
                                    "from: [{role: parent}, {role: reviewer}]")

# The same ask with nobody delegated: only a person answers.
PLAIN = """
nodes:
  a:
    prompt: a
    ask:
      prompt: "how should this land?"
      otherwise: human
      options:
        ship: {description: land it}
        leave: {description: leave it to a person}
  b: {prompt: b}
body:
  try: [a, b]
  catch: {declined: b}
"""


def _compile(src):
    return frontend.compile_text("cflow: 2\nname: t\n" + textwrap.dedent(src))


class Drv:
    """A run driven by ``w1`` whose parent session is ``boss`` (or none)."""

    def __init__(self, tmp_path, *, src=ASKED, parent="boss", driver="w1", scope=SCOPE):
        self.cwd = str(tmp_path)
        self.scope = scope
        chart = _compile(src)
        runtime.start(chart, cwd=self.cwd, scope=self.scope, caps={}, now=T0,
                      driver=driver, driver_parent=parent, mesh="m")

    def kw(self, now=T0):
        return {"cwd": self.cwd, "scope": self.scope, "now": now}

    def status(self, now=T0):
        return runtime.status(cwd=self.cwd, scope=self.scope, now=now)

    def ask(self, now=T0):
        return self.status(now)["asks"][0]

    def events(self, kind):
        return [e for e in runtime.journal(cwd=self.cwd, scope=self.scope) if e.get("event") == kind]


def _member(handle, role, session=None):
    return responders.Responder(session=session or f"s-{handle}", handle=handle, role=role,
                                local=True, reachability="idle")


def _pool(**kw):
    members = {"rev": _member("rev", "reviewer")}
    base = dict(mesh="m", me="w1", members=members, reachable={"rev"},
                ancestors=[], siblings=set())
    base.update(kw)
    return lambda **_: responders.Pool(**base)


class Nudges:
    """Captures what would be typed into the parent's terminal."""

    def __init__(self):
        self.sent = []

    def __call__(self, session, message, cwd=None):
        self.sent.append((session, message))
        return [session]


def _route(d, *, pool=None, now=T0, nudges=None):
    nudges = nudges if nudges is not None else Nudges()
    sent = []
    asks.route(pool_fn=pool or _pool(),
               deliver_fn=lambda rec, ctx: sent.append(rec), nudge_fn=nudges,
               **d.kw(now))
    return nudges, sent


# -- the declaration -----------------------------------------------------------


def _only_ask(chart):
    return [n for n in chart.nodes.values() if isinstance(n.get("ask"), dict) and n.get("ask")]


def test_a_delegate_allow_list_compiles():
    ast = _only_ask(_compile(ASKED))[0].get("ask")
    assert ast["delegate"] == {"allow": ["ship"]}
    assert ast["timeout"] == 60


def test_a_delegate_allow_name_must_be_one_of_the_options():
    with pytest.raises(frontend.FrontendError, match="do not have"):
        _compile(ASKED.replace("allow: [ship]", "allow: [ships]"))


def test_a_delegate_allow_list_must_be_a_non_empty_list_of_names():
    with pytest.raises(frontend.FrontendError, match="non-empty"):
        _compile(ASKED.replace("allow: [ship]", "allow: []"))
    with pytest.raises(frontend.FrontendError, match="non-empty"):
        _compile(ASKED.replace("allow: [ship]", "allow: ship"))


def test_delegate_refuses_an_unknown_key():
    with pytest.raises(frontend.FrontendError, match="delegate"):
        _compile(ASKED.replace("allow: [ship]", "allow: [ship], deny: [freeze]"))


# -- routing -------------------------------------------------------------------


def test_the_parent_group_is_resolved_from_the_session_hierarchy(tmp_path):
    d = Drv(tmp_path)
    nudges, delivered = _route(d)
    rec = d.ask()
    assert rec["asked"] == ["boss"]
    assert rec["resolution"] == "asked"
    assert rec["deadline"] == pytest.approx(T0 + 60)
    # typed into the parent's terminal, not posted to the mesh
    assert [s for s, _ in nudges.sent] == ["boss"]
    assert delivered == []
    assert "you may answer 'ship'" in nudges.sent[0][1]


def test_without_a_parent_the_group_is_skipped_and_a_person_answers(tmp_path):
    d = Drv(tmp_path, parent="")
    _route(d)
    rec = d.ask()
    assert rec["resolution"] == "human"
    assert rec["asked"] == []
    assert any("no parent session" in str(s) for s in rec["skipped"])


def test_the_parent_is_reported_by_who_answers(tmp_path):
    d = Drv(tmp_path)
    seen = asks.who_answers("a", cwd=d.cwd, scope=SCOPE, pool_fn=_pool())
    assert seen["groups"][0]["asks"] == ["boss"]
    assert seen["resolves"] is True


def test_who_answers_reports_a_run_without_a_parent(tmp_path):
    d = Drv(tmp_path, parent="")
    seen = asks.who_answers("a", cwd=d.cwd, scope=SCOPE, pool_fn=_pool())
    assert seen["groups"][0]["asks"] == []
    assert "no parent session" in seen["groups"][0]["reason"]


# -- answering -----------------------------------------------------------------


def test_the_parent_may_land_an_allowed_option(tmp_path):
    d = Drv(tmp_path)
    _route(d)
    runtime.answer(d.ask()["ask"], "ship", "it is ours to land", by_session="boss", **d.kw())
    st = d.status()
    assert (st.get("asks") or []) == []
    assert d.events("ask_answered")[-1]["decision"] == "ship"


def test_an_option_outside_the_allow_list_is_refused_and_goes_to_a_person(tmp_path):
    d = Drv(tmp_path)
    _route(d)
    rec = runtime.answer(d.ask()["ask"], "freeze", "not mine really", by_session="boss",
                         **d.kw())
    assert rec.get("refused") == "freeze"
    st = d.ask()
    # nothing applied: still open, and now with a person (no other group left)
    assert st["resolution"] == "human"
    assert d.events("ask_delegated_refused")[-1]["decision"] == "freeze"
    assert d.events("ask_delegated_refused")[-1]["allow"] == ["ship"]
    # the person may still answer, and lands whatever they pick
    runtime.approve("a", decision="freeze", caller=runtime.CALLER_USER, **d.kw())
    assert (d.status().get("asks") or []) == []


def test_a_refusal_leaves_the_question_with_the_rest_of_the_group(tmp_path):
    d = Drv(tmp_path, src=ASKED_THEN_REVIEWER)
    nudges, delivered = _route(d)
    assert d.ask()["asked"] == ["boss"]
    runtime.answer(d.ask()["ask"], "leave", "no", by_session="boss", **d.kw())
    # the parent abstained, so the question moved to the next group on the spot
    _route(d, nudges=nudges)
    assert d.ask()["asked"] == ["rev"]


def test_no_answer_within_the_timeout_moves_it_to_a_person(tmp_path):
    d = Drv(tmp_path)
    _route(d)
    assert d.ask()["resolution"] == "asked"
    runtime.status(now=T0 + 61, tick=True, cwd=d.cwd, scope=SCOPE)
    assert d.ask(now=T0 + 61)["resolution"] == "human"
    moved = [e for e in runtime.journal(cwd=d.cwd, scope=SCOPE)
             if e.get("event") == "ask_timeout"]
    assert moved and moved[-1]["asked"] == ["boss"]


def test_a_person_is_never_restricted_by_the_allow_list(tmp_path):
    d = Drv(tmp_path)
    _route(d)
    # a person answers an open, still-delegated ask -- any option
    runtime.approve("a", decision="freeze", caller=runtime.CALLER_USER, **d.kw())
    assert (d.status().get("asks") or []) == []
    assert d.events("ask_delegated_refused") == []


def test_without_delegate_a_responder_picks_any_option(tmp_path):
    d = Drv(tmp_path, src=ASKED.replace("      delegate: {allow: [ship]}\n", ""),
            parent="")
    assert d.ask()["delegate"] is None
    runtime.approve("a", decision="freeze", caller=runtime.CALLER_USER, **d.kw())
    assert (d.status().get("asks") or []) == []


def test_the_allow_list_is_shown_with_the_ask(tmp_path):
    d = Drv(tmp_path)
    assert d.ask()["delegate"] == {"allow": ["ship"]}
    _route(d)
    assert asks.pending_for("boss")
