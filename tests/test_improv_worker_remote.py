"""``improv-worker-remote``: the PR-landing variant is a layer, not a copy.

The bundled worker lands through a local ``--no-ff`` merge the leader runs.
A worker whose landing is a pull request on a (private, enterprise) GitHub
host has the same round -- intake, targeted tests, the two landing doors, the
rebase gate, peer review, the wait, the landed gate, wrapup -- and a different
*medium*: the branch is pushed, a PR points at the reviewed tip, and the
evidence of landing is that the fetched remote base contains that tip.

These tests pin the shape of that arrangement rather than its prose:

* it is written as ``extends: improv-worker`` and overrides nothing of the
  base's instructions (the overlay rule improv-pm already states: rewriting a
  step's prose makes one rule run two ways under one name);
* the four places it does touch are the ones the medium needs -- a
  ``remote-setup`` step after ``branch-setup``, a ``pr-open`` step after peer
  review passes, the ``landed`` checklist fetching before it asks, and no
  ``default_role`` (the wizard keeps picking the local variant);
* the gates are the unchanged scripts, aimed by the branch's upstream;
* this repository's project copy composes over the project ``improv-worker``
  and so inherits its ``verify`` gates -- the whole reason that copy exists;
* the leader treats a ``pr:`` row as one more candidate in the same table,
  merged by ``gh pr merge`` with the reviewed tip pinned.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from claude_launcher.cflow import model, state as state_mod

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / ".claunch" / "workflows"
NAME = "improv-worker-remote"
BASE = "improv-worker"


def _bundled_doc(name: str) -> dict:
    path = dict(state_mod.bundled_workflows())[name]
    return model.read_doc(path.read_text(encoding="utf-8"), where=str(path))


@pytest.fixture(scope="module")
def remote() -> model.Workflow:
    return state_mod.load_bundled(NAME)


@pytest.fixture(scope="module")
def worker() -> model.Workflow:
    return state_mod.load_bundled(BASE)


# --------------------------------------------------------------------------- #
# it is a layer
# --------------------------------------------------------------------------- #
def test_it_extends_the_bundled_worker_and_model_load_refuses_it():
    doc = _bundled_doc(NAME)
    assert model.extends_ref(doc) == BASE
    with pytest.raises(model.WorkflowError, match="extends"):
        model.load(dict(state_mod.bundled_workflows())[NAME])


def test_load_bundled_composes_against_the_shipped_sibling(remote, worker):
    """The bundle is not a search layer, so the base of a shipped layer is
    the shipped file next to it -- and the composed workflow carries the
    base's whole round."""
    assert remote.name == NAME
    assert set(worker.steps) <= set(remote.steps)
    assert remote.start == worker.start == "intake"
    assert remote.filter_roles.roles == worker.filter_roles.roles == ("worker",)


def test_load_bundled_names_a_missing_sibling(tmp_path, monkeypatch):
    """A shipped layer over a base the bundle does not ship is a packaging
    error, and the message names the file that wanted it."""
    (tmp_path / "layer.yaml").write_text("extends: nope\nname: layer\n", "utf-8")
    monkeypatch.setattr(state_mod, "bundled_workflows_dir", lambda: tmp_path)
    with pytest.raises(model.WorkflowError, match="ships no such workflow"):
        state_mod.load_bundled("layer")


def test_the_layer_overrides_no_instructions_of_the_base():
    """improv-pm's overlay rule, applied to the shipped layer: it may add
    steps, wire ``next`` and change gates, but a base step's prose is the
    base's. Every step id the layer names that the base also has carries no
    ``instructions`` of its own."""
    layer = _bundled_doc(NAME)["steps"]
    base = _bundled_doc(BASE)["steps"]
    rewritten = [
        step for step, raw in layer.items()
        if step in base and isinstance(raw, dict) and "instructions" in raw
    ]
    assert rewritten == [], (
        f"the layer rewrites the prose of base step(s) {rewritten}; add a step "
        "or change a gate instead"
    )


