"""Fleet presets (``claude_launcher/fleet.py``): the built-in preset, the
validator, the expansion into a work spec, and the preset store."""

import copy

import pytest
import yaml

from claude_launcher import fleet, workspec
from claude_launcher import workspec_import as wi
from test_workspec_import import FakeBackend

BUILTIN = fleet.load(fleet.BUILTIN[fleet.DEFAULT_NAME])
LAUNCH = {"task": "로그인 개편\n세부 요구", "workspace": "ws", "project": "proj",
          "default_profile": "sr", "name": "fleet-t"}


def _preset():
    return copy.deepcopy(BUILTIN)


def test_the_builtin_preset_is_valid_and_has_the_coordinator_shape():
    assert fleet.validate(BUILTIN) == []
    rows = fleet.shape(BUILTIN)
    assert [r["handle"] for r in rows] == [
        "coord", "lead1", "lead1-w1", "lead1-w2", "lead2", "lead2-w1", "lead2-w2"]
    assert [(r["kind"], r["workflow"], r["relation"]) for r in rows[:3]] == [
        ("coordinator", "improv-coordinator-v2", ""),
        ("sub-lead", "improv-worker-v2", "independent"),
        ("worker", "improv-worker-v2", "stacked")]
    assert [r["issue"] for r in rows[:3]] == ["lane", "lane", "pending"]


def test_counts_come_from_params_and_are_clamped():
    rows = fleet.shape(BUILTIN, {"leads": 3, "workers": 0})
    assert [r["handle"] for r in rows] == ["coord", "lead1", "lead2", "lead3"]
    # max 6 per the preset; 99 is clamped, not refused
    assert sum(r["kind"] == "sub-lead" for r in fleet.shape(BUILTIN, {"leads": 99})) == 6
    with pytest.raises(fleet.FleetError, match="unknown param"):
        fleet.shape(BUILTIN, {"nope": 1})
    with pytest.raises(fleet.FleetError, match="integer"):
        fleet.shape(BUILTIN, {"leads": "many"})


@pytest.mark.parametrize("mutate, needle", [
    (lambda d: d["root"].update(kind="reviewer"), "root cannot be a reviewer"),
    (lambda d: d["root"]["children"][0]["children"][0].update(
        children=[{"id": "x", "kind": "worker"}]), "a worker holds no children"),
    (lambda d: d["root"]["children"][0].update(count="ghost"), "unknown param 'ghost'"),
    (lambda d: (d["root"]["children"][0].update(issue="pending"),
                d["root"]["children"][0]["children"][0].update(issue="lane")),
     "lane needs its parent to hold an issue"),
    (lambda d: d["root"]["children"][0].update(handle="lead{x}"), "unknown placeholder {x}"),
    (lambda d: d["root"]["children"][0]["children"][0].update(id="lead"),
     "duplicate group id 'lead'"),
    (lambda d: d.update(extra=1), "unknown key(s) extra"),
    (lambda d: d["params"]["leads"].update(default=9), "min <= default <= max"),
])
def test_validate_names_each_problem(mutate, needle):
    doc = _preset()
    mutate(doc)
    errs = fleet.validate(doc)
    assert any(needle in e for e in errs), errs


def test_a_repeating_handle_or_an_oversized_fleet_is_refused():
    doc = _preset()
    doc["root"]["children"][0]["children"][0]["handle"] = "w{n}"  # same under each lead
    assert any("repeats" in e for e in fleet.validate(doc))
    doc = _preset()
    doc["params"]["leads"].update(default=6, max=6)
    doc["params"]["workers"].update(default=6, max=6)
    assert any("more than 40" in e for e in fleet.validate(doc))


def test_expand_makes_a_spec_the_work_spec_validator_accepts():
    out = fleet.expand(BUILTIN, LAUNCH)
    spec = out["spec"]
    assert workspec.validate(spec) == []
    assert spec["leader"] == {"mode": "none"}
    assert (spec["workspace"], spec["project"], spec["name"]) == ("ws", "proj", "fleet-t")
    assert spec["mesh"] == {"mode": "new"}
    root = spec["agents"][0]
    assert root == {"id": "coord", "kind": "coordinator", "parent": "none",
                    "workflow": "improv-coordinator-v2", "profile": "sr",
                    "task": "로그인 개편\n세부 요구"}
    lead = next(a for a in spec["agents"] if a["id"] == "lead1")
    assert lead["inputs"] == {"relation": "independent"} and "profile" not in lead
    w = next(a for a in spec["agents"] if a["id"] == "lead1-w1")
    assert (w["parent"], w["start_issue"], w["inputs"]) == ("lead1", "pending", {"relation": "stacked"})
    # the board: a root epic, a lane epic per lead under it, nothing for workers
    issues = {i["key"]: i for i in spec["issues"]}
    assert set(issues) == {"root", "lane-lead1", "lane-lead2"}
    assert (issues["root"]["type"], issues["root"]["assignee"], issues["root"]["title"]) == \
        ("epic", "coord", "로그인 개편")
    assert (issues["lane-lead1"]["parent"], issues["lane-lead1"]["type"]) == ("root", "epic")
    assert yaml.safe_load(out["yaml"]) == spec


