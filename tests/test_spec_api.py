"""The dashboard's work spec routes (``daemon/spec_api.py``) over a fake
executor backend: check, plan and import, and the 400s a page shows."""

import asyncio

import yaml
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher import workspec_import as wi
from claude_launcher.daemon import spec_api
from test_workspec_import import EXAMPLE, FakeBackend

TEXT = EXAMPLE.read_text(encoding="utf-8")


def _run(scenario, backend=None):
    async def go():
        app = web.Application()
        spec_api.install(app, backend=lambda doc: backend or FakeBackend())
        async with TestClient(TestServer(app)) as client:
            async def post(path, body, status=200):
                response = await client.post("/api/spec/" + path, json=body)
                assert response.status == status, await response.text()
                return await response.json()
            await scenario(post, client)
    asyncio.run(go())


def test_check_answers_the_leader_choice_the_page_offers(home):
    async def scenario(post, _client):
        out = await post("check", {"yaml": TEXT})
        assert out["errors"] == []
        assert out["spec"]["mesh"] == "spec-night-sense"
        assert out["spec"]["leader_mode"] == "ask"
        assert out["spec"]["candidates"] == ["s29"]
        assert (out["spec"]["agents"], out["spec"]["issues"]) == (4, 4)
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


def test_import_runs_the_executor_and_reports_a_stop_as_resumable(home):
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
