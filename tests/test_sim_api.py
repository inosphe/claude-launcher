"""HTTP routes for simulations (daemon/sim_api.py): caller identity, owner
notices (end of campaign, answers, user requests), exit-hook release."""

import asyncio
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher.daemon import sim_api
from claude_launcher.sim import store as S

SPEC = "The door opens when closed.\n"


class FakeSession:
    def __init__(self, name, cwd, project="proj"):
        self.sdef = SimpleNamespace(name=name, cwd=cwd, project=project)
        self.exited = False
        self.held_at = None
        self.delivered = []

    def queue_delivery(self, text):
        self.delivered.append(text)
        return True


class FakeManager:
    def __init__(self, sessions):
        self.sessions = {s.sdef.name: s for s in sessions}
        self.exit_hooks = []

    def get(self, name):
        if name not in self.sessions:
            raise KeyError(name)
        return self.sessions[name]

    def end(self, name):
        s = self.sessions[name]
        s.exited = True
        for hook in self.exit_hooks:
            hook(s)


PATCH = {
    "machines": [{"id": "door", "states": ["closed", "open"], "initial": "closed"}],
    "events": [{"name": "open", "kind": "actor"}],
    "rules": [{"id": "r.open", "on": {"event": "open"}, "when": {"sm": "door", "is": "closed"},
               "do": [{"sm": "door", "to": "open"}],
               "source": [{"ref": "spec.md", "quote": "The door opens when closed."}]}],
}


def run(tmp_path, scenario):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "spec.md").write_text(SPEC, encoding="utf-8")
    s1, s2 = FakeSession("s1", str(root)), FakeSession("s2", str(root))
    mgr = FakeManager([s1, s2])
    app = web.Application()
    app["manager"] = mgr
    sim_api.install(app, S.SimStore(tmp_path / "sims", live=lambda n: sim_api._live(mgr, n)))
    async def go():
        async with TestClient(TestServer(app)) as client:
            async def req(method, path, body=None, status=200):
                resp = await client.request(method, path, json=body)
                data = await resp.json()
                assert resp.status == status, data
                return data
            await scenario(req, mgr, s1, s2)

    asyncio.run(go())


