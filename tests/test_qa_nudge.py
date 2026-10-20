"""Turn-end QA nudges (claunch-165w6): the request, the turn reader, the
clock's busy -> idle edge and its code-side skips, the Jev client, and the
settings door the web page uses.

No test reaches the real Jev endpoint: the clock's tests replace
``jev.system_one``; the client's tests stand up a local aiohttp server.
"""

from __future__ import annotations

import asyncio
import os
import stat
import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher import store
from claude_launcher.daemon import jev, qa_nudge
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.mesh import MeshManager

BEARER = {"Authorization": "Bearer sekrit"}


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #
class _Ready:
    def __init__(self, on: bool) -> None:
        self.on = on

    def is_set(self) -> bool:
        return self.on


class _FakeSession:
    def __init__(self, name: str, status: str = "idle", mcp: bool = True) -> None:
        self.sdef = SessionDef(name=name, cwd="/nonexistent/proj")
        self.status_value = status
        self.idle_for = 60.0
        self.mcp = mcp
        self.delivered: list = []
        self.deliver_ok = True
        self._last_human_input = 0.0

    def status(self, threshold=None):
        return self.status_value

    def idle_since(self):
        return self.idle_for if self.status_value == "idle" else None

    def mcp_ready_event(self):
        return _Ready(self.mcp)

    async def deliver(self, text, **kw):
        self.delivered.append(text)
        return self.deliver_ok


class _FakeManager:
    def __init__(self, *sessions) -> None:
        self.sessions = {s.sdef.name: s for s in sessions}

    def list(self):
        return list(self.sessions.values())

    def get(self, name):
        return self.sessions[name]


class _FakeQa:
    def __init__(self) -> None:
        self.seq = {}

    def rail(self):
        return {k: {"agent_seq": v} for k, v in self.seq.items()}


def _rec(seq, role, *blocks, sidechain=False):
    return {"seq": seq, "role": role, "sidechain": sidechain, "blocks": list(blocks)}


def _text(t):
    return {"type": "text", "text": t}


def _tool(name):
    return {"type": "tool_use", "name": name, "text": "{}"}


def _result():
    return {"type": "tool_result", "text": "ok"}


ASKING = [
    _rec(0, "user", _text("fix the bug")),
    _rec(1, "assistant", _tool("Bash")),
    _rec(2, "user", _result()),
    _rec(3, "assistant", _text("Two ways to fix it. Which one do you want, A or B?")),
]


@pytest.fixture
def on(home, monkeypatch):
    """Feature on, a key in the environment, transcripts and Jev faked."""
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    store.set_daemon_field("qa_nudge", True)
    monkeypatch.setenv("TYPESAFE_API_KEY", "k-test")
    state = {"records": list(ASKING), "calls": [], "answer": ("use_qa", 0.9)}

    def page(name, sdef, *, before=None, limit=40):
        return {"records": state["records"], "source": f"/t/{name}.jsonl"}

    async def system_one(cfg, st, questions, *, key=None, model=None):
        state["calls"].append((st, questions, key))
        choice, conf = state["answer"]
        return {"model": "jev-1.13.0", "usage": {"input_tokens": 300, "output_tokens": 20},
                "answers": {"action": {"type": "choice", "choice": choice,
                                       "confidence": conf,
                                       "probabilities": {choice: conf}}}}

    monkeypatch.setattr(qa_nudge.transcript_view, "page", page)
    monkeypatch.setattr(jev, "system_one", system_one)
    return state


def _clock(*sessions, qa=None):
    return qa_nudge.QaNudgeClock(_FakeManager(*sessions), qa, settle=5.0)


def _turn(clock, session, now=100.0):
    """One busy poll then one idle poll; returns what the idle poll made due."""
    session.status_value = "busy"
    assert clock.scan(now) == []
    session.status_value = "idle"
    return clock.scan(now + 10)


