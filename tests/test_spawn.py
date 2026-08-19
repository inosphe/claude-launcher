"""Agent-initiated spawning: the policy, the session tree, and who commands whom.

Split from the mesh tests on purpose — the tree is a property of *sessions*
and holds whether or not a mesh exists. What the tree does to messaging lives
in ``test_member_graph.py``.
"""

from __future__ import annotations

import pytest

from claude_launcher import spawn, store, workspaces
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
        "borrow": None,
        "null_token": False,
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
# auth: borrow shares profile's gate, null has none
# --------------------------------------------------------------------------- #
def test_borrow_is_gated_behind_allow_profile():
    """Same gate as profile because it is the same question — whose login
    does the child hold. The denial names the key that grants it."""
    with pytest.raises(spawn.SpawnDenied) as exc:
        spawn.check(
            _policy(), {"borrow": "lender"}, parent=PARENT, depth=0, children=0
        )
    assert "spawn.allow_profile" in str(exc.value)


def test_an_unlocked_borrow_travels_and_replaces_inherited_tokenlessness():
    policy = _policy(allow_profile=True)
    parent = {**PARENT, "null_token": True}
    child = spawn.check(
        policy, {"borrow": "lender"}, parent=parent, depth=0, children=0
    )
    assert child["borrow"] == "lender"
    assert child["null_token"] is False


def test_null_is_never_gated_and_replaces_an_inherited_borrow():
    """--null takes a credential away rather than granting one, so the
    shipped (all-locked) policy still allows it."""
    parent = {**PARENT, "borrow": "lender"}
    child = spawn.check(
        _policy(), {"null_token": True}, parent=parent, depth=0, children=0
    )
    assert child["null_token"] is True
    assert child["borrow"] is None


def test_a_child_authenticates_the_way_its_parent_does():
    parent = {**PARENT, "borrow": "lender"}
    child = spawn.check(_policy(), {}, parent=parent, depth=0, children=0)
    assert child["borrow"] == "lender"


def test_a_harness_swap_drops_the_inherited_auth_with_the_args():
    """borrow/null are claude-only machinery; dragged onto another harness
    they would fail the spawn over a field nobody in the request named."""
    policy = _policy(allow_harness=["codex"])
    parent = {**PARENT, "borrow": "lender", "null_token": False}
    child = spawn.check(
        policy, {"harness": "codex"}, parent=parent, depth=0, children=0
    )
    assert child["borrow"] is None
    assert child["null_token"] is False


# --------------------------------------------------------------------------- #
# workspace: the directory as a pick, not a spelling
# --------------------------------------------------------------------------- #
def test_a_workspace_resolves_to_the_directory_it_names(tmp_path):
    """And with the stock policy: this is the one unlock that starts open,
    because it offers a list the user vouched for rather than a value the
    agent invents."""
    ws = tmp_path / "hq"
    ws.mkdir()
    workspaces.add(str(ws), name="hq")
    child = spawn.check(
        _policy(), {"workspace": "hq"}, parent=PARENT, depth=0, children=0
    )
    assert child["cwd"] == str(ws.resolve())
    assert child["profile"] == "work"  # only the directory moved


def test_an_empty_registry_leaves_nothing_to_choose(tmp_path):
    """What makes the open default safe: with no workspace registered there
    is no directory an agent can name, so the child stays where its parent
    is whatever it asks for."""
    child = spawn.check(_policy(), {}, parent=PARENT, depth=0, children=0)
    assert child["cwd"] == PARENT["cwd"]
    with pytest.raises(spawn.SpawnDenied) as exc:
        spawn.check(
            _policy(), {"workspace": "hq"}, parent=PARENT, depth=0, children=0
        )
    assert "(none)" in str(exc.value)


def test_the_open_default_can_still_be_shut(tmp_path):
    """A fleet that registered its workspaces for the browser, not for its
    agents, turns the one open unlock off."""
    ws = tmp_path / "hq"
    ws.mkdir()
    workspaces.add(str(ws), name="hq")
    with pytest.raises(spawn.SpawnDenied) as exc:
        spawn.check(
            _policy(allow_workspace=False),
            {"workspace": "hq"},
            parent=PARENT,
            depth=0,
            children=0,
        )
    assert "spawn.allow_workspace" in str(exc.value)


def test_a_workspace_may_also_be_named_by_its_path(tmp_path):
    """Both spellings are what a caller has in hand — the name the listing
    printed, or the path its shell completed."""
    ws = tmp_path / "hq"
    ws.mkdir()
    workspaces.add(str(ws), name="hq")
    child = spawn.check(
        _policy(),
        {"workspace": str(ws)},
        parent=PARENT,
        depth=0,
        children=0,
    )
    assert child["cwd"] == str(ws.resolve())


