"""Whose tree the machine gate actually ran in.

A run is keyed to a directory and its ``verify`` executes there, so a session
standing in somebody else's checkout gets a green suite out of a tree it is
not editing — and that green is filed as the machine's word on its work. The
engine cannot stop it (the fleet runs that way today) but it must not stay
quiet about it: these tests pin the detection and the two places it surfaces.
"""

from __future__ import annotations

import sys

import pytest
import yaml

from claude_launcher import daemon_client
from claude_launcher.cflow import checkout, engine, state as state_mod

from test_daemon_wedge import _stub_connect

pytest_plugins = []


LINEAR = yaml.safe_dump(
    {"steps": {"only": {"instructions": "do it"}}},
)


def _verified_yaml() -> str:
    """A workflow whose single step passes its verify unconditionally."""
    always_ok = f'"{sys.executable}" -c "import sys; sys.exit(0)"'
    return yaml.safe_dump(
        {"steps": {"build": {"instructions": "build it", "verify": always_ok}}}
    )


@pytest.fixture
def flow_dir(home, tmp_path, monkeypatch):
    """An isolated project cwd with a project workflow dir."""
    proj = tmp_path / "proj"
    (proj / ".claunch" / "workflows").mkdir(parents=True)
    monkeypatch.chdir(proj)
    monkeypatch.delenv(state_mod.SESSION_ENV, raising=False)
    return proj


def _write(proj, name, text):
    path = proj / ".claunch" / "workflows" / f"{name}.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _sessions(monkeypatch, *rows, fail=None):
    """Answer ``GET /api/sessions`` with these session documents."""

    class FakeClient:
        def get(self, path, **kw):
            if fail:
                raise daemon_client.DaemonClientError(fail)
            assert path == "/api/sessions", path
            return {"sessions": list(rows)}

        def post(self, path, body, **kw):
            return {}

    _stub_connect(monkeypatch, daemon_client, FakeClient)


def _row(name, cwd, **extra):
    return {"name": name, "cwd": str(cwd), "status": "idle", **extra}


# --------------------------------------------------------------------------- #
# inspect: what the daemon says about where everyone stands
# --------------------------------------------------------------------------- #
def test_a_session_alone_in_its_checkout_is_not_warned_about(flow_dir, monkeypatch):
    _sessions(monkeypatch, _row("s1", flow_dir), _row("s2", flow_dir / "elsewhere"))
    occ = checkout.inspect(session="s1", cwd=str(flow_dir))
    assert (occ.mismatch, occ.shared, occ.problem) == (False, False, None)
    assert checkout.warning(occ) is None


def test_a_neighbour_in_the_same_checkout_is_named(flow_dir, monkeypatch):
    """The s26 shape: a worker spawned without a worktree stands in its
    parent's checkout, so both drive git in one tree."""
    _sessions(monkeypatch, _row("kid", flow_dir), _row("parent", flow_dir))
    occ = checkout.inspect(session="kid", cwd=str(flow_dir))
    assert occ.shared and occ.peers == ("parent",)
    assert not occ.mismatch
    note = checkout.warning(occ)
    assert "parent" in note and "own worktree" in note


def test_an_exited_neighbour_does_not_count(flow_dir, monkeypatch):
    """A dead session's directory is nobody's — warning about it would train
    the reader to ignore the warning."""
    _sessions(
        monkeypatch,
        _row("kid", flow_dir),
        _row("ghost", flow_dir, status="exited", exited=True),
    )
    occ = checkout.inspect(session="kid", cwd=str(flow_dir))
    assert (occ.peers, occ.shared) == ((), False)
    assert checkout.warning(occ) is None


def test_a_run_keyed_away_from_its_session_is_a_mismatch(flow_dir, monkeypatch):
    """The run works here; the daemon says the session lives elsewhere."""
    _sessions(monkeypatch, _row("s1", flow_dir / "somewhere-else"))
    occ = checkout.inspect(session="s1", cwd=str(flow_dir))
    assert occ.mismatch
    note = checkout.warning(occ)
    assert str(flow_dir.resolve()) in note and "not editing" in note


