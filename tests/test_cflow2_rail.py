"""The session rail's cflow v2 line (``GET /api/cflow2/rail``).

Each registered session whose own directory holds a v2 run under the session's own
name gets one entry, keyed by the session: the workflow, where the run stands
and -- the part the rail exists for -- whether a person owes it something
(``gated``) and what (``owed``). A user choice and a loop limit are owed; a
running task is not. Sessions without a run are left out, and an entry is
recomputed only when the run's state or chart file changed.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from claude_launcher.cflow2 import executors, layers, runtime
from claude_launcher.daemon import api as api_mod, cflow2_approve, cflow2_web
from claude_launcher.daemon.manager import SessionManager

PICK = """\
cflow: 2
name: pick
description: a user choice, then a bounded loop
nodes:
  a: {title: Step A, prompt: do a}
  b: {title: Step B, prompt: do b}
  c: {title: Step C, prompt: do c}
body:
  seq:
    - choose:
        prompt: which way?
        chooser: user
        options:
          left: {then: a}
          right: {then: b}
    - loop: {while: "true", max: 1, do: c}
"""

ASKME = """\
cflow: 2
name: askme
description: an ask nobody is routed to, so it falls to a person
nodes:
  a: {title: Step A, prompt: do a, ask: {prompt: "may I a?"}}
  b: {title: Step B, prompt: do b}
body:
  try: [a, b]
  catch:
    declined: b
"""


#: A brief ask (format ``pick``) whose options, recommendation and
#: require_reason come from the question the previous task wrote, not from
#: the rail's defaults; ``otherwise: human`` lets it fall to a person.
ASKBRIEF = """cflow: 2
name: askbrief
description: a brief ask with declared options that falls to a person
question_types:
  pick:
    description: pick one
    options: options
    recommendation: rec
    fields:
      problem: {type: text, required: true, label: problem}
      options:
        type: list
        required: true
        min: 2
        label: options
        item:
          name: {type: name, required: true, label: name}
          summary: {type: str, required: true, label: summary}
      rec: {type: ref, of: options, required: true, label: recommendation}
nodes:
  frame:
    title: Frame
    prompt: frame
    returns: {ok: {brief: "question:pick"}}
  pose:
    title: Pose
    prompt: pose
    ask: {brief: "${result['frame'].value.brief}", format: pick, require_reason: true,
          otherwise: human}
body: [frame, pose]
"""

BRIEF = {
    "problem": "how wide?",
    "options": [{"name": "narrow", "summary": "keep it small"},
                {"name": "full", "summary": "cover everything"}],
    "rec": "NARROW",
}


@pytest.fixture
def proj(home, tmp_path, monkeypatch):
    d = tmp_path / "proj"
    v2 = d / ".claunch" / "workflows" / "v2"
    v2.mkdir(parents=True)
    (v2 / "pick.yaml").write_text(PICK, encoding="utf-8")
    (v2 / "askme.yaml").write_text(ASKME, encoding="utf-8")
    (v2 / "askbrief.yaml").write_text(ASKBRIEF, encoding="utf-8")
    monkeypatch.setattr(layers, "package_dir", lambda: tmp_path / "no-package-v2")
    monkeypatch.setattr(cflow2_web, "_RAIL", {})
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    monkeypatch.chdir(d)
    return d


def _rail(cwd, *names):
    return cflow2_web.rail_entries([(n, cwd) for n in names])


TYPED = """\
cflow: 2
name: typed
description: declares start inputs
inputs:
  goal: {type: text, default: ""}
  relation: {type: enum, choices: [stacked, independent], default: stacked}
  max_rounds: {type: int, required: false}
  scope: {type: list, default: []}
nodes:
  w: {title: W, prompt: work}
