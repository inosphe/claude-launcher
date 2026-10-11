"""The simulation run session (S2): the daemon side (runner attach, authority,
verdicts, work, done, exit) and the real campaign loop of
``sim/pi/campaign.mjs`` driven against the real routes with scripted model
answers. The pi/pi-durable half (runner.mjs) needs the installed runtime and
a model; it is exercised by a live run, not here."""

import asyncio
import json
import shutil
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher.daemon import sim_api
from claude_launcher.sim import runtime
from claude_launcher.sim import store as S

SPEC = "The door opens when closed.\n"
CAMPAIGN = Path(sim_api.__file__).resolve().parents[1] / "sim" / "pi" / "campaign.mjs"


class FakeSession:
    def __init__(self, name, cwd):
        self.sdef = SimpleNamespace(name=name, cwd=cwd, project="proj")
        self.exited = False
        self.held_at = None
        self.delivered = []
        self.killed = False

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

    def kill(self, name, force=False):
        self.end(name)

    def end(self, name):
        s = self.sessions[name]
        s.killed = s.exited = True
        for hook in self.exit_hooks:
            hook(s)


def setup(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "spec.md").write_text(SPEC, encoding="utf-8")
    owner, run = FakeSession("s1", str(root)), FakeSession("sim-run", str(root))
    mgr = FakeManager([owner, run])
    app = web.Application()
    app["manager"] = mgr
    sims = sim_api.install(app, S.SimStore(tmp_path / "sims", live=lambda n: sim_api._live(mgr, n)))
    return app, sims, mgr, owner, run, root


def started(sims, settings=None):
    sim = sims.create(title="door", owner="s1", actor="s1", root=sims_root(sims), settings=settings or {})
    sims.start(sim["id"], "s1", round=False)
    sims.attach_runner(sim["id"], "sim-run")
    return sim["id"]


def sims_root(sims):
    return str(sims.root.parent / "repo")


def test_runner_acts_for_owner_and_done_tells_owner(tmp_path):
    app, sims, mgr, owner, run, root = setup(tmp_path)
    sid = started(sims)

    async def go():
        async with TestClient(TestServer(app)) as c:
            async def req(method, path, body=None, status=200):
                resp = await c.request(method, path, json=body)
                data = await resp.json()
                assert resp.status == status, data
                return data
            # The run session may change the model; a third session may not.
            patch = {"machines": [{"id": "door", "states": ["closed", "open"], "initial": "closed"}],
                     "events": [{"name": "open", "kind": "actor"}],
                     "rules": [{"id": "r.open", "on": {"event": "open"}, "when": {"sm": "door", "is": "closed"},
                                "do": [{"sm": "door", "to": "open"}],
                                "source": [{"ref": "spec.md", "quote": "The door opens when closed."}]}]}
            res = await req("POST", f"/api/sims/{sid}/model", {"session": "sim-run", "patch": patch})
            assert res["ok"] and "round" not in res  # the run session runs its own rounds
            await req("POST", f"/api/sims/{sid}/model", {"session": "s9", "patch": patch}, status=403)
            work = await req("GET", f"/api/sims/{sid}/work")
            assert [u["id"] for u in work["unjudged"]] == ["r.open"]
            # Only the attached run session reads its configuration.
            await req("GET", f"/api/sims/{sid}/runner?session=s1", status=403)
            conf = await req("GET", f"/api/sims/{sid}/runner?session=sim-run")
            assert conf["judge"] is None and "provisional" in conf["guide"]
            # Verdicts: fabricated retracts the rule.
            v = await req("POST", f"/api/sims/{sid}/verdicts", {"session": "sim-run", "key": "j1", "items": [
                {"id": "r.open", "verdict": "fabricated", "why": "x"}]})
            assert v["retracted"] == ["r.open"]
            assert (await req("GET", f"/api/sims/{sid}/model"))["model"]["rules"] == []
            again = await req("POST", f"/api/sims/{sid}/verdicts", {"session": "sim-run", "key": "j1", "items": []})
            assert again["duplicate"]
            await req("POST", f"/api/sims/{sid}/runner/done", {"session": "s1", "reason": "x"}, status=403)
            done = await req("POST", f"/api/sims/{sid}/runner/done", {"session": "sim-run", "reason": "no progress"})
            assert done["notified"] and "finished" in owner.delivered[-1]
            sim = await req("GET", f"/api/sims/{sid}")
            assert sim["sim"]["runner"] is None and sim["sim"]["state"] == "stopped"
            assert "no progress" in sim["sim"]["state_reason"]
            # After detaching, the run session has no authority left.
            await req("POST", f"/api/sims/{sid}/model", {"session": "sim-run", "patch": patch}, status=403)
    asyncio.run(go())


