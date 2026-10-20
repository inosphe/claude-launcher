"""Routing for mega threads (daemon/megathread_route.py, claunch-v5nhp).

T1 scores: log-odds from the distribution; a response without it falls back
   to choice + confidence; zero and one are clamped so the log is finite
T2 judge: one member over the threshold routes alone; two or more are offered
   to the user; none offers the best-ranked; every call failing is "error"
T3 the state carries the session record, its briefing and the thread tail;
   the thread keeps its last messages in order
T4 evaluate asks once per member with the feature's model and sums tokens
T5 a member whose call fails does not sink the others
T6 policy reads the odds-ratio threshold and the budget
"""

import asyncio
import math
from types import SimpleNamespace

import pytest

from claude_launcher.daemon import megathread_route as P
from claude_launcher.daemon import jev
from claude_launcher.threads import store as T


def test_t1_scores_from_the_distribution_and_fallback():
    s = P.scores({"choice": "mine", "confidence": 0.9,
                  "probabilities": {"mine": 0.92, "not_mine": 0.08}})
    assert s["p_mine"] == pytest.approx(0.92)
    assert s["logit"] == pytest.approx(math.log(0.92 / 0.08))
    # No distribution: the choice and its confidence stand in.
    s = P.scores({"choice": "not_mine", "confidence": 0.8})
    assert s["p_mine"] == pytest.approx(0.2) and s["p_not_mine"] == pytest.approx(0.8)
    assert s["logit"] < 0
    # Certainty is clamped: ln(0) would be -inf.
    s = P.scores({"choice": "mine", "confidence": 1.0, "probabilities": {"mine": 1.0, "not_mine": 0.0}})
    assert math.isfinite(s["logit"]) and s["logit"] > 0


def test_t2_judge_branches():
    threshold = math.log(9)
    one = P.judge([{"thread": "a", "logit": 3.0}, {"thread": "b", "logit": 0.5}], threshold)
    assert one["verdict"] == "auto" and [r["thread"] for r in one["options"]] == ["a"]

    two = P.judge([{"thread": "a", "logit": 3.0}, {"thread": "b", "logit": 2.5}], threshold)
    assert two["verdict"] == "choose" and len(two["options"]) == 2
    assert [r["thread"] for r in two["options"]] == ["a", "b"]

    none = P.judge([{"thread": "a", "logit": 2.0}, {"thread": "b", "logit": 1.0}], threshold)
    assert none["verdict"] == "none"
    assert [r["thread"] for r in none["options"]] == ["a", "b"]  # ranked, still offered

    # Nobody clears the threshold: the best five are still offered.
    poor = P.judge([{"thread": f"t{i}", "logit": 2.0 - i / 10} for i in range(8)], threshold,
                   top_n=5)
    assert poor["verdict"] == "none" and len(poor["options"]) == 5
    assert [r["thread"] for r in poor["options"]] == ["t0", "t1", "t2", "t3", "t4"]

    dead = P.judge([{"thread": "a", "error": "jev: no key"},
                    {"thread": "b", "error": "jev: timeout"}], threshold)
    assert dead["verdict"] == "error" and len(dead["errors"]) == 2


class FakeSession:
    def __init__(self, name, task="", cwd="/tmp/work"):
        self.sdef = SimpleNamespace(
            name=name, cwd=cwd, task=task, issue="", model="m", project="p",
            kind="worker", role="worker", conversation_id=None, harness="claude", profile="",
        )
        self.info = lambda: {"status": "idle"}


class FakeManager:
    def __init__(self, sessions):
        self.sessions = {s.sdef.name: s for s in sessions}

    def get(self, name):
        if name not in self.sessions:
            raise KeyError(name)
        return self.sessions[name]


