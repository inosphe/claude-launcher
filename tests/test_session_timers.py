"""The session timer line's daemon half (claunch-7dzae): the provider
registry in ``daemon/session_timers.py``, the cflow2 provider that reads a
run's held choices, closed windows, waits, pauses and ask deadlines
(``cflow2_web.run_timers`` / ``session_timers``), and the
``GET /api/sessions/<name>/timers`` route."""

from __future__ import annotations

import textwrap
import time

import pytest

from claude_launcher.cflow2 import executors, frontend, runtime
from claude_launcher.daemon import cflow2_web
from claude_launcher.daemon import session_timers as st


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    monkeypatch.delenv("CLAUNCH_SCRATCH", raising=False)
    executors.register(None)
    cflow2_web._TIMERS.clear()
    yield
    executors.register(None)
    cflow2_web._TIMERS.clear()


# -- the registry --------------------------------------------------------------------------

def test_timer_needs_a_due_time_and_a_known_state():
    assert st.timer("x", "k", "1", "l", None) is None
    assert st.timer("x", "k", "1", "l", "soon") is None
    t = st.timer("x", "k", 1, "", 12, since="3", scope="s", link="#/wf/a")
    assert t == {"source": "x", "kind": "k", "id": "1", "label": "k", "due": 12.0,
                 "since": 3.0, "state": "waiting", "detail": "", "scope": "s",
                 "link": "#/wf/a"}
    with pytest.raises(ValueError):
        st.timer("x", "k", "1", "l", 1, state="ringing")


def test_collect_sorts_soonest_first_and_isolates_a_failing_provider():
    seen = []

    def late(ctx):
        seen.append((ctx.session, ctx.cwd, ctx.now))
        return [st.timer("late", "a", "1", "b", 50), None]

    def soon(ctx):
        return [st.timer("soon", "a", "1", "a", 10)]

    def broken(ctx):
        raise RuntimeError("store unreadable")

    out = st.collect("s1", "/repo", now=5.0,
                     providers={"late": late, "broken": broken, "soon": soon})
    assert [t["source"] for t in out["timers"]] == ["soon", "late"]
    assert out["errors"] == [{"source": "broken", "error": "store unreadable"}]
    assert out["session"] == "s1" and out["now"] == 5.0
    assert seen == [("s1", "/repo", 5.0)]


def test_register_adds_a_provider_the_default_collect_asks(monkeypatch):
    monkeypatch.setattr(st, "PROVIDERS", {})
    st.register("extra", lambda ctx: [st.timer("extra", "clock", "c", "c", ctx.now + 1)])

    @st.register("deco")
    def deco(ctx):
        return []

    assert list(st.PROVIDERS) == ["extra", "deco"]
    assert [t["source"] for t in st.collect("s", "", now=1.0)["timers"]] == ["extra"]


def test_the_cflow2_provider_is_registered_and_needs_a_directory():
    assert "cflow2" in st.PROVIDERS
    assert st.cflow2_provider(st.Context(session="s", cwd="", now=0.0)) == []


# -- reading a status payload --------------------------------------------------------------

def test_run_timers_reads_every_kind_from_a_status_payload():
    payload = {
        "choices": [{
            "instance": "r/await@9", "node": "r/await", "label": "await-landing",
            "options": [
                {"name": "landed"},
                {"name": "nudge", "interval": 600, "window_opens_at": "1970-01-01T00:16:40Z"},
                {"name": "ping", "interval": 60, "window_opens_at": "1970-01-01T00:01:40Z"},
            ],
            "held": {"option": "nudge", "reason": "no answer\n yet", "since": 700.0,
                     "opens_at": "1970-01-01T00:16:40Z"},
        }],
        "waits": [
            {"instance": "w@3", "node": "settle", "kind": "wait", "describe": "board",
             "seconds": 60, "due": 160.0},
            {"instance": "p@4", "node": "poll-loop", "kind": "pause", "resume_at": 90.0},
            {"instance": "w@5", "node": "probe-only", "kind": "wait", "due": None},
        ],
        "asks": [{"instance": "a@6", "node": "review", "label": "review",
                  "asked": ["s2"], "deadline": 900.0},
                 {"instance": "a@7", "node": "human", "deadline": None}],
    }
    got = {t["kind"] + ":" + t["id"]: t for t in cflow2_web.run_timers(payload, "s1")}
    hold = got["hold:s1:r/await@9:nudge"]
    assert hold["label"] == "await-landing → nudge" and hold["state"] == "held"
    assert hold["due"] == 1000.0 and hold["since"] == 400.0 and hold["scope"] == "s1"
    assert "interval 600s" in hold["detail"] and "no answer yet" in hold["detail"]
    # the held option is not listed again as a closed window; another one is
    window = got["window:s1:r/await@9:ping"]
    assert window["state"] == "closed" and window["due"] == 100.0 and window["since"] == 40.0
    assert "window:s1:r/await@9:nudge" not in got
    assert got["wait:s1:w@3"]["since"] == 100.0 and got["wait:s1:w@3"]["detail"] == "board"
    assert got["pause:s1:p@4"]["due"] == 90.0
    assert got["ask:s1:a@6"]["due"] == 900.0 and "s2" in got["ask:s1:a@6"]["detail"]
    # no due time, no countdown
    assert "wait:s1:w@5" not in got and "ask:s1:a@7" not in got
    assert len(got) == 5


