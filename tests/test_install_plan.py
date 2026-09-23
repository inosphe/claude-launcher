"""The install dry run (``fsplan`` + ``install_plan``) and its daemon routes.

What is pinned here:

- a dry run writes nothing, for every target, and reports what the real run
  then writes — byte for byte, because the preview runs the same code;
- a second pass over a file in one plan sees the first (the deny and allow
  merges into one ``settings.json``);
- ``cflow update`` without ``--force`` keeps an edited copy, with it backs the
  copy up and replaces it — in the preview as in the run;
- Defender is only read in a dry run, never asked to change;
- the three ``/api/install`` routes.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from claude_launcher import defender, fsplan, install, install_plan, profile, workspaces
from claude_launcher.cflow import state as cflow_state
from claude_launcher.daemon import api


def snapshot(*roots: Path) -> dict:
    """Every file under ``roots`` with its bytes — the disk, for comparing."""
    out = {}
    for root in roots:
        if root.exists():
            for p in root.rglob("*"):
                if p.is_file():
                    out[str(p)] = p.read_bytes()
    return out


@pytest.fixture
def no_powershell(monkeypatch):
    """Defender off the table: no PowerShell ever runs from these tests."""
    calls = []

    def fake(script):
        calls.append(script)
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(defender, "_powershell", fake)
    return calls


@pytest.fixture
def roots(home, tmp_path, no_powershell):
    return (home, tmp_path / ".claude-config", tmp_path / "proj")


def test_a_dry_run_of_every_target_writes_nothing(roots, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    workspaces.add(str(project), name="proj")
    profile.create("work")
    before = snapshot(*roots)
    ids = [t.id for t in install_plan.targets()]
    assert "install:project:proj" in ids and "install:profile:work" in ids
    for target_id in ids:
        report = install_plan.plan(target_id)
        assert report["dry_run"] is True
    assert snapshot(*roots) == before


def test_the_preview_is_what_the_run_then_writes(roots):
    report = install_plan.plan("install:global")
    kinds = {c["kind"] for c in report["changes"]}
    assert kinds == {fsplan.CREATE}
    cats = {c["category"] for c in report["changes"]}
    assert {"mcp", "skill", "guard", "workflow", "seed-record"} <= cats
    assert set(report["effects"]) == cats

    install_plan.apply("install:global")
    for change in report["changes"]:
        assert Path(change["path"]).read_bytes().decode("utf-8") == change["after"]

    again = install_plan.plan("install:global")
    assert again["summary"][fsplan.CREATE] == 0
    assert again["summary"][fsplan.UPDATE] == 0


def test_two_merges_into_one_settings_file_preview_as_one_change(roots):
    report = install_plan.plan("install:global")
    rows = [c for c in report["changes"] if c["category"] == "guard"]
    assert len(rows) == 1
    doc = json.loads(rows[0]["after"])
    assert set(install.GATE_DENY_RULES) <= set(doc["permissions"]["deny"])
    assert set(install.GATE_ALLOW_RULES) <= set(doc["permissions"]["allow"])


def test_a_project_install_previews_only_inside_the_project(roots, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    workspaces.add(str(project), name="proj")
    report = install_plan.plan("install:project:proj")
    assert report["changes"]
    for c in report["changes"]:
        Path(c["path"]).resolve().relative_to(project.resolve())


def _seed_then_edit(name_suffix=".yaml"):
    install.install_into_user()
    layer = cflow_state.global_workflows_dir()
    target = sorted(layer.glob(f"*{name_suffix}"))[0]
    target.write_text(target.read_text(encoding="utf-8") + "\n# mine\n", encoding="utf-8")
    return target


def test_cflow_update_keeps_an_edited_copy_without_force(roots):
    edited = _seed_then_edit()
    before = snapshot(*roots)
    report = install_plan.plan("cflow-update")
    paths = {c["path"] for c in report["changes"] if c["kind"] != fsplan.UNCHANGED}
    assert str(edited) not in paths
    assert any(edited.stem in line and "kept" in line for line in report["lines"])
    assert snapshot(*roots) == before


def test_cflow_update_force_backs_up_then_replaces(roots):
    edited = _seed_then_edit()
    before = snapshot(*roots)
    report = install_plan.plan("cflow-update:force")
    by_path = {c["path"]: c for c in report["changes"]}
    assert by_path[str(edited)]["kind"] == fsplan.UPDATE
    bak = str(edited.with_name(edited.name + ".bak"))
    assert by_path[bak]["kind"] == fsplan.CREATE
    assert by_path[bak]["category"] == "backup"
    assert "# mine" in by_path[bak]["after"]
    assert snapshot(*roots) == before

    install_plan.apply("cflow-update:force")
    assert "# mine" not in edited.read_text(encoding="utf-8")
    assert "# mine" in Path(bak).read_text(encoding="utf-8")


def test_a_dry_run_reads_defender_and_never_asks_it_to_change(
    roots, no_powershell, monkeypatch
):
    monkeypatch.setattr(sys, "platform", "win32")
    report = install_plan.plan("install:global")
    assert no_powershell, "the exclusion list should have been read"
    assert not any("Add-MpPreference" in s for s in no_powershell)
    assert any("would register" in line for line in report["lines"])


def test_the_overview_skips_defender(roots, no_powershell, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    body = install_plan.overview()
    assert not no_powershell
    ids = [row["id"] for row in body["targets"]]
    assert "install:global" in ids and "cflow-update:force" in ids


# --------------------------------------------------------------------------- #
# the daemon routes
# --------------------------------------------------------------------------- #
class _Request:
    def __init__(self, payload=None, query=None):
        self._payload = payload
        self.query = query or {}

    async def json(self):
        if self._payload is None:
            raise ValueError("no body")
        return self._payload


def call(handler, **kw):
    resp = asyncio.run(handler(_Request(**kw)))
    return resp.status, json.loads(resp.text)


def test_the_overview_route_lists_targets(roots):
    status, body = call(api.h_install_overview)
    assert status == 200
    assert any(row["id"] == "install:global" for row in body["targets"])


def test_the_plan_route_needs_a_known_target(roots):
    assert call(api.h_install_plan)[0] == 400
    assert call(api.h_install_plan, query={"target": "nope"})[0] == 404
    status, body = call(api.h_install_plan, query={"target": "install:global"})
    assert status == 200 and body["dry_run"] is True


def test_the_apply_route_runs_the_install(roots):
    assert call(api.h_install_apply, payload={})[0] == 400
    assert call(api.h_install_apply, payload={"target": "nope"})[0] == 404
    status, body = call(api.h_install_apply, payload={"target": "install:global"})
    assert status == 200 and body["dry_run"] is False
    assert any(line.startswith("mcp server") for line in body["lines"])
    for change in body["changes"]:
        assert Path(change["path"]).is_file()