def test_an_unregistered_workspace_is_refused_with_the_registered_ones(tmp_path):
    """The refusal an agent can act on: it cannot see the registry, so being
    told only 'no' leaves it guessing at names."""
    ws = tmp_path / "hq"
    ws.mkdir()
    workspaces.add(str(ws), name="hq")
    with pytest.raises(spawn.SpawnDenied) as exc:
        spawn.check(
            _policy(),
            {"workspace": "somewhere-else"},
            parent=PARENT,
            depth=0,
            children=0,
        )
    assert "hq" in str(exc.value)


def test_a_registered_directory_that_is_gone_is_refused_here(tmp_path):
    """A workspace on a removable drive is legitimately absent half the time.
    Catching it in the policy is what the registry is *for*: the alternative
    surfaces later as a harness that could not start."""
    ws = tmp_path / "hq"
    ws.mkdir()
    workspaces.add(str(ws), name="hq")
    ws.rmdir()
    with pytest.raises(spawn.SpawnDenied) as exc:
        spawn.check(
            _policy(), {"workspace": "hq"}, parent=PARENT, depth=0, children=0
        )
    assert "not there" in str(exc.value)


def test_workspace_and_cwd_together_are_refused_rather_than_ranked(tmp_path):
    ws = tmp_path / "hq"
    ws.mkdir()
    workspaces.add(str(ws), name="hq")
    with pytest.raises(spawn.SpawnDenied) as exc:
        spawn.check(
            _policy(allow_cwd=True),
            {"workspace": "hq", "cwd": "/tmp/elsewhere"},
            parent=PARENT,
            depth=0,
            children=0,
        )
    assert "not both" in str(exc.value)


def test_the_two_directory_unlocks_are_independent(tmp_path):
    """The picker being open must not open the text box: they are different
    risks, so a raw path stays refused under the stock policy."""
    ws = tmp_path / "hq"
    ws.mkdir()
    workspaces.add(str(ws), name="hq")
    with pytest.raises(spawn.SpawnDenied) as exc:
        spawn.check(
            _policy(), {"cwd": str(ws)}, parent=PARENT, depth=0, children=0
        )
    # even though that exact directory is registered: the *spelling* is what
    # allow_cwd governs, and 'workspace' is the way to name it
    assert "spawn.allow_cwd" in str(exc.value)


def test_capabilities_lists_the_workspaces_not_just_the_field(tmp_path):
    """Every other name in ``may_choose`` is one the agent can already spell;
    a workspace name exists only in a registry it cannot read."""
    ws = tmp_path / "hq"
    ws.mkdir()
    workspaces.add(str(ws), name="hq")
    report = spawn.capabilities(_policy(), depth=0, children=0)
    assert "workspace" in report["may_choose"]
    assert [w["name"] for w in report["workspaces"]] == ["hq"]
    assert report["workspaces"][0]["path"] == str(ws.resolve())


def test_capabilities_stays_quiet_about_workspaces_when_locked(tmp_path):
    ws = tmp_path / "hq"
    ws.mkdir()
    workspaces.add(str(ws), name="hq")
    report = spawn.capabilities(
        _policy(allow_workspace=False), depth=0, children=0
    )
    assert "workspace" not in report["may_choose"]
    assert "workspaces" not in report


def test_capabilities_lists_the_profiles_only_when_the_field_is_unlocked():
    """Same courtesy as workspaces: profile names live in a registry the
    agent cannot read, so an unlocked field names its options."""
    from claude_launcher import profile

    profile.create("work")
    profile.create("other")
    report = spawn.capabilities(_policy(), depth=0, children=0)
    assert "profile" not in report["may_choose"]
    assert "borrow" not in report["may_choose"]
    assert "profiles" not in report

    report = spawn.capabilities(_policy(allow_profile=True), depth=0, children=0)
    assert "profile" in report["may_choose"]
    assert "borrow" in report["may_choose"]  # the same unlock covers both
    assert report["profiles"] == ["other", "work"]


def test_capabilities_always_offers_null_token():
    report = spawn.capabilities(_policy(), depth=0, children=0)
    assert "null_token" in report["may_choose"]


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


# --------------------------------------------------------------------------- #
# the two doors
#
# ``new-session`` and ``spawn`` build the same thing by different rights, and
# the difference only shows up later: a session created through the human door
# from inside an agent's session has no parent, no inherited mesh, and no way
# to report back. The daemon cannot tell the callers apart — an HTTP request
# carries no caller environment — so the CLI is where the split is kept.
# --------------------------------------------------------------------------- #
def test_new_session_refuses_from_inside_a_session_and_writes_the_spawn_line(
    monkeypatch, capsys, tmp_path
):
    from claude_launcher import cli

    workspaces.add(str(tmp_path), name="hq")
    monkeypatch.setenv("CLAUNCH_SESSION", "s7")
    capsys.readouterr()

    code = cli.main([
        "new-session", "-s", "coder", "--profile", "nc",
        "-c", str(tmp_path), "--role", "coder", "--workflow", "ecs-change",
    ])

    assert code == 2
    err = capsys.readouterr().err
    assert "'s7'" in err
    # the whole command, not just its name: translating the flags is the step
    # that sent the caller to the wrong door in the first place
    assert "claunch spawn -s coder --workspace hq" in err
    assert "--role coder" in err and "--workflow ecs-change" in err
    # ...and the field that now needs the policy's say-so travels with the
    # note naming the key that grants it
    assert "--profile nc" in err
    assert "spawn.allow_profile" in err
    assert "no --mesh needed" in err


