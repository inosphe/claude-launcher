"""Where a workflow name resolves, and who gets told which file won.

Two layers answer a name — the project's ``.claunch/workflows/`` and the
global ``~/.claude-launcher/workflows/`` — and the nearest one wins. That was
always true and never tested; what is new is that the global layer is
actually populated (by ``claunch install --global``, and by ``cflow add``),
so the same name really can exist twice. These tests pin both halves: the
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
    """A project directory, with the global layer pointed somewhere throwaway."""
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.chdir(proj)
    return proj


@pytest.fixture
def monkeypatch_stdin(monkeypatch):
    """Feed a string (or EOF) to ``input``, the way a live prompt would read it."""
    holder = {}

    def feed(text):
        holder["text"] = text

    monkeypatch.setattr("builtins.input", lambda prompt="": holder.get("text", ""))
    return feed


# --------------------------------------------------------------------------- #
# resolution
# --------------------------------------------------------------------------- #
def test_the_global_layer_answers_when_the_project_does_not(project, home):
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
# what ships, and how it gets to the global layer
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


def test_the_worker_workflow_keeps_its_isolation_rules():
    """``improv-worker``'s intake must carry the two isolation rules.

    A worker spawned without a worktree lands in its parent's checkout, and
    the intake step is the one place it is told to notice that and move: a
    new feature gets a new branch, and a shared checkout gets a new worktree,
    with the determination procedure spelled out. A later rewording that
    drops these anchors would silently un-teach the rule, so pin them here.
    """
    bundled = dict(state_mod.bundled_workflows())
    intake = model.load(bundled["improv-worker"]).steps["intake"]
    for anchor in (
        "새 피처 브랜치",                      # new feature -> new branch
        "새 워크트리",                         # shared checkout -> new worktree
        "git rev-parse --show-toplevel",       # primary determination
        "--git-common-dir",                    # fallback when parent cwd is unknown
        "$CLAUNCH_SESSION",                    # how a worker names itself
    ):
        assert anchor in intake.instructions, f"intake lost its {anchor!r} rule"
    assert "작업 위치" in intake.done_when


def test_the_improv_workflows_carry_no_repo_specific_verify():
    """The improv pair ships to every repository; a verify would not.

    A suite command is a property of one repository, and a canonical
    ``verify`` hard-codes it for all of them — a run in any other repo
    blocks on a command that cannot exit 0 there. The policy is layering:
    the canonical files carry none, and a repository that wants a machine
    check overrides in its project layer (``.claunch/workflows/``).
    """
    bundled = dict(state_mod.bundled_workflows())
    for name in ("improv-worker", "improv-leader"):
        wf = model.load(bundled[name])
        for step_id, step in wf.steps.items():
            assert step.verify is None, (
                f"{name}:{step_id} carries a verify — repo-specific commands "
                "belong in the project layer"
            )


def test_the_bundled_improv_worker_teaches_the_nested_merge_contract():
    """A worker that spawned sub-workers folds their branches upward as a
    tree: each finished child branch is collected with a ``--no-ff`` merge
    into the worker's own branch, and integration is then *requested* from
    the parent session (leader -> master review, worker -> --no-ff merge
    into its branch) — the worker never merges master itself. The bundled
    file is the teaching copy of that contract; this pins it."""
    bundled = dict(state_mod.bundled_workflows())
    wf = model.load(bundled["improv-worker"])

    collect = wf.steps["commit"].instructions
    assert "자식" in collect and "--no-ff" in collect
    assert "트리" in collect

    landing = wf.steps["landing"].select
    assert "상위" in landing.prompt
    assert set(landing.options) == {"request", "hold"}

    request = wf.steps["integration-request"].instructions
    assert "상위 세션" in request and "--no-ff" in request
    assert "master를 직접 머지하지 않는다" in request


def test_the_leader_does_not_hide_a_human_decision_behind_an_agent_chooser():
    """``standby``'s exit must be a fact the agent can see for itself.

    ``chooser: agent`` is read by the engine as *the agent is working*: no
    ``waiting_selection``, no dashboard button, no run event, nothing that
    tells a human a decision is pending — while the reminder clock keeps
    typing "if you are mid-work, keep going". So a human instruction is the
    one thing such a select may not wait on. It did once, and a leader run
    sat on it for ninety minutes with six finished branches behind it.

    The trigger is now evidence, and the gate that actually protects master
    is the ``ask`` on the very next step — a pure user gate (no ``from``),
    which the engine *does* surface. Both halves are pinned here: dropping
    the second one would make the first one reckless.
    """
    bundled = dict(state_mod.bundled_workflows())
    wf = model.load(bundled["improv-leader"])

    standby = wf.steps["standby"]
    assert standby.select.chooser == "agent"
    for text in (standby.select.prompt, standby.instructions):
        assert "사용자의 지시" not in text, (
            "standby waits on a user instruction behind an agent chooser — "
            "nothing surfaces that wait to a human"
        )
    # ...and what it waits on instead is the evidence the next gate judges.
    assert "증거" in standby.select.options["integrate"].description

    gate = wf.steps["integrate"].ask
    assert gate is not None
    assert not gate.delegate.candidates, "master's gate stopped being the user's"
    assert gate.delegate.otherwise == model.OTHERWISE_HUMAN


def test_a_global_install_seeds_the_global_layer(project, home):
    lines = install_mod.install_into_user()
    assert [line for line in lines if line.startswith("workflow ->")]
    for name, _ in state_mod.bundled_workflows():
        assert (home / "workflows" / f"{name}.yaml").is_file()
    # and they are findable from a project that declares nothing itself
    assert "feature-dev" in dict(state_mod.list_workflows())


def test_a_project_install_stays_inside_the_project(project, home):
    """An install writes only inside its scope — the machine layer is the
    global (and profile) install's business."""
    lines = install_mod.install_into_project(project)
    assert not [line for line in lines if line.startswith("workflow ->")]
    assert not (home / "workflows").exists()


