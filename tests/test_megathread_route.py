"""Routing for mega threads (daemon/megathread_route.py, claunch-v5nhp).

T1 scores: log-odds from the distribution; a response without it falls back
   to choice + confidence; zero and one are clamped so the log is finite
T2 judge: one member over the threshold routes alone; two or more are offered
   to the user; none offers the best-ranked; every call failing is "error";
   one hit beside a failed call is offered rather than taken; the threshold
   comparison is inclusive
T3 the state carries the session record, its briefing and the thread tail;
   the thread keeps its last messages in order
T4 evaluate asks once per member with the feature's model and sums tokens
T5 a member whose call fails does not sink the others, and its call is still
   charged to the routing
T6 policy reads the odds-ratio threshold and the budget, and falls back on a
   value that is not a finite number
T7 a member whose thread is gone is that member's error, not the routing's
T8 a preview failure names the session that failed even when neither session
   has a thread (every unthreaded member shares thread "")
T9 the preview's record (claunch-wlk6g): the branch is read from git's own
   files in a plain checkout and in a linked worktree; draft_facts carries
   workspace, branch, parent and at most DRAFT_ISSUES issue titles
T10 the preview asks about a topic: the document names it "topic", carries
   the facts and the briefing's progress, and the criteria no longer send
   finished work to not_mine; a routing's document is unchanged
T11 every scored member's logit comes back, so the form can move its
   threshold without asking again, and one preview is one call per member
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

    # One hit beside a failed call is offered, not taken: the member that was
    # not scored may have been the intended one, and delivering quietly would
    # leave the user unaware of where the message went.
    partial = P.judge([{"thread": "a", "logit": 3.0},
                       {"thread": "b", "error": "jev: timeout"}], threshold)
    assert partial["verdict"] == "choose"
    assert [r["thread"] for r in partial["options"]] == ["a"]
    assert [e["thread"] for e in partial["errors"]] == ["b"]

    # The comparison is ">=": a member exactly on the threshold takes it.
    edge = P.judge([{"thread": "a", "logit": threshold},
                    {"thread": "b", "logit": threshold - 1e-9}], threshold)
    assert edge["verdict"] == "auto" and edge["options"][0]["thread"] == "a"


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


class StoreStub:
    """The thread store as ``build_state`` uses it: title and tail only."""

    def __init__(self, missing=()):
        self.missing = set(missing)

    def get(self, tid):
        if tid in self.missing:
            raise KeyError(tid)
        return {"title": tid, "status": "open"}

    def tail(self, tid, limit=12):
        return []


def test_t5_one_failure_does_not_sink_the_rest_and_is_still_charged():
    async def ask(cfg, state, questions, model):
        if state["session"]["name"] == "s1":
            raise jev.JevError("HTTP 500")
        return {"model": model, "usage": {"input_tokens": 10},
                "answers": {P.QUESTION_ID: {"type": "choice", "choice": "mine",
                                            "confidence": 0.95,
                                            "probabilities": {"mine": 0.95, "not_mine": 0.05}}}}

    members = [{"session": "s1", "thread": "th-a"}, {"session": "s2", "thread": "th-b"}]
    out = asyncio.run(P.evaluate(FakeManager([FakeSession("s1"), FakeSession("s2")]),
                                 StoreStub(), members, "where should this go?", cfg={}, ask=ask))
    # s2 cleared the threshold, but s1's call failed: the hit is offered, and
    # nothing is delivered while the user has not answered.
    assert out["verdict"] == "choose"
    assert [r["session"] for r in out["options"]] == ["s2"]
    assert out["errors"] and out["errors"][0]["thread"] == "th-a"
    # The failed call was still made and billed; it carries the estimate so
    # the budget window sees the whole spend.
    assert out["tokens"] > 10


def test_t7_a_missing_thread_is_that_members_error_only():
    async def ask(cfg, state, questions, model):
        return {"model": model, "usage": {"input_tokens": 7},
                "answers": {P.QUESTION_ID: {"type": "choice", "choice": "mine",
                                            "confidence": 0.95,
                                            "probabilities": {"mine": 0.95, "not_mine": 0.05}}}}

    members = [{"session": "s1", "thread": "th-gone"}, {"session": "s2", "thread": "th-b"}]
    # A thread deleted under the cluster used to escape the member's try and
    # fail the whole request after the other members' calls were paid for.
    out = asyncio.run(P.evaluate(FakeManager([FakeSession("s1"), FakeSession("s2")]),
                                 StoreStub(missing={"th-gone"}), members,
                                 "where should this go?", cfg={}, ask=ask))
    assert out["verdict"] == "choose" and [r["session"] for r in out["options"]] == ["s2"]
    assert out["errors"][0]["thread"] == "th-gone"
    assert "KeyError" in out["errors"][0]["error"]
    # No state, no call, no spend for that member; the other call's 7 stand.
    assert out["tokens"] == 7


def test_t8_a_preview_failure_names_the_session_that_failed():
    async def ask(cfg, state, questions, model):
        if state["session"]["name"] == "s2":
            raise jev.JevError("HTTP 500")
        return {"model": model, "usage": {"input_tokens": 5},
                "answers": {P.QUESTION_ID: {"type": "choice", "choice": "mine",
                                            "confidence": 0.95,
                                            "probabilities": {"mine": 0.95, "not_mine": 0.05}}}}

    class NoThreads:
        """Every session is scored without a thread -- the preview's normal
        case, and the one where every member's thread id is ""."""

        def list(self, name, closed=False):
            return {"threads": []}

    out = asyncio.run(P.draft(FakeManager([FakeSession("s1"), FakeSession("s2")]),
                              NoThreads(), ["s1", "s2"], "무엇을 함께 볼까", cfg={}, ask=ask))
    rows = {r["session"]: r for r in out["members"]}
    assert rows["s1"]["logit"] is not None and "error" not in rows["s1"]
    # The failure is s2's own row. Reading it back by thread id used to land
    # on s1, the first member that also had "".
    assert rows["s2"]["logit"] is None and "HTTP 500" in rows["s2"]["error"]
    assert out["recommended"] == ["s1"]


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

    # A value that is not finite cannot define a threshold or a ceiling: ln of
    # NaN is NaN and every comparison against it is false; int(inf) raises.
    broken = P.policy({"megathread_threshold_ratio": float("nan"),
                       "megathread_jev_budget_tokens": float("inf"),
                       "megathread_max_chars": float("nan")})
    assert broken["threshold"] == pytest.approx(math.log(9.0))
    assert broken["budget_tokens"] == 10_000_000 and broken["max_chars"] == 12000