def _journal():
    path = qa_nudge.journal_path()
    if not path.exists():
        return []
    import json
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# --------------------------------------------------------------------------- #
# the request
# --------------------------------------------------------------------------- #
def test_request_is_one_choice_over_the_registry_with_none_first():
    state, questions = qa_nudge.build_request("Proceed?", 8000)
    assert state == {"agent_final_message": "Proceed?"}
    q = questions["action"]
    assert q["type"] == "choice"
    assert list(q["criteria"]) == [a.key for a in qa_nudge.ACTIONS]
    assert list(q["criteria"])[0] == "none"
    assert "`agent_final_message`" in q["instructions"]


def test_a_new_action_joins_the_choice():
    extra = qa_nudge.ACTIONS + (qa_nudge.NudgeAction("b", "criteria b", "msg b"),)
    _, questions = qa_nudge.build_request("x", 100, extra)
    assert questions["action"]["criteria"]["b"] == "criteria b"


def test_clip_keeps_head_and_tail_within_the_cap():
    text = "H" * 3000 + "M" * 20000 + "the question?"
    out = qa_nudge.clip(text, 8000)
    assert out.startswith("H" * 1500)
    assert out.endswith("M" * 100 + "the question?")
    assert "characters omitted" in out
    body = out.replace(out[out.index("\n["):out.index("]\n") + 2], "")
    assert len(body) == 8000
    assert qa_nudge.clip("short", 8000) == "short"


def test_decide_falls_back_to_none_under_the_threshold_or_on_unknown():
    def resp(choice, conf):
        return {"answers": {"action": {"choice": choice, "confidence": conf}}}
    assert qa_nudge.decide(resp("use_qa", 0.9), 0.6)[0].key == "use_qa"
    assert qa_nudge.decide(resp("use_qa", 0.5), 0.6)[0].key == "none"
    assert qa_nudge.decide(resp("bogus", 0.99), 0.6)[0].key == "none"
    assert qa_nudge.decide({}, 0.6)[0].key == "none"


# --------------------------------------------------------------------------- #
# the turn
# --------------------------------------------------------------------------- #
def test_read_turn_takes_the_final_text_after_the_last_user_text():
    turn = qa_nudge.read_turn(ASKING)
    assert turn.reason is None
    assert turn.text.startswith("Two ways")
    assert turn.seq == 3
    assert turn.used_qa is False


def test_read_turn_sees_qa_ask_in_any_harness_spelling():
    for name in ("mcp__claunch__qa_ask", "claunch.qa_update"):
        recs = ASKING[:3] + [_rec(3, "assistant", _tool(name)), _rec(4, "user", _result()),
                              _rec(5, "assistant", _text("Asked in the pane."))]
        assert qa_nudge.read_turn(recs).used_qa is True


def test_read_turn_ignores_qa_in_an_earlier_turn_and_sidechains():
    recs = [_rec(0, "user", _text("a")), _rec(1, "assistant", _tool("mcp__claunch__qa_ask")),
            _rec(2, "user", _text("answer")),
            _rec(3, "assistant", _text("sidechain"), sidechain=True),
            _rec(4, "assistant", _text("Shall I continue?"))]
    turn = qa_nudge.read_turn(recs)
    assert turn.used_qa is False and turn.text == "Shall I continue?"


def test_read_turn_skips_a_turn_that_ended_on_a_tool_call():
    recs = ASKING[:1] + [_rec(1, "assistant", _text("running"), _tool("Bash"))]
    assert qa_nudge.read_turn(recs).reason == "ended_without_text"
    assert qa_nudge.read_turn(ASKING[:1]).reason == "no_assistant_reply"


# --------------------------------------------------------------------------- #
# the clock
# --------------------------------------------------------------------------- #
def test_off_keeps_no_watches_and_judges_nothing(home):
    assert store.DAEMON_DEFAULTS["qa_nudge"] is False
    s = _FakeSession("s1")
    clock = _clock(s)
    assert _turn(clock, s) == []
    assert clock._watch == {}