def test_reinstalling_does_not_undo_an_edit(project, home):
    install_mod.install_into_user()
    edited = home / "workflows" / "feature-dev.yaml"
    edited.write_text("name: mine\nsteps:\n  only:\n    instructions: x\n", "utf-8")

    lines = install_mod.install_into_user()
    assert edited.read_text(encoding="utf-8").startswith("name: mine")
    assert any("kept; yours differs" in line for line in lines)


def test_a_reinstall_reports_the_layer_as_up_to_date(project, home):
    """Unchanged files get no line each, but not silence either — silence
    reads as an omission ('did it skip the workflows?')."""
    install_mod.install_into_user()
    lines = install_mod.install_into_user()
    assert not [line for line in lines if line.startswith("workflow ->")]
    assert any(line.startswith("workflow layer -> up to date") for line in lines)


def test_a_forced_seed_replaces_an_edit(project, home):
    install_mod.install_into_user()
    edited = home / "workflows" / "feature-dev.yaml"
    edited.write_text("name: mine\nsteps:\n  only:\n    instructions: x\n", "utf-8")

    cflow_install.seed_global_workflows(force=True)
    assert model.load(edited).name == "feature-dev"


def test_seeding_carries_workflow_sidecar_assets(project, home):
    """A verify script must land where its workflow's verify command looks:
    the global layer, next to the yaml — a yaml seeded without it is broken."""
    cflow_install.seed_global_workflows()
    assert (home / "workflows" / "e2e-session-roundtrip-verify.mjs").is_file()


def test_seeding_reports_an_untouched_copy_as_unchanged(project, home):
    cflow_install.seed_global_workflows()
    again = cflow_install.seed_global_workflows()
    assert {outcome for _, _, outcome in again} == {cflow_install.UNCHANGED}