body: w
"""


def test_rail_entry_carries_the_start_inputs(proj):
    """``inputs`` (claunch-76iqh): the values the run was launched with, so
    "Spawn same setup" can seed a child's form from the rail row instead of
    the schema defaults."""
    cwd = str(proj)
    v2 = proj / ".claunch" / "workflows" / "v2"
    (v2 / "typed.yaml").write_text(TYPED, encoding="utf-8")
    runtime.start(layers.load("typed", cwd), cwd=cwd, scope="s1", caps={},
                  inputs={"goal": "carry the variables", "relation": "stacked",
                          "max_rounds": 5, "scope": ["src/a"]})
    r = _rail(cwd, "s1")["s1"]
    assert r["inputs"]["goal"] == "carry the variables"
    assert r["inputs"]["relation"] == "stacked"
    assert r["inputs"]["max_rounds"] == 5
    assert r["inputs"]["scope"] == ["src/a"]

    # A run started with nothing left out comes back as its schema default,
    # not as an absent key -- the child's box shows where the value stands.
    runtime.start(layers.load("typed", cwd), cwd=cwd, scope="s2", caps={})
    r = _rail(cwd, "s2")["s2"]
    assert r["inputs"]["relation"] == "stacked" and r["inputs"]["scope"] == []

    # A workflow that declares no inputs carries none.
    runtime.start(layers.load("pick", cwd), cwd=cwd, scope="s3", caps={})
    assert _rail(cwd, "s3")["s3"]["inputs"] is None


def test_rail_entry_follows_the_run_and_flags_what_a_person_owes(proj):
    cwd = str(proj)
    runtime.start(layers.load("pick", cwd), cwd=cwd, scope="s1", caps={})

    rows = _rail(cwd, "s1", "s2")
    assert set(rows) == {"s1"}  # s2 has no run here
    r = rows["s1"]
    assert r["workflow"] == "pick" and r["status"] == "running"
    assert r["scope"] == "s1@main" and r["cwd"] == str(runtime.RunStore(cwd, "s1").cwd)
    assert r["gated"] is True and len(r["owed"]) == 1
    assert r["owed"][0].startswith("choose: ")

    runtime.select(None, "left", cwd=cwd, scope="s1", caller=runtime.CALLER_USER)
    r = _rail(cwd, "s1")["s1"]
    assert r["gated"] is False and r["owed"] == []
    assert r["current"] == ["Step A"]

    task = runtime.status(cwd=cwd, scope="s1")["tasks"][0]["instance"]
    runtime.report(task, cwd=cwd, scope="s1")
    task = runtime.status(cwd=cwd, scope="s1")["tasks"][0]["instance"]
    runtime.report(task, cwd=cwd, scope="s1")
    r = _rail(cwd, "s1")["s1"]
    assert r["status"] == "waiting_approval"
    assert r["gated"] is True
    assert any(o.startswith("loop_limit: ") for o in r["owed"])


def test_rail_entry_carries_the_run_clocks(proj, monkeypatch):
    """``started``/``updated``/``entered_at`` (claunch-c2emv): the Chart page
    reads them to tell a run that is still moving from one that has sat in
    the same task, which ``current`` alone cannot show."""
    cwd = str(proj)
    runtime.start(layers.load("pick", cwd), cwd=cwd, scope="s1", caps={})
    r = _rail(cwd, "s1")["s1"]
    state = runtime.RunStore(cwd, "s1").load_state()
    (active,) = state["active"]
    assert r["started"] == state["started"]
    assert r["updated"] == state["updated"]
    assert r["entered_at"] == pytest.approx(state["inst"][active]["entered_at"])
    first = r["entered_at"]

    # Moving the run into a task replaces the active instance, so the stamp
    # moves with it -- that is what "still moving" means to the chart.
    runtime.select(None, "left", cwd=cwd, scope="s1", caller=runtime.CALLER_USER)
    r = _rail(cwd, "s1")["s1"]
    assert r["current"] == ["Step A"]
    assert r["entered_at"] > first
    moved = runtime.RunStore(cwd, "s1").load_state()
    assert r["updated"] == moved["updated"]
    assert r["updated"] >= state["updated"]  # the clock is second-resolution

    # A run that cannot be read still answers with the clock keys, as None,
    # so a client never has to tell "absent" from "no clock".
    def boom(self):
        raise RuntimeError("damaged slot")

    monkeypatch.setattr(cflow2_web.store2.RunStore, "load_state", boom)
    err = cflow2_web.rail_entry(cwd, "s1")
    assert err["status"] == "error"
    assert err["started"] is None and err["updated"] is None and err["entered_at"] is None


def test_rail_answers_carry_what_the_terminal_strip_posts(proj):
    """``answers`` (claunch-bpziz): each owed item with the instance and
    options the run page posts, so the terminal strip's press -- the same
    body -- moves the run."""
    cwd = str(proj)
    runtime.start(layers.load("pick", cwd), cwd=cwd, scope="s1", caps={})

    (a,) = _rail(cwd, "s1")["s1"]["answers"]
    assert a["kind"] == "choose" and a["prompt"] == "which way?"
    assert [o["name"] for o in a["options"]] == ["left", "right"]
    assert a["require_reason"] is False
    runtime.select(a["instance"], "left", cwd=cwd, scope="s1", caller=runtime.CALLER_USER)
    assert _rail(cwd, "s1")["s1"]["answers"] == []

    for _ in range(2):
        task = runtime.status(cwd=cwd, scope="s1")["tasks"][0]["instance"]
        runtime.report(task, cwd=cwd, scope="s1")
    (lim,) = _rail(cwd, "s1")["s1"]["answers"]
    assert lim["kind"] == "loop_limit" and lim["max"] == 1
    runtime.extend_loop(lim["instance"], 0, stop=True, cwd=cwd, scope="s1")
    r = _rail(cwd, "s1")["s1"]
    assert r["gated"] is False and r["answers"] == []


@pytest.mark.parametrize("decision, then", [("approve", "Step A"), ("decline", "Step B")])
def test_rail_ask_answer_names_the_instance_the_approve_handler_takes(
        proj, decision, then):
    """The ``ask`` answer (claunch-uebgg): an ask that fell to a person
    rides the rail with its instance, question and the default
    approve/decline presses, and the strip's body -- ``node`` = that
    instance, ``decision`` = the pressed option -- moves the run through the
    same handler as ``/api/cflow2/approve``."""
    executors.register(None)
    cwd = str(proj)
    try:
        runtime.start(layers.load("askme", cwd), cwd=cwd, scope="s1", caps={})
        r = _rail(cwd, "s1")["s1"]
        assert r["status"] == "waiting_approval" and r["gated"] is True
        (a,) = r["answers"]
        assert a["kind"] == "ask" and a["prompt"] == "may I a?"
        assert a["instance"] and a["label"] == "a"
        assert [o["name"] for o in a["options"]] == [runtime.ASK_APPROVE, runtime.ASK_DECLINE]
        assert a["require_reason"] is False and a["recommended"] is None

        cflow2_approve.approve(r["cwd"], r["scope"],
                               {"node": a["instance"], "decision": decision, "reason": ""},
                               nudge_fn=lambda *x: None)
        r = _rail(cwd, "s1")["s1"]
        assert r["gated"] is False and r["answers"] == []
        assert r["status"] == "running" and r["current"] == [then]
    finally:
        executors.register(None)



URGENT = """\
cflow: 2
name: urgent
description: an urgent ask routed to a reviewer first, then a plain ask
nodes:
  a: {title: Step A, prompt: do a,
      ask: {prompt: "start now?", urgent: true, from: [{role: reviewer}]}}
  b: {title: Step B, prompt: do b, ask: {prompt: "may I b?"}}