def test_only_a_busy_to_idle_edge_is_judged(on):
    s = _FakeSession("s1")
    clock = _clock(s)
    # Idle from the start (feature just switched on): the old turn is left alone.
    assert clock.scan(0.0) == []
    assert clock.scan(10.0) == []
    # Not settled yet: idle for less than SETTLE.
    s.status_value = "busy"
    clock.scan(20.0)
    s.status_value, s.idle_for = "idle", 1.0
    assert clock.scan(30.0) == []
    s.idle_for = 6.0
    assert clock.scan(31.0) == ["s1"]
    # Pending: not due twice while its judgment runs.
    assert clock.scan(32.0) == []


def test_a_turn_that_starts_while_one_is_judged_is_not_lost(on):
    on["answer"] = ("none", 0.9)
    s = _FakeSession("s1")
    clock = _clock(s)
    assert _turn(clock, s) == ["s1"]
    s.status_value = "busy"          # a person typed while Jev was answering
    assert clock.scan(120.0) == []
    run(clock.judge("s1"))
    s.status_value = "idle"
    assert clock.scan(130.0) == ["s1"]


def test_use_qa_is_delivered_and_journaled(on):
    s = _FakeSession("s1")
    clock = _clock(s)
    assert _turn(clock, s) == ["s1"]
    assert run(clock.judge("s1")) == "use_qa"
    assert s.delivered == [qa_nudge.USE_QA_MESSAGE]
    st, questions, key = on["calls"][0]
    assert key == "k-test"
    assert st["agent_final_message"].startswith("Two ways")
    row = _journal()[-1]
    assert row["action"] == "use_qa" and row["delivered"] is True
    assert row["usage"]["input_tokens"] == 300
    # re-armed: the next turn needs a new busy edge
    assert clock.scan(500.0) == []


@pytest.mark.parametrize("answer", [("none", 0.95), ("use_qa", 0.4)])
def test_none_or_a_weak_answer_types_nothing(on, answer):
    on["answer"] = answer
    s = _FakeSession("s1")
    clock = _clock(s)
    _turn(clock, s)
    assert run(clock.judge("s1")) == "none"
    assert s.delivered == []


def test_code_side_skips_never_call_jev(on, monkeypatch):
    # no claunch MCP: qa_ask does not exist for this session
    s = _FakeSession("s1", mcp=False)
    clock = _clock(s)
    _turn(clock, s)
    assert run(clock.judge("s1")) is None
    assert _journal()[-1]["skip"] == "no_qa_tools"
    # the turn used the pane (transcript)
    s.mcp = True
    on["records"] = ASKING[:3] + [_rec(3, "assistant", _tool("mcp__claunch__qa_ask")),
                                  _rec(4, "user", _result()),
                                  _rec(5, "assistant", _text("Asked you in the QA pane?"))]
    _turn(clock, s)
    run(clock.judge("s1"))
    assert _journal()[-1]["skip"] == "used_qa"
    # no key anywhere
    on["records"] = list(ASKING)
    monkeypatch.delenv("TYPESAFE_API_KEY")
    _turn(clock, s)
    run(clock.judge("s1"))
    assert _journal()[-1]["skip"] == "no_key"
    assert on["calls"] == []


def test_a_qa_store_write_during_the_turn_counts_as_using_the_pane(on):
    qa = _FakeQa()
    qa.seq["s1"] = 4
    s = _FakeSession("s1")
    clock = _clock(s, qa=qa)
    _turn(clock, s)
    qa.seq["s1"] = 5  # the agent asked through the API (no tool name in the transcript)
    run(clock.judge("s1"))
    assert _journal()[-1]["skip"] == "used_qa"
    assert on["calls"] == []


def test_the_same_turn_is_judged_once(on):
    s = _FakeSession("s1")
    clock = _clock(s)
    on["answer"] = ("none", 0.9)
    _turn(clock, s)
    run(clock.judge("s1"))
    # a busy flicker with no new reply (e.g. a status redraw)
    _turn(clock, s, now=200.0)
    run(clock.judge("s1"))
    assert len(on["calls"]) == 1
    assert _journal()[-1]["skip"] == "already_judged"