# --------------------------------------------------------------------------- #
# claunch cflow add
# --------------------------------------------------------------------------- #
def test_add_promotes_a_project_workflow_by_name(project, home, capsys):
    """The common case: I wrote it here, I want it everywhere.

    By name, not by path — otherwise using the global layer means knowing
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


def test_add_project_takes_a_directory(project, home, tmp_path):
    _write(home / "workflows" / "tiny.yaml")
    other = tmp_path / "other-proj"
    assert cli.main(["cflow", "add", "tiny", "--project", str(other)]) == 0
    assert (other / ".claunch" / "workflows" / "tiny.yaml").is_file()


def test_add_global_is_the_default_and_may_be_spelled_out(project, home):
    mine = _write(project / ".claunch" / "workflows" / "tiny.yaml")
    assert cli.main(["cflow", "add", "tiny", "--global"]) == 0
    assert (home / "workflows" / "tiny.yaml").read_bytes() == mine.read_bytes()


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
    global layer. The run is driving a snapshot of the project file, so
    reporting it as the global one would be a lie.
    """
    _write(home / "workflows" / "tiny.yaml")
    mine = _write(project / ".claunch" / "workflows" / "tiny.yaml")
    engine.start("tiny")
    mine.unlink()

    assert engine.status()["origin"] == state_mod.LAYER_PROJECT
    assert engine.status()["source"] == str(mine)


# --------------------------------------------------------------------------- #
# what the shipped improv pair teaches about verification
# --------------------------------------------------------------------------- #
def _bundled(name):
    return model.load(dict(state_mod.bundled_workflows())[name])


def test_the_worker_does_not_fold_into_a_retired_or_frozen_branch():
    """A worktree that runs several rounds outlives the rule written for one.

    "Fold the feature branch into the worktree branch" assumes that branch is
    still this round's. Once it has landed in master, folding revives a strand
    behind master; once it is under integration review, folding moves a tip
    somebody else is judging. Both are one-way, so the rule has to name them
    and say what to hand over instead.
    """
    commit = _bundled("improv-worker").steps["commit"]
    assert "git branch --contains" in commit.instructions
    for anchor in ("은퇴", "동결", "심사", "피처 브랜치"):
        assert anchor in commit.instructions, f"commit lost its {anchor!r} case"
    assert "은퇴" in commit.done_when


def test_the_worker_review_says_what_a_number_is_a_verdict_about():
    """A suite number is not a verdict until it names its tree and window.

    Every anchor here is a rule that cost the fleet a wrong conclusion once:
    which tree ran, which packages, who else was sweeping, and what the check
    cannot see.
    """
    review = _bundled("improv-worker").steps["review"]
    for anchor in (
        "--directory",                     # which tree
        "uv sync --extra test",            # ...and which packages
        "import pytest, xdist",            # verify by import, not by existence
        "트리 해시",                        # numbers carry their tree
        "--collect-only",                  # baselines cost nothing
        "Get-CimInstance",                 # the literal probe, not a description
        "distinct basetemp",               # concurrent sweeps
        "1~3초",                           # the startup blind spot
        "지금 시작한다",                    # ...and the only defence there is
        "popen-gw",                        # a dead run's numbers are on disk
        "verify` 필드가 있으면",            # ...and leaving such a step is a sweep
    ):
        assert anchor in review.instructions, f"review lost its {anchor!r} rule"


def test_the_worker_review_admits_what_the_scan_cannot_see():
    """A check whose blind spots are undocumented gets built upon."""
    review = _bundled("improv-worker").steps["review"]
    assert "로컬 프로세스만" in review.instructions
    assert "Name 제한을 없애지 마라" in review.instructions


def test_the_leader_checks_who_else_stands_in_the_tree_before_merging():
    """The merge and the sweep happen in one checkout; the preflight names who
    else is in it, and the existing user gate decides. It is deliberately not
    a wall: a leader cannot move sessions outside its own subtree, and a gate
    that blocks forever is a gate that gets bypassed on day one."""
    leader = _bundled("improv-leader")
    assert "integrate-preflight" in leader.steps
    pre = leader.steps["integrate-preflight"]
    assert "claunch cflow checkout" in pre.instructions
    assert pre.next == "integrate"
    # reachable: standby's integrate option must route through it
    assert leader.steps["standby"].select.options["integrate"].next == "integrate-preflight"
    # and the decision stays with the human gate that already exists
    assert "preflight" in leader.steps["integrate"].entry_prompt