def test_run_timers_shows_a_batch_chip_in_place_of_the_window_and_hold_chips():
    # claunch-1o6az: "integrate batch 2/5" counts down to 300 s after the first request
    batch = {"collection": "landing", "status": "requested", "count": 2, "size": 5,
             "interval": 300, "first_at": "1970-01-01T00:01:40Z",
             "opens_at": "1970-01-01T00:06:40Z", "opens_epoch": 400.0}
    choice = {"instance": "r/standby@9", "node": "r/standby", "label": "standby",
              "options": [{"name": "integrate", "interval": 300,
                           "window_opens_at": "1970-01-01T00:06:40Z", "batch": batch},
                          {"name": "wind-down"}]}
    got = {t["kind"] + ":" + t["id"]: t
           for t in cflow2_web.run_timers({"choices": [choice]}, "s1")}
    chip = got["batch:s1:r/standby@9:integrate:batch"]
    assert chip["label"] == "integrate batch 2/5" and chip["due"] == 400.0
    assert chip["since"] == 100.0 and chip["state"] == "closed"
    assert len(got) == 1                      # no second chip for the same window
    choice["held"] = {"option": "integrate", "reason": "r", "since": 150.0,
                      "opens_at": "1970-01-01T00:06:40Z"}
    got = {t["kind"]: t for t in cflow2_web.run_timers({"choices": [choice]}, "s1")}
    assert list(got) == ["batch"] and got["batch"]["state"] == "held"
    # a batch the full size already opened has no countdown
    batch.update(count=5, opens_at=None, opens_epoch=None)
    assert cflow2_web.run_timers({"choices": [choice]}, "s1") == []


# -- a real run ----------------------------------------------------------------------------

AWAIT = """
    nodes:
      landing-nudge: {prompt: N}
    body:
      loop:
        max: 10
        while: "true"
        do:
          id: await-landing
          choose:
            options:
              landed: {then: {break: null}}
              nudge: {then: [landing-nudge], interval: 600}
"""


def _start(cwd, scope, now):
    chart = frontend.compile_text("cflow: 2\nname: t\n" + textwrap.dedent(AWAIT))
    runtime.start(chart, cwd=str(cwd), scope=scope, caps={}, now=now, driver=scope)


def _kw(cwd, scope, now):
    return {"cwd": str(cwd), "scope": scope, "now": now}


def test_a_held_nudge_and_its_window_reach_the_session_line(tmp_path):
    now = time.time()
    _start(tmp_path, "s1", now - 120)
    kw = _kw(tmp_path, "s1", now - 120)
    runtime.select(None, "nudge", "first", **kw)
    task = runtime.status(**kw)["tasks"][0]
    runtime.report(task["instance"], **kw)

    # back at await-landing with the nudge window closed: a countdown, nothing held
    out = st.collect("s1", str(tmp_path), now=now)
    assert [(t["kind"], t["state"]) for t in out["timers"]] == [("window", "closed")]
    win = out["timers"][0]
    assert abs(win["due"] - (now + 480)) <= 1 and win["since"] == pytest.approx(win["due"] - 600)
    assert win["link"].startswith("#/wf/s1%40main%7C")  # the slot names its daemon

    # picking nudge inside the window holds it: the same line now says held
    runtime.select(None, "nudge", "no answer yet", **_kw(tmp_path, "s1", now - 60))
    out = st.collect("s1", str(tmp_path), now=now)
    assert [(t["kind"], t["label"]) for t in out["timers"]] == [
        ("hold", "await-landing → nudge")]
    assert abs(out["timers"][0]["due"] - (now + 480)) <= 1
    assert out["timers"][0]["since"] == pytest.approx(out["timers"][0]["due"] - 600)
    assert out["errors"] == []