def test_profiles_resolve_member_then_group_then_preset_then_tier():
    doc = _preset()
    doc["root"]["children"][0]["children"][0]["tier"] = "economy"
    out = fleet.expand(doc, {**LAUNCH, "profiles": {"lead": "ds4", "lead2-w1": "kimi"}},
                       tiers=lambda t: ("glm-flash", "small") if t == "economy" else None)
    by = {r["id"]: r for r in out["agents"]}
    assert by["lead1"]["profile"] == "ds4"            # group pick
    assert by["lead2-w1"]["profile"] == "kimi"        # member pick wins
    assert (by["lead1-w1"]["profile"], by["lead1-w1"]["model"]) == ("glm-flash", "small")  # tier
    doc["root"]["children"][0]["children"][0].pop("tier")
    out = fleet.expand(doc, LAUNCH)
    assert {r["id"]: r["inherits"] for r in out["agents"]}["lead1-w1"] is True


def test_the_root_needs_a_profile_and_a_task():
    with pytest.raises(fleet.FleetError, match="needs a profile"):
        fleet.expand(BUILTIN, {**LAUNCH, "default_profile": ""})
    with pytest.raises(fleet.FleetError, match="opening task"):
        fleet.expand(BUILTIN, {**LAUNCH, "task": "  "})


def test_an_existing_mesh_is_attached_with_prefixed_handles():
    """An import reuses a handle's holder, so in an existing mesh every
    member's handle carries a per-launch prefix."""
    out = fleet.expand(BUILTIN, {**LAUNCH, "mesh": "claunch", "mesh_exists": True,
                                 "prefix": "f1"})
    spec = out["spec"]
    assert spec["mesh"] == {"mode": "attach", "name": "claunch"}
    assert [a["handle"] for a in spec["agents"]][:2] == ["f1-coord", "f1-lead1"]
    assert out["prefix"] == "f1" and out["agents"][1]["handle"] == "f1-lead1"
    auto = fleet.expand(BUILTIN, {**LAUNCH, "mesh": "claunch", "mesh_exists": True})
    assert auto["prefix"].startswith("f") and len(auto["prefix"]) == 9
    new = fleet.expand(BUILTIN, {**LAUNCH, "mesh": "brand-new"})
    assert new["prefix"] == "" and "handle" not in new["spec"]["agents"][0]
    with pytest.raises(fleet.FleetError, match="prefix"):
        fleet.expand(BUILTIN, {**LAUNCH, "prefix": "Bad Prefix"})


def test_the_expanded_spec_imports_as_a_tree_with_the_right_spawn_bodies():
    out = fleet.expand(BUILTIN, {**LAUNCH, "profiles": {"w": "glm-flash"}})
    fake = FakeBackend()
    wi.execute(out["spec"], fake)
    spawns = [c for c in fake.calls if c[0] == "spawn"]
    assert [(p, b["handle"]) for _, p, b in spawns] == [
        (None, "coord"), ("s-coord", "lead1"), ("s-lead1", "lead1-w1"), ("s-lead1", "lead1-w2"),
        ("s-coord", "lead2"), ("s-lead2", "lead2-w1"), ("s-lead2", "lead2-w2")]
    root = spawns[0][2]
    assert (root["profile"], root["project"], root["workflow"], root["role"]) == \
        ("sr", "proj", "improv-coordinator-v2", "worker")
    assert root["issue"] == fake.find_issues("spec:fleet-t")["spec:fleet-t:root"]["id"]
    assert "lead1 (sub-lead, relation independent): 이슈" in root["task"]
    lead = spawns[1][2]
    assert lead["cflow_inputs"] == {"relation": "independent"} and "project" not in lead
    assert "flow_start, side: stack" in lead["task"]
    assert "시작 이슈 대기" in lead["task"]
    w = spawns[2][2]
    assert (w["beads"], w["profile"], w["cflow_inputs"]) == \
        ("existing-pending", "glm-flash", {"relation": "stacked"})
    assert "issue" not in w and "flow_input" in w["task"]
    lane = fake.issues[lead["issue"]]
    assert lane["parent"] == root["issue"] and lane["type"] == "epic"


def test_storage_saves_lists_overrides_and_deletes(home):
    assert [p["name"] for p in fleet.list_presets()] == [fleet.DEFAULT_NAME]
    doc = _preset()
    doc["name"] = "mine"
    row = fleet.save_preset(yaml.safe_dump(doc, allow_unicode=True))
    assert row["source"] == "user" and row["errors"] == []
    assert (fleet.presets_dir() / "mine.yaml").is_file()
    doc["name"] = fleet.DEFAULT_NAME
    doc["title"] = "내 기본"
    assert fleet.save_preset(yaml.safe_dump(doc))["overrides"] == "builtin"
    rows = {p["name"]: p for p in fleet.list_presets()}
    assert rows[fleet.DEFAULT_NAME]["title"] == "내 기본"
    fleet.delete_preset(fleet.DEFAULT_NAME)
    assert {p["name"]: p for p in fleet.list_presets()}[fleet.DEFAULT_NAME]["source"] == "builtin"
    with pytest.raises(fleet.FleetError, match="built-in cannot be deleted"):
        fleet.delete_preset(fleet.DEFAULT_NAME)
    bad = _preset()
    bad["root"]["kind"] = "reviewer"
    with pytest.raises(fleet.FleetError, match="invalid preset"):
        fleet.save_preset(yaml.safe_dump(bad))
    assert fleet.get_preset("mine")[0]["name"] == "mine"