def test_the_leader_gate_points_at_a_record_that_can_exist():
    """The collection table cannot be filed as a report: standby is a select
    step and `report` is refused there. Its home is the select's reason."""
    prompt = _bundled("improv-leader").steps["integrate"].entry_prompt
    assert "reason" in prompt
    assert "select_confirmed" in prompt
    standby = _bundled("improv-leader").steps["standby"]
    assert standby.is_select  # the premise of the rule above
    assert "reason" in standby.instructions


def test_the_leader_treats_a_clean_merge_as_unproven():
    """Text silence is not runtime safety, and the absence of a conflict
    marker is exactly what removes the place a human would look."""
    integrate = _bundled("improv-leader").steps["integrate"]
    for anchor in ("충돌 없음은 안전 판정이 아니다", "같은 모듈", "스텁", "프리뷰"):
        assert anchor in integrate.instructions, f"integrate lost {anchor!r}"


# --------------------------------------------------------------------------- #
# claunch cflow update — bring stale global copies up to the package
# --------------------------------------------------------------------------- #
def _fake_bundle(tmp_path, monkeypatch, files):
    """Point the packaged-workflow source at a throwaway directory.

    ``bundled_workflows()`` resolves through ``bundled_workflows_dir()`` alone
    (the package directory), so seeding and update read the fake copy and the
    tests never touch the real package.
    """
    pkg = tmp_path / "bundle"
    pkg.mkdir()
    for name, body in files.items():
        (pkg / name).write_text(body, encoding="utf-8")
    monkeypatch.setattr(state_mod, "bundled_workflows_dir", lambda: pkg)
    return pkg


def test_update_replaces_a_stale_global_copy(project, home, tmp_path, monkeypatch):
    pkg = _fake_bundle(tmp_path, monkeypatch, {"tiny.yaml": TINY.format(name="tiny", desc="v1")})
    cflow_install.seed_global_workflows()
    # The package moves on; the layer still holds the seeded bytes.
    (pkg / "tiny.yaml").write_text(TINY.format(name="tiny", desc="v2"), encoding="utf-8")

    outcomes = cflow_install.update_global_workflows([], can_ask=False)
    states = {name: outcome for name, outcome, applied, _ in outcomes}
    assert states["tiny"] == cflow_install.STALE
    assert "v2" in (home / "workflows" / "tiny.yaml").read_text("utf-8")


def test_update_seeds_a_missing_global_copy(project, home, tmp_path, monkeypatch):
    _fake_bundle(tmp_path, monkeypatch, {"tiny.yaml": TINY.format(name="tiny", desc="v1")})
    outcomes = cflow_install.update_global_workflows([], can_ask=False)
    states = {name: outcome for name, outcome, applied, _ in outcomes}
    assert states["tiny"] == cflow_install.SEEDED
    assert (home / "workflows" / "tiny.yaml").is_file()


def test_update_leaves_an_unchanged_copy_alone(project, home, tmp_path, monkeypatch):
    _fake_bundle(tmp_path, monkeypatch, {"tiny.yaml": TINY.format(name="tiny", desc="v1")})
    cflow_install.seed_global_workflows()
    outcomes = cflow_install.update_global_workflows([], can_ask=False)
    states = {name: outcome for name, outcome, applied, _ in outcomes}
    assert states["tiny"] == cflow_install.UNCHANGED


def test_update_refuses_an_edited_copy_without_force(project, home, tmp_path, monkeypatch):
    _fake_bundle(tmp_path, monkeypatch, {"tiny.yaml": TINY.format(name="tiny", desc="v1")})
    cflow_install.seed_global_workflows()
    # A human edits the layer copy; the package does not move.
    (home / "workflows" / "tiny.yaml").write_text(
        TINY.format(name="tiny", desc="mine"), encoding="utf-8"
    )

    outcomes = cflow_install.update_global_workflows([], can_ask=False)
    states = {name: (outcome, applied) for name, outcome, applied, _ in outcomes}
    assert states["tiny"] == (cflow_install.EDITED, False)
    # Not applied: no --force, no live terminal.
    assert "mine" in (home / "workflows" / "tiny.yaml").read_text("utf-8")
    assert not (home / "workflows" / "tiny.yaml").with_name("tiny.yaml.bak").exists()


