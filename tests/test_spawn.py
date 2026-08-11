"""Agent-initiated spawning: the policy, the session tree, and who commands whom.

Split from the mesh tests on purpose — the tree is a property of *sessions*
and holds whether or not a mesh exists. What the tree does to messaging lives
in ``test_member_graph.py``.
"""

from __future__ import annotations

import pytest

from claude_launcher import spawn, store
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import ManagerError, SessionManager

pytestmark = pytest.mark.usefixtures("home")


PARENT = {
    "name": "lead",
    "harness": "py",
    "profile": "work",
    "cwd": "/tmp/project",
    "args": ["--flag"],
    "env": {"A": "1"},
}


def _policy(**overrides) -> spawn.SpawnPolicy:
    if overrides:
        store.update(lambda doc: doc.update({"spawn": overrides}))
    return spawn.SpawnPolicy.load()


# --------------------------------------------------------------------------- #
# what a child inherits
# --------------------------------------------------------------------------- #
def test_child_inherits_everything_that_decides_what_runs():
    child = spawn.check(_policy(), {}, parent=PARENT, depth=0, children=0)
    assert child == {
        "harness": "py",
        "profile": "work",
        "cwd": "/tmp/project",
        "args": ["--flag"],
        "env": {"A": "1"},
    }


@pytest.mark.parametrize(
    "field, value",
    [
        ("profile", "other"),
        ("cwd", "/tmp/elsewhere"),
        ("args", ["--yolo"]),
        ("env", {"TOKEN": "x"}),
    ],
)
def test_gated_fields_are_refused_by_default(field, value):
    with pytest.raises(spawn.SpawnDenied) as exc:
        spawn.check(_policy(), {field: value}, parent=PARENT, depth=0, children=0)
    # The message has to name the key that would allow it: an agent that is
    # told only "denied" will retry the same call.
    assert f"spawn.allow_{field}" in str(exc.value)


def test_an_unlocked_field_is_taken_from_the_request():
    policy = _policy(allow_cwd=True)
    child = spawn.check(
        policy, {"cwd": "/tmp/elsewhere"}, parent=PARENT, depth=0, children=0
    )
    assert child["cwd"] == "/tmp/elsewhere"
    assert child["profile"] == "work"  # still inherited


def test_env_is_merged_over_the_parents_not_replaced():
    policy = _policy(allow_env=True)
    child = spawn.check(
        policy, {"env": {"B": "2"}}, parent=PARENT, depth=0, children=0
    )
    assert child["env"] == {"A": "1", "B": "2"}


# --------------------------------------------------------------------------- #
# harness
# --------------------------------------------------------------------------- #
def test_a_harness_swap_needs_an_explicit_unlock():
    with pytest.raises(spawn.SpawnDenied) as exc:
        spawn.check(
            _policy(), {"harness": "codex"}, parent=PARENT, depth=0, children=0
        )
    assert "spawn.allow_harness" in str(exc.value)


def test_swapping_the_harness_drops_the_inherited_args():
    """Those flags were written for another program; carrying them over is a
    spawn failure at best and a misread flag at worst."""
    policy = _policy(allow_harness=["codex"])
    child = spawn.check(
        policy, {"harness": "codex"}, parent=PARENT, depth=0, children=0
    )
    assert child["harness"] == "codex"
    assert child["args"] == []


def test_naming_your_own_harness_is_not_a_swap():
    child = spawn.check(
        _policy(), {"harness": "py"}, parent=PARENT, depth=0, children=0
    )
    assert child["harness"] == "py"
    assert child["args"] == ["--flag"]  # kept: nothing was swapped


# --------------------------------------------------------------------------- #
# limits
# --------------------------------------------------------------------------- #
def test_depth_limit_stops_runaway_nesting():
    policy = _policy(max_depth=2)
    with pytest.raises(spawn.SpawnDenied) as exc:
        spawn.check(policy, {}, parent=PARENT, depth=2, children=0)
    assert "max_depth" in str(exc.value)


def test_child_limit_stops_runaway_fanout():
    policy = _policy(max_children=2)
    with pytest.raises(spawn.SpawnDenied) as exc:
        spawn.check(policy, {}, parent=PARENT, depth=0, children=2)
    assert "max_children" in str(exc.value)


def test_spawning_can_be_switched_off_entirely():
    policy = _policy(enabled=False)
    with pytest.raises(spawn.SpawnDenied):
        spawn.check(policy, {}, parent=PARENT, depth=0, children=0)