def test_an_unregistered_directory_is_named_as_the_missing_step(
    monkeypatch, capsys, tmp_path
):
    """The refusal has to be actionable by the agent reading it, and 'use
    spawn' is not actionable when the directory it needs cannot be reached
    from spawn at all. Registering one is the user's call, so say so."""
    from claude_launcher import cli

    monkeypatch.setenv("CLAUNCH_SESSION", "s7")
    capsys.readouterr()

    assert cli.main(["new-session", "-c", str(tmp_path), "--profile", "nc"]) == 2
    err = capsys.readouterr().err
    assert "not a registered workspace" in err
    assert f"claunch workspace add {tmp_path}" in err


def test_detached_is_the_way_out(monkeypatch, tmp_path):
    """A session genuinely unrelated to the one it was typed in still has to
    be creatable — the refusal is a default, not a wall."""
    from claude_launcher import cli, daemon_client

    monkeypatch.setenv("CLAUNCH_SESSION", "s7")
    reached = {}

    class _Client:
        base_url = "http://x"

        def get(self, path):
            return {}  # the relay status line every session command prints

        def post(self, path, body=None):
            reached["body"] = body
            return {"name": "solo", "harness": "claude", "profile": "nc"}

    monkeypatch.setattr(daemon_client, "ensure_running", lambda: _Client())

    assert cli.main([
        "new-session", "-s", "solo", "--profile", "nc",
        "-c", str(tmp_path), "--detached",
    ]) == 0
    assert reached["body"]["name"] == "solo"
    # nobody's child: --detached does not smuggle a parent through
    assert "parent" not in reached["body"]


def test_cli_spawn_sends_the_gated_fields_it_grew(monkeypatch, tmp_path):
    """--profile/--null/--env and the arg remainder all travel in the request;
    whether they are allowed stays the daemon's call, not this command's."""
    from claude_launcher import cli, daemon_client

    reached = {}

    class _Client:
        base_url = "http://x"

        def get(self, path):
            return {}

        def post(self, path, body=None):
            reached["path"], reached["body"] = path, body
            return {"session": {"name": "kid", "cwd": str(tmp_path)}}

    monkeypatch.setattr(daemon_client, "ensure_running", lambda: _Client())

    assert cli.main([
        "spawn", "--parent", "lead", "-s", "kid", "--profile", "other",
        "--null", "--env", "K=V", "--", "--model", "opus",
    ]) == 0
    assert reached["path"] == "/api/sessions/lead/children"
    body = reached["body"]
    assert body["profile"] == "other"
    assert body["null_token"] is True
    assert body["env"] == {"K": "V"}
    assert body["args"] == ["--model", "opus"]

    assert cli.main(["spawn", "--parent", "lead", "--borrow", "lender"]) == 0
    assert reached["body"]["borrow"] == "lender"
    # unset flags must not travel as empty keys the daemon then gates on
    for absent in ("null_token", "args", "env", "profile"):
        assert absent not in reached["body"], absent


def test_cli_spawn_refuses_null_with_borrow_at_the_parser(capsys):
    from claude_launcher import cli

    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["spawn", "--null", "--borrow", "x"])
    assert "not allowed with" in capsys.readouterr().err


def test_new_session_carries_its_auth_choice_to_the_daemon(monkeypatch, tmp_path):
    from claude_launcher import cli, daemon_client

    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    reached = {}

    class _Client:
        base_url = "http://x"

        def get(self, path):
            return {}

        def post(self, path, body=None):
            reached["body"] = body
            return {"name": "s", "harness": "claude", "profile": "nc"}

    monkeypatch.setattr(daemon_client, "ensure_running", lambda: _Client())

    assert cli.main([
        "new-session", "--profile", "nc", "-c", str(tmp_path), "--borrow",
        "lender",
    ]) == 0
    assert reached["body"]["borrow"] == "lender"
    assert "null_token" not in reached["body"]

    assert cli.main([
        "new-session", "--profile", "nc", "-c", str(tmp_path), "--null",
    ]) == 0
    assert reached["body"]["null_token"] is True
    assert "borrow" not in reached["body"]


def test_outside_a_session_new_session_is_untouched(monkeypatch, tmp_path):
    from claude_launcher import cli, daemon_client

    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    reached = {}

    class _Client:
        base_url = "http://x"

        def get(self, path):
            return {}

        def post(self, path, body=None):
            reached["body"] = body
            return {"name": "s0", "harness": "claude", "profile": "nc"}

    monkeypatch.setattr(daemon_client, "ensure_running", lambda: _Client())

    assert cli.main(["new-session", "--profile", "nc", "-c", str(tmp_path)]) == 0
    assert reached["body"]["profile"] == "nc"