def write_run_log(d, rows):
    """A run directory holding its ``<run>.events.jsonl`` (the layout a
    session's ``SIM_RUN_DIR`` names, e.g. gds6's sim2 runner)."""
    import json as _json
    d.mkdir(parents=True, exist_ok=True)
    (d / (d.name + ".events.jsonl")).write_text(
        "".join(_json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    return d


def run_dir_session(mgr, tmp_path, run_dir, name="sim-run-dir"):
    """A live session whose chart pane reads ``run_dir``."""
    sess = FakeSession(name, str(tmp_path))
    sess.sdef.env = {"SIM_RUN_DIR": str(run_dir)}
    mgr.sessions[name] = sess
    return name


def test_session_creates_owns_and_is_told_when_it_ends(tmp_path):
    async def scenario(req, mgr, s1, s2):
        made = await req("POST", "/api/sims", {"title": "door", "session": "s1",
                                               "settings": {"max_rounds": 1}}, status=201)
        sim = made["sim"]
        assert sim["owner"] == "s1" and sim["project"] == "proj" and sim["root"].endswith("repo")
        assert not made["nudged"] and not s1.delivered  # the session asked; nothing typed back
        sid = sim["id"]
        bad = await req("POST", f"/api/sims/{sid}/model", {"session": "s2", "patch": PATCH}, status=403)
        assert "owned by s1" in bad["error"]
        assert (await req("POST", f"/api/sims/{sid}/model", {"session": "s1", "patch": PATCH}))["ok"]
        rejected = await req("POST", f"/api/sims/{sid}/model", {"session": "s1", "patch": {
            "rules": [{**PATCH["rules"][0], "source": [{"ref": "spec.md", "quote": "nope"}]}]}})
        assert rejected["ok"] is False and rejected["rejected"]
        # No interpreter profile: the start faults and nothing runs (claunch-efnbt.8).
        faulted = await req("POST", f"/api/sims/{sid}/start", {"session": "s1"}, status=409)
        assert faulted["fault"] == "no-profile" and "interpreter profile" in faulted["error"]
        assert faulted["sim"]["state"] == "fault" and faulted["sim"]["state_reason"] == faulted["error"]
        assert not faulted["sim"]["last_round"] and not s1.delivered
        # A single walk stays available to the owner.
        await req("POST", f"/api/sims/{sid}/round", {"session": "s1"})
        # Another session reads it.
        shown = await req("GET", f"/api/sims/{sid}")
        assert shown["rounds"][-1]["round"] == 1 and shown["owner_live"]
        assert (await req("GET", f"/api/sims/{sid}/findings?status=open"))["total"] >= 1
        assert (await req("GET", f"/api/sims/{sid}/gaps"))["batches"]
    run(tmp_path, scenario)


def test_rail_list_exposes_ownership_with_compact_metadata(tmp_path):
    async def scenario(req, mgr, s1, s2):
        made = await req("POST", "/api/sims", {"title": "door", "session": "s1"}, status=201)
        sim = made["sim"]
        rows = (await req("GET", "/api/sims?view=rail&owner=s1&project=proj"))["sims"]
        assert rows == [{
            **{k: sim[k] for k in ("id", "owner", "title", "state", "updated_at", "run_rounds")},
            "max_rounds": sim["settings"]["max_rounds"], "runner_session": None,
        }]
        assert (await req("GET", "/api/sims?view=rail&owner=s2"))["sims"] == []
        assert "model_stats" in (await req("GET", "/api/sims"))["sims"][0]
    run(tmp_path, scenario)


def test_user_creates_for_a_session_and_answers(tmp_path):
    async def scenario(req, mgr, s1, s2):
        made = await req("POST", "/api/sims", {"title": "door", "goal": "find gaps", "owner": "s2"}, status=201)
        sid = made["sim"]["id"]
        assert made["nudged"] and "sim_show " + sid in s2.delivered[-1]
        assert "no interpreter profile yet" in s2.delivered[-1] and f"before sim_start {sid}" in s2.delivered[-1]
        assert "write them in 한국어" in s2.delivered[-1]
        await req("POST", f"/api/sims/{sid}/model", {"session": "s2", "patch": PATCH})
        await req("POST", f"/api/sims/{sid}/round", {"session": "s2"})
        key = "unhandled|door|open|open"
        await req("POST", f"/api/sims/{sid}/resolve", {"session": "s2", "items": [
            {"key": key, "action": "ask", "question": "and then?"}]})
        await req("POST", f"/api/sims/{sid}/answer", {"session": "s2", "key": key, "text": "x"}, status=403)
        ans = await req("POST", f"/api/sims/{sid}/answer", {"key": key, "text": "stays open"})
        assert ans["nudged"] and "stays open" in s2.delivered[-1]
        n = await req("POST", f"/api/sims/{sid}/nudge", {"text": "look at the lock too"})
        assert n["to"] == "s2" and "look at the lock too" in s2.delivered[-1]
    run(tmp_path, scenario)


def test_held_owner_keeps_ownership(tmp_path):
    async def scenario(req, mgr, s1, s2):
        sid = (await req("POST", "/api/sims", {"title": "door", "session": "s1"}, status=201))["sim"]["id"]
        s1.exited, s1.held_at = True, "2026-10-11T00:00:00+00:00"
        assert sim_api._live(mgr, "s1")
        await req("POST", f"/api/sims/{sid}/claim", {"session": "s2"}, status=409)
    run(tmp_path, scenario)


def test_exit_releases_and_user_transfers(tmp_path):
    async def scenario(req, mgr, s1, s2):
        sid = (await req("POST", "/api/sims", {"title": "door", "session": "s1"}, status=201))["sim"]["id"]
        await req("POST", f"/api/sims/{sid}/model", {"session": "s1", "patch": PATCH})
        await req("POST", f"/api/sims/{sid}/start", {"session": "s1"}, status=409)
        await req("POST", f"/api/sims/{sid}/claim", {"session": "s2"}, status=409)
        mgr.end("s1")
        got = (await req("GET", f"/api/sims/{sid}"))["sim"]
        assert got["owner"] == "" and got["state"] == "fault"
        await req("POST", f"/api/sims/{sid}/transfer", {"session": "s2", "to": "s2"}, status=403)
        moved = await req("POST", f"/api/sims/{sid}/transfer", {"to": "s2"})
        assert moved["sim"]["owner"] == "s2" and "owner of simulation" in s2.delivered[-1]
        await req("POST", f"/api/sims/{sid}/nudge", {}, status=200)
        await req("POST", f"/api/sims/{sid}/release", {"session": "s2"})
        await req("POST", f"/api/sims/{sid}/nudge", {}, status=409)
        listed = await req("GET", "/api/sims?project=proj")
        assert [s["id"] for s in listed["sims"]] == [sid]
        await req("DELETE", f"/api/sims/{sid}")
        await req("GET", f"/api/sims/{sid}", status=404)
    run(tmp_path, scenario)


def test_user_accepts_an_edit_proposal_and_the_owner_is_told(tmp_path):
    async def scenario(req, mgr, s1, s2):
        sid = (await req("POST", "/api/sims", {"title": "door", "session": "s1"}, status=201))["sim"]["id"]
        await req("POST", f"/api/sims/{sid}/model", {"session": "s1", "patch": PATCH})
        await req("POST", f"/api/sims/{sid}/round", {"session": "s1"})
        key = "unhandled|door|open|open"
        item = {"file": "spec.md", "old": "", "new": "Opening an open door does nothing.", "kind": "complete",
                "reason": "user answer", "findings": [key]}
        await req("POST", f"/api/sims/{sid}/proposals", {"session": "s2", "items": [item]}, status=403)
        made = await req("POST", f"/api/sims/{sid}/proposals", {"session": "s1", "items": [item]})
        pid = made["done"][0]
        assert (await req("GET", f"/api/sims/{sid}/proposals"))["proposals"][0]["id"] == pid
        await req("POST", f"/api/sims/{sid}/proposals/{pid}/decide", {"session": "s1", "accept": True}, status=403)
        res = await req("POST", f"/api/sims/{sid}/proposals/{pid}/decide", {"accept": True, "note": "go"})
        assert res["nudged"] and pid in s1.delivered[-1] and "append to spec.md" in s1.delivered[-1]
        assert "Opening an open door does nothing." in s1.delivered[-1]
    run(tmp_path, scenario)


def test_run_session_events_are_read_by_session_and_by_tail(tmp_path):
    """The run pane of a pi-durable session reads its runner's event log
    (claunch-neeya): only that session's lines, and only the tail it lacks."""
    import json as _json

    async def scenario(req, mgr, s1, s2):
        sid = (await req("POST", "/api/sims", {"title": "door", "session": "s1"}, status=201))["sim"]["id"]
        log = tmp_path / "sims" / sid / "durable.events.jsonl"
        rows = [
            {"session": f"{sid}-run1", "event": "session_start"},
            {"session": f"{sid}-run1", "event": "ask", "role": "B", "key": "formalize:1"},
            {"session": f"{sid}-run2", "event": "session_start"},
            {"session": f"{sid}-run1", "event": "formalize", "attempt": 1, "ok": True},
        ]
        log.write_text("".join(_json.dumps(r) + "\n" for r in rows) + '{"session": "half', encoding="utf-8")
        got = await req("GET", f"/api/sessions/{sid}-run1/durable")
        assert [e["event"] for e in got["events"]] == ["session_start", "ask", "formalize"]
        assert got["total"] == 3 and got["sim"] == sid and got["live"] is False
        assert got["sessions"] == [f"{sid}-run1", f"{sid}-run2"]
        tail = await req("GET", f"/api/sessions/{sid}-run1/durable?since=2")
        assert [e["event"] for e in tail["events"]] == ["formalize"] and tail["since"] == 2
        # A live run session is found by its env, whatever its name.
        run = FakeSession("renamed", str(tmp_path))
        run.sdef.env = {"CLAUNCH_SIM_ID": sid}
        mgr.sessions["renamed"] = run
        live = await req("GET", "/api/sessions/renamed/durable")
        assert live["live"] is True and live["events"] == [] and live["total"] == 0
        assert (await req("GET", f"/api/sims/{sid}/events?since=1"))["total"] == 4
        await req("GET", "/api/sessions/s1/durable", status=404)
        await req("GET", "/api/sessions/sim-00000000-run1/durable", status=404)
        await req("GET", f"/api/sessions/{sid}-run1/durable?since=x", status=400)
    run(tmp_path, scenario)


def test_run_dir_session_events_are_read_from_its_run_directory(tmp_path):
    """A pi-durable session that names its run directory in SIM_RUN_DIR
    (gds6's sim2 runner, no CLAUNCH_SIM_ID) is read from
    ``<run>/<run>.events.jsonl`` -- the chart pane used to get a 404
    "is not a simulation run session" for it (claunch-neeya.1)."""
    import json as _json

    async def scenario(req, mgr, s1, s2):
        run_dir = tmp_path / "runs" / "20261013-040416-simulation"
        run_dir.mkdir(parents=True)
        rows = [
            {"role": "sim-run", "event": "session_start", "reason": "startup"},
            {"role": "sim-run", "event": "run_start", "rounds": 1},
            {"role": "sim-run", "event": "ask", "askRole": "B", "key": "B:clash|4bf:0"},
        ]
        (run_dir / f"{run_dir.name}.events.jsonl").write_text(
            "".join(_json.dumps(r) + "\n" for r in rows) + '{"event": "as', encoding="utf-8")
        name = "sim-20261013-040416-simulation"
        sess = FakeSession(name, str(tmp_path))
        sess.sdef.env = {"SIM_ROLE": "sim-run", "SIM_RUN_DIR": str(run_dir)}
        mgr.sessions[name] = sess
        got = await req("GET", f"/api/sessions/{name}/durable")
        assert [e["event"] for e in got["events"]] == ["session_start", "run_start", "ask"]
        assert got["total"] == 3 and got["sim"] == run_dir.name and got["live"] is True
        assert got["run_dir"] == str(run_dir)
        tail = await req("GET", f"/api/sessions/{name}/durable?since=2")
        assert [e["event"] for e in tail["events"]] == ["ask"] and tail["since"] == 2
        # A run directory without its log is a 404, and so is a session with neither env.
        empty = FakeSession("empty", str(tmp_path))
        empty.sdef.env = {"SIM_RUN_DIR": str(tmp_path / "nowhere")}
        mgr.sessions["empty"] = empty
        await req("GET", "/api/sessions/empty/durable", status=404)
        miss = await req("GET", "/api/sessions/s1/durable", status=404)
        assert "SIM_RUN_DIR" in miss["error"]
    run(tmp_path, scenario)


def test_run_dir_session_reports_the_run_it_continued_from(tmp_path):
    """A run that forked from an earlier one logs ``continued`` (gds6-j936.8);
    the chart is answered with that run on the first poll only -- a tail poll
    asks for the live run alone (claunch-0n5jr)."""
    async def scenario(req, mgr, s1, s2):
        runs = tmp_path / "runs"
        prev = write_run_log(runs / "r1", [
            {"event": "run_start", "rounds": 1},
            {"event": "run_end", "state": "done"},
        ])
        cur = write_run_log(runs / "r2", [
            {"event": "continued", "from": {"runId": "r1", "runDir": str(prev), "round": 26}},
            {"event": "run_start", "rounds": 1},
        ])
        name = run_dir_session(mgr, tmp_path, cur)
        got = await req("GET", f"/api/sessions/{name}/durable")
        assert [e["event"] for e in got["events"]] == ["continued", "run_start"]
        assert [a["runId"] for a in got["ancestors"]] == ["r1"]
        assert got["ancestors"][0]["round"] == 26
        assert [e["event"] for e in got["ancestors"][0]["events"]] == ["run_start", "run_end"]
        tail = await req("GET", f"/api/sessions/{name}/durable?since=1")
        assert [e["event"] for e in tail["events"]] == ["run_start"]
        assert "ancestors" not in tail
    run(tmp_path, scenario)


def test_continued_chain_is_capped_and_returned_oldest_first(tmp_path):
    """A chain of forks is followed from the newest run back, up to five
    ancestors, and reported oldest first (claunch-0n5jr)."""
    async def scenario(req, mgr, s1, s2):
        runs = tmp_path / "runs"
        prev = write_run_log(runs / "r0", [{"event": "run_start"}])
        for i in range(1, 7):
            rows = [{"event": "run_start"}] if i == 0 else [
                {"event": "continued", "from": {"runId": f"r{i - 1}", "runDir": str(prev), "round": i - 1}},
                {"event": "run_start"},
            ]
            prev = write_run_log(runs / f"r{i}", rows)
        name = run_dir_session(mgr, tmp_path, prev)
        got = await req("GET", f"/api/sessions/{name}/durable")
        assert [a["runId"] for a in got["ancestors"]] == ["r1", "r2", "r3", "r4", "r5"]
        assert [a["round"] for a in got["ancestors"]] == [1, 2, 3, 4, 5]
        assert all(a["events"] for a in got["ancestors"])
    run(tmp_path, scenario)


def test_continued_dir_outside_the_runs_folder_is_ignored(tmp_path):
    """``runDir`` comes from a log file: only a directory beside this run's
    own -- the same ``runs/`` folder -- is read, and a missing one is skipped
    quietly (claunch-0n5jr)."""
    async def scenario(req, mgr, s1, s2):
        outside = write_run_log(tmp_path / "elsewhere" / "r9", [{"event": "run_start"}])
        missing = tmp_path / "runs" / "gone"
        cur = write_run_log(tmp_path / "runs" / "r2", [
            {"event": "continued", "from": {"runId": "r9", "runDir": str(outside), "round": 9}},
        ])
        name = run_dir_session(mgr, tmp_path, cur)
        assert (await req("GET", f"/api/sessions/{name}/durable"))["ancestors"] == []
        cur = write_run_log(tmp_path / "runs" / "r2", [
            {"event": "continued", "from": {"runId": "gone", "runDir": str(missing), "round": 8}},
        ])
        assert (await req("GET", f"/api/sessions/{name}/durable"))["ancestors"] == []
        # A log that continued itself is not its own ancestor either.
        cur = write_run_log(tmp_path / "runs" / "r2", [
            {"event": "continued", "from": {"runId": "r2", "runDir": str(cur), "round": 7}},
        ])
        assert (await req("GET", f"/api/sessions/{name}/durable"))["ancestors"] == []
    run(tmp_path, scenario)


def test_a_run_without_continued_has_no_ancestors(tmp_path):
    """A run that continued nothing is answered exactly as before, plus an
    empty ``ancestors`` on the first poll (claunch-0n5jr)."""
    async def scenario(req, mgr, s1, s2):
        cur = write_run_log(tmp_path / "runs" / "r2", [{"event": "run_start"}])
        name = run_dir_session(mgr, tmp_path, cur)
        got = await req("GET", f"/api/sessions/{name}/durable")
        assert got["ancestors"] == []
        assert [e["event"] for e in got["events"]] == ["run_start"]
    run(tmp_path, scenario)


def test_user_leaves_title_and_goal_for_the_owner_to_fill(tmp_path):
    """「세션에 맡기기」: no run, a fill request to the owner, start refused
    until sim_configure has given the title and the goal."""
    async def scenario(req, mgr, s1, s2):
        made = await req("POST", "/api/sims", {"title": "", "goal": "", "owner": "s2", "fill": True,
                                               "sources": ["spec.md"],
                                               "settings": {"max_rounds": 3}}, status=201)
        sim = made["sim"]
        sid = sim["id"]
        assert sim["fill_pending"] is True and sim["title"] == "" and sim["state"] == "idle"
        assert sim["settings"]["max_rounds"] == 3 and sim["sources"] == ["spec.md"]
        told = s2.delivered[-1]
        assert made["nudged"] and f"sim_configure {sid}" in told and "title and goal" in told
        assert "sources)" not in told  # the user gave the sources
        # The rail row says it is waiting, and has no title to show.
        row = (await req("GET", "/api/sims?view=rail&owner=s2"))["sims"][0]
        assert row["fill_pending"] is True and row["title"] == ""
        # Nobody runs it before the owner has named it.
        refused = await req("POST", f"/api/sims/{sid}/start", {"session": "s2"}, status=409)
        assert "title and goal" in refused["error"]
        await req("POST", f"/api/sims/{sid}/start", {}, status=409)
        # A single round is refused the same way.
        once = await req("POST", f"/api/sims/{sid}/round", {"session": "s2"}, status=409)
        assert "title and goal" in once["error"]
        # An empty nudge repeats the fill request.
        await req("POST", f"/api/sims/{sid}/nudge", {})
        assert f"sim_configure {sid}" in s2.delivered[-1]
        # Half filled is still waiting.
        half = await req("POST", f"/api/sims/{sid}/settings", {"session": "s2", "title": "door"})
        assert half["sim"]["fill_pending"] is True
        await req("POST", f"/api/sims/{sid}/start", {"session": "s2"}, status=409)
        done = await req("POST", f"/api/sims/{sid}/settings", {"session": "s2", "goal": "find gaps"})
        assert "fill_pending" not in done["sim"]
        # Filled, it gets past the fill check to the interpreter check.
        faulted = await req("POST", f"/api/sims/{sid}/start", {"session": "s2"}, status=409)
        assert faulted["fault"] == "no-profile" and faulted["sim"]["state"] == "fault"
    run(tmp_path, scenario)


def test_fill_with_a_title_given_asks_only_for_the_goal(tmp_path):
    async def scenario(req, mgr, s1, s2):
        made = await req("POST", "/api/sims", {"title": "door", "owner": "s2", "fill": True}, status=201)
        assert made["sim"]["fill_pending"] is True
        told = s2.delivered[-1]
        assert "left its goal for you" in told and "title: door" in told and "as sources" in told
        # Both given: the fill flag has nothing to wait for; the usual create request goes out.
        full = await req("POST", "/api/sims", {"title": "door", "goal": "g", "owner": "s2", "fill": True}, status=201)
        assert "fill_pending" not in full["sim"] and "left its" not in s2.delivered[-1]
        # Without an owner nobody would fill it.
        await req("POST", "/api/sims", {"title": "", "fill": True}, status=400)
        # And without the flag a title is still required.
        await req("POST", "/api/sims", {"title": "", "owner": "s2"}, status=400)
    run(tmp_path, scenario)


def test_defaults_are_saved_checked_and_used_by_new_simulations(tmp_path, monkeypatch):
    from claude_launcher import store as config_store
    monkeypatch.setattr(sim_api, "pi_profiles", lambda: ["work", "judge"])
    monkeypatch.setattr(sim_api, "pi_models", lambda names: {"work": ["w-big", "w-small"], "judge": ["j-1"]})

    async def scenario(req, mgr, s1, s2):
        bad = [
            {"interpreter_profile": "nope"},
            {"interpreter_profile": "work", "interpreter_model": "j-1"},
            {"interpreter_model": "w-big"},
            {"interpreter_profile": "work", "judge_model": "j-1"},  # no judge profile: a model of "work"
            {"interpreter_profile": 3},
        ]
        for body in bad:
            await req("PUT", "/api/sims/defaults", body, status=400)
        saved = await req("PUT", "/api/sims/defaults", {
            "interpreter_profile": "work", "interpreter_model": "w-big",
            "judge_profile": "", "judge_model": "w-small"})
        assert saved["defaults"] == {"interpreter_profile": "work", "interpreter_model": "w-big",
                                     "judge_profile": "", "judge_model": "w-small"}
        daemon = config_store.load().get("daemon") or {}
        assert daemon["simulation_interpreter_model"] == "w-big" and "simulation_judge_profile" not in daemon
        prof = await req("GET", "/api/sims/profiles")
        assert prof["defaults"]["judge_model"] == "w-small"
        made = (await req("POST", "/api/sims", {"title": "door", "owner": "s2"}, status=201))["sim"]
        assert {k: made["settings"][k] for k in sim_api.DEFAULT_KEYS} == saved["defaults"]
        # Another interpreter profile and no model: the default model is not that profile's.
        other = (await req("POST", "/api/sims", {"title": "door", "owner": "s2",
                                                 "settings": {"interpreter_profile": "judge"}}, status=201))["sim"]
        assert other["settings"]["interpreter_model"] == "" and other["settings"]["judge_model"] == ""
        # A judge profile of its own: the interpreter keeps its default model, the judge does not.
        own = (await req("POST", "/api/sims", {"title": "door", "owner": "s2",
                                               "settings": {"judge_profile": "judge"}}, status=201))["sim"]
        assert own["settings"]["interpreter_model"] == "w-big" and own["settings"]["judge_model"] == ""
        # Clearing removes the keys.
        await req("PUT", "/api/sims/defaults", {})
        assert not any(k.startswith("simulation_") for k in (config_store.load().get("daemon") or {}))
    run(tmp_path, scenario)


# -- purpose: spec | design (claunch-efnbt.10) ---------------------------------

def test_purpose_warns_a_source_check_without_sources_and_labels_the_notice(tmp_path):
    # Four cases: spec/design x sources named/none. Only spec without sources
    # warns; the purpose never refuses a create or a start (here the start
    # faults on the missing profile alone, claunch-efnbt.8), and the warning
    # rides on the create response, the record (start, screen) and the notice.
    async def scenario(req, mgr, s1, s2):
        for purpose in ("spec", "design"):
            for sources in ([], ["spec.md"]):
                made = await req("POST", "/api/sims", {"title": "door", "session": "s1", "sources": sources,
                                                       "settings": {"max_rounds": 1, "purpose": purpose}}, status=201)
                warned = purpose == "spec" and not sources
                assert bool(made["warnings"]) is warned, (purpose, sources)
                assert made["sim"]["purpose"]["warnings"] == made["warnings"]
                sid = made["sim"]["id"]
                await req("POST", f"/api/sims/{sid}/model", {"session": "s1", "patch": PATCH})
                started = await req("POST", f"/api/sims/{sid}/start", {"session": "s1"}, status=409)
                assert started["fault"] == "no-profile"
                assert started["sim"]["purpose"]["warnings"] == made["warnings"]
                await req("POST", f"/api/sims/{sid}/round", {"session": "s1"})
                notice = sim_api.ended_text((await req("GET", f"/api/sims/{sid}"))["sim"])
                label = "원천 확인" if purpose == "spec" else "설계 검토 — 원천 확인 아님"
                assert f"Purpose: 「{label}」 ({purpose})" in notice
                assert "규칙 1개 중 0개가 원천 없음(provisional)" in notice
                assert ("sources를 지정하거나 purpose를 design으로 바꾸세요" in notice) is warned
        bad = await req("POST", "/api/sims", {"title": "x", "session": "s1", "settings": {"purpose": "audit"}}, status=400)
        assert "purpose must be one of spec, design" in bad["error"]
    run(tmp_path, scenario)


def test_user_created_source_check_without_sources_warns_the_owner(tmp_path):
    async def scenario(req, mgr, s1, s2):
        await req("POST", "/api/sims", {"title": "door", "owner": "s2"}, status=201)
        assert "purpose를 design으로 바꾸세요" in s2.delivered[-1]
        await req("POST", "/api/sims", {"title": "door", "owner": "s2", "settings": {"purpose": "design"}}, status=201)
        assert "purpose를" not in s2.delivered[-1]
    run(tmp_path, scenario)
