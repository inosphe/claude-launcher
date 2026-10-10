"""The gate behind ``improv-worker``'s ``workspace-check`` step.

A worker round used to go from ``intake`` straight to ``branch-setup``, and
nothing in between asked whether the directory the branch was about to be cut
in is the one the round's work belongs to. On this machine that is not a
theoretical gap: six registered workspaces, dozens of linked worktrees under
one of them, and a session's directory fixed by whoever spawned it. The report
that prompted this (claunch-vc5ma) is that rounds do start in the wrong place,
and that it is noticed at the integration request rather than at the branch.

What these pin is which answer comes back, because the three are acted on
differently and the workflow routes two of them to a person:

* ``0`` every axis that could be measured agrees
* ``1`` a measurable axis disagrees -- the location is wrong
* ``2`` nothing could be measured -- unjudged, which is not the same as
  judged correct, and is the answer a run must not be able to walk past

The axes are pinned separately from the exit code on purpose. Each one can
go unmeasurable by itself in a legitimate round -- an issueless round has no
board record, a session in the main checkout has no worktree to be crowded out
of -- so "this axis said nothing" must not read as "this axis said no", and
the exit code must still come from whichever axes did speak.

The daemon and git are both faked. What the tool does with them is read four
routes and two ``rev-parse`` answers; a real daemon would add a process and a
port, and a real repository would add a checkout, and neither would pin
anything this does not.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "tools" / "workspace_check.py"


def _load():
    spec = importlib.util.spec_from_file_location("workspace_check_under_test", GATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


workspace_check = _load()


class _FakeClient:
    """The four routes the tool reads, and a record of what it asked for."""

    def __init__(self, routes):
        self.routes = routes
        self.asked = []

    def get(self, path):
        self.asked.append(path)
        answer = self.routes.get(path)
        if isinstance(answer, Exception):
            raise answer
        if answer is None:
            raise workspace_check.daemon_client.DaemonClientError(f"no route {path}")
        return answer


@pytest.fixture
def repos(tmp_path):
    """Two real directories standing in for two checkouts.

    Real ones rather than invented strings: ``_norm`` runs ``realpath``, and on
    this machine a worktree path can carry a junction, so a test that compared
    strings git never produced would pass on a spelling the tool never sees.
    """
    mine = tmp_path / "claude-launcher"
    other = tmp_path / "gds6"
    worktree = mine / ".claude" / "worktrees" / "w1"
    for d in (mine, other, worktree):
        d.mkdir(parents=True)
    return {"mine": mine, "other": other, "worktree": worktree}


@pytest.fixture
def gate(monkeypatch, repos):
    """Install a fake daemon and a fake git; hand back what each answered.

    ``git`` is faked at :func:`workspace_check._git`, which is the one place
    the tool shells out. The map is keyed by directory and answers the two
    ``rev-parse`` questions the tool asks: which repository owns this
    directory, and is this a linked worktree.
    """
    mine, other, worktree = repos["mine"], repos["other"], repos["worktree"]

    #: directory -> (git-dir, git-common-dir). Equal pair = main checkout.
    tree = {
        str(mine): (str(mine / ".git"), str(mine / ".git")),
        str(other): (str(other / ".git"), str(other / ".git")),
        str(worktree): (str(mine / ".git" / "worktrees" / "w1"), str(mine / ".git")),
    }

    def fake_git(cwd, *args):
        pair = tree.get(str(Path(cwd)))
        if pair is None:
            return ""
        return pair[1] if args[-1] == "--git-common-dir" else pair[0]

    monkeypatch.setattr(workspace_check, "_git", fake_git)
    monkeypatch.setenv("CLAUNCH_SESSION", "s1")

    def install(routes, client=True):
        c = _FakeClient(routes) if client else None
        monkeypatch.setattr(workspace_check.daemon_client, "connect", lambda: c)
        return c

    return install


def _issue(path, *, name="claude-launcher", known=True, board=None):
    """The shape ``GET /api/beads/<id>`` answers with, as measured."""
    return {
        "workspace": name,
        "workspace_known": known,
        "workspaces": [{"name": name, "path": str(path)}],
        "issue": {"source_repo_path": str(path) if board is None else board},
    }


def _session(cwd, issue="claunch-aaaaa"):
    return {"name": "s1", "cwd": str(cwd), "issue": issue}


def _sessions(*rows):
    return {"sessions": list(rows)}


def _roster(*members, mesh="claunch"):
    """The shape ``GET /api/mesh`` answers with, trimmed to what is read.

    ``members`` are ``(session, role)`` pairs, all hosted on this machine.
    """
    return {"meshes": [{
        "name": mesh,
        "members": [
            {"handle": f"{s}-h", "session": s, "role": role, "roles": [role], "local": True}
            for s, role in members
        ],
    }]}


def _run(mod, **kw):
    argv = ["workspace_check.py"]
    for key, value in kw.items():
        argv += [f"--{key}", str(value)]
    return mod.main(argv)


def test_every_axis_agreeing_is_the_pass(gate, repos, capsys):
    client = gate({
        "/api/sessions/s1": _session(repos["mine"]),
        "/api/beads/claunch-aaaaa": _issue(repos["mine"]),
        "/api/sessions": _sessions({"name": "s1", "cwd": str(repos["worktree"]), "status": "busy"}),
    })
    assert _run(workspace_check, cwd=repos["worktree"]) == 0
    out = capsys.readouterr().out
    assert "MATCH" in out
    assert out.count("[ok  ]") == 3
    # The issue id is not passed in: the tool takes it off the session record,
    # which is where the daemon puts it when it registers a round's issue.
    assert "/api/beads/claunch-aaaaa" in client.asked


def test_an_issue_in_another_repository_is_a_mismatch(gate, repos, capsys):
    gate({
        "/api/sessions/s1": _session(repos["mine"]),
        "/api/beads/claunch-aaaaa": _issue(repos["other"], name="gds6"),
        "/api/sessions": _sessions({"name": "s1", "cwd": str(repos["worktree"]), "status": "busy"}),
    })
    assert _run(workspace_check, cwd=repos["worktree"]) == 1
    out = capsys.readouterr().out
    assert "MISMATCH" in out and "issue" in out
    # Both sides named, because the person answering the approval has to see
    # which two directories were compared, not that a comparison failed.
    assert str(repos["other"]).lower() in out.lower()


def test_a_session_that_wandered_out_of_its_repository_is_a_mismatch(gate, repos, capsys):
    """Axis 2 stands on its own, with no issue in the round at all.

    This is the case an issueless round would otherwise have no check for:
    ``--no-issue`` sessions reach ``branch-setup`` by their own route, and the
    board axis has nothing to say about them.
    """
    gate({
        "/api/sessions/s1": _session(repos["mine"], issue=""),
        "/api/sessions": _sessions({"name": "s1", "cwd": str(repos["other"]), "status": "busy"}),
    })
    assert _run(workspace_check, cwd=repos["other"]) == 1
    out = capsys.readouterr().out
    assert "MISMATCH" in out and "session" in out


def test_another_live_session_in_the_same_linked_worktree_is_a_mismatch(gate, repos, capsys):
    """One working tree, one checked-out branch, two writers.

    The second session's ``git switch -c`` in ``branch-setup`` moves the
    first one's files under it, and neither is told.
    """
    gate({
        "/api/sessions/s1": _session(repos["mine"]),
        "/api/beads/claunch-aaaaa": _issue(repos["mine"]),
        "/api/sessions": _sessions(
            {"name": "s1", "cwd": str(repos["worktree"]), "status": "busy"},
            {"name": "s2", "cwd": str(repos["worktree"]), "status": "idle"},
        ),
    })
    assert _run(workspace_check, cwd=repos["worktree"]) == 1
    out = capsys.readouterr().out
    assert "MISMATCH" in out and "occupancy" in out
    assert "s2" in out


def test_a_session_that_moved_into_the_worktree_occupies_it(gate, repos, capsys):
    """Its record still names the main checkout; ``work_cwd`` says where it
    works now (claunch-4zc51.1.2), and that is where it is a second writer."""
    gate({
        "/api/sessions/s1": _session(repos["mine"]),
        "/api/beads/claunch-aaaaa": _issue(repos["mine"]),
        "/api/sessions": _sessions(
            {"name": "s1", "cwd": str(repos["worktree"]), "status": "busy"},
            {"name": "s2", "cwd": str(repos["mine"]), "status": "idle",
             "work_cwd": str(repos["worktree"])},
        ),
    })
    assert _run(workspace_check, cwd=repos["worktree"]) == 1
    out = capsys.readouterr().out
    assert "occupancy" in out and "s2" in out


def test_an_exited_session_does_not_occupy_a_worktree(gate, repos, capsys):
    """A session that has ended is not standing anywhere.

    Session records outlive their processes -- this machine's list carried 739
    rows when the tool was written, most of them ``exited`` -- so counting
    every row would make every reused worktree a mismatch.
    """
    gate({
        "/api/sessions/s1": _session(repos["mine"]),
        "/api/beads/claunch-aaaaa": _issue(repos["mine"]),
        "/api/sessions": _sessions(
            {"name": "s1", "cwd": str(repos["worktree"]), "status": "busy"},
            {"name": "s0", "cwd": str(repos["worktree"]), "status": "exited"},
        ),
    })
    assert _run(workspace_check, cwd=repos["worktree"]) == 0
    assert "MATCH" in capsys.readouterr().out


def test_the_main_checkout_is_not_judged_on_occupancy(gate, repos, capsys):
    """Sessions share the main checkout routinely, and ``branch-setup`` knows.

    That step's own text cuts a worktree when the checkout is shared. Failing
    the round here would stop what the workflow already handles a step later.
    """
    gate({
        "/api/sessions/s1": _session(repos["mine"]),
        "/api/beads/claunch-aaaaa": _issue(repos["mine"]),
        "/api/sessions": _sessions(
            {"name": "s1", "cwd": str(repos["mine"]), "status": "busy"},
            {"name": "s2", "cwd": str(repos["mine"]), "status": "busy"},
        ),
    })
    assert _run(workspace_check, cwd=repos["mine"]) == 0
    out = capsys.readouterr().out
    assert "MATCH" in out
    assert "[?   ] occupancy" in out


def _occupied_by(gate, repos, row, roster):
    """A round in the linked worktree with one more live session ``row`` in it."""
    routes = {
        "/api/sessions/s1": _session(repos["mine"]),
        "/api/beads/claunch-aaaaa": _issue(repos["mine"]),
        "/api/sessions": _sessions(
            {"name": "s1", "cwd": str(repos["worktree"]), "status": "busy"},
            {"cwd": str(repos["worktree"]), "status": "idle", **row},
        ),
    }
    if roster is not None:
        routes["/api/mesh"] = roster
    return gate(routes)


def test_this_sessions_own_reviewer_child_does_not_occupy_the_worktree(gate, repos, capsys):
    """``reviewer-ensure`` stands its reviewer in the worker's worktree.

    Spawned with no worktree, the child's directory is the parent's, and it
    lives until the run ends -- so counting it failed every round after the
    first (claunch-kg3oe.1.1: round 2 of run-8d3410ce needed a person to pass
    this gate over s38, the run's own reviewer). The pass still names it, so
    a reader can tell "nobody here" from "only the reviewer here".
    """
    _occupied_by(gate, repos, {"name": "s2", "parent": "s1"}, _roster(("s1", "worker"), ("s2", "reviewer")))
    assert _run(workspace_check, cwd=repos["worktree"]) == 0
    out = capsys.readouterr().out
    assert "MATCH" in out
    assert "[ok  ] occupancy" in out and "reviewer child (s2)" in out


def test_a_child_that_is_not_a_reviewer_still_occupies_the_worktree(gate, repos, capsys):
    """Being this session's child is not the exemption; being its reviewer is.

    A worker child given no worktree of its own is exactly the second writer
    this axis exists for.
    """
    _occupied_by(gate, repos, {"name": "s2", "parent": "s1"}, _roster(("s1", "worker"), ("s2", "worker")))
    assert _run(workspace_check, cwd=repos["worktree"]) == 1
    out = capsys.readouterr().out
    assert "MISMATCH" in out and "[FAIL] occupancy" in out and "s2" in out


def test_a_reviewer_subrole_does_not_make_a_writer_a_reviewer(gate, repos, capsys):
    """The primary role sets a member's stance; a subrole does not change it.

    A worker that also answers review questions (``roles: [worker,
    reviewer]``) still writes, so it is counted.
    """
    roster = _roster(("s1", "worker"), ("s2", "worker"))
    roster["meshes"][0]["members"][1]["roles"] = ["worker", "reviewer"]
    _occupied_by(gate, repos, {"name": "s2", "parent": "s1"}, roster)
    assert _run(workspace_check, cwd=repos["worktree"]) == 1
    assert "[FAIL] occupancy" in capsys.readouterr().out


def test_another_sessions_reviewer_still_occupies_the_worktree(gate, repos, capsys):
    """The exemption is this session's own reviewer, not any reviewer.

    A reviewer that some other session stood here is not reading this round's
    branch, and nothing about it was arranged by this run.
    """
    _occupied_by(gate, repos, {"name": "s2", "parent": "s9"}, _roster(("s2", "reviewer"), ("s9", "worker")))
    assert _run(workspace_check, cwd=repos["worktree"]) == 1
    out = capsys.readouterr().out
    assert "MISMATCH" in out and "[FAIL] occupancy" in out and "s2" in out


def test_a_reviewer_on_another_machine_is_not_taken_for_a_local_child(gate, repos, capsys):
    """Session names are unique per machine only; a mirrored mesh lists others.

    A remote member that happens to share the child's session name and holds
    ``reviewer`` says nothing about the session standing here.
    """
    roster = _roster(("s1", "worker"), ("s2", "reviewer"))
    roster["meshes"][0]["members"][1]["local"] = False
    _occupied_by(gate, repos, {"name": "s2", "parent": "s1"}, roster)
    assert _run(workspace_check, cwd=repos["worktree"]) == 1
    assert "[FAIL] occupancy" in capsys.readouterr().out


def test_with_no_child_here_the_roster_is_not_read(gate, repos, capsys):
    """No child in the worktree: the axis reads what it read before, and no more.

    The roster route is left out of the fake on purpose -- asking for it would
    be recorded, and the answer is decided without it.
    """
    client = _occupied_by(gate, repos, {"name": "s2"}, None)
    assert _run(workspace_check, cwd=repos["worktree"]) == 1
    out = capsys.readouterr().out
    assert "[FAIL] occupancy" in out and "s2" in out
    assert "/api/mesh" not in client.asked


def test_an_unreadable_roster_excludes_nobody_and_says_so(gate, repos, capsys):
    """Without the roster the role cannot be read, so the child is counted.

    That is the answer the axis gave before reviewer children were excluded,
    and the line says the roster was the reason, so the person at the
    approval knows a working daemon route would have changed it.
    """
    roster = workspace_check.daemon_client.DaemonClientError("mesh roster timed out")
    _occupied_by(gate, repos, {"name": "s2", "parent": "s1"}, roster)
    assert _run(workspace_check, cwd=repos["worktree"]) == 1
    out = capsys.readouterr().out
    assert "[FAIL] occupancy" in out
    assert "cannot read the mesh roster" in out


def test_an_unreadable_issue_does_not_sink_a_round_the_other_axes_pass(gate, repos, capsys):
    """One axis going quiet is not the same as the check failing.

    The exit code comes from the axes that spoke. What the output must still
    carry is that the board axis said nothing, so a reader can tell a
    three-axis pass from a one-axis pass.
    """
    gate({
        "/api/sessions/s1": _session(repos["mine"]),
        "/api/beads/claunch-aaaaa": workspace_check.daemon_client.DaemonClientError(
            "br show claunch-aaaaa --json failed (3): ISSUE_NOT_FOUND"
        ),
        "/api/sessions": _sessions({"name": "s1", "cwd": str(repos["worktree"]), "status": "busy"}),
    })
    assert _run(workspace_check, cwd=repos["worktree"]) == 0
    out = capsys.readouterr().out
    assert "[?   ] issue" in out and "ISSUE_NOT_FOUND" in out


def test_an_issue_that_contradicts_itself_is_unmeasurable_rather_than_decided(gate, repos, capsys):
    """Two sources, no winner.

    The workspace name and the board that minted the issue normally point at
    the same directory. When they do not, the record itself is wrong, and
    picking one of them would turn a thing a person has to read into a
    verdict nobody checked.
    """
    gate({
        "/api/sessions/s1": _session(repos["mine"]),
        "/api/beads/claunch-aaaaa": _issue(repos["mine"], board=str(repos["other"])),
        "/api/sessions": _sessions({"name": "s1", "cwd": str(repos["worktree"]), "status": "busy"}),
    })
    assert _run(workspace_check, cwd=repos["worktree"]) == 0
    out = capsys.readouterr().out
    assert "[?   ] issue" in out and "contradicts itself" in out


def test_the_long_path_prefix_is_taken_off_before_comparing(gate, repos, capsys):
    """``br`` records ``source_repo_path`` through ``\\\\?\\``.

    Measured on this machine: ``\\\\?\\F:\\works\\claude-launcher``. Git and
    the daemon both answer without the prefix, so leaving it on makes every
    round a mismatch against its own repository.
    """
    gate({
        "/api/sessions/s1": _session(repos["mine"]),
        "/api/beads/claunch-aaaaa": _issue(
            repos["mine"], known=False, board="\\\\?\\" + str(repos["mine"])
        ),
        "/api/sessions": _sessions({"name": "s1", "cwd": str(repos["worktree"]), "status": "busy"}),
    })
    assert _run(workspace_check, cwd=repos["worktree"]) == 0
    assert "[ok  ] issue" in capsys.readouterr().out


def test_an_unregistered_workspace_name_falls_back_to_the_board(gate, repos, capsys):
    """``workspace_known: false`` means the name resolves to no path here.

    A name that cannot be turned into a directory is not evidence about a
    directory, so the axis uses the one source that is still a path.
    """
    gate({
        "/api/sessions/s1": _session(repos["mine"]),
        "/api/beads/claunch-aaaaa": _issue(repos["mine"], name="elsewhere", known=False),
        "/api/sessions": _sessions({"name": "s1", "cwd": str(repos["worktree"]), "status": "busy"}),
    })
    assert _run(workspace_check, cwd=repos["worktree"]) == 0
    assert "the board that minted it" in capsys.readouterr().out


def test_nothing_measurable_cannot_tell(gate, repos, capsys):
    """No issue, no session record, no repository: the location is unjudged.

    ``2`` rather than ``0`` is the whole point of the code existing. The
    workflow sends it to the same human approval a mismatch goes to, because
    a location nobody could measure is not a location anybody approved.
    """
    gate({
        "/api/sessions/": workspace_check.daemon_client.DaemonClientError("no name"),
        "/api/sessions": _sessions(),
    })
    assert workspace_check.main(
        ["workspace_check.py", "--session", "", "--issue", "", "--cwd", str(repos["mine"].parent)]
    ) == 2
    out = capsys.readouterr().out
    assert "cannot tell" in out
    assert "not the same as judged correct" in out


def test_an_unreachable_daemon_cannot_tell(gate, repos, capsys):
    """Every axis but git's part of it is read off the daemon.

    Without it there is no issue, no session record and no session list, so
    the tool has no opinion -- and says so rather than passing the round on
    the strength of the one thing it could still see.
    """
    gate({}, client=False)
    assert _run(workspace_check, cwd=repos["worktree"]) == 2
    assert "cannot tell" in capsys.readouterr().out


def test_the_check_never_starts_a_daemon(gate, repos, monkeypatch):
    """``connect``, not ``ensure_running`` -- the rule ``keepalive_check`` set.

    A gate that starts a daemon changes the machine in order to answer a
    question about it, and a daemon that was not running had no session list
    worth reading anyway.
    """
    gate({}, client=False)

    def boom(*a, **k):
        raise AssertionError("the check started a daemon")

    monkeypatch.setattr(workspace_check.daemon_client, "ensure_running", boom)
    assert _run(workspace_check, cwd=repos["worktree"]) == 2


def test_a_directory_git_does_not_claim_is_reported_rather_than_guessed(gate, repos, capsys):
    """Outside a repository there is nothing to compare, and the line says so."""
    gate({
        "/api/sessions/s1": _session(repos["mine"], issue=""),
        "/api/sessions": _sessions({"name": "s1", "cwd": str(repos["worktree"]), "status": "busy"}),
    })
    assert _run(workspace_check, cwd=repos["mine"].parent) == 2
    out = capsys.readouterr().out
    assert "git does not claim this directory" in out


def test_an_explicit_issue_wins_over_the_session_record(gate, repos):
    """``--issue`` is how a round with a different main issue is checked.

    The session record carries the issue the daemon registered at creation; a
    revisit round's main issue is the next one off the queue, and ``intake``
    is where that is decided. The gate has to be able to be told.
    """
    client = gate({
        "/api/sessions/s1": _session(repos["mine"], issue="claunch-aaaaa"),
        "/api/beads/claunch-bbbbb": _issue(repos["mine"]),
        "/api/sessions": _sessions({"name": "s1", "cwd": str(repos["worktree"]), "status": "busy"}),
    })
    assert _run(workspace_check, cwd=repos["worktree"], issue="claunch-bbbbb") == 0
    assert "/api/beads/claunch-aaaaa" not in client.asked