def test_the_layer_touches_only_the_places_the_medium_needs():
    """The overlay's whole footprint, by step id. A new key here is a new
    divergence from the local worker and has to be argued into this list."""
    layer = _bundled_doc(NAME)["steps"]
    assert set(layer) == {
        "branch-setup",  # next -> remote-setup
        "remote-setup",  # new
        "peer-review",  # pass -> pr-open
        "pr-open",  # new
        "await-landing",  # title only (a hook for the project layer's probe)
        "landed",  # checklist items
    }
    assert layer["branch-setup"] == {"next": "remote-setup"}
    assert layer["peer-review"] == {"select": {"options": {"pass": {"next": "pr-open"}}}}
    assert set(layer["await-landing"]) == {"title"}
    assert set(layer["landed"]) == {"checklist"}


# --------------------------------------------------------------------------- #
# the round, rewired
# --------------------------------------------------------------------------- #
def test_remote_setup_sits_between_branch_setup_and_work(remote, worker):
    assert worker.steps["branch-setup"].next == "work"
    assert remote.steps["branch-setup"].next == "remote-setup"
    assert remote.steps["remote-setup"].next == "work"
    text = remote.steps["remote-setup"].instructions
    # what it fixes: the remote (from git config, never guessed), the gh
    # client on that host, the push safety, and the upstream that aims the gates
    assert "claunch.pr.remote" in text
    assert "gh auth status --hostname" in text
    assert "--set-upstream-to=<remote>/<base>" in text
    assert "push.default" in text
    assert "GH_ENTERPRISE_TOKEN" in text


def test_pr_open_sits_between_peer_review_pass_and_the_request(remote, worker):
    assert worker.steps["peer-review"].select.options["pass"].next == "integration-request"
    assert remote.steps["peer-review"].select.options["pass"].next == "pr-open"
    assert remote.steps["pr-open"].next == "integration-request"
    # the other door out of peer review is untouched
    assert remote.steps["peer-review"].select.options["changes"].next == "work"
    text = remote.steps["pr-open"].instructions
    assert "gh pr create" in text
    assert "-R <host>/<owner>/<repo>" in text
    assert "HEAD:refs/heads/<브랜치>" in text  # explicit refspec, never a bare push
    assert "--force-with-lease" in text
    assert "pr: <url>" in text  # the marker piece the leader keys on


def test_the_request_and_the_wait_are_the_base_steps(remote, worker):
    """The request itself (in_review, the LANDING REQUEST comment, the nudge)
    and the wait are inherited verbatim -- the layer changes the medium, not
    the protocol."""
    for step in ("integration-request", "await-landing", "rebase", "landing", "wrapup"):
        assert remote.steps[step].instructions == worker.steps[step].instructions, step
    assert remote.steps["await-landing"].select.options["landed"].next == "landed"


def test_the_landed_gate_fetches_then_asks_the_upstream(remote, worker):
    """The base asks whether some other local branch merged the tip. A PR's
    merge commit lives on the remote base and is not in this repository
    until fetched -- so the item fetches first (the upstream's remote, which
    remote-setup pointed at ``<remote>/<base>``) and asks that ref."""
    base_items = {i.id: i.check for i in worker.steps["landed"].checklist.items}
    items = {i.id: i.check for i in remote.steps["landed"].checklist.items}
    assert set(items) == set(base_items) == {"merged", "frozen"}
    assert items["frozen"] == base_items["frozen"]
    assert "--fetch" in items["merged"]
    assert items["merged"].endswith("tools/landed_check.py --fetch --target @{upstream}")
    # the rest of the gate is the base's: same door out, same prompt
    assert remote.steps["landed"].checklist.then == worker.steps["landed"].checklist.then == "wrapup"
    assert remote.steps["landed"].done_when == worker.steps["landed"].done_when