def test_a_malformed_spawn_block_reads_as_the_defaults():
    """A YAML typo on a path an agent triggers must not surface as a broken
    feature — the limits fall back, they do not vanish and do not explode."""
    store.update(lambda doc: doc.update({"spawn": "yes please"}))
    policy = spawn.SpawnPolicy.load()
    assert policy.max_children == spawn.DEFAULTS["max_children"]
    assert policy.allow_args is False


def test_capabilities_answers_before_a_refusal_is_provoked():
    policy = _policy(max_children=2, allow_cwd=True)
    report = spawn.capabilities(policy, depth=0, children=2)
    assert report["can_spawn"] is False
    assert report["children_remaining"] == 0
    assert any("child limit" in b for b in report["blocked_by"])
    assert "cwd" in report["may_choose"]


# --------------------------------------------------------------------------- #
# the session tree
# --------------------------------------------------------------------------- #
def _tree() -> SessionManager:
    """lead -> (w1 -> w1a, w2), plus an unrelated root."""
    mgr = SessionManager(idle_threshold=1.0, scrollback=10, restore_default=False)
    for name, parent in (
        ("lead", None), ("w1", "lead"), ("w2", "lead"), ("w1a", "w1"), ("solo", None),
    ):
        mgr._sessions[name] = _FakeSession(name, parent)
    return mgr


class _FakeSession:
    """Just enough of a session for the tree walks — they read only the def."""

    exited = False

    def __init__(self, name: str, parent):
        self.sdef = SessionDef(name=name, harness="py", parent=parent)


def test_children_and_descendants():
    mgr = _tree()
    assert mgr.children("lead") == ["w1", "w2"]
    assert mgr.descendants("lead") == ["w1", "w2", "w1a"]
    assert mgr.children("w2") == []


def test_depth_counts_resolvable_ancestors():
    mgr = _tree()
    assert mgr.depth("lead") == 0
    assert mgr.depth("w1") == 1
    assert mgr.depth("w1a") == 2


def test_a_dangling_parent_makes_a_root():
    """The parent may be killed and cleared while its children run on — a
    child that pointed at a name nobody has must not become unreachable."""
    mgr = _tree()
    del mgr._sessions["lead"]
    assert mgr.depth("w1") == 0
    assert mgr.ancestors("w1") == []


def test_a_parent_cycle_terminates():
    """Not reachable through spawn (the parent must pre-exist), but a
    hand-edited sessions.json can produce one and must not hang the daemon."""
    mgr = _tree()
    mgr._sessions["lead"] = _FakeSession("lead", "w1a")
    assert mgr.depth("w1a") <= 3
    assert "w1a" not in mgr.ancestors("w1a")


def test_authority_runs_down_the_tree_only():
    mgr = _tree()
    assert mgr.commands("lead", "w1") is True
    assert mgr.commands("lead", "w1a") is True   # grandchild
    assert mgr.commands("w1", "lead") is False   # never upward
    assert mgr.commands("w1", "w2") is False     # never sideways
    assert mgr.commands("lead", "solo") is False


def test_require_commands_names_the_rule():
    mgr = _tree()
    with pytest.raises(ManagerError) as exc:
        mgr.require_commands("w1", "w2")
    assert "spawned" in str(exc.value)


# --------------------------------------------------------------------------- #
# role: one word, two authorities
#
# A session's role comes from the packaged vocabulary and injects a stance
# into a claude system prompt; a member's role comes from the mesh's own
# vocabulary and applies to any harness. A spawn names one word and means
# both, so the session half is dropped — never raised — wherever it does not
# apply, or a custom vocabulary and every non-claude harness would become
# un-spawnable.
# --------------------------------------------------------------------------- #
def test_a_packaged_role_on_a_claude_child_becomes_its_stance():
    assert SessionManager._spawn_role("reviewer", "claude") == "reviewer"


def test_a_role_is_dropped_for_a_harness_with_no_system_prompt():
    assert SessionManager._spawn_role("reviewer", "codex") is None


def test_a_role_outside_the_packaged_vocabulary_is_dropped_not_raised():
    """A mesh may replace the vocabulary wholesale; such a name is a legal
    member role with no session stance behind it."""
    assert SessionManager._spawn_role("gardener", "claude") is None


def test_no_role_is_still_no_role():
    assert SessionManager._spawn_role("", "claude") is None
    assert SessionManager._spawn_role(None, "claude") is None
