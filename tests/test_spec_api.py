"""The dashboard's work spec routes (``daemon/spec_api.py``) over a fake
executor backend: check, plan and import, and the 400s a page shows."""

import asyncio

import yaml
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher import workspaces
from claude_launcher import workspec_import as wi
from claude_launcher.daemon import spec_api
from test_workspec_import import EXAMPLE, FakeBackend

TEXT = EXAMPLE.read_text(encoding="utf-8")


def _register(tmp_path, name="shelter-zero"):
    """The example spec's workspace, registered at a throwaway directory."""
    d = tmp_path / name
    d.mkdir()
    return workspaces.add(str(d), name=name).path


def _run(scenario, backend=None, built=None):
    def make(doc, board):
        if built is not None:
            built.append((doc.get("workspace"), board))
        return backend or FakeBackend()

    async def go():
        app = web.Application()
        spec_api.install(app, backend=make)
        async with TestClient(TestServer(app)) as client:
            async def post(path, body, status=200):
                response = await client.post("/api/spec/" + path, json=body)
                assert response.status == status, await response.text()
                return await response.json()
            await scenario(post, client)
    asyncio.run(go())


def test_check_answers_the_leader_choice_the_page_offers(home, tmp_path):
    path = _register(tmp_path)

    async def scenario(post, _client):
        out = await post("check", {"yaml": TEXT})
        assert out["errors"] == []
        assert out["spec"]["mesh"] == "spec-night-sense"
        assert out["spec"]["leader_mode"] == "ask"
        assert out["spec"]["candidates"] == ["s29"]
        assert (out["spec"]["agents"], out["spec"]["issues"]) == (4, 4)
        assert out["spec"]["board_dir"] == path
        assert out["spec"]["board_error"] == ""
        assert out["spec"]["workspaces"] == ["shelter-zero"]
    _run(scenario)


def test_a_bad_spec_is_a_400_with_every_error_line(home):
    doc = yaml.safe_load(TEXT)
    doc["agents"][1]["kind"] = ["worker"]
    doc["issues"][1]["review"] = "nobody"

    async def scenario(post, client):
        out = await post("check", {"yaml": yaml.safe_dump(doc)}, 400)
        assert "agents[1].kind: must be a string" in out["errors"]
        out = await post("check", {"yaml": "a: [unclosed"}, 400)
        assert out["errors"][0].startswith("not valid YAML")
        await post("check", {"yaml": ""}, 400)
        await post("plan", {"yaml": yaml.safe_dump(doc), "leader": "own"}, 400)
        bad = await client.post("/api/spec/check", data="{",
                                headers={"Content-Type": "application/json"})
        assert bad.status == 400
    _run(scenario)


def test_plan_lists_the_actions_for_the_chosen_leader(home):
    async def scenario(post, _client):
        out = await post("plan", {"yaml": TEXT, "leader": "own"})
        dos = [a["do"] for a in out["actions"]]
        assert dos[0] == "reference" and "mesh-create" in dos
        lead = next(a for a in out["actions"]
                    if a["do"] == "spawn" and a["agent"] == "leader")
        assert lead["workflow"] == "improv-leader" and lead["parent"] is None
        assert len(out["lines"]) == len(out["actions"])
        assert out["lines"][0].startswith("reference")
        shared = await post("plan", {"yaml": TEXT, "leader": "shared:s29"})
        assert any(a["do"] == "mesh-join" and a["session"] == "s29"
                   for a in shared["actions"])
        # The spec says ask: no choice is an answer about the choice.
        missing = await post("plan", {"yaml": TEXT}, 400)
        assert missing["leader_error"] is True
        assert "own or" in missing["error"]
    _run(scenario)


