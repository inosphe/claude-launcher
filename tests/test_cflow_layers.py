"""Where a workflow name resolves, and who gets told which file won.

Two layers answer a name — the project's ``.claunch/workflows/`` and the
shared ``~/.claude-launcher/workflows/`` — and the nearest one wins. That was
always true and never tested; what is new is that the shared layer is
actually populated (by ``claunch install``, and by ``cflow add``), so the
same name really can exist twice. These tests pin both halves: the
resolution, and the reporting of what resolution passed over.
"""

from __future__ import annotations

import pytest

from claude_launcher import cli, install as install_mod
from claude_launcher.cflow import (
    engine,
    install as cflow_install,
    model,
    state as state_mod,
)

TINY = """
name: {name}
description: {desc}
steps:
  only:
    instructions: do the thing
"""


def _write(path, name="tiny", desc="a workflow"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TINY.format(name=name, desc=desc), encoding="utf-8")
    return path


@pytest.fixture
def project(tmp_path, monkeypatch, home):
    """A project directory, with the shared layer pointed somewhere throwaway."""
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.chdir(proj)
    return proj


# --------------------------------------------------------------------------- #
# resolution
# --------------------------------------------------------------------------- #
def test_the_shared_layer_answers_when_the_project_does_not(project, home):
    _write(home / "workflows" / "tiny.yaml")
    found = state_mod.locate("tiny")
    assert found.path == home / "workflows" / "tiny.yaml"
    assert found.origin == state_mod.LAYER_GLOBAL
    assert found.shadows == ()


def test_the_project_wins_and_says_what_it_beat(project, home):
    shared = _write(home / "workflows" / "tiny.yaml", desc="the shared one")
    mine = _write(project / ".claunch" / "workflows" / "tiny.yaml", desc="mine")

    found = state_mod.locate("tiny")
    assert found.path == mine
    assert found.origin == state_mod.LAYER_PROJECT
    # The point of the whole exercise: the loser is named, not dropped.
    assert found.shadows == (shared,)
    assert found.overrides


def test_listing_carries_the_same_answer_as_resolving(project, home):
    _write(home / "workflows" / "tiny.yaml")
    _write(home / "workflows" / "shared-only.yaml", name="shared-only")
    mine = _write(project / ".claunch" / "workflows" / "tiny.yaml")

    listed = {w.name: w for w in state_mod.resolved_workflows()}
    assert set(listed) == {"tiny", "shared-only"}
    assert listed["tiny"].path == mine
    assert listed["tiny"].shadows == (home / "workflows" / "tiny.yaml",)
    assert listed["shared-only"].shadows == ()
    # the old pair-shaped view still agrees with the rich one
    assert dict(state_mod.list_workflows())["tiny"] == mine


def test_an_explicit_path_belongs_to_no_layer(project, home):
    path = _write(project / "elsewhere" / "tiny.yaml")
    found = state_mod.locate(str(path))
    assert found.path == path
    assert found.origin == state_mod.LAYER_FILE


def test_an_unknown_name_names_both_layers_it_looked_in(project, home):
    with pytest.raises(model.WorkflowError) as exc:
        state_mod.locate("nope")
    message = str(exc.value)
    assert str(project / ".claunch" / "workflows") in message
    assert str(home / "workflows") in message


# --------------------------------------------------------------------------- #
# what ships, and how it gets to the shared layer
# --------------------------------------------------------------------------- #
def test_the_packaged_workflows_are_valid_and_current():
    """A shipped default is read as a teaching example; it must not be stale.

    ``feature-dev`` used to exist twice — a string in ``cflow/install.py`` and
    a file in this checkout's ``.claunch/`` — and the two taught different
    syntax. One file now, and it may not teach a deprecated form.
    """
    bundled = dict(state_mod.bundled_workflows())
    assert "feature-dev" in bundled and "delegated-dev" in bundled
    for name, path in bundled.items():
        wf = model.load(path)
        assert wf.name == name
        assert wf.step_count() > 0
        assert not wf.deprecations, f"{name} teaches a deprecated form"


def test_install_seeds_the_shared_layer(project, home):
    lines = install_mod.install_into_project(project)
    assert [line for line in lines if line.startswith("workflow ->")]
    for name, _ in state_mod.bundled_workflows():
        assert (home / "workflows" / f"{name}.yaml").is_file()
    # and they are findable from a project that declares nothing itself
    assert "feature-dev" in dict(state_mod.list_workflows())


def test_reinstalling_does_not_undo_an_edit(project, home):
    install_mod.install_into_project(project)
    edited = home / "workflows" / "feature-dev.yaml"
    edited.write_text("name: mine\nsteps:\n  only:\n    instructions: x\n", "utf-8")

    lines = install_mod.install_into_project(project)
    assert edited.read_text(encoding="utf-8").startswith("name: mine")
    assert any("kept; yours differs" in line for line in lines)


def test_a_forced_seed_replaces_an_edit(project, home):
    install_mod.install_into_project(project)
    edited = home / "workflows" / "feature-dev.yaml"
    edited.write_text("name: mine\nsteps:\n  only:\n    instructions: x\n", "utf-8")

    cflow_install.seed_global_workflows(force=True)
    assert model.load(edited).name == "feature-dev"


