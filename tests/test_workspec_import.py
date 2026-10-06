"""The work spec executor (``workspec_import``) against a fake backend.

Covers the example spec end to end (shared and own leader), a rerun that
creates nothing, the attach leader check, and the CLI's dry-run commands.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from claude_launcher import cli_spec, workspec, workspec_import as wi

EXAMPLE = Path(__file__).resolve().parents[1] / "docs" / "examples" / \
    "work-spec.example.yaml"


def example() -> dict:
    return workspec.load(EXAMPLE.read_text(encoding="utf-8"))


class FakeBackend(wi.Backend):
    """An in-memory board and daemon; ``calls`` records every write."""

    def __init__(self) -> None:
        self.issues: dict = {}      # id -> issue
        self.meshes: dict = {}      # name -> {"members": {...}, "links": set, "roles": doc}
        self.calls: list = []
        self.files: set = {"Source/Game/Systems/Sense.h"}
        self.n = 0

    def _w(self, *call):
        self.calls.append(call)

    def find_issues(self, label):
        return {i["external_ref"]: i for i in self.issues.values()
                if label in i["labels"]}

    def create_issue(self, **kw):
        self.n += 1
        iid = f"t-{self.n}"
        self.issues[iid] = {"id": iid, "assignee": None, "deps": [],
                            "comments": [], **kw}
        self._w("create_issue", kw["external_ref"])
        return iid

    def add_dep(self, issue, blocked_by):
        self.issues[issue]["deps"].append(blocked_by)
        self._w("add_dep", issue, blocked_by)

    def comment(self, issue, text):
        self.issues[issue]["comments"].append(text)
        self._w("comment", issue, text)

    def blockers(self, issue):
        return set(self.issues[issue]["deps"])

    def comments(self, issue):
        return list(self.issues[issue]["comments"])

    def assign(self, issue, session):
        self.issues[issue]["assignee"] = session
        self._w("assign", issue, session)

    def get_mesh(self, mesh):
        m = self.meshes.get(mesh)
        if m is None:
            return None
        return {"members": list(m["members"].values()),
                "member_links": [{"a": a, "b": b, "enabled": True}
                                 for a, b in m["links"]]}

    def create_mesh(self, mesh):
        self.meshes[mesh] = {"members": {}, "links": set(), "roles": None}
        self._w("create_mesh", mesh)

    def get_roles(self, mesh):
        doc = self.meshes[mesh]["roles"]
        return {"custom": doc is not None,
                "yaml": yaml.safe_dump(doc) if doc else ""}

    def put_roles(self, mesh, doc):
        self.meshes[mesh]["roles"] = copy.deepcopy(doc)
        self._w("put_roles", mesh)

    def _add(self, mesh, session, handle, role):
        self.meshes[mesh]["members"][handle] = {
            "handle": handle, "session": session, "role": role,
            "roles": [role], "category": "running"}

    def join(self, mesh, session, handle, role):
        self._add(mesh, session, handle, role)
        self._w("join", mesh, session, handle)

    def spawn(self, parent, body):
        self.n += 1
        name = f"s-{body['handle']}"
        self._add(body["mesh"], name, body["handle"], body["role"])
        self._w("spawn", parent, copy.deepcopy(body))
        return name

    def connect(self, mesh, a, b):
        self.meshes[mesh]["links"].add((a, b))
        self._w("connect", mesh, a, b)

    def file_exists(self, path):
        return any(path.endswith(f) for f in self.files)


def _spawns(fb):
    return {c[2]["handle"]: (c[1], c[2]) for c in fb.calls if c[0] == "spawn"}


def test_shared_import_builds_board_mesh_and_agents():
    fb = FakeBackend()
    log = wi.execute(example(), fb, leader_choice="shared:s29", base_dir="/ws")
    by_ref = {i["external_ref"]: i for i in fb.issues.values()}
    assert len(by_ref) == 6  # 2 contracts + frame + W1 + W2 + human
    frame, w1, w2 = (by_ref[f"spec:night-sense:{k}"] for k in ("frame", "W1", "W2"))
    human = by_ref["spec:night-sense:human"]
    # epic first, children point at it; deps and the decision comment land
    assert w1["parent"] == frame["id"] and frame["parent"] is None
    assert w2["deps"] == [w1["id"]] and human["deps"] == [w1["id"]]
    assert w2["comments"] == ["BLOCKED: decision impl.night-source"]
    assert human["labels"] == ["spec:night-sense", "human"]
    # bodies: the board's headings, references and contracts filled in
    assert frame["description"].startswith("## 목표\n야간에")
    assert "R2 야간 계수는" in frame["description"]  # epic carries every reference
    assert "## 완료 증거 기준\n- rescan" in w1["description"]
    contract_id = by_ref["spec:night-sense:contract:C.store.night-multiplier"]["id"]
    assert f"C.store.night-multiplier: {contract_id}" in w1["description"]
    assert "## 미결 결정\n- impl.night-source" in w2["description"]
    assert "## 사람 몫\n- Source/AllComponents.h" in human["description"]
    # mesh: created, subrole uploaded as a role whose stance is its prompt
    mesh = fb.meshes["spec-night-sense"]
    assert mesh["roles"]["roles"]["shader"]["stance"].startswith("너는 셰이더")
    assert mesh["members"]["lead"]["session"] == "s29"
    # agents: parents first, under the right parent session, with the issue
    sp = _spawns(fb)
    assert list(sp) == ["worker-lead2", "worker-ue1", "worker-sys1", "reviewer-1"]
    assert sp["worker-lead2"][0] == "s29"
    assert sp["worker-ue1"][0] == "s-worker-lead2"
    assert sp["worker-lead2"][1]["issue"] == frame["id"]
    assert "stack 서브 런" in sp["worker-lead2"][1]["task"]
    ue1 = sp["worker-ue1"][1]
    assert ue1["subroles"] == ["shader"] and ue1["issue"] == w1["id"]
    assert ue1["task"].startswith("너는 셰이더")  # prompt before the assignment
    assert ue1["worktree"] == "spec-night-sense-ue1"
    assert ue1["workspace"] == "shelter-zero"
    rv = sp["reviewer-1"][1]
    # the API's key (no_issue is the MCP tool's and the daemon ignores it)
    assert rv["beads"] is False and "no_issue" not in rv
    assert rv["workflow"] == "-" and "worktree" not in rv
    assert all("beads" not in b for h, (_, b) in sp.items() if h != "reviewer-1")
    assert w1["id"] in rv["task"]
    assert ("spec-night-sense", "worker-sys1", "worker-ue1") in \
        [c[1:] for c in fb.calls if c[0] == "connect"]
    assert all(r["result"] in (wi.CREATED, wi.DONE, wi.SKIPPED) for r in log)


def test_rerun_writes_nothing():
    fb = FakeBackend()
    wi.execute(example(), fb, leader_choice="shared:s29", base_dir="/ws")
    before = len(fb.calls)
    log = wi.execute(example(), fb, leader_choice="shared:s29", base_dir="/ws")
    assert fb.calls[before:] == []
    assert {r["result"] for r in log} <= {wi.EXISTS, wi.SKIPPED, wi.DONE}
    assert not [r for r in log if r["result"] == wi.DONE and r["do"] != "reference"]


def test_rerun_after_a_stop_continues():
    fb = FakeBackend()
    fb.files = set()  # the file reference is missing: stops before any write
    with pytest.raises(wi.SpecImportError, match="no file"):
        wi.execute(example(), fb, leader_choice="shared:s29", base_dir="/ws")
    assert fb.calls == []
    fb.files = {"Source/Game/Systems/Sense.h"}
    wi.execute(example(), fb, leader_choice="shared:s29", base_dir="/ws")
    assert len(fb.issues) == 6


def test_own_leader_is_a_top_level_session_and_needs_a_profile():
    with pytest.raises(wi.SpecImportError, match="--profile"):
        wi.execute(example(), FakeBackend(), leader_choice="own", base_dir="/ws")
    fb = FakeBackend()
    wi.execute(example(), fb, leader_choice="own", profile="p1", base_dir="/ws")
    parent, body = _spawns(fb)["leader"]
    assert parent is None and body["profile"] == "p1"
    assert body["name"] == "lead-night-sense" and body["role"] == "leader"
    assert body["workflow"] == "improv-leader" and body["beads"] is False
    assert _spawns(fb)["worker-lead2"][0] == "s-leader"


def test_attach_checks_the_live_leader_before_writing():
    doc = example()
    doc["mesh"] = {"mode": "attach", "name": "team"}
    fb = FakeBackend()
    fb.create_mesh("team")
    fb._add("team", "s29", "boss", "leader")
    fb.calls.clear()
    with pytest.raises(wi.SpecImportError, match="not the leader"):
        wi.execute(doc, fb, leader_choice="shared:s7", base_dir="/ws")
    assert fb.calls == []
    wi.execute(doc, fb, base_dir="/ws")
    assert _spawns(fb)["worker-lead2"][0] == "s29"
    assert "부모: boss" in _spawns(fb)["reviewer-1"][1]["task"]


def test_roles_merge_into_an_existing_custom_set():
    doc = example()
    doc["mesh"] = {"mode": "attach", "name": "team"}
    fb = FakeBackend()
    fb.create_mesh("team")
    fb._add("team", "s29", "boss", "leader")
    fb.meshes["team"]["roles"] = {"version": 1, "roles": {"qa": {"stance": "x"}}}
    wi.execute(doc, fb, base_dir="/ws")
    assert set(fb.meshes["team"]["roles"]["roles"]) == {"qa", "shader"}


def test_cli_check_and_plan(capsys):
    import argparse
    ns = argparse.Namespace(file=str(EXAMPLE), leader="shared:s29", json=False)
    assert cli_spec._cmd_check(ns) == 0
    assert capsys.readouterr().out.strip() == "ok"
    assert cli_spec._cmd_plan(ns) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0].startswith("reference")
    assert "mesh-join" in out and "session=s29" in out
    ns.leader = ""
    assert cli_spec._cmd_plan(ns) == 2  # leader.mode ask needs a choice
    assert "own or shared" in capsys.readouterr().err


def test_queue_beyond_the_first_issue_is_assigned_once():
    doc = example()
    doc["issues"].append({"key": "W3", "type": "task", "parent": "frame",
                          "priority": 3, "title": "후속", "assignee": "ue1"})
    fb = FakeBackend()
    wi.execute(doc, fb, leader_choice="shared:s29", base_dir="/ws")
    w3 = next(i for i in fb.issues.values()
              if i["external_ref"] == "spec:night-sense:W3")
    assert w3["assignee"] == "s-worker-ue1"
    assert _spawns(fb)["worker-ue1"][1]["issue"] != w3["id"]  # W1 (P2) first
    before = len(fb.calls)
    wi.execute(doc, fb, leader_choice="shared:s29", base_dir="/ws")
    assert fb.calls[before:] == []


def test_rerun_after_a_stop_mid_board_writes_missing_deps_and_comments_once():
    class Flaky(FakeBackend):
        fail_at = 5  # the 5th create: contracts 2 + frame + W1 exist, W2 fails

        def create_issue(self, **kw):
            if self.n + 1 == self.fail_at:
                self.fail_at = 0
                raise wi.SpecImportError("br create failed")
            return super().create_issue(**kw)

    fb = Flaky()
    with pytest.raises(wi.SpecImportError, match="br create failed"):
        wi.execute(example(), fb, leader_choice="shared:s29", base_dir="/ws")
    assert len(fb.issues) == 4
    wi.execute(example(), fb, leader_choice="shared:s29", base_dir="/ws")
    by_ref = {i["external_ref"]: i for i in fb.issues.values()}
    w1 = by_ref["spec:night-sense:W1"]["id"]
    assert by_ref["spec:night-sense:W2"]["deps"] == [w1]
    assert by_ref["spec:night-sense:human"]["deps"] == [w1]
    assert by_ref["spec:night-sense:W2"]["comments"] == [
        "BLOCKED: decision impl.night-source"]
    before = len(fb.calls)
    wi.execute(example(), fb, leader_choice="shared:s29", base_dir="/ws")
    assert fb.calls[before:] == []


def test_rerun_after_a_stop_mid_deps_completes_them():
    class Flaky(FakeBackend):
        armed = True

        def add_dep(self, issue, blocked_by):
            if self.armed and self.issues[issue]["external_ref"].endswith(":human"):
                self.armed = False
                raise wi.SpecImportError("dep failed")
            super().add_dep(issue, blocked_by)

    fb = Flaky()
    with pytest.raises(wi.SpecImportError, match="dep failed"):
        wi.execute(example(), fb, leader_choice="shared:s29", base_dir="/ws")
    wi.execute(example(), fb, leader_choice="shared:s29", base_dir="/ws")
    by_ref = {i["external_ref"]: i for i in fb.issues.values()}
    w1 = by_ref["spec:night-sense:W1"]["id"]
    assert by_ref["spec:night-sense:W2"]["deps"] == [w1]  # not written twice
    assert by_ref["spec:night-sense:human"]["deps"] == [w1]
    assert len(by_ref["spec:night-sense:W2"]["comments"]) == 1