def test_the_check_is_quiet_when_it_cannot_be_made(flow_dir, monkeypatch):
    """No daemon, an unknown name, an unmanaged run: no answer is not an
    error, and 'could not check' on every daemonless run is noise."""
    _stub_connect(monkeypatch, daemon_client, lambda: None)
    down = checkout.inspect(session="s1", cwd=str(flow_dir))
    assert down.problem and checkout.warning(down) is None

    _sessions(monkeypatch, fail="boom")
    broken = checkout.inspect(session="s1", cwd=str(flow_dir))
    assert broken.problem and checkout.warning(broken) is None

    _sessions(monkeypatch, _row("someone-else", flow_dir / "x"))
    unknown = checkout.inspect(session="s1", cwd=str(flow_dir))
    assert unknown.problem and checkout.warning(unknown) is None

    assert checkout.inspect(session=state_mod.DEFAULT_SCOPE, cwd=str(flow_dir)).problem


def test_two_spellings_of_one_directory_are_the_same_place(flow_dir, monkeypatch):
    """The daemon's copy and the run's copy arrive by different routes, so
    the comparison canonicalises both — otherwise every run reads as
    misplaced."""
    _sessions(monkeypatch, _row("s1", str(flow_dir) + "//."))
    occ = checkout.inspect(session="s1", cwd=str(flow_dir))
    assert not occ.mismatch and checkout.warning(occ) is None


# --------------------------------------------------------------------------- #
# engine: where the warning comes out
# --------------------------------------------------------------------------- #
def test_start_reports_a_shared_checkout_and_journals_it(flow_dir, monkeypatch):
    _write(flow_dir, "linear", LINEAR)
    monkeypatch.setenv(state_mod.SESSION_ENV, "kid")
    _sessions(monkeypatch, _row("kid", flow_dir), _row("parent", flow_dir))

    payload = engine.start("linear")
    assert "parent" in payload["checkout"]
    started = [e for e in state_mod.read_journal() if e["event"] == "started"][0]
    assert "parent" in started["checkout"]


def test_a_green_verify_from_a_shared_tree_says_so(flow_dir, monkeypatch):
    """The case the whole check exists for: the gate passes, and the pass is
    the thing that must not be read as proof."""
    _write(flow_dir, "verified", _verified_yaml())
    monkeypatch.setenv(state_mod.SESSION_ENV, "kid")
    _sessions(monkeypatch, _row("kid", flow_dir), _row("parent", flow_dir))

    engine.start("verified")
    engine.report("built it")
    payload = engine.next_step()

    assert payload["status"] == "done"  # the gate still passed: advisory, not fatal
    assert "parent" in payload["checkout"]
    passed = [e for e in state_mod.read_journal() if e["event"] == "verify_passed"][0]
    assert "parent" in passed["checkout"]


def test_an_isolated_run_carries_no_such_note(flow_dir, monkeypatch):
    _write(flow_dir, "verified", _verified_yaml())
    monkeypatch.setenv(state_mod.SESSION_ENV, "kid")
    _sessions(monkeypatch, _row("kid", flow_dir), _row("parent", flow_dir / "own"))

    payload = engine.start("verified")
    assert "checkout" not in payload
    engine.report("built it")
    assert "checkout" not in engine.next_step()
    events = [e for e in state_mod.read_journal() if e["event"] == "verify_passed"]
    assert "checkout" not in events[0]


# --------------------------------------------------------------------------- #
# the CLI surface the leader workflow calls
# --------------------------------------------------------------------------- #
def _run_cli(monkeypatch, capsys, *argv, session="kid"):
    """Through the real argument parser, not the handler.

    Calling ``_cmd_checkout`` with a hand-built ``Namespace`` is what these
    tests used to do, and it measured nothing about the command: the handler
    landed in ``220627a`` without the ``add_parser`` line that reaches it, so
    for a week ``claunch cflow checkout`` answered "invalid choice" while
    every test here was green. ``cli.main`` is the surface the leader
    workflow's ``integrate-preflight`` actually calls.
    """
    from claude_launcher import cli

    monkeypatch.setenv(state_mod.SESSION_ENV, session)
    code = cli.main(["cflow", "checkout", *argv])
    return code, capsys.readouterr().out