def test_t3_build_state_and_thread_tail(tmp_path):
    threads = T.ThreadStore(tmp_path / "threads.sqlite3")
    t = threads.open(T.USER, "s1", "log format")["thread"]
    for i in range(15):
        threads.post(T.USER, t["id"], f"message {i}")
    member = {"session": "s1", "thread": t["id"]}
    state = P.build_state(FakeManager([FakeSession("s1", task="fix the log format")]), threads,
                          member, "which log level should this use?", max_chars=12000)
    # The message is the question being answered; without it Jev is asked to
    # judge a thread against nothing and every member looks like a hit.
    assert state["message"] == "which log level should this use?"
    assert state["session"]["name"] == "s1" and state["session"]["status"] == "idle"
    assert state["session"]["task"] == "fix the log format"
    assert state["thread"]["title"] == "log format"
    bodies = [m["text"] for m in state["thread"]["messages"]]
    assert len(bodies) == P.THREAD_MESSAGES
    assert bodies[-1] == "message 14"  # the tail, not the head


def test_t4_evaluate_asks_once_per_member():
    seen = []

    async def ask(cfg, state, questions, model):
        seen.append((state["session"]["name"], model, questions))
        if state["session"]["name"] == "s1":
            probs = {"mine": 0.95, "not_mine": 0.05}
        else:
            probs = {"mine": 0.2, "not_mine": 0.8}
        return {"model": model, "usage": {"input_tokens": 100},
                "answers": {P.QUESTION_ID: {"type": "choice", "choice": "mine",
                                            "confidence": 0.9, "probabilities": probs}}}

    # build_state is the only thing that touches the thread store; a stub
    # keeps this test about the calls, not the schema.
    class StoreStub:
        def get(self, tid):
            return {"title": tid, "status": "open"}

        def tail(self, tid, limit=12):
            return []

    members = [{"session": "s1", "thread": "th-a"}, {"session": "s2", "thread": "th-b"}]
    cfg = {"jev_model_megathread": "jev-test", "megathread_threshold_ratio": 9.0}
    out = asyncio.run(P.evaluate(FakeManager([FakeSession("s1"), FakeSession("s2")]),
                                 StoreStub(), members, "where should this go?", cfg=cfg, ask=ask))
    assert out["verdict"] == "auto" and len(out["options"]) == 1
    assert out["options"][0]["session"] == "s1"
    assert out["tokens"] == 200
    assert [name for name, _, _ in seen] == ["s1", "s2"]
    assert all(model == "jev-test" for _, model, _ in seen)


def test_t5_one_failure_does_not_sink_the_rest():
    async def ask(cfg, state, questions, model):
        if state["session"]["name"] == "s1":
            raise jev.JevError("HTTP 500")
        return {"model": model, "usage": {"input_tokens": 10},
                "answers": {P.QUESTION_ID: {"type": "choice", "choice": "mine",
                                            "confidence": 0.95,
                                            "probabilities": {"mine": 0.95, "not_mine": 0.05}}}}

    class StoreStub:
        def get(self, tid):
            return {"title": tid, "status": "open"}

        def tail(self, tid, limit=12):
            return []

    members = [{"session": "s1", "thread": "th-a"}, {"session": "s2", "thread": "th-b"}]
    out = asyncio.run(P.evaluate(FakeManager([FakeSession("s1"), FakeSession("s2")]),
                                 StoreStub(), members, "where should this go?", cfg={}, ask=ask))
    assert out["verdict"] == "auto" and out["options"][0]["session"] == "s2"
    assert out["errors"] and out["errors"][0]["thread"] == "th-a"


def test_t6_policy_reads_the_threshold_and_budget():
    pol = P.policy({"megathread_threshold_ratio": 4.0, "megathread_jev_budget_tokens": 1000,
                    "megathread_max_chars": 500, "megathread_concurrency": 3,
                    "jev_model_megathread": "jev-1.14.0"})
    assert pol["threshold"] == pytest.approx(math.log(4.0))
    assert pol["budget_tokens"] == 1000 and pol["max_chars"] == 500
    assert pol["concurrency"] == 3 and pol["model"] == "jev-1.14.0"
    default = P.policy({})
    assert default["threshold"] == pytest.approx(math.log(9.0))
    assert default["budget_tokens"] == 10_000_000
    assert default["model"] == jev.DEFAULT_MODEL
