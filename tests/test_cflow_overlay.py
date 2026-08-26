"""A project file may be a LAYER over a workflow, not a copy of it.

Two layers answer a workflow name and the nearest one wins — whole file
against whole file. That resolution is fine while the two files are genuinely
different workflows, and it is the wrong shape for the case this repository
lives in: the prose, the graph and the protocol of ``improv-worker`` ship to
every repository, while what its steps *check* names tools that exist only
here. Whole-file shadowing makes those four commands cost a copy of the other
thousand lines, and this checkout measured the bill — a 1073-line packaged
workflow copied to 1130 lines, kept in step by a 216-line regex grafter
(``tools/sync_project_layer.py``) written for no other reason than that the
copy drifts.

``extends:`` is the other shape: the project file names its base and carries
only the properties it changes. These tests pin what that means —

* the merge itself (mappings recurse, everything else replaces, ``null``
  deletes), because those three rules are the whole contract a workflow
  author writes against;
* where a base is looked for, and in particular that it is never looked for
  *upward* — a global workflow must be the same workflow for every project
  that runs it;
* that a run snapshots the MERGE. A run reads its snapshot from then on, so a
  layered start that snapshotted only the overlay would be a run with a hole
  in it, opening at the first step whose instructions lived in the base;
* that a file carrying an unresolved ``extends`` is REFUSED rather than
  half-parsed. Silently ignoring the key would hand back a workflow missing
  everything the base carried, and the first thing to notice would be a step
  that is not there.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from claude_launcher import cli
from claude_launcher.cflow import engine, model, state as state_mod

BASE = """
name: base-flow
description: the packaged one
max_visits: 7
steps:
  intake:
    instructions: state the goal
    next: work
  work:
    instructions: do the work
    verify: echo packaged
    next: end