def test_import_runs_the_executor_and_reports_a_stop_as_resumable(home, tmp_path):
    _register(tmp_path)
    fb = FakeBackend()

    async def scenario(post, _client):
        out = await post("import", {"yaml": TEXT, "leader": "shared:s29"})
        assert out["ok"] is True
        assert {r["do"] for r in out["log"]} >= {"issue", "spawn", "connect"}
        assert len(fb.issues) == 6
        again = await post("import", {"yaml": TEXT, "leader": "shared:s29"})
        assert {r["result"] for r in again["log"]} <= {
            wi.EXISTS, wi.SKIPPED, wi.DONE}
        # own needs a profile: the stop keeps the log and says it resumes
        stop = await post("import", {"yaml": TEXT, "leader": "own"}, 409)
        assert "--profile" in stop["error"] and stop["resumable"] is True
        assert stop["log"]
    _run(scenario, backend=fb)


def test_the_board_is_a_registered_workspace_never_the_daemons_cwd(home, tmp_path):
    """The daemon's current directory is wherever it was started and it
    serves every project: an import names its board or is refused."""
    doc = yaml.safe_load(TEXT)
    del doc["workspace"]
    bare = yaml.safe_dump(doc)
    path = _register(tmp_path, "night-ws")
    built, fb = [], FakeBackend()

    async def scenario(post, _client):
        out = await post("check", {"yaml": bare})
        assert out["spec"]["board_dir"] == ""
        assert "names no workspace" in out["spec"]["board_error"]
        # No workspace in the spec or the body: refused, nothing built.
        stop = await post("import", {"yaml": bare, "leader": "shared:s29"}, 400)
        assert stop["workspace_error"] is True and built == []
        # An unregistered name is refused the same way.
        stop = await post("import", {"yaml": bare, "leader": "shared:s29",
                                     "workspace": "nowhere"}, 400)
        assert "no workspace named 'nowhere'" in stop["error"] and built == []
        # The body's workspace fills the gap: check shows it, the spawned
        # sessions get it and the backend writes the board there.
        out = await post("check", {"yaml": bare, "workspace": "night-ws"})
        assert out["spec"]["board_dir"] == path
        await post("plan", {"yaml": bare, "leader": "shared:s29",
                             "workspace": "night-ws"})
        await post("import", {"yaml": bare, "leader": "shared:s29",
                              "workspace": "night-ws"})
        assert built == [("night-ws", path)]
        spawned = [c[2] for c in fb.calls if c[0] == "spawn"]
        assert spawned and {b["workspace"] for b in spawned} == {"night-ws"}
        # A body that contradicts the spec's own workspace is refused.
        clash = await post("check", {"yaml": TEXT, "workspace": "night-ws"}, 400)
        assert clash["workspace_error"] is True
    _run(scenario, backend=fb, built=built)


def test_check_answers_the_preview_and_the_line_of_each_error(home):
    """What the page draws while the text is edited: a valid spec's
    sessions and work, and for a bad one the YAML line of every error."""
    async def scenario(post, client):
        out = await post("check", {"yaml": TEXT})
        ids = [s["id"] for s in out["preview"]["sessions"]]
        assert ids == ["leader", "lead2", "ue1", "sys1", "rv1"]
        assert {t["key"] for t in out["preview"]["tasks"]} == \
            {i["key"] for i in yaml.safe_load(TEXT)["issues"]}

        bad = TEXT.replace("    kind: reviewer", "    kind: reviewr")
        out = await post("check", {"yaml": bad}, 400)
        assert len(out["lines"]) == len(out["errors"])
        row = next(i for i, l in enumerate(bad.splitlines(), 1)
                   if "kind: reviewr" in l)
        assert out["lines"][out["errors"].index(
            "agents.rv1.kind: one of sub-lead, worker, reviewer")] == row
        out = await post("check", {"yaml": "spec: 1\nagents: [\n"}, 400)
        assert out["lines"][0] and "preview" not in out

        template = await client.get("/api/spec/template")
        assert template.status == 200
        text = (await template.json())["yaml"]
        out = await post("check", {"yaml": text})
        assert out["errors"] == [] and out["preview"]["tasks"]
    _run(scenario)