def test_seeding_reports_an_untouched_copy_as_unchanged(project, home):
    cflow_install.seed_global_workflows()
    again = cflow_install.seed_global_workflows()
    assert {outcome for _, _, outcome in again} == {cflow_install.UNCHANGED}


# --------------------------------------------------------------------------- #
# claunch cflow add
# --------------------------------------------------------------------------- #
def test_add_promotes_a_project_workflow_by_name(project, home, capsys):
    """The common case: I wrote it here, I want it everywhere.

    By name, not by path — otherwise using the shared layer means knowing
    where both layers keep their files, which is the thing nobody knows.
    """
    mine = _write(project / ".claunch" / "workflows" / "tiny.yaml")

    assert cli.main(["cflow", "add", "tiny"]) == 0
    shared = home / "workflows" / "tiny.yaml"
    assert shared.read_bytes() == mine.read_bytes()
    # ...and it says so, because the project copy still wins *here*
    assert "project wins" in capsys.readouterr().out


def test_add_refuses_a_workflow_that_does_not_parse(project, home, capsys):
    bad = project / "bad.yaml"
    bad.write_text("name: bad\nstart: nowhere\nsteps: {}\n", encoding="utf-8")

    assert cli.main(["cflow", "add", str(bad)]) == 1
    assert not (home / "workflows" / "bad.yaml").exists()
    assert "error" in capsys.readouterr().err


def test_add_will_not_quietly_replace_a_different_file(project, home, capsys):
    _write(home / "workflows" / "tiny.yaml", desc="the shared one")
    _write(project / ".claunch" / "workflows" / "tiny.yaml", desc="mine")

    assert cli.main(["cflow", "add", "tiny"]) == 1
    assert "--force" in capsys.readouterr().err
    assert "the shared one" in (home / "workflows" / "tiny.yaml").read_text("utf-8")

    assert cli.main(["cflow", "add", "tiny", "--force"]) == 0
    assert "mine" in (home / "workflows" / "tiny.yaml").read_text("utf-8")


def test_add_refuses_to_copy_a_file_onto_itself(project, home, capsys):
    _write(home / "workflows" / "tiny.yaml")
    assert cli.main(["cflow", "add", "tiny"]) == 1
    assert "already the global copy" in capsys.readouterr().err


def test_add_can_install_into_the_project_instead(project, home):
    _write(home / "workflows" / "tiny.yaml")
    assert cli.main(["cflow", "add", "tiny", "--project", "--name", "forked"]) == 0
    assert (project / ".claunch" / "workflows" / "forked.yaml").is_file()


def test_add_refuses_one_name_for_several_workflows(project, home, capsys):
    _write(home / "workflows" / "a.yaml", name="a")
    _write(home / "workflows" / "b.yaml", name="b")
    assert cli.main(["cflow", "add", "a", "b", "--name", "one", "--project"]) == 1
    assert "--name renames one" in capsys.readouterr().err


def test_add_reports_each_of_several_and_fails_on_any(project, home, capsys):
    _write(project / ".claunch" / "workflows" / "good.yaml", name="good")
    (project / "bad.yaml").write_text("steps: {}\n", encoding="utf-8")

    assert cli.main(["cflow", "add", "good", str(project / "bad.yaml")]) == 1
    # the good one still landed — one bad argument is not a reason to skip work
    assert (home / "workflows" / "good.yaml").is_file()


# --------------------------------------------------------------------------- #
# a run remembers which file it was
# --------------------------------------------------------------------------- #
def test_a_run_records_the_file_and_the_layer_it_came_from(project, home):
    shared = _write(home / "workflows" / "tiny.yaml")

    engine.start("tiny")
    status = engine.status()
    assert status["source"] == str(shared)
    assert status["origin"] == state_mod.LAYER_GLOBAL

    started = [e for e in state_mod.read_journal() if e["event"] == "started"][0]
    assert started["source"] == str(shared)
    assert started["shadowed"] == []


def test_a_run_records_what_its_workflow_overrode(project, home):
    shared = _write(home / "workflows" / "tiny.yaml")
    mine = _write(project / ".claunch" / "workflows" / "tiny.yaml")

    engine.start("tiny")
    assert engine.status()["source"] == str(mine)
    assert engine.status()["origin"] == state_mod.LAYER_PROJECT
    started = [e for e in state_mod.read_journal() if e["event"] == "started"][0]
    assert started["shadowed"] == [str(shared)]


def test_the_recorded_layer_survives_the_file_moving_underneath(project, home):
    """The run's answer is the one that was true when it started.

    Deleting the project copy mid-run makes the same name resolve to the
    shared layer. The run is driving a snapshot of the project file, so
    reporting it as the shared one would be a lie.
    """
    _write(home / "workflows" / "tiny.yaml")
    mine = _write(project / ".claunch" / "workflows" / "tiny.yaml")
    engine.start("tiny")
    mine.unlink()

    assert engine.status()["origin"] == state_mod.LAYER_PROJECT
    assert engine.status()["source"] == str(mine)