def test_the_command_names_the_neighbours_and_still_exits_zero(
    flow_dir, monkeypatch, capsys
):
    """It reports; it does not gate.

    A leader cannot move sessions outside its own subtree, so a non-zero exit
    here would block every integration on something the decider cannot fix —
    and a gate that blocks forever is bypassed on day one. The decision belongs
    to the user gate that already exists; this command only makes sure nobody
    reaches it unknowingly.
    """
    _sessions(monkeypatch, _row("kid", flow_dir), _row("parent", flow_dir))
    code, out = _run_cli(monkeypatch, capsys)
    assert code == 0
    assert "parent" in out
    assert "warning" in out


def test_the_command_is_quiet_when_the_checkout_is_the_sessions_own(
    flow_dir, monkeypatch, capsys
):
    _sessions(monkeypatch, _row("kid", flow_dir), _row("parent", flow_dir / "own"))
    code, out = _run_cli(monkeypatch, capsys)
    assert code == 0
    assert "(none)" in out
    assert "warning" not in out


def test_the_command_says_it_could_not_ask_rather_than_reporting_all_clear(
    flow_dir, monkeypatch, capsys
):
    """"Could not ask" and "nobody is there" are different answers, and a
    reader who cannot tell them apart reads silence as an all-clear."""
    _stub_connect(monkeypatch, daemon_client, lambda: None)
    code, out = _run_cli(monkeypatch, capsys)
    assert code == 0
    assert "unknown" in out
    assert "(none)" not in out


def test_the_subcommand_is_reachable_from_the_parser(monkeypatch):
    """The registration itself, pinned apart from what the handler prints.

    ``improv-leader``'s ``integrate-preflight`` names this command in its
    instructions and requires its output in ``done_when``, so an unregistered
    handler makes that step impossible to complete honestly.
    """
    from claude_launcher import cli, cli_cflow

    args = cli.build_parser().parse_args(["cflow", "checkout"])
    assert args.func is cli_cflow._cmd_checkout
    assert args.session is None


def test_the_command_can_be_asked_about_a_session_other_than_this_one(
    flow_dir, monkeypatch, capsys
):
    """``--session`` is what the handler reads; without it registered the
    flag is a parser error rather than an override."""
    _sessions(monkeypatch, _row("kid", flow_dir), _row("parent", flow_dir))
    code, out = _run_cli(monkeypatch, capsys, "--session", "parent", session="kid")
    assert code == 0
    # Asked as 'parent', the neighbour standing in this tree is 'kid'.
    assert "kid" in out
    assert "warning" in out


# --------------------------------------------------------------------------- #
# own_checkout: which tree a gate under tools/ should ask about
# --------------------------------------------------------------------------- #
# inspect() reports the mismatch; own_checkout() resolves it. The gates
# landed_check and merge_ready took "the run's directory" for "my branch",
# so a run keyed at the repository root asked whether *master* had landed --
# a question with no true answer, and both answered it with confidence
# (landed_check "not yet", exit 1; merge_ready "ready", exit 0).
def test_an_explicit_directory_wins_without_asking_anyone(flow_dir, monkeypatch):
    """``--repo`` must never depend on a daemon being up.

    It is what tests and hand-runs pass, and it is the escape a worker is told
    to use when the machine cannot find its tree. A daemon that is down or
    answering nonsense may not change what an explicitly named path means.
    """
    _sessions(monkeypatch, fail="boom")
    monkeypatch.setenv(state_mod.SESSION_ENV, "s1")
    where, how = checkout.own_checkout(str(flow_dir / "named"))
    assert how == checkout.NAMED
    assert where == state_mod.resolve_cwd(str(flow_dir / "named"))


def test_the_session_s_own_tree_beats_the_directory_the_run_is_keyed_to(
    flow_dir, monkeypatch
):
    """The defect's exact shape: run keyed at the root, session in a worktree.

    Measured on a live mesh before this test existed -- the gate read the
    root's HEAD (``master``) and reported "not yet" about a branch that had
    in fact landed.
    """
    mine = flow_dir / "worktrees" / "feature"
    _sessions(monkeypatch, _row("s1", mine))
    monkeypatch.setenv(state_mod.SESSION_ENV, "s1")
    where, how = checkout.own_checkout(cwd=str(flow_dir))
    assert how == checkout.SESSION
    assert where == state_mod.resolve_cwd(str(mine))