def test_t9_draft_facts_and_the_branch_from_git_files(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/master\n", encoding="utf-8")
    (repo / "src").mkdir()
    assert P.git_branch(str(repo / "src")) == "master", "a subdirectory finds its checkout"
    # A linked worktree: `.git` is a file naming the gitdir that holds HEAD.
    gitdir = repo / ".git" / "worktrees" / "wt"
    gitdir.mkdir(parents=True)
    (gitdir / "HEAD").write_text("ref: refs/heads/s1-topic\n", encoding="utf-8")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
    assert P.git_branch(str(wt)) == "s1-topic"
    (gitdir / "HEAD").write_text("0123456789abcdef0123456789abcdef01234567\n", encoding="utf-8")
    assert P.git_branch(str(wt)) == "", "a detached head is no branch"
    assert P.git_branch(str(tmp_path / "nowhere")) == ""
    assert P.git_branch("") == ""

    sdef = SimpleNamespace(name="s1", cwd=str(repo), parent="s0")
    issues = [{"id": f"x-{i}", "status": "open", "title": f"title {i}", "description": "long"}
              for i in range(P.DRAFT_ISSUES + 2)]
    facts = P.draft_facts("s1", sdef, workspace="claunch", issues=issues)
    assert facts["workspace"] == "claunch" and facts["branch"] == "master"
    assert facts["parent"] == "s0"
    assert [i["id"] for i in facts["issues"]] == [f"x-{i}" for i in range(P.DRAFT_ISSUES)]
    # Only id, status and title travel -- a description would be the whole
    # board in every prompt.
    assert facts["issues"][0] == {"id": "x-0", "status": "open", "title": "title 0"}
    # Nothing known is nothing sent.
    assert P.draft_facts("s1", SimpleNamespace(name="s1", cwd="")) == {}


def test_t10_the_preview_document_names_a_topic_and_carries_the_facts(tmp_path, monkeypatch):
    from claude_launcher.daemon import briefing

    monkeypatch.setattr(briefing, "cached_result", lambda name: {"briefing": {
        "one-line-job-description": "로그 형식 정리", "goal": "g", "now": "n",
        "progress": "PR #1 merged", "state": "waiting"}})
    seen = []

    async def ask(cfg, state, questions, model):
        seen.append((state, questions))
        return {"model": model, "usage": {"input_tokens": 3},
                "answers": {P.QUESTION_ID: {"type": "choice", "choice": "mine", "confidence": 0.9,
                                            "probabilities": {"mine": 0.9, "not_mine": 0.1}}}}

    class NoThreads:
        def list(self, name, closed=False):
            return {"threads": []}

    manager = FakeManager([FakeSession("s1", task="t")])
    facts = {"s1": {"workspace": "claunch", "issues": [{"id": "x-1", "title": "로그 형식"}],
                    "name": "not this"}}
    asyncio.run(P.draft(manager, NoThreads(), ["s1"], "로그", cfg={}, ask=ask, facts=facts))
    state, qs = seen[0]
    assert state["topic"] == "로그" and "message" not in state
    assert state["session"]["workspace"] == "claunch"
    assert state["session"]["issues"] == [{"id": "x-1", "title": "로그 형식"}]
    assert state["session"]["name"] == "s1", "the record's own field wins over a fact"
    assert "PR #1 merged" in state["briefing"], "the preview reads progress"
    criteria = qs[P.QUESTION_ID]["criteria"]
    assert "has done" in criteria[P.MINE]
    assert "finished" not in criteria[P.NOT_MINE]

    # A routing keeps the document it was measured with.
    seen.clear()
    asyncio.run(P.evaluate(manager, StoreStub(), [{"session": "s1", "thread": "th-a"}],
                           "a message", cfg={}, ask=ask))
    state, qs = seen[0]
    assert state["message"] == "a message" and "topic" not in state
    assert "workspace" not in state["session"] and "issues" not in state["session"]
    assert "PR #1 merged" not in state["briefing"]
    assert qs[P.QUESTION_ID]["instructions"] == P.INSTRUCTIONS


def test_t11_every_logit_comes_back_from_one_call_per_member():
    calls = []
    scores = {"s1": 0.95, "s2": 0.8, "s3": 0.3}

    async def ask(cfg, state, questions, model):
        name = state["session"]["name"]
        calls.append(name)
        p = scores[name]
        return {"model": model, "usage": {"input_tokens": 1},
                "answers": {P.QUESTION_ID: {"type": "choice", "choice": "mine", "confidence": p,
                                            "probabilities": {"mine": p, "not_mine": 1 - p}}}}

    class NoThreads:
        def list(self, name, closed=False):
            return {"threads": []}

    out = asyncio.run(P.draft(FakeManager([FakeSession(n) for n in scores]), NoThreads(),
                              list(scores), "주제", cfg={}, ask=ask))
    assert sorted(calls) == ["s1", "s2", "s3"], "one call per member, no more"
    rows = {r["session"]: r for r in out["members"]}
    # Not only the recommended one: a member under the bar keeps its score,
    # which is what the form's slider moves over.
    for name, p in scores.items():
        assert rows[name]["logit"] == pytest.approx(math.log(p / (1 - p)))
    assert out["recommended"] == ["s1"]
    assert out["threshold"] == pytest.approx(math.log(9))
