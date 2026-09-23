"""Dashboard workflow editing keeps source YAML and uses the runtime parser."""

from pathlib import Path
import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher.cflow import editor, model, state
from claude_launcher.daemon import api


def _layers(tmp_path, monkeypatch):
    project = tmp_path / ".claunch" / "workflows"
    global_dir = tmp_path / "global"
    project.mkdir(parents=True)
    global_dir.mkdir()
    monkeypatch.setattr(state, "global_workflows_dir", lambda: global_dir)
    return project, global_dir


def test_editor_preserves_source_and_validates_inherited_graph(tmp_path, monkeypatch):
    project, global_dir = _layers(tmp_path, monkeypatch)
    base = global_dir / "demo.yaml"
    base.write_text("name: demo\nsteps:\n  start:\n    instructions: base\n", encoding="utf-8")
    overlay = project / "demo.yaml"
    overlay.write_text("# local note\nextends: demo\nfuture_option:\n  nested: 7\nsteps:\n  start:\n    verify: echo ready\n", encoding="utf-8")
    cwd = str(tmp_path)

    rows = editor.declarations(cwd)
    assert [(r["layer"], r["active"]) for r in rows] == [
        ("project", True), ("global", False)]
    source = editor.read(cwd, "demo", "project")
    assert source["text"].startswith("# local note\n")
    draft = source["text"].replace("echo ready", "echo changed")
    checked = editor.validate(cwd, "demo", "project", draft)
    assert checked["steps"] == 1
    assert checked["bases"] == [str(base)]
    saved = editor.save(cwd, "demo", "project", draft, source["revision"])
    assert overlay.read_text(encoding="utf-8") == draft
    assert "future_option:\n  nested: 7" in saved["text"]
    assert saved["validation"]["start"] == "start"
    assert state.load_workflow("demo", cwd).workflow.steps["start"].verify.command == "echo changed"


def test_editor_rejects_invalid_and_stale_drafts_without_writing(tmp_path, monkeypatch):
    _, global_dir = _layers(tmp_path, monkeypatch)
    path = global_dir / "demo.yaml"
    original = "name: demo\nsteps:\n  start:\n    instructions: work\n"
    path.write_text(original, encoding="utf-8")
    cwd = str(tmp_path)
    source = editor.read(cwd, "demo", "global")
    with pytest.raises(model.WorkflowError):
        editor.save(cwd, "demo", "global", "steps: [invalid]\n", source["revision"])
    assert path.read_text(encoding="utf-8") == original
    path.write_text(original + "# another editor\n", encoding="utf-8")
    with pytest.raises(editor.EditConflict):
        editor.save(cwd, "demo", "global", original, source["revision"])
    with pytest.raises(model.WorkflowError):
        editor.read(cwd, "../../demo", "global")
    assert path.read_text(encoding="utf-8").endswith("# another editor\n")


def test_editor_checks_active_overlay_when_base_changes(tmp_path, monkeypatch):
    project, global_dir = _layers(tmp_path, monkeypatch)
    base = global_dir / "demo.yaml"
    original = ("name: demo\nsteps:\n  start:\n    instructions: work\n    next: second\n"
                "  second:\n    instructions: done\n")
    base.write_text(original, encoding="utf-8")
    (project / "demo.yaml").write_text(
        "extends: demo\nsteps:\n  start:\n    next: second\n", encoding="utf-8")
    draft = ("name: demo\nsteps:\n  start:\n    instructions: done\n")
    # The global source is valid alone; the overlay still points at `second`.
    with pytest.raises(model.WorkflowError):
        editor.validate(str(tmp_path), "demo", "global", draft)
    assert base.read_text(encoding="utf-8") == original


def test_editor_detects_change_during_validation(tmp_path, monkeypatch):
    _, global_dir = _layers(tmp_path, monkeypatch)
    path = global_dir / "demo.yaml"
    original = "name: demo\nsteps:\n  start:\n    instructions: work\n"
    path.write_text(original, encoding="utf-8")
    source = editor.read(str(tmp_path), "demo", "global")
    real_chmod = editor.os.chmod

    def change_source_before_replace(temporary, mode):
        real_chmod(temporary, mode)
        path.write_text(original + "# external edit\n", encoding="utf-8")

    monkeypatch.setattr(editor.os, "chmod", change_source_before_replace)
    with pytest.raises(editor.EditConflict):
        editor.save(str(tmp_path), "demo", "global", original + "# draft\n",
                    source["revision"])
    assert path.read_text(encoding="utf-8").endswith("# external edit\n")


def test_editor_accepts_current_bundled_workflow_specs(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    bundled = root / "src" / "claude_launcher" / "workflows"
    monkeypatch.setattr(state, "global_workflows_dir", lambda: bundled)
    for path in bundled.glob("*.yaml"):
        source = editor.read(str(tmp_path), path.stem, "global")
        assert editor.validate(str(tmp_path), path.stem, "global", source["text"])["steps"] > 0


def test_editor_http_roundtrip_and_conflict(tmp_path, monkeypatch):
    _, global_dir = _layers(tmp_path, monkeypatch)
    path = global_dir / "demo.yaml"
    path.write_text("name: demo\nsteps:\n  start:\n    instructions: work\n", encoding="utf-8")

    async def run():
        app = web.Application()
        app.router.add_get("/api/cflow/definitions", api.h_cflow_definitions)
        app.router.add_get("/api/cflow/definition", api.h_cflow_definition)
        app.router.add_post("/api/cflow/definition/validate", api.h_cflow_definition_validate)
        app.router.add_put("/api/cflow/definition", api.h_cflow_definition_save)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            query = {"cwd": str(tmp_path)}
            listed = await (await client.get("/api/cflow/definitions", params=query)).json()
            assert len(listed["definitions"]) == 1
            row = listed["definitions"][0]
            source = await (await client.get("/api/cflow/definition", params={
                **query, "name": row["name"], "layer": row["layer"], "path": row["path"]})).json()
            draft = {**source, "cwd": str(tmp_path), "text": source["text"] + "# kept\n"}
            valid = await client.post("/api/cflow/definition/validate", json=draft)
            assert (await valid.json())["validation"]["steps"] == 1
            saved = await client.put("/api/cflow/definition", json=draft)
            assert saved.status == 200
            assert path.read_text(encoding="utf-8").endswith("# kept\n")
            stale = await client.put("/api/cflow/definition", json=draft)
            assert stale.status == 409
            assert (await stale.json())["error"]
        finally:
            await client.close()

    asyncio.run(run())