def test_a_session_the_daemon_cannot_place_falls_back_to_the_run_directory(
    flow_dir, monkeypatch
):
    """Degrade to the old answer, never to an error.

    A gate that failed because a best-effort lookup failed would be worse
    than the defect it fixes: the same contract :func:`inspect` keeps.
    """
    _sessions(monkeypatch, _row("somebody-else", flow_dir / "x"))
    monkeypatch.setenv(state_mod.SESSION_ENV, "s1")
    assert checkout.own_checkout(cwd=str(flow_dir)) == (
        state_mod.resolve_cwd(str(flow_dir)),
        checkout.RUN_CWD,
    )


def test_no_daemon_falls_back_to_the_run_directory(flow_dir, monkeypatch):
    _stub_connect(monkeypatch, daemon_client, lambda: None)
    monkeypatch.setenv(state_mod.SESSION_ENV, "s1")
    assert checkout.own_checkout(cwd=str(flow_dir)) == (
        state_mod.resolve_cwd(str(flow_dir)),
        checkout.RUN_CWD,
    )


def test_an_unmanaged_session_falls_back_to_the_run_directory(
    flow_dir, monkeypatch
):
    """No ``CLAUNCH_SESSION``: there is no recorded home to look up."""
    monkeypatch.delenv(state_mod.SESSION_ENV, raising=False)
    assert checkout.own_checkout(cwd=str(flow_dir)) == (
        state_mod.resolve_cwd(str(flow_dir)),
        checkout.RUN_CWD,
    )


# --------------------------------------------------------------------------- #
# own_checkout: the answer follows the NAME, and nothing else does
# --------------------------------------------------------------------------- #
# The tests above pin which of the two directories wins. None of them pins
# what happens when the *name* handed in is not the run's own, and that gap
# is where a defect lived: the daemon's clock runs a checklist item through
# `engine.run_probe`, which launched it with no `env=`, so the subprocess
# inherited the daemon's `CLAUNCH_SESSION` -- one session's name for every
# run on the machine. `own_checkout` then answered about that session, and
# the gate measured a checkout the run had never touched.
#
# Measured on this machine (issues `claunch-04ru`, `claunch-mmvm`), with
# `checkout.py` unmodified and `CLAUNCH_SESSION` the only variable changed,
# running `tools/landed_check.py` by hand inside three worker worktrees:
#
#     worktree   asked as its own session      asked as the daemon's (s127)
#     s250       landed: 7e859434ea5d, exit 0  cannot tell ... is on master, exit 2
#     s267       landed: a4e568320186, exit 0  cannot tell ... is on master, exit 2
#     s268       landed: b63b1ab6e488, exit 0  cannot tell ... is on master, exit 2
#
# The daemon's recorded directory for all three was already their own
# worktree, so no priority between `session_cwd` and `run_cwd` could have
# changed those answers -- both named the same place. Only the name did.
#
# So the fix belongs at the caller (`engine.probe_env` writes the run's own
# scope into the probe's environment), and what is pinned HERE is the reason
# that caller has to exist: this function is a lookup keyed by a name, and it
# is faithful to whatever name it is given. Nothing below asserts that a
# wrong name produces a right answer; a test that let it would be describing
# a `own_checkout` that ignores its own argument.
def test_the_answer_follows_the_name_it_is_asked_under(flow_dir, monkeypatch):
    """One run directory, two names, two answers.

    This is the whole mechanism in one assertion. A caller that cannot
    guarantee the name is the run's own cannot use this function's answer,
    and there is no defence inside it -- being faithful to the name IS the
    contract.
    """
    mine = flow_dir / "worktrees" / "feature"
    theirs = flow_dir / "worktrees" / "somebody-else"
    _sessions(monkeypatch, _row("mine", mine), _row("theirs", theirs))
    monkeypatch.setenv(state_mod.SESSION_ENV, "mine")

    asked_as_mine = checkout.own_checkout(session="mine", cwd=str(flow_dir))
    asked_as_theirs = checkout.own_checkout(session="theirs", cwd=str(flow_dir))

    assert asked_as_mine == (state_mod.resolve_cwd(str(mine)), checkout.SESSION)
    assert asked_as_theirs == (state_mod.resolve_cwd(str(theirs)), checkout.SESSION)
    assert asked_as_mine != asked_as_theirs