def test_only_the_sessions_own_runs_and_side_runs_are_read(tmp_path, monkeypatch):
    # A side run's slot is started only beside a main run whose definition
    # declares it (runtime.start_side), so the slot list and the payloads are
    # stubbed: what is under test is which slots the session reads.
    read = []
    monkeypatch.setattr(cflow2_web.store2, "list_scopes",
                        lambda cwd: ["other", "s1", "s1+stack", "s1~d1", "s10"])
    monkeypatch.setattr(cflow2_web, "_rail_stamp", lambda cwd, scope: (scope,))

    def status(cwd, scope, tick):
        read.append(scope)
        return {"waits": [{"instance": "w@1", "node": "n", "kind": "pause",
                           "resume_at": 5.0}]}

    monkeypatch.setattr(runtime, "status", status)
    got = cflow2_web.session_timers(str(tmp_path), "s1")
    assert read == ["s1", "s1+stack"]
    assert [t["scope"] for t in got] == ["s1", "s1+stack"]
    assert got[1]["link"] == "#/wf/s1%2Bstack%7C" + cflow2_web.quote(str(tmp_path), safe="")


def test_timers_are_cached_until_the_run_changes(tmp_path, monkeypatch):
    now = time.time()
    _start(tmp_path, "s1", now)
    calls = []
    real = runtime.status
    monkeypatch.setattr(runtime, "status", lambda **kw: calls.append(kw) or real(**kw))
    cflow2_web.session_timers(str(tmp_path), "s1")
    cflow2_web.session_timers(str(tmp_path), "s1")
    assert len(calls) == 1
    kw = _kw(tmp_path, "s1", now)
    runtime.select(None, "nudge", "first", **kw)  # writes the state file
    before = len(calls)
    cflow2_web.session_timers(str(tmp_path), "s1")
    assert len(calls) == before + 1
    cflow2_web.session_timers(str(tmp_path), "s1")
    assert len(calls) == before + 1


# -- the route -----------------------------------------------------------------------------

def test_session_timers_api(home, tmp_path):
    import asyncio
    from types import SimpleNamespace

    from aiohttp.test_utils import TestClient, TestServer

    from claude_launcher.daemon.api import build_app
    from claude_launcher.daemon.manager import SessionManager

    now = time.time()
    _start(tmp_path, "a", now - 120)
    kw = _kw(tmp_path, "a", now - 120)
    runtime.select(None, "nudge", "first", **kw)
    runtime.report(runtime.status(**kw)["tasks"][0]["instance"], **kw)
    runtime.select(None, "nudge", "still nothing", **_kw(tmp_path, "a", now - 60))

    async def run():
        manager = SessionManager(idle_threshold=0.5, scrollback=100, restore_default=False)
        known = {"a": SimpleNamespace(sdef=SimpleNamespace(name="a", cwd=str(tmp_path)))}
        real_get = manager.get
        manager.get = lambda name: known[name] if name in known else real_get(name)
        app = build_app(manager, "secret", started_at=time.monotonic())
        client = TestClient(TestServer(app))
        await client.start_server()
        headers = {"Authorization": "Bearer secret"}
        try:
            assert (await client.get("/api/sessions/a/timers")).status == 401
            resp = await client.get("/api/sessions/a/timers", headers=headers)
            assert resp.status == 200
            body = await resp.json()
            assert body["session"] == "a" and body["errors"] == []
            assert [(t["source"], t["kind"], t["state"]) for t in body["timers"]] == [
                ("cflow2", "hold", "held")]
            assert abs(body["timers"][0]["due"] - (now + 480)) <= 2
            missing = await client.get("/api/sessions/zz/timers", headers=headers)
            assert missing.status == 400
        finally:
            await client.close()
            await manager.shutdown_all()

    asyncio.run(run())