"""


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def project(tmp_path, monkeypatch, home):
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.chdir(proj)
    return proj


@pytest.fixture
def layers(project, home):
    """The packaged workflow globally, and the project layer left empty."""
    _write(home / "workflows" / "base-flow.yaml", BASE)
    return project, home


# --------------------------------------------------------------------------- #
# the merge itself
# --------------------------------------------------------------------------- #
def test_mappings_merge_one_property_at_a_time():
    merged = model.merge_docs(
        {"steps": {"a": {"instructions": "keep", "verify": "old"}, "b": {}}},
        {"steps": {"a": {"verify": "new"}}},
    )
    # The point of the feature in one assertion: naming one field of one step
    # reaches that field and nothing else.
    assert merged == {
        "steps": {"a": {"instructions": "keep", "verify": "new"}, "b": {}}
    }


def test_lists_and_scalars_replace_rather_than_accumulate():
    merged = model.merge_docs(
        {"filter_roles": {"roles": ["worker", "leader"]}, "max_visits": 7},
        {"filter_roles": {"roles": ["reviewer"]}, "max_visits": 2},
    )
    assert merged["filter_roles"]["roles"] == ["reviewer"]
    assert merged["max_visits"] == 2


def test_an_explicit_null_deletes_what_omitting_would_inherit():
    merged = model.merge_docs(
        {"steps": {"a": {"verify": "packaged", "next": "b"}}},
        {"steps": {"a": {"verify": None}}},
    )
    assert merged == {"steps": {"a": {"next": "b"}}}


def test_the_base_is_not_mutated_by_a_layer_over_it():
    base = {"steps": {"a": {"verify": "packaged"}}}
    model.merge_docs(base, {"steps": {"a": {"verify": "mine"}}})
    assert base == {"steps": {"a": {"verify": "packaged"}}}


def test_composing_folds_the_chain_from_its_far_end():
    merged = model.compose_docs(
        [
            {"extends": "middle", "steps": {"a": {"verify": "nearest"}}},
            {"extends": "base", "steps": {"a": {"verify": "middle", "next": "b"}}},
            {"name": "base", "steps": {"a": {"instructions": "do"}, "b": {}}},
        ]
    )
    assert merged["name"] == "base"
    assert merged["steps"]["a"] == {
        "instructions": "do",
        "next": "b",
        "verify": "nearest",
    }
    # The key named an edge of a chain that no longer exists once folded.
    assert model.EXTENDS_KEY not in merged


# --------------------------------------------------------------------------- #
# loading a layer
# --------------------------------------------------------------------------- #
def test_a_project_layer_inherits_the_base_and_replaces_one_field(layers):
    project, home = layers
    mine = _write(
        project / ".claunch" / "workflows" / "base-flow.yaml",
        "extends: base-flow\nsteps:\n  work:\n    verify: pytest -q\n",
    )

    composed = state_mod.load_workflow("base-flow")

    assert composed.path == mine
    assert composed.bases == (home / "workflows" / "base-flow.yaml",)
    assert composed.layered
    wf = composed.workflow
    # Everything the base carried is here, from a four-line file.
    assert wf.name == "base-flow"
    assert wf.description == "the packaged one"
    assert wf.max_visits == 7
    assert set(wf.steps) == {"intake", "work"}
    assert wf.steps["intake"].instructions.strip() == "state the goal"
    assert wf.steps["work"].instructions.strip() == "do the work"
    # And the one property the layer exists for is the layer's.
    assert wf.steps["work"].verify.command == "pytest -q"


def test_a_layer_may_add_a_step_the_base_does_not_have(layers):
    project, _home = layers
    _write(
        project / ".claunch" / "workflows" / "base-flow.yaml",
        "extends: base-flow\n"
        "steps:\n"
        "  work:\n"
        "    next: sweep\n"
        "  sweep:\n"
        "    instructions: this repository sweeps\n"
        "    next: end\n",
    )
    wf = state_mod.load_workflow("base-flow").workflow
    assert set(wf.steps) == {"intake", "work", "sweep"}
    assert wf.steps["work"].next == "sweep"


def test_a_layer_may_drop_an_inherited_property_with_null(layers):
    project, _home = layers
    _write(
        project / ".claunch" / "workflows" / "base-flow.yaml",
        "extends: base-flow\nsteps:\n  work:\n    verify: ~\n",
    )
    wf = state_mod.load_workflow("base-flow").workflow
    assert wf.steps["work"].verify is None


def test_a_file_that_extends_nothing_composes_to_its_own_bytes(layers):
    _project, home = layers
    composed = state_mod.load_workflow("base-flow")
    assert composed.chain == (home / "workflows" / "base-flow.yaml",)
    assert not composed.layered
    # Verbatim, not re-serialised: layering costs nothing where it is unused,
    # and a run of an ordinary workflow snapshots the file a human wrote.
    assert composed.text == BASE


def test_a_layer_may_extend_a_sibling_in_its_own_layer(project, home):
    _write(project / ".claunch" / "workflows" / "base-flow.yaml", BASE)
    _write(
        project / ".claunch" / "workflows" / "variant.yaml",
        "extends: base-flow\nname: variant\n",
    )
    composed = state_mod.load_workflow("variant")
    assert composed.bases == (project / ".claunch" / "workflows" / "base-flow.yaml",)
    assert composed.workflow.name == "variant"


def test_a_layer_may_name_its_base_by_path(project, home):
    base = _write(project / "elsewhere" / "base-flow.yaml", BASE)
    _write(
        project / ".claunch" / "workflows" / "variant.yaml",
        f"extends: {Path('..') / '..' / 'elsewhere' / 'base-flow.yaml'}\n"
        "name: variant\n",
    )
    composed = state_mod.load_workflow("variant")
    assert composed.bases == (base,)


def test_a_base_is_never_searched_upward(project, home):
    """A global workflow cannot pick up a project's file of the same name.

    Otherwise a base would mean something different in every directory it was
    run from, and one project could quietly redefine what another's runs are
    built on.
    """
    _write(project / ".claunch" / "workflows" / "base-flow.yaml", BASE)
    _write(home / "workflows" / "layered.yaml", "extends: base-flow\nname: layered\n")

    with pytest.raises(model.WorkflowError) as exc:
        state_mod.load_workflow("layered")
    message = str(exc.value)
    assert "base-flow" in message
    assert "never upward" in message


def test_a_cycle_is_named_rather_than_looped(project, home):
    _write(
        project / ".claunch" / "workflows" / "a.yaml", "extends: b.yaml\nname: a\n"
    )
    _write(
        project / ".claunch" / "workflows" / "b.yaml", "extends: a.yaml\nname: b\n"
    )
    with pytest.raises(model.WorkflowError) as exc:
        state_mod.load_workflow("a")
    assert "cycle" in str(exc.value)


def test_a_missing_base_says_which_file_wanted_it(project, home):
    mine = _write(
        project / ".claunch" / "workflows" / "variant.yaml",
        "extends: nowhere\nname: variant\n",
    )
    with pytest.raises(model.WorkflowError) as exc:
        state_mod.load_workflow("variant")
    message = str(exc.value)
    assert str(mine) in message
    assert "nowhere" in message


def test_an_empty_extends_is_refused_at_the_key():
    with pytest.raises(model.WorkflowError) as exc:
        model.extends_ref({"extends": "   "})
    assert "empty" in str(exc.value)


def test_a_path_loader_refuses_a_file_it_cannot_compose(project, home):
    mine = _write(
        project / ".claunch" / "workflows" / "variant.yaml",
        "extends: base-flow\nname: variant\n",
    )
    # model.load holds a path and no layers: it must not hand back a workflow
    # with the base's half missing.
    with pytest.raises(model.WorkflowError) as exc:
        model.load(mine)
    assert "load_workflow" in str(exc.value)


def test_parsing_a_doc_that_still_carries_extends_is_refused():
    with pytest.raises(model.WorkflowError) as exc:
        model.parse("extends: base\nsteps:\n  a:\n    instructions: x\n")
    assert "load_workflow" in str(exc.value)


# --------------------------------------------------------------------------- #
# a run of a layered workflow
# --------------------------------------------------------------------------- #
def test_a_run_snapshots_the_merge_not_the_overlay(layers):
    project, home = layers
    _write(
        project / ".claunch" / "workflows" / "base-flow.yaml",
        "extends: base-flow\nsteps:\n  work:\n    verify: pytest -q\n",
    )

    payload = engine.start("base-flow", context="t")
    assert payload["step_id"] == "intake"
    # The run reads its snapshot from here on; a snapshot of the overlay alone
    # would open at a step whose instructions are not in it.
    snapshot = yaml.safe_load(
        (state_mod.scope_dir() / "workflow.yaml").read_text(encoding="utf-8")
    )
    assert set(snapshot["steps"]) == {"intake", "work"}
    assert snapshot["steps"]["work"]["verify"] == "pytest -q"
    assert model.EXTENDS_KEY not in snapshot

    status = engine.status()
    assert status["source"] == str(
        project / ".claunch" / "workflows" / "base-flow.yaml"
    )
    assert status["origin"] == state_mod.LAYER_PROJECT
    # Which file it is a layer OVER — "the project one" stopped being an
    # answer the moment the project file was four lines.
    assert status["extends"] == [str(home / "workflows" / "base-flow.yaml")]


def test_an_ordinary_run_says_nothing_about_layers(layers):
    engine.start("base-flow", context="t")
    assert "extends" not in engine.status()


def test_editing_the_base_mid_run_does_not_move_a_running_position(layers):
    project, home = layers
    _write(
        project / ".claunch" / "workflows" / "base-flow.yaml",
        "extends: base-flow\nsteps:\n  work:\n    verify: pytest -q\n",
    )
    engine.start("base-flow", context="t")
    _write(home / "workflows" / "base-flow.yaml", BASE.replace("state the goal", "CHANGED"))
    assert "CHANGED" not in engine.status()["instructions"]


# --------------------------------------------------------------------------- #
# authoring one: `cflow add --overlay`, and the listings
# --------------------------------------------------------------------------- #
def test_add_overlay_writes_a_layer_instead_of_a_copy(layers, capsys):
    project, home = layers

    assert cli.main(["cflow", "add", "base-flow", "--project", "--overlay"]) == 0

    mine = project / ".claunch" / "workflows" / "base-flow.yaml"
    text = mine.read_text(encoding="utf-8")
    # The whole point: what lands is a layer, not the base's bytes again.
    assert "extends: base-flow" in text
    assert "state the goal" not in text
    assert len(text.splitlines()) < len(BASE.splitlines()) + 12
    composed = state_mod.load_workflow("base-flow")
    assert composed.bases == (home / "workflows" / "base-flow.yaml",)
    assert set(composed.workflow.steps) == {"intake", "work"}
    assert "inherited" in capsys.readouterr().out


def test_add_overlay_refuses_a_layer_that_could_not_find_its_base(project, home, capsys):
    """A layer written where its base cannot be reached is removed, not left.

    Promoting a project's layer to the global layer is the way to get one: a
    base is searched from the extending file's own layer downward, so a global
    file cannot reach the project sibling the project file was extending. The
    stub is loaded back after it is written for exactly this — otherwise the
    file sits there until somebody tries to run it.
    """
    _write(project / ".claunch" / "workflows" / "base-flow.yaml", BASE)
    _write(
        project / ".claunch" / "workflows" / "variant.yaml",
        "extends: base-flow\nname: variant\n",
    )

    assert cli.main(["cflow", "add", "variant", "--overlay"]) == 1

    assert "would not compose" in capsys.readouterr().err
    assert not (home / "workflows" / "variant.yaml").exists()


def test_ls_and_show_say_what_a_layer_is_a_layer_over(layers, capsys):
    _project, home = layers
    _write(
        _project_layer(layers) / "base-flow.yaml",
        "extends: base-flow\nsteps:\n  work:\n    verify: pytest -q\n",
    )

    assert cli.main(["cflow", "ls"]) == 0
    listed = capsys.readouterr().out
    assert f"extends [{home / 'workflows' / 'base-flow.yaml'}]" in listed
    # The step count is the merge's, not the four-line file's.
    assert "2 steps" in listed

    assert cli.main(["cflow", "show", "base-flow"]) == 0
    shown = capsys.readouterr().out
    assert f"extends: {home / 'workflows' / 'base-flow.yaml'}" in shown


def _project_layer(layers) -> Path:
    project, _home = layers
    return project / ".claunch" / "workflows"