def test_it_does_not_volunteer_for_the_worker_role(remote, worker):
    """Two workflows volunteering for ``worker`` would settle by priority and
    then by name; the local variant is the default and nobody should have to
    remember which name sorts first. The PR variant is chosen explicitly."""
    assert worker.default_role == "worker"
    assert remote.default_role is None
    # but it is still driven by workers only, and its children run the local
    # worker: they merge into this branch, and only this layer reaches the remote
    assert remote.filter_roles.type == "whitelist"
    assert remote.default_child_cflow == "improv-worker"


# --------------------------------------------------------------------------- #
# this repository's project copy
# --------------------------------------------------------------------------- #
def test_the_project_copy_composes_over_the_project_worker_with_its_gates():
    """The project layer exists to graft ``verify``/``awaits`` on. A layer
    file there resolves its base from its own layer downward, so the project
    copy of improv-worker-remote inherits the project improv-worker's gates
    without spelling them."""
    composed = model.compose(
        PROJECT / f"{NAME}.yaml", resolve=state_mod.base_resolver(str(ROOT))
    )
    assert composed.layered
    assert composed.bases == (PROJECT / f"{BASE}.yaml",)
    wf = composed.workflow
    assert wf.steps["review"].verify is not None
    assert "changed_tests.py" in wf.steps["review"].verify.command
    assert wf.steps["rebase"].verify is not None
    assert "merge_ready.py" in wf.steps["rebase"].verify.command
    assert wf.steps["wrapup"].verify is not None
    # and the one thing the project copy adds itself: the wait's probe fetches
    probe = wf.steps["await-landing"].awaits.command(wf.steps["await-landing"])
    assert "--fetch" in probe
    assert "merge_ready.py" in probe


def test_the_project_copy_is_the_packaged_layer_plus_its_grafted_fields():
    """Same drift rule as the other overrides (tools/sync_project_layer.py):
    strip the grafted fields and the project copy is the packaged file."""
    packaged = _bundled_doc(NAME)
    project = model.read_doc((PROJECT / f"{NAME}.yaml").read_text(encoding="utf-8"))
    for step in project["steps"].values():
        if isinstance(step, dict):
            step.pop("awaits", None)
            step.pop("verify", None)
    assert project == packaged


# --------------------------------------------------------------------------- #
# the leader's side
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("layer", ["bundled", "project"])
def test_the_leader_reads_a_pr_row_as_a_candidate_in_the_same_table(layer):
    if layer == "bundled":
        leader = state_mod.load_bundled("improv-leader")
    else:
        leader = model.load(PROJECT / "improv-leader.yaml")
    standby = leader.steps["standby"].instructions
    preflight = leader.steps["integrate-preflight"].instructions
    integrate = leader.steps["integrate"].instructions
    # the row: same table, keyed by the marker piece the worker writes
    assert "pr: <url>" in standby and "PR 행" in standby
    # the screening: the same merge_ready gate, after a fetch
    assert "PR 후보" in preflight
    assert "git fetch <remote>" in preflight
    assert "merge_ready.py" in preflight
    # the merge: gh, merge commit, reviewed tip pinned, master followed and pushed
    assert "gh pr merge" in integrate
    assert "--merge --match-head-commit <tip>" in integrate
    assert "git merge --ff-only <remote>/<base>" in integrate
    assert "git push <remote> master:refs/heads/<base>" in integrate
    # the base push is the leader's own call, on fast-forward evidence only
    assert "git merge-base --is-ancestor <remote>/<base> master" in integrate
    # and the chore(beads) commit rides to the remote base too
    assert "git push <remote> master:refs/heads/<base>" in leader.steps["sweep"].instructions


def test_the_leader_spawns_the_pr_worker_by_name():
    leader = state_mod.load_bundled("improv-leader")
    intake = leader.steps["intake"].instructions
    assert "workflow: improv-worker-remote" in intake
    assert "claunch.pr.remote" in intake
    assert "gh auth status --hostname <host>" in intake
