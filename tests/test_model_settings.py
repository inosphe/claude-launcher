"""Settings model choices reach launch argv without replacing harness options."""

import asyncio
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher import harnesses, lineage, profile, runner, store
from claude_launcher.daemon import harness
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.manager import SessionManager


@pytest.mark.parametrize("name,model_id,argument", [
    ("codex", "gpt-6-sol", "model=gpt-6-sol"),
    ("claude", "claude-example", "--model=claude-example"),
])
def test_model_settings_reach_launch_and_display(home, tmp_path, name, model_id, argument):
    p = profile.create("work")
    lineage.set_harness(p, name)
    original = harnesses.get(name)
    store.update(lambda doc: doc.update({"model_choices": {name: {"custom": model_id}}}))
    entry = harnesses.get(name)
    assert entry.command == original.command
    assert entry.args == original.args
    assert entry.auth == original.auth
    assert entry.models == ["custom"]
    definition = harness.normalize(harness.SessionDef(
        name="test", profile="work", cwd=str(tmp_path), model="custom",
    ))
    argv, _, _ = harness.build_command(definition)
    assert argument in argv
    assert runner.model_ids(p, [entry])[name]["custom"] == model_id
    # The process-wide packaged cache must survive edits and resets intact.
    assert harnesses.registry({})[name] == original


def test_override_is_independent_of_custom_harness_definition(home):
    doc = {"harnesses": {"custom": {"command": "my-cli", "args": ["--flag"]}},
           "model_choices": {"custom": {"fast": "vendor/fast"}, "codex": {}}}
    reg = harnesses.registry(doc)
    assert reg["custom"].command == ["my-cli"]
    assert reg["custom"].args == ["--flag"]
    assert reg["custom"].model_aliases == {"fast": "vendor/fast"}
    assert reg["codex"].models == []
    assert reg["codex"].model_aliases == {}


@pytest.mark.parametrize("value", [[], "oops", {"": "id"}, {"a": ""},
                                       {"a b": "id"}, {"a": 4}, {"a": "bad\nvalue"},
                                       {"a": "bad\x00value"}, {"a": "x=y"}])
def test_invalid_manual_config_falls_back_to_declaration(home, value):
    with pytest.raises(harnesses.HarnessConfigError):
        harnesses.validate_model_choices(value)
    assert harnesses.registry({"model_choices": {"codex": value}})["codex"] == harnesses.get("codex")


def test_api_model_save_reset_and_validation(home):
    async def run():
        mgr = SessionManager(restore_default=False)
        app = build_app(mgr, "auth", started_at=time.monotonic())
        headers = {"Authorization": "Bearer auth"}
        url = "/api/harnesses/codex/models"
        store.update(lambda doc: doc.update({"unrelated": {"keep": True}}))
        try:
            async with TestClient(TestServer(app)) as client:
                assert (await client.put(url, json={"models": {}})).status == 401
                for body in ({}, [], {"models": []}, {"models": {"a": ""}}):
                    assert (await client.put(url, json=body, headers=headers)).status == 400
                assert "model_choices" not in store.load()
                resp = await client.put(url, json={"models": {"new": "gpt-6-luna"}}, headers=headers)
                assert resp.status == 200
                assert (await resp.json())["harness"]["models"] == ["new"]
                assert store.load()["model_choices"]["codex"] == {"new": "gpt-6-luna"}
                resp = await client.get("/api/harnesses", headers=headers)
                rows = {h["name"]: h for h in (await resp.json())["harnesses"]}
                assert rows["codex"]["model_aliases"] == {"new": "gpt-6-luna"}
                resp = await client.put("/api/harnesses/missing/models", json={"models": {}}, headers=headers)
                assert resp.status == 404
                resp = await client.put(url, json={"models": None}, headers=headers)
                assert resp.status == 200
                assert "gpt6-sol" in (await resp.json())["harness"]["models"]
                assert "model_choices" not in store.load()
                assert store.load()["unrelated"] == {"keep": True}
        finally:
            await mgr.shutdown_all()

    asyncio.run(run())