def test_end_in_a_round_waits_for_the_run_session(tmp_path):
    """The run session still proposes edits after the last round: the owner
    is told once, at runner/done, with the proposals in the notice."""
    app, sims, mgr, owner, run, root = setup(tmp_path)
    sid = started(sims, {"max_rounds": 1})

    async def go():
        async with TestClient(TestServer(app)) as c:
            before = len(owner.delivered)
            resp = await c.post(f"/api/sims/{sid}/round", json={"session": "sim-run"})
            data = await resp.json()
            assert resp.status == 200 and data["result"]["ended"]["deferred"], data
            assert not data["notified"] and len(owner.delivered) == before
            # What the editor would have left behind.
            S._write_json(sims._dir(sid) / "proposals.json", {"e-1": {"id": "e-1", "status": "proposed", "findings": []}})
            resp = await c.post(f"/api/sims/{sid}/runner/done", json={"session": "sim-run", "reason": "round 1"})
            data = await resp.json()
            assert data["notified"] and len(owner.delivered) == before + 1
            assert "1 edit proposal(s)" in owner.delivered[-1] and f"sim_proposals {sid}" in owner.delivered[-1]
    asyncio.run(go())
    # The process exiting afterwards tells nobody again.
    mgr.end("sim-run")
    assert sum("finished" in t for t in owner.delivered) == 1


def test_run_session_exit_after_a_deferred_end_tells_the_owner(tmp_path):
    app, sims, mgr, owner, run, root = setup(tmp_path)
    sid = started(sims, {"max_rounds": 1})
    assert sims.run_round(sid, "sim-run")["ended"]["deferred"]
    before = len(owner.delivered)
    mgr.end("sim-run")
    assert len(owner.delivered) == before + 1 and "finished" in owner.delivered[-1]
    assert sims.get(sid)["runner"] is None


def test_runner_exit_ends_campaign_and_stop_kills_runner(tmp_path):
    app, sims, mgr, owner, run, root = setup(tmp_path)
    sid = started(sims)
    mgr.end("sim-run")
    sim = sims.get(sid)
    assert sim["state"] == "stopped" and "exited" in sim["state_reason"] and sim["runner"] is None
    assert "finished" in owner.delivered[-1]

    run2 = FakeSession("sim-run2", str(root))
    mgr.sessions["sim-run2"] = run2
    sims.start(sid, "s1", round=False)
    sims.attach_runner(sid, "sim-run2")

    async def go():
        async with TestClient(TestServer(app)) as c:
            resp = await c.post(f"/api/sims/{sid}/stop", json={})
            assert resp.status == 200
            assert (await resp.json())["notified"]
    asyncio.run(go())
    assert run2.killed and sims.get(sid)["runner"] is None


def test_start_without_runtime_falls_back_to_owner(tmp_path, monkeypatch):
    app, sims, mgr, owner, run, root = setup(tmp_path)
    sim = sims.create(title="door", owner="s1", actor="s1", root=str(root),
                      settings={"interpreter_profile": "ds4"})
    monkeypatch.setattr(sim_api, "pi_profiles", lambda: ["ds4"])
    monkeypatch.setattr(runtime, "runner_path", lambda d=None: None)

    async def go():
        async with TestClient(TestServer(app)) as c:
            resp = await c.post(f"/api/sims/{sim['id']}/start", json={"session": "s1"})
            data = await resp.json()
            assert resp.status == 200, data
            assert "not installed" in data["manual"]
    asyncio.run(go())


def test_runtime_status_and_install(tmp_path):
    d = tmp_path / "rt"
    assert runtime.status(d)["installed"] is False
    assert runtime.runner_path(d) is None
    calls = []

    def fake_npm(cmd, cwd, **kw):
        calls.append(cmd)
        (Path(cwd) / "node_modules" / "@earendil-works" / "pi-durable").mkdir(parents=True)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    if not shutil.which("npm"):
        pytest.skip("npm is not on PATH")
    res = runtime.install(d, run=fake_npm)
    assert res["action"] == "installed" and calls and calls[0][1] == "ci"
    assert (d / "runner.mjs").is_file() and (d / "campaign.mjs").is_file()
    assert runtime.install(d, run=fake_npm)["action"] == "current" and len(calls) == 1
    (d / "runner.mjs").write_text("old", encoding="utf-8")
    assert runtime.runner_path(d).read_text(encoding="utf-8") != "old"  # a launch copies the current one


