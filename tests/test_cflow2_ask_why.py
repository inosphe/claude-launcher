"""An ask card says why it is open and who was asked before a person
(claunch-0rjnn): ``because`` is the choice the driver took to reach the ask
with its reason, ``route`` the responder groups the question passed through.
s6086's landing-review stood before a person showing only its static prompt;
the delta-0 reason and s15's abstain were in the journal alone."""

from __future__ import annotations

import textwrap

import pytest

from claude_launcher.cflow import responders
from claude_launcher.cflow2 import asks, executors, frontend, runtime

T0 = 1_000_000.0
SCOPE = "w1"

CHART = """
    nodes:
      review:
        prompt: wait
        ask:
          prompt: "ship it?"
          from: [{role: worker, scope: ancestor}, {role: leader, scope: sibling}]
          otherwise: human
          timeout: 60
          options:
            request: {outcome: ok, description: queue it}
            hold: {outcome: hold, description: freeze}
        returns: {ok: {}, hold: {}}
    body:
      - id: landing
        choose:
          chooser: agent
          prompt: land?
          options:
            request: {then: []}
            escalate: {then: [review]}
"""


@pytest.fixture(autouse=True)
def _no_dispatcher(monkeypatch):
    executors.register(None)
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    yield
    executors.register(None)


def _start(tmp_path):
    chart = frontend.compile_text("cflow: 2\nname: t\n" + textwrap.dedent(CHART))
    kw = {"cwd": str(tmp_path), "scope": SCOPE, "now": T0}
    runtime.start(chart, cwd=kw["cwd"], scope=SCOPE, caps={}, now=T0)
    return kw


def _pool(**over):
    lead = responders.Responder(session="s-lead", handle="lead", role="leader",
                                local=True, reachability="idle")
    base = dict(mesh="m", me=SCOPE, members={"lead": lead}, reachable={"lead"},
                ancestors=[], siblings={"lead"})
    base.update(over)
    return lambda **_: responders.Pool(**base)


class Outbox:
    def __init__(self):
        self.sent = []

    def __call__(self, rec, ctx):
        self.sent.append((rec, ctx))


def test_the_ask_carries_the_choice_and_reason_that_opened_it(tmp_path):
    kw = _start(tmp_path)
    st = runtime.select("landing", "escalate", "delta 0: already landed as 3fc44afd", **kw)
    (a,) = st["asks"]
    assert a["because"]["option"] == "escalate"
    assert a["because"]["choice"] == "landing"
    assert a["because"]["reason"] == "delta 0: already landed as 3fc44afd"
    assert a["because"]["by"] == runtime.CALLER_AGENT
    assert a["route"] == []


def test_an_ask_under_no_choice_has_no_because(tmp_path):
    chart = frontend.compile_text("cflow: 2\nname: t\n" + textwrap.dedent("""
        nodes:
          a: {prompt: a, ask: {prompt: "ok?", otherwise: human}}
        body: [a]
    """))
    st = runtime.start(chart, cwd=str(tmp_path), scope=SCOPE, caps={}, now=T0)
    st = runtime.status(cwd=str(tmp_path), scope=SCOPE, now=T0)
    assert st["asks"][0]["because"] is None


def test_the_route_names_each_group_and_why_it_moved_on(tmp_path):
    kw = _start(tmp_path)
    runtime.select("landing", "escalate", "delta 0", **kw)
    out = Outbox()
    asks.route(cwd=kw["cwd"], scope=SCOPE, now=T0, pool_fn=_pool(), deliver_fn=out)
    rec, _ = out.sent[0]
    # the responder reads the reason in its message too
    text = asks.question_text(rec, workflow="t", sender=SCOPE, cwd=kw["cwd"], scope=SCOPE)
    assert "Why this question is open: at 'landing'" in text and "delta 0" in text
    runtime.answer(rec["id"], "abstain", reason="not my repository", by_session="s-lead",
                   **kw)
    a = runtime.status(**kw)["asks"][0]
    assert a["resolution"] == "human"
    route = a["route"]
    assert [r["group"] for r in route] == [0, 1]
    assert route[0]["candidates"] == "worker (ancestor)" and route[0]["reason"]
    assert route[1]["candidates"] == "leader (sibling)" and route[1]["who"] == "lead"
    assert "not my repository" in route[1]["reason"]
    # the old field keeps its shape
    assert a["skipped"] == [r["reason"] for r in route]