def test_the_daemons_name_answers_with_the_daemons_checkout(flow_dir, monkeypatch):
    """The measured defect, at this layer, with the run standing in its own tree.

    The run is keyed to the worker's worktree and the daemon records the
    worker there too -- so `session_cwd` and `run_cwd` agree and the priority
    between them is not in play. Asked under the daemon's own session, this
    still answers the repository root, because that is where the daemon
    stands. Downstream, `tools/landed_check.py` reads that root's HEAD, finds
    the integration target, and reports "cannot tell" for a branch that had
    in fact landed.

    If this test ever goes red, read the change carefully before accepting
    it: making a wrong name produce a right answer would hide the caller-side
    defect rather than fix it, and it was measured that the two are separate
    (the wrong name is claunch-04ru; no priority change reaches it).
    """
    root = flow_dir
    worktree = flow_dir / "worktrees" / "s267-open-triage"
    _sessions(
        monkeypatch,
        _row("s267", worktree),
        _row("s127", root),
    )
    monkeypatch.setenv(state_mod.SESSION_ENV, "s267")

    # The name the probe inherited from the daemon, not the run's own.
    assert checkout.own_checkout(session="s127", cwd=str(worktree)) == (
        state_mod.resolve_cwd(str(root)),
        checkout.SESSION,
    )


def test_the_runs_own_scope_answers_with_the_runs_own_tree(flow_dir, monkeypatch):
    """The contract a correctly-named caller gets, and it holds twice over.

    With the run's own scope loaded, both of this function's branches name
    the same checkout: the lookup answers with that session's recorded
    directory, or it fails and the run's ``cwd`` stands. That property is
    what makes writing the name in (rather than deleting the variable) the
    right shape for the caller-side fix -- the answer does not depend on the
    daemon being reachable.
    """
    worktree = flow_dir / "worktrees" / "s267-open-triage"
    _sessions(monkeypatch, _row("s267", worktree), _row("s127", flow_dir))
    monkeypatch.setenv(state_mod.SESSION_ENV, "s267")
    with_daemon = checkout.own_checkout(cwd=str(worktree))
    assert with_daemon == (state_mod.resolve_cwd(str(worktree)), checkout.SESSION)

    # Same question, no daemon to answer it: the fallback names the same tree.
    _stub_connect(monkeypatch, daemon_client, lambda: None)
    without_daemon = checkout.own_checkout(cwd=str(worktree))
    assert without_daemon == (state_mod.resolve_cwd(str(worktree)), checkout.RUN_CWD)
    assert with_daemon[0] == without_daemon[0]


def test_a_name_standing_in_another_repository_is_answered_with_that_repository(
    flow_dir, tmp_path, monkeypatch
):
    r"""The sharpest form of the same contract: the answer leaves the repository.

    Nothing here compares the looked-up directory against the run's own, so a
    name belonging to a session that works in a DIFFERENT repository is
    answered with that repository. The gate downstream does not get a wrong
    verdict about this branch -- it gets no verdict at all.

    Measured on this machine, same procedure as the block above (``checkout.py``
    unmodified, ``--repo`` omitted, ``CLAUNCH_SESSION`` the only variable):

        asked as   answer                                  landed_check says
        s265       ...\worktrees\s127-04ru-probe-env       not yet, exit 1
        s127       F:\works\claude-launcher                cannot tell ... on master, exit 2
        s264       F:\works\report-kanban                  could not read HEAD in ..., exit 2

    The third row is a different failure from the second and worth keeping
    apart: one names a checkout of this repository that cannot answer the
    question, the other names a checkout that is not of this repository at
    all. Both reach the same exit code, so the exit code does not tell them
    apart -- this test does.
    """
    elsewhere = tmp_path / "another-repository"
    worktree = flow_dir / "worktrees" / "feature"
    _sessions(monkeypatch, _row("mine", worktree), _row("stranger", elsewhere))
    monkeypatch.setenv(state_mod.SESSION_ENV, "mine")

    where, how = checkout.own_checkout(session="stranger", cwd=str(worktree))
    assert (where, how) == (state_mod.resolve_cwd(str(elsewhere)), checkout.SESSION)
    # Not merely a different directory: outside the run's tree entirely.
    assert not where.startswith(state_mod.resolve_cwd(str(flow_dir)))