DRIVER = textwrap.dedent("""
    import { runCampaign } from %(campaign)s;
    const [base, simId, session] = process.argv.slice(2);
    const api = async (method, route, body) => {
      const r = await fetch(base + route, { method, headers: body ? { "Content-Type": "application/json" } : {},
        ...(body ? { body: JSON.stringify(body) } : {}) });
      const data = await r.json();
      if (!r.ok) throw new Error(`${method} ${route}: ${r.status} ${data.error}`);
      return data;
    };
    const model = {
      machines: [{ id: "door", states: ["closed", "open"], initial: "closed" }],
      events: [{ name: "open", kind: "actor" }, { name: "close", kind: "actor" }],
      rules: [{ id: "r.open", on: { event: "open" }, when: { sm: "door", is: "closed" },
                do: [{ sm: "door", to: "open" }], source: [{ ref: "spec.md", quote: "The door opens when closed." }] }],
    };
    const bad = JSON.parse(JSON.stringify(model));
    bad.rules[0].source[0].quote = "The door flies.";
    const fix = { rules: [
      { id: "r.close", on: { event: "close" }, when: { sm: "door", is: "open" }, do: [{ sm: "door", to: "closed" }], provisional: { why: "symmetry" } },
      { id: "r.open2", on: { event: "open" }, when: { sm: "door", is: "open" }, do: [], provisional: { why: "no-op" } },
      { id: "r.close2", on: { event: "close" }, when: { sm: "door", is: "closed" }, do: [], provisional: { why: "no-op" } },
    ] };
    const asked = [];
    const ask = async (role, key, prompt) => {
      asked.push(`${role}:${key}`);
      if (key === "formalize:1") return "```json\\n" + JSON.stringify(bad) + "\\n```";
      if (key === "formalize:2") {
        if (!prompt.includes("word for word")) throw new Error("the retry must carry the rejection");
        return "Here.\\n```json\\n" + JSON.stringify(model) + "\\n```";
      }
      if (key.startsWith("requote:")) {
        if (!prompt.includes("no longer in its file")) throw new Error("the requote prompt must say why");
        const r = JSON.parse(JSON.stringify(model.rules[0]));
        r.source[0].quote = "The door swings open when it is closed.";
        return JSON.stringify({ patch: { rules: [r] } });
      }
      if (key.startsWith("edit:")) {
        if (!prompt.includes("stays open")) throw new Error("the editor must see the user's answer");
        return JSON.stringify([{ file: "spec.md", old: "", new: "Opening an open door leaves it open.", kind: "complete",
                                 reason: "the user's answer", findings: ["unhandled|door|open|open"] }]);
      }
      if (key.startsWith("judge:")) return JSON.stringify([{ section: "rules", id: "r.open", verdict: "confirmed", why: "quoted" }]);
      if (key.startsWith("C:triage:") || key.startsWith("triage:")) {
        // Opening an open door: intended. Anything else: the sources are silent.
        const keys = [...new Set([...prompt.matchAll(/"key": "([^"]+)"/g)].map((m) => m[1]))];
        return JSON.stringify(keys.map((k) => ({ key: k, verdict: k.endsWith("|open|open") ? "intended" : "spec", why: "scripted" })));
      }
      if (key.startsWith("gap:")) {
        if (prompt.includes('"key": "unhandled|door|open|open"')) throw new Error("an intended finding reached the interpreter");
        if (!prompt.includes('"judge"')) throw new Error("the gap prompt must carry the triage");
        return JSON.stringify({ patch: fix, resolve: [] });
      }
      throw new Error("unexpected question " + key);
    };
    const progress = [];
    const res = await runCampaign({ simId, session, api, ask, guide: "GUIDE", progress: (p) => progress.push(p.stage) });
    console.log(JSON.stringify({ res, asked, progress }));
""")