def test_after_a_nudge_wait_for_a_person_and_the_cooldown(on):
    store.set_daemon_field("qa_nudge_cooldown", 0.0)
    s = _FakeSession("s1")
    clock = _clock(s)
    _turn(clock, s)
    run(clock.judge("s1"))
    assert len(s.delivered) == 1
    # the agent answered the nudge with another plain-text question
    on["records"] = on["records"] + [_rec(4, "user", _text("[claunch qa-nudge] ...")),
                                     _rec(5, "assistant", _text("Still: A or B?"))]
    _turn(clock, s)
    run(clock.judge("s1"))
    assert _journal()[-1]["skip"] == "awaiting_human"
    assert len(s.delivered) == 1
    # a person typed: judged again
    s._last_human_input = time.monotonic() + 1
    on["records"] = on["records"] + [_rec(6, "user", _text("hm")),
                                     _rec(7, "assistant", _text("A or B, then?"))]
    _turn(clock, s)
    run(clock.judge("s1"))
    assert len(s.delivered) == 2
    # cooldown blocks even after a person typed
    store.set_daemon_field("qa_nudge_cooldown", 3600.0)
    s._last_human_input = time.monotonic() + 2
    on["records"] = on["records"] + [_rec(8, "user", _text("?")),
                                     _rec(9, "assistant", _text("Which?"))]
    _turn(clock, s)
    run(clock.judge("s1"))
    assert _journal()[-1]["skip"] == "cooldown"


def test_no_nudge_into_a_session_that_moved_on(on, monkeypatch):
    s = _FakeSession("s1")
    clock = _clock(s)
    _turn(clock, s)

    async def busy_meanwhile(cfg, st, questions, *, key=None, model=None):
        s.status_value = "busy"
        return {"answers": {"action": {"choice": "use_qa", "confidence": 0.9}}}

    monkeypatch.setattr(jev, "system_one", busy_meanwhile)
    run(clock.judge("s1"))
    assert s.delivered == []
    assert _journal()[-1]["why"] == "no_longer_idle"


def test_jev_error_is_journaled_not_raised(on, monkeypatch):
    async def boom(cfg, st, questions, *, key=None, model=None):
        raise jev.JevError("Jev HTTP 422")
    monkeypatch.setattr(jev, "system_one", boom)
    s = _FakeSession("s1")
    clock = _clock(s)
    _turn(clock, s)
    assert run(clock.judge("s1")) is None
    assert _journal()[-1]["skip"] == "jev_error: Jev HTTP 422"


def test_the_clock_starts_and_stops(on):
    async def scenario():
        clock = qa_nudge.QaNudgeClock(_FakeManager(), poll=0.01)
        clock.start()
        await asyncio.sleep(0.05)
        assert clock.running
        await clock.shutdown()
        assert not clock.running
    run(scenario())


# --------------------------------------------------------------------------- #
# the Jev client
# --------------------------------------------------------------------------- #
def _jev_server(statuses, seen):
    async def handler(request):
        seen.append((request.headers.get("Authorization"), await request.json()))
        status = statuses.pop(0)
        if status != 200:
            return web.json_response({"error": "x"}, status=status)
        return web.json_response({"model": "jev-1.13.0", "usage": {},
                                  "answers": {"action": {"choice": "none"}}})
    app = web.Application()
    app.router.add_post("/v1/systemone", handler)
    return app


def test_client_posts_the_documented_shape_and_retries_429(home, monkeypatch):
    monkeypatch.setattr(jev, "RETRY_DELAY", 0.0)
    seen: list = []

    async def scenario():
        server = TestServer(_jev_server([429, 200], seen))
        await server.start_server()
        try:
            cfg = {"jev_base_url": str(server.make_url("/")), "jev_model": "jev-1.13.0"}
            out = await jev.system_one(cfg, {"a": 1}, {"q": {"type": "noul"}}, key="kk")
        finally:
            await server.close()
        return out

    out = run(scenario())
    assert out["answers"]["action"]["choice"] == "none"
    assert len(seen) == 2
    auth, body = seen[-1]
    assert auth == "Bearer kk"
    assert body == {"model": "jev-1.13.0", "state": {"a": 1},
                    "questions": {"q": {"type": "noul"}}}