def test_update_with_force_backs_up_then_replaces(project, home, tmp_path, monkeypatch):
    _fake_bundle(tmp_path, monkeypatch, {"tiny.yaml": TINY.format(name="tiny", desc="v1")})
    cflow_install.seed_global_workflows()
    edited = home / "workflows" / "tiny.yaml"
    edited.write_text(TINY.format(name="tiny", desc="mine"), encoding="utf-8")

    outcomes = cflow_install.update_global_workflows([], force=True, can_ask=False)
    states = {name: (outcome, applied) for name, outcome, applied, _ in outcomes}
    assert states["tiny"] == (cflow_install.EDITED, True)
    # The edit is what the .bak keeps; the layer copy is the package again.
    assert (home / "workflows" / "tiny.yaml").is_file()
    assert "v1" in (home / "workflows" / "tiny.yaml").read_text("utf-8")
    bak = home / "workflows" / "tiny.yaml.bak"
    assert bak.is_file()
    assert "mine" in bak.read_text("utf-8")


def test_update_an_unknown_copy_refuses_without_force(project, home, tmp_path, monkeypatch):
    """A file with no seed record is not provably stale — leave it alone."""
    _fake_bundle(tmp_path, monkeypatch, {"tiny.yaml": TINY.format(name="tiny", desc="v1")})
    (home / "workflows" / "tiny.yaml").write_text(
        TINY.format(name="tiny", desc="pre-sidecar"), encoding="utf-8"
    )
    outcomes = cflow_install.update_global_workflows([], can_ask=False)
    states = {name: (outcome, applied) for name, outcome, applied, _ in outcomes}
    assert states["tiny"] == (cflow_install.UNKNOWN, False)
    assert "pre-sidecar" in (home / "workflows" / "tiny.yaml").read_text("utf-8")


def test_update_can_ask_defers_to_the_person(project, home, tmp_path, monkeypatch, monkeypatch_stdin):
    """A live terminal is asked — a 'no' (or EOF) is a refusal, not assent."""
    _fake_bundle(tmp_path, monkeypatch, {"tiny.yaml": TINY.format(name="tiny", desc="v1")})
    cflow_install.seed_global_workflows()
    (home / "workflows" / "tiny.yaml").write_text(
        TINY.format(name="tiny", desc="mine"), encoding="utf-8"
    )

    # EOF (no answer) must not become a default-yes.
    monkeypatch_stdin("")
    outcomes = cflow_install.update_global_workflows([], can_ask=True)
    states = {name: (outcome, applied) for name, outcome, applied, _ in outcomes}
    assert states["tiny"] == (cflow_install.EDITED, False)
    assert "mine" in (home / "workflows" / "tiny.yaml").read_text("utf-8")

    # An explicit yes replaces it, with the edit preserved in .bak.
    monkeypatch_stdin("y\n")
    outcomes = cflow_install.update_global_workflows([], can_ask=True)
    states = {name: (outcome, applied) for name, outcome, applied, _ in outcomes}
    assert states["tiny"] == (cflow_install.EDITED, True)
    assert "v1" in (home / "workflows" / "tiny.yaml").read_text("utf-8")
    assert "mine" in (home / "workflows" / "tiny.yaml.bak").read_text("utf-8")


def test_seed_writes_a_record_loaded_by_update(project, home, tmp_path, monkeypatch):
    """The sidecar is what lets update tell stale from edited — it must
    survive a re-read."""
    _fake_bundle(tmp_path, monkeypatch, {"tiny.yaml": TINY.format(name="tiny", desc="v1")})
    cflow_install.seed_global_workflows()
    assert (home / "workflows" / ".seeded.json").is_file()
    record = cflow_install.seed_record(home / "workflows")
    assert "tiny.yaml" in record and len(record["tiny.yaml"]) == 64