def drive(tmp_path, app, sid):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the campaign loop")
    driver = tmp_path / "driver.mjs"
    driver.write_text(DRIVER % {"campaign": json.dumps(CAMPAIGN.as_uri())}, encoding="utf-8")

    async def go():
        async with TestClient(TestServer(app)) as c:
            base = str(c.make_url("")).rstrip("/")
            proc = await asyncio.create_subprocess_exec(
                node, str(driver), base, sid, "sim-run",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, err = await asyncio.wait_for(proc.communicate(), 60)
            assert proc.returncode == 0, err.decode()
            return json.loads(out.decode().strip().splitlines()[-1])
    return asyncio.run(go())


def test_campaign_requotes_rules_whose_source_changed(tmp_path):
    app, sims, mgr, owner, run, root = setup(tmp_path)
    sim = sims.create(title="door", owner="s1", actor="s1", root=str(root))
    rule = {"id": "r.open", "on": {"event": "open"}, "when": {"sm": "door", "is": "closed"},
            "do": [{"sm": "door", "to": "open"}], "source": [{"ref": "spec.md", "quote": "The door opens when closed."}]}
    sims.patch_model(sim["id"], "s1", {"machines": [{"id": "door", "states": ["closed", "open"], "initial": "closed"}],
                                       "events": [{"name": "open", "kind": "actor"}, {"name": "close", "kind": "actor"}],
                                       "rules": [rule]})
    (root / "spec.md").write_text("The door swings open when it is closed.\n", encoding="utf-8")
    assert sims.start(sim["id"], "s1", round=False)["carry"]["stale_sources"]
    sims.attach_runner(sim["id"], "sim-run")
    got = drive(tmp_path, app, sim["id"])
    assert got["asked"][0].startswith("B:requote:rules:r.open:r0") and "B:formalize:1" not in got["asked"]
    assert sims.work(sim["id"])["stale"] == []
    assert "swings" in sims.model(sim["id"])["rules"][0]["source"][0]["quote"]


def test_campaign_loop_against_the_daemon(tmp_path):
    app, sims, mgr, owner, run, root = setup(tmp_path)
    sid = started(sims)
    got = drive(tmp_path, app, sid)
    assert got["res"]["ended"] and "nothing open" in got["res"]["reason"]
    assert got["asked"][:3] == ["B:formalize:1", "B:formalize:2", "C:judge:rules:r.open:r0"]
    assert any(a.startswith("B:gap:r1:") for a in got["asked"])
    assert {"formalize", "judge", "round", "gaps"} <= set(got["progress"])
    sim = sims.get(sid)
    assert sim["state"] == "idle" and sim["model_stats"]["rules"] == 4
    assert any(a.startswith("C:triage:r1:") for a in got["asked"])
    f = sims.findings(sid)["unhandled|door|open|open"]
    assert f["status"] == "dismissed" and f["resolution"]["reason"] == "triage: scripted"
    assert sims.verdicts(sid)["rules:r.open"]["verdict"] == "confirmed"


def test_editor_proposes_edits_after_the_campaign(tmp_path):
    app, sims, mgr, owner, run, root = setup(tmp_path)
    sim = sims.create(title="door", owner="s1", actor="s1", root=str(root), settings={"max_edits": 2})
    sims.patch_model(sim["id"], "s1", {"machines": [{"id": "door", "states": ["closed", "open"], "initial": "closed"}],
                                       "events": [{"name": "open", "kind": "actor"}],
                                       "rules": [{"id": "r.open", "on": {"event": "open"}, "when": {"sm": "door", "is": "closed"},
                                                  "do": [{"sm": "door", "to": "open"}], "provisional": {"why": "x"}}]})
    sims.run_round(sim["id"], "s1")
    key = "unhandled|door|open|open"
    sims.resolve(sim["id"], "s1", [{"key": key, "action": "ask", "question": "and then?"}])
    sims.answer(sim["id"], S.USER, key, "it stays open")
    sims.attach_runner(sim["id"], "sim-run")
    got = drive(tmp_path, app, sim["id"])  # idle campaign: straight to the editor
    assert got["res"]["reason"].startswith("campaign idle") and got["asked"][-1].startswith("B:edit:")
    (p,) = sims.proposals(sim["id"]).values()
    assert p["kind"] == "complete" and p["findings"] == [key] and p["by"] == "sim-run"
    assert (root / "spec.md").read_text(encoding="utf-8") == SPEC