def test_client_raises_without_echoing_the_key(home, monkeypatch):
    monkeypatch.setattr(jev, "RETRY_DELAY", 0.0)
    seen: list = []

    async def scenario():
        server = TestServer(_jev_server([401], seen))
        await server.start_server()
        try:
            cfg = {"jev_base_url": str(server.make_url("/"))}
            with pytest.raises(jev.JevError) as err:
                await jev.system_one(cfg, "s", {}, key="secret-key")
        finally:
            await server.close()
        return str(err.value)

    message = run(scenario())
    assert message == "Jev HTTP 401"
    with pytest.raises(jev.JevError):
        monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
        run(jev.system_one({}, "s", {}))


def test_key_file_wins_over_the_environment_and_is_private(home, monkeypatch):
    monkeypatch.setenv("MY_JEV", "from-env")
    cfg = {"jev_api_key_env": "MY_JEV"}
    assert jev.resolve_key(cfg) == ("from-env", "env:MY_JEV")
    jev.write_key_file("from-file")
    assert jev.resolve_key(cfg) == ("from-file", "file")
    if os.name == "posix":
        assert stat.S_IMODE(jev.key_path().stat().st_mode) == 0o600
    jev.write_key_file("")
    assert not jev.key_path().exists()
    assert jev.resolve_key(cfg)[1] == "env:MY_JEV"


# --------------------------------------------------------------------------- #
# the settings door
# --------------------------------------------------------------------------- #
def test_the_api_reads_and_writes_the_settings_without_the_key(home, monkeypatch):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)

    async def scenario():
        app = build_app(mgr, "sekrit", started_at=time.monotonic(), mesh=MeshManager(mgr))
        assert isinstance(app["qa_nudge_clock"], qa_nudge.QaNudgeClock)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.get("/api/qa-nudge", headers=BEARER)
            s = (await resp.json())["settings"]
            assert s["enabled"] is False and s["key_set"] is False
            assert s["jev_model"] == "jev-latest" and s["actions"] == ["none", "use_qa"]

            resp = await client.put("/api/qa-nudge", headers=BEARER, json={
                "enabled": True, "min_confidence": 0.75, "cooldown": 120,
                "max_chars": 4000, "jev_model": "jev-1.13.0", "api_key": "sk-1",
            })
            body = await resp.text()
            assert resp.status == 200, body
            assert "sk-1" not in body
            s = (await resp.json())["settings"]
            assert s["enabled"] is True and s["min_confidence"] == 0.75
            assert s["cooldown"] == 120.0 and s["max_chars"] == 4000
            assert s["key_set"] is True and s["key_source"] == "file"
            cfg = store.daemon_config()
            assert cfg["qa_nudge"] is True and cfg["jev_model"] == "jev-1.13.0"
            assert "sk-1" not in repr(cfg)

            # partial and clearing: blank model returns to the default
            resp = await client.put("/api/qa-nudge", headers=BEARER, json={"jev_model": ""})
            assert (await resp.json())["settings"]["jev_model"] == "jev-latest"
            assert store.daemon_config()["qa_nudge_min_confidence"] == 0.75

            for bad in ({"min_confidence": 2}, {"max_chars": "x"}, {"api_key": 3},
                        {"jev_model": 5}):
                resp = await client.put("/api/qa-nudge", headers=BEARER, json=bad)
                assert resp.status == 400, bad

            resp = await client.put("/api/qa-nudge", headers=BEARER, json={"api_key": ""})
            assert (await resp.json())["settings"]["key_set"] is False
        finally:
            await client.close()

    run(scenario())
