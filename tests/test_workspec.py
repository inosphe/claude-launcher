"""Work spec (docs/work-spec.md): validation and the dry-run import plan.

A. The packaged example is valid and plans under both leader choices
B. Leader choice: ask refuses without a choice; own spawns; shared joins an
   existing session (candidates enforced); own is impossible on attach
C. Agent tree: exclusive roles only through the leader block; parent must be
   leader or sub-lead; cycles and depth over spawn.max_depth are refused
D. Issues: blocked_by / parent / review / refs / contracts resolve; cycles
   are refused; epics are held only by a sub-lead
E. Subroles: prompt required, must not shadow a mesh role
F. Plan order: board before mesh, leader before agents, parents first
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from claude_launcher import workspec as ws

EXAMPLE = Path(__file__).resolve().parents[1] / "docs/examples/work-spec.example.yaml"


@pytest.fixture
def doc() -> dict:
    return ws.load(EXAMPLE.read_text(encoding="utf-8"))


def _errs(doc: dict) -> str:
    return "\n".join(ws.validate(doc))


# A ------------------------------------------------------------------------

def test_example_is_valid(doc):
    assert ws.validate(doc) == []


def test_load_refuses_non_mapping():
    with pytest.raises(ws.SpecError):
        ws.load("- a\n- b\n")


# B ------------------------------------------------------------------------

def test_ask_without_choice_refuses(doc):
    with pytest.raises(ws.SpecError, match="own or shared"):
        ws.plan(doc)


def test_own_spawns_a_leader_in_the_spec_mesh(doc):
    acts = ws.plan(doc, "own")
    lead = [a for a in acts if a.get("agent") == "leader"]
    assert lead == [{"do": "spawn", "agent": "leader", "role": "leader",
                     "mesh": "spec-night-sense", "workflow": "improv-leader",
                     "parent": None}]
    assert {"do": "mesh-create", "mesh": "spec-night-sense"} in acts


def test_shared_joins_the_existing_session(doc):
    acts = ws.plan(doc, "shared:s29")
    lead = [a for a in acts if a.get("agent") == "leader"]
    assert lead == [{"do": "mesh-join", "agent": "leader", "session": "s29",
                     "mesh": "spec-night-sense", "handle": "lead",
                     "role": "leader"}]
    assert not any(a["do"] == "spawn" and a["agent"] == "leader" for a in acts)


def test_shared_with_single_candidate_needs_no_session(doc):
    assert ws.resolve_leader(doc, "shared")["session"] == "s29"


def test_shared_outside_candidates_refused(doc):
    with pytest.raises(ws.SpecError, match="candidates"):
        ws.plan(doc, "shared:s77")


def test_spec_default_mode_is_used_without_choice(doc):
    doc["leader"]["mode"] = "own"
    assert ws.plan(doc)[0]["do"] == "reference"


def test_own_impossible_on_attach(doc):
    doc["mesh"] = {"mode": "attach", "name": "claunch"}
    doc["leader"]["mode"] = "own"
    assert "impossible with mesh.mode attach" in _errs(doc)
    doc["leader"]["mode"] = "ask"
    with pytest.raises(ws.SpecError, match="attaching"):
        ws.plan(doc, "own")
    acts = ws.plan(doc, "shared:s29")
    assert not any(a["do"] == "mesh-create" for a in acts)


def test_attach_never_joins_a_leader(doc):
    """On attach the mesh already has its leader: no mesh-join, a check."""
    doc["mesh"] = {"mode": "attach", "name": "claunch"}
    named = ws.plan(doc, "shared:s29")
    lead = [a for a in named if a.get("agent") == "leader"]
    assert lead == [{"do": "leader-check", "agent": "leader",
                     "mesh": "claunch", "session": "s29"}]
    # ask on attach is not a refusal: there is nothing left to choose
    assert ws.validate(doc) == []
    anyone = [a for a in ws.plan(doc) if a.get("agent") == "leader"]
    assert anyone == [{"do": "leader-check", "agent": "leader",
                       "mesh": "claunch", "session": None}]
    assert not any(a["do"] == "mesh-join" for a in ws.plan(doc))
    assert ws.plan(doc)[0]["do"] == "leader-check"  # before any board write


# C ------------------------------------------------------------------------

def test_second_leader_role_refused(doc):
    doc["agents"][0]["role"] = "leader"
    assert "exclusive" in _errs(doc)


def test_leader_id_reserved(doc):
    doc["agents"][0]["id"] = "leader"
    assert "reserved" in _errs(doc)


def test_worker_cannot_hold_children(doc):
    doc["agents"][2]["parent"] = "ue1"
    assert "only the leader or a sub-lead" in _errs(doc)


def test_parent_cycle_refused(doc):
    doc["agents"][0]["parent"] = "lead2b"
    doc["agents"].append({"id": "lead2b", "kind": "sub-lead", "parent": "lead2"})
    assert "parent cycle" in _errs(doc)


def test_depth_over_max_refused(doc):
    chain = [{"id": f"l{i}", "kind": "sub-lead", "parent": f"l{i-1}" if i else "leader"}
             for i in range(4)]
    doc["agents"].extend(chain)
    assert "exceeds spawn.max_depth" in _errs(doc)


def test_unknown_role_refused(doc):
    doc["agents"][1]["role"] = "wizard"
    assert "unknown role" in _errs(doc)


def test_custom_role_from_roles_block(doc):
    doc["roles"] = {"roles": {"ue-dev": {"stance": "UE 전문 워커"}}}
    doc["agents"][1]["role"] = "ue-dev"
    assert ws.validate(doc) == []


# D ------------------------------------------------------------------------

def test_unknown_blocked_by_refused(doc):
    doc["issues"][2]["blocked_by"] = ["W9"]
    assert "unknown issue 'W9'" in _errs(doc)


def test_blocked_by_cycle_refused(doc):
    doc["issues"][1]["blocked_by"] = ["W2"]
    assert "blocked_by cycle" in _errs(doc)


def test_parent_must_be_epic(doc):
    doc["issues"][2]["parent"] = "W1"
    assert "not an epic" in _errs(doc)


def test_review_must_be_a_reviewer(doc):
    doc["issues"][1]["review"] = "sys1"
    assert "not a reviewer" in _errs(doc)


def test_dangling_refs_and_contracts_refused(doc):
    doc["issues"][1]["refs"] = ["ref.nope"]
    doc["issues"][1]["contracts"] = ["C.nope"]
    out = _errs(doc)
    assert "unknown reference 'ref.nope'" in out
    assert "unknown contract 'C.nope'" in out


def test_epic_held_only_by_sub_lead(doc):
    doc["issues"][0]["assignee"] = "ue1"
    assert "only by a sub-lead" in _errs(doc)


def test_work_contract_keys_checked(doc):
    doc["issues"][1]["contract"]["owner"] = "x"
    assert "contract: unknown key(s) owner" in _errs(doc)


def test_decision_gate_must_exist(doc):
    doc["decisions"][0]["gates"] = ["W7"]
    assert "unknown issue 'W7'" in _errs(doc)


# E ------------------------------------------------------------------------

def test_subrole_needs_prompt(doc):
    doc["subroles"]["unreal"]["prompt"] = " "
    assert "a prompt is required" in _errs(doc)


def test_subrole_must_not_shadow_a_role(doc):
    doc["subroles"]["reviewer"] = {"prompt": "x"}
    assert "collides with the mesh role" in _errs(doc)


def test_undefined_subrole_refused(doc):
    doc["agents"][2]["subroles"] = ["godot"]
    assert "undefined subrole 'godot'" in _errs(doc)


# F ------------------------------------------------------------------------

def test_plan_order(doc):
    acts = ws.plan(doc, "own")
    first = {}
    for i, a in enumerate(acts):
        first.setdefault(a["do"], i)
    assert first["issue"] < first["dep"] < first["mesh-create"]
    assert first["mesh-create"] < first["mesh-roles"] < first["spawn"]
    spawned = [a["agent"] for a in acts if a["do"] == "spawn"]
    assert spawned.index("leader") < spawned.index("lead2") < spawned.index("ue1")
    epic = next(i for i, a in enumerate(acts) if a.get("key") == "frame")
    w1 = next(i for i, a in enumerate(acts) if a.get("key") == "W1")
    assert epic < w1


def test_plan_points_each_agent_at_its_issue(doc):
    acts = ws.plan(doc, "own")
    by_agent = {a["agent"]: a for a in acts if a["do"] == "spawn"}
    assert by_agent["ue1"]["issue"] == "W1"
    assert by_agent["ue1"]["subroles"] == ["unreal"]
    assert by_agent["lead2"]["issue"] == "frame"
    assert {"do": "stack", "agent": "lead2"} in acts


def test_queue_beyond_first_issue_is_assigned(doc):
    extra = copy.deepcopy(doc["issues"][1])
    extra.update(key="W3", priority=3, title="후속")
    extra.pop("blocked_by", None)
    doc["issues"].append(extra)
    acts = ws.plan(doc, "own")
    assert {"do": "assign", "issue": "W3", "agent": "ue1"} in acts


def test_plan_refuses_invalid_spec(doc):
    doc["issues"][2]["blocked_by"] = ["W9"]
    with pytest.raises(ws.SpecError, match="invalid spec"):
        ws.plan(doc, "own")


def test_plan_labels_and_decision_blocks(doc):
    acts = ws.plan(doc, "own")
    human = next(a for a in acts if a.get("key") == "human")
    assert human["labels"] == ["spec:night-sense", "human"]
    assert {"do": "comment", "issue": "W2",
            "text": "BLOCKED: decision impl.night-source"} in acts


# G: malformed input is reported, never raised --------------------------------

@pytest.mark.parametrize("key, value, expect", [
    ("mesh", "foo", "mesh: must be a mapping"),
    ("leader", "own", "leader: must be a mapping"),
    ("subroles", ["unreal"], "subroles: must be a mapping"),
    ("issues", {"W1": {}}, "issues: must be a list"),
])
def test_top_level_type_errors(doc, key, value, expect):
    doc[key] = value
    assert ws.validate(doc) == [expect]


def test_string_where_a_list_belongs(doc):
    doc["agents"][1]["subroles"] = "unreal"
    doc["issues"][2]["blocked_by"] = "W1"
    doc["decisions"][0]["gates"] = "W2"
    out = ws.validate(doc)
    assert "agents[1].subroles: must be a list of strings" in out
    assert "issues[2].blocked_by: must be a list of strings" in out
    assert "decisions[0].gates: must be a list of strings" in out
    assert not any("'u'" in e for e in out)  # never iterated per letter


def test_nested_mapping_type_errors(doc):
    doc["leader"]["shared"] = "s29"
    doc["issues"][1]["contract"] = ["accept"]
    out = ws.validate(doc)
    assert "leader.shared: must be a mapping" in out
    assert "issues[1].contract: must be a mapping" in out


def test_non_mapping_document():
    assert ws.validate(["x"]) == ["spec: must be a mapping"]