body: [a, b]
"""


def test_rail_urgent_asks_follow_an_urgent_ask_until_it_is_answered(proj, monkeypatch):
    """``urgent_asks`` (claunch-x76l8.1): the run's ``urgent: true`` asks,
    open and settled, so the dashboard puts one on its notification channel
    only while its resolution is ``human`` and records how it ended. A plain
    ask never appears there, and its rail answer stays unflagged."""
    executors.register(None)
    cwd = str(proj)
    v2 = proj / ".claunch" / "workflows" / "v2"
    (v2 / "urgent.yaml").write_text(URGENT, encoding="utf-8")
    try:
        runtime.start(layers.load("urgent", cwd), cwd=cwd, scope="s1", caps={})
        r = _rail(cwd, "s1")["s1"]
        (u,) = r["urgent_asks"]
        # still routed to the reviewer role: not the person's yet, and the
        # rail owes nothing -- the dashboard leaves it alone
        assert u["status"] == "open" and u["resolution"] != "human"
        assert u["label"] == "a" and r["answers"] == []

        st = runtime.RunStore(cwd, "s1")
        state = st.load_state()
        state["asks"][u["id"]]["resolution"] = runtime.RES_HUMAN
        for ap in state["approvals"]:
            ap["resolution"] = runtime.RES_HUMAN
        state["status"] = runtime.ST_WAITING
        st.save_state(state)
        r = _rail(cwd, "s1")["s1"]
        (u,) = r["urgent_asks"]
        assert u["resolution"] == "human"
        (a,) = r["answers"]
        assert a["kind"] == "ask" and a["ask"] == u["id"] and a["urgent"] is True

        cflow2_approve.approve(r["cwd"], r["scope"],
                               {"node": a["instance"], "decision": "approve", "reason": ""},
                               nudge_fn=lambda *x: None)
        r = _rail(cwd, "s1")["s1"]
        (u,) = r["urgent_asks"]
        assert u["status"] == "answered" and u["decision"] == "approve" and u["by"]

        task = runtime.status(cwd=cwd, scope="s1")["tasks"][0]["instance"]
        runtime.report(task, cwd=cwd, scope="s1")
        r = _rail(cwd, "s1")["s1"]
        assert [x["id"] for x in r["urgent_asks"]] == [u["id"]]  # b is not urgent
        (b,) = r["answers"]
        assert b["kind"] == "ask" and b["urgent"] is False
    finally:
        executors.register(None)


def test_rail_ask_answer_carries_the_briefs_options_recommendation_and_reason(proj):
    """The ask answer maps what the question declares (claunch-uebgg review):
    the brief's option names, its recommendation (read from the nested
    ``question``) and ``require_reason`` -- and the strip's body with a
    reason moves the run on the picked option."""
    executors.register(None)
    cwd = str(proj)
    try:
        runtime.start(layers.load("askbrief", cwd), cwd=cwd, scope="s1", caps={})
        task = runtime.status(cwd=cwd, scope="s1")["tasks"][0]["instance"]
        runtime.report(task, value={"brief": BRIEF}, cwd=cwd, scope="s1")
        r = _rail(cwd, "s1")["s1"]
        assert r["gated"] is True
        (a,) = r["answers"]
        assert a["kind"] == "ask" and a["label"] == "pose"
        names = [o["name"] for o in a["options"]]
        assert names[:2] == ["narrow", "full"]
        assert runtime.ASK_APPROVE not in names
        assert a["require_reason"] is True
        assert a["recommended"] == "narrow"

        cflow2_approve.approve(r["cwd"], r["scope"],
                               {"node": a["instance"], "decision": "full",
                                "reason": "the whole surface"},
                               nudge_fn=lambda *x: None)
        st = runtime.RunStore(cwd, "s1").load_state()
        assert st["result"]["pose"]["value"]["decision"] == "full"
        assert _rail(cwd, "s1")["s1"]["answers"] == []
    finally:
        executors.register(None)

def test_rail_prompt_is_one_bounded_line():
    flat = cflow2_web._rail_prompt("a\n  b\tc")
    assert flat == "a b c"
    long = cflow2_web._rail_prompt("x" * 1000)
    assert len(long) == cflow2_web.RAIL_PROMPT_MAX and long.endswith("…")


def test_rail_entry_is_recomputed_only_when_the_run_files_change(proj, monkeypatch):
    cwd = str(proj)
    runtime.start(layers.load("pick", cwd), cwd=cwd, scope="s1", caps={})
    calls = []
    real = cflow2_web.rail_entry
    monkeypatch.setattr(cflow2_web, "rail_entry",
                        lambda c, s: calls.append(s) or real(c, s))
    first = _rail(cwd, "s1")
    assert _rail(cwd, "s1") == first and calls == ["s1"]
    runtime.select(None, "left", cwd=cwd, scope="s1", caller=runtime.CALLER_USER)
    assert _rail(cwd, "s1")["s1"]["gated"] is False and calls == ["s1", "s1"]


@pytest.mark.parametrize("markers", [
    {"exited": False},
    {"exited": True},
    {"exited": True, "held_at": "2026-10-12T00:00:00+00:00"},
    {"exited": True, "paused_at": "2026-10-12T00:00:00+00:00"},
    {"exited": True, "archived_at": "2026-10-12T00:00:00+00:00"},
], ids=["live", "killed", "hold", "paused", "archived"])
def test_rail_endpoint_keys_entries_by_registered_session(proj, monkeypatch, markers):
    from aiohttp.test_utils import TestClient, TestServer

    cwd = str(proj)
    runtime.start(layers.load("pick", cwd), cwd=cwd, scope="s1", caps={})
    sessions = [SimpleNamespace(sdef=SimpleNamespace(name=n, cwd=c), **markers)
                for n, c in (("s1", cwd), ("s2", cwd), ("s3", ""))]

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=50, restore_default=True)
        monkeypatch.setattr(mgr, "list", lambda *a, **k: sessions)
        app = api_mod.build_app(mgr, "sekrit", started_at=time.monotonic())
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            unauth = (await client.get("/api/cflow2/rail")).status
            resp = await client.get("/api/cflow2/rail",
                                    headers={"Authorization": "Bearer sekrit"})
            return unauth, resp.status, await resp.json()
        finally:
            await client.close()

    unauth, status, body = asyncio.run(run())
    assert unauth == 401 and status == 200
    assert set(body["rows"]) == {"s1"}
    assert body["rows"]["s1"]["workflow"] == "pick"
    assert body["rows"]["s1"]["gated"] is True
