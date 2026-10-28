"""``claunch gate landing-target`` (claunch-od1mx.1).

The landing gate used to pass five machine checks and then file a request
whose routing was tried only afterwards, as the daemon pushed it. When the
routing failed the board had already moved to ``in_review`` and the failure
arrived as a journal line and a nudge loop that reported success twenty-five
times over about four hours without sending anything (claunch-od1mx,
``20261027-192120-21d28c``).

This gate is the question asked *first*: is there a session that would take
the request? It adds no routing rule -- it runs the daemon's
``landing_route.Router``, the same code the push clock runs, through the
endpoint in ``daemon/api.py``. What is pinned here is the contract the
workflow branches on, and in particular the one distinction that makes the
gate safe to act on:

* ``0`` there is a target, ``1`` there is none, ``2`` this could not be told;
* and **``2`` is never spelled as ``1``**. A round that reads "could not
  tell" as "nobody can merge" holds a person to a re-routing decision nobody
  showed was needed; a round that reads it as ``0`` files a request at a
  target that may not be there. The Router marks the difference itself
  (``Target.undecidable``, set where its walk stopped on a fact it could not
  read), so the gate never matches on the wording of a reason.

The last test here is the one that keeps that honest: it asserts the Router
still raises the flag at each place it gives up on reading something, so a
new unreadable branch added to the walk without a flag is red rather than
silently reclassified as "there is nobody".
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import landing_target as gate  # noqa: E402  (the tools/ entry, as a gate runs it)


# --------------------------------------------------------------------------- #
# the exit codes
# --------------------------------------------------------------------------- #
def _target(**over):
    holder = {"cwd": "/repo", "run": "main", "main": True, "version": "v2"}
    doc = {"owner": "lead1", "holder": holder, "why": "", "passed": [],
           "refused": [], "undecidable": False}
    doc.update(over)
    return doc


def test_a_target_is_zero():
    assert gate.verdict(_target()) == gate.TARGET == 0


def test_nobody_is_one():
    doc = {"owner": None, "holder": None,
           "why": "parent 'p1' holds no 'merger' role in the worker's mesh ('m')",
           "passed": [], "refused": [], "undecidable": False}
    assert gate.verdict(doc) == gate.NO_TARGET == 1


def test_the_daemon_not_answering_is_not_nobody():
    """No answer is not an answer. Without this the gate would send a person
    a re-routing decision on the strength of a daemon that was merely
    restarting."""
    assert gate.verdict(None) == gate.CANNOT_TELL == 2


def test_an_unreadable_walk_is_not_nobody():
    """The distinction the workflow acts on: the Router says it could not
    read something, so 'nobody can merge' has not been established."""
    doc = {"owner": None, "holder": None, "why": "the worker's mesh membership "
           "could not be read", "passed": [], "refused": [], "undecidable": True}
    assert gate.verdict(doc) == gate.CANNOT_TELL == 2


def test_undecidable_wins_over_a_target_too():
    """Defensive: a self-contradicting answer is not a green. A walk that
    says both 'here is a holder' and 'I could not read the roster' has not
    shown the holder is routable."""
    assert gate.verdict(_target(undecidable=True)) == gate.CANNOT_TELL


# --------------------------------------------------------------------------- #
# the render: the Router's own sentences, not a paraphrase
# --------------------------------------------------------------------------- #
def test_the_no_target_line_carries_the_routers_sentence():
    why = ("parent 's1524' holds no 'merger' role in the worker's mesh ('s1524'); "
           "mesh leader fallback: no leader in mesh 's1524'")
    doc = {"owner": None, "holder": None, "why": why, "passed": [], "refused": [],
           "undecidable": False}
    line = gate._render(doc, gate.NO_TARGET)
    assert why in line
    assert line.startswith("no target:")


def test_the_target_line_names_who_was_passed_over():
    doc = _target(passed=["parent 'p1' holds no 'merger' role in the worker's mesh ('m')"])
    line = gate._render(doc, gate.TARGET)
    assert "lead1" in line and "/repo" in line
    assert "passed over" in line


# --------------------------------------------------------------------------- #
# the wiring: the workflow's command is this gate
# --------------------------------------------------------------------------- #
def test_claunch_gate_knows_the_name():
    from claude_launcher import cli_gate

    assert cli_gate.GATES["landing-target"] == "landing_target"
    # ...and the tools/ entry exists, so `claunch gate` inside a checkout
    # runs the tree it is checking rather than an installed copy.
    script = Path(__file__).resolve().parents[1] / "tools" / "landing_target.py"
    assert script.is_file()


def test_it_loads_on_the_standard_library_alone():
    """`uv run --no-sync` never populates a worktree's .venv, so a gate that
    imports a third-party module fails to start -- and to the workflow a
    failed start is exit 2, i.e. 'could not tell' on every round. The gate
    reaches the daemon through ``daemon_client``, which is stdlib-only by
    design, and nothing else."""
    import subprocess

    out = subprocess.run(
        [sys.executable, "-S", "-E", "-c",
         "import sys; sys.path.insert(0, sys.argv[1]);"
         "import claude_launcher.gates.landing_target as g; print(g.CANNOT_TELL)",
         str(Path(__file__).resolve().parents[1] / "src")],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    assert out.returncode == 0, out.stderr[-1500:]
    assert out.stdout.strip() == "2"


# --------------------------------------------------------------------------- #
# the Router's flag -- the reason the codes can be trusted
# --------------------------------------------------------------------------- #
def _blind_router(monkeypatch_src: str):
    """A Router whose mesh raises where the walk reads membership."""
    from claude_launcher.daemon import landing_route

    class _BrokenMesh:
        def meshes_for_session(self, scope):
            raise RuntimeError("roster unreadable")

    class _Manager:
        def get(self, name):
            raise KeyError(name)

    return landing_route.Router(_Manager(), _BrokenMesh(), lambda s, r: [])


def test_an_unreadable_mesh_membership_sets_undecidable():
    """``Router.merger_role`` cannot read the worker's meshes, so it has not
    shown that anybody lacks the role. The flag is what tells the probe so;
    without it the walk would be a confident 'nobody'."""
    router = _blind_router("")
    found = router.stacked("/repo", "w1")
    assert found.holder is None
    assert found.undecidable is True
    assert "could not be read" in found.why


def test_the_independent_direction_flags_too():
    """``relation: independent`` routes through ``Router.independent``, whose
    walk is the leader one. A flag set only in ``stacked`` would leave every
    independent run reading an unreadable roster as a definite nobody --
    the arrangement this issue was measured in."""
    found = _blind_router("").independent("/repo", "w1")
    assert found.holder is None
    assert found.undecidable is True, found.why


def test_a_definite_nobody_is_not_undecidable():
    """The mirror case: a walk that read everything and found nobody. This is
    the answer the workflow sends to a person, so it must not be flagged."""
    from claude_launcher.daemon import landing_route

    class _EmptyMesh:
        members: dict = {}

    class _MeshOk:
        def meshes_for_session(self, scope):
            return [{"mesh": "m", "handle": "w", "role": "worker"}]

        def get(self, name):
            return _EmptyMesh()

        def is_local_member(self, mesh, m):
            return True

    class _Mgr:
        def get(self, name):
            raise KeyError(name)

    found = landing_route.Router(_Mgr(), _MeshOk(), lambda s, r: []).stacked("/repo", "w1")
    assert found.holder is None
    assert found.undecidable is False, found.why


@pytest.mark.parametrize("where", ["merger_role", "merger", "leader"])
def test_every_unreadable_branch_raises_the_flag(where):
    """Guard the guard: the flag is only worth having if every place the walk
    gives up on reading actually sets it.

    Each branch is driven directly, not through ``stacked()``: the leader
    walk reads the same membership, so a broken ``merger_role`` is still
    caught by the fallback and the stacked-level test cannot tell the two
    apart. That is exactly how a branch loses its flag without any test
    going red.
    """
    from claude_launcher.daemon import landing_route

    class _BrokenMesh:
        def meshes_for_session(self, scope):
            raise RuntimeError("roster unreadable")

    class _Mgr:
        def __init__(self, names=()):
            self.names = names

        def get(self, name):
            if name in self.names:
                return type("S", (), {"exited": False, "sdef": None})()
            raise KeyError(name)

    def _holders(session, repo):
        raise RuntimeError("slot unreadable")

    blind: list = []
    if where == "merger_role":
        # the mesh membership the role is read in
        landing_route.Router(_Mgr(), _BrokenMesh(), _holders).merger_role(
            "w1", "p1", blind)
    elif where == "merger":
        # the run slots a session drives. The session must hold the role,
        # or the walk returns before it ever reads the slots -- the branch
        # under test.
        r = landing_route.Router(_Mgr(names={"p1"}), _MergerMesh(), _holders)
        holder, why = r.merger("p1", "/repo", "w1", blind)
        assert holder is None and "could not be read" in why, why
    else:
        # the mesh membership the leader search is read in
        landing_route.Router(_Mgr(), _BrokenMesh(), _holders).leader(
            "w1", [], "/repo", tried=set(), blind=blind)
    assert blind, f"{where} swallowed an unreadable source without recording it"


class _QuietMesh:
    """A mesh that reads normally and holds nobody."""

    def meshes_for_session(self, scope):
        return [{"mesh": "m", "handle": "p", "role": "worker"}]

    def get(self, name):
        return type("M", (), {"members": {}})()

    def is_local_member(self, mesh, member):
        return True


class _MergerMesh(_QuietMesh):
    """The same, with ``p1`` holding ``merger`` -- so the walk reaches the
    run slots instead of stopping at the role."""

    def get(self, name):
        member = type("R", (), {"session": "p1", "roles": ["merger"],
                                "handle": "p", "role": "worker"})()
        return type("M", (), {"members": {"p": member}})()
