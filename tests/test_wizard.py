"""The ``new-session --wizard`` form: what it offers, and what it answers with.

The terminal half (raw mode, ConsoleMode, the alternate screen) needs a real
console, so it is driven through the same seams :mod:`test_attach` uses: keys
are fed in as bytes and the form is read back as the lines it would paint.
Everything else is the form itself, which is deliberately free of I/O.
"""

from __future__ import annotations

import argparse

import pytest

from claude_launcher import attach as attach_mod
from claude_launcher import cli_sessions, wizard, worktree


class FakeSources(wizard.Sources):
    """Everything the daemon would publish, decided by the test instead."""

    def __init__(self, *, repo=True, workflows=None, meshes=True):
        self._repo = repo
        self._workflows = workflows if workflows is not None else {}
        self._meshes = meshes
        self.workflow_calls = []

    def harnesses(self):
        return [
            {"name": "claude", "available": True, "description": "Claude Code"},
            {"name": "codex", "available": False, "description": "Codex"},
        ]

    def profiles(self):
        return ["work", "ds4"]

    def workspaces(self):
        return [
            {"name": "api", "path": "/srv/api", "exists": True},
            {"name": "gone", "path": "/srv/gone", "exists": False},
        ]

    def roles(self):
        return [{"name": "worker", "aliases": ["hand"], "stance": "do the work"}]

    def resumable(self):
        return [{"name": "old", "status": "exited", "conversation_id": "u1"}]

    def meshes(self):
        return [{"name": "team"}] if self._meshes else []

    def members(self, mesh):
        return ["lead", "api"] if mesh == "team" else []

    def workflows(self, cwd):
        self.workflow_calls.append(cwd)
        return self._workflows.get(cwd, [])

    def git(self, cwd):
        if not self._repo:
            return {"repo": False, "branch": "", "branches": [], "worktrees": []}
        # A branch per directory, so a test can tell "the branch this checkout
        # is cut from" apart from any other.
        branch = {"/srv/api": "api-main"}.get(cwd, "master")
        return {
            "repo": True, "branch": branch,
            "branches": [branch, "topic", "old-thing"],
            "worktrees": ["review"],
        }


def form(**kw) -> wizard.Wizard:
    sources = kw.pop("sources", None) or FakeSources(**kw)
    wiz = wizard.Wizard(sources, cwd="/work/repo")
    wiz.color = False
    return wiz


def focus_on(wiz: wizard.Wizard, key: str) -> None:
    """Put the cursor on a field the way a person would: with the arrow keys."""
    for _ in range(len(wiz.fields)):
        wiz.handle("up")
    for _ in range(len(wiz.fields)):
        if wiz.current.key == key:
            return
        wiz.handle("down")
    raise AssertionError(f"never reached {key!r} (hidden or disabled?)")


def pick(wiz: wizard.Wizard, key: str, label: str) -> None:
    """Open ``key``'s picker and choose the option labelled ``label``."""
    focus_on(wiz, key)
    wiz.handle("enter")
    assert wiz.mode == wizard.PICK
    wiz.handle("home")
    for _ in range(len(wiz.current.options)):
        if wiz.current.options[wiz.pick].label.startswith(label):
            wiz.handle("enter")
            return
        wiz.handle("down")
    raise AssertionError(f"no option {label!r} in {key!r}")


# --------------------------------------------------------------------------- #
# keyboard
# --------------------------------------------------------------------------- #
def test_decode_keys_names_the_keys_a_form_needs():
    assert wizard.decode_keys("\x1b[A") == ["up"]
    assert wizard.decode_keys("\x1bOB") == ["down"]
    assert wizard.decode_keys("\x1b[3~") == ["delete"]
    assert wizard.decode_keys("\r") == ["enter"]
    assert wizard.decode_keys("\x13") == ["submit"]
    assert wizard.decode_keys("\x03") == ["cancel"]
    assert wizard.decode_keys("\x1b") == ["escape"]
    # text arrives a character at a time, mixed freely with keys
    assert wizard.decode_keys("ab\x1b[Dc") == ["a", "b", "left", "c"]


def test_decode_keys_swallows_sequences_it_does_not_know():
    """A mouse report or an unmapped key must not be typed into a field."""
    assert wizard.decode_keys("\x1b[<0;1;1M") == []
    assert wizard.decode_keys("\x1b[15~x") == ["x"]


def test_width_counts_wide_cells_twice():
    assert wizard.width("abc") == 3
    assert wizard.width("한글") == 4
    assert wizard.fit("abcdef", 5) == "ab..."
    assert wizard.fit("abc", 5) == "abc"
    assert wizard.width(wizard.pad("한글", 8)) == 8


# --------------------------------------------------------------------------- #
# what the form offers
# --------------------------------------------------------------------------- #
def test_every_closed_set_is_a_picker_not_a_text_box():
    wiz = form()
    for key in ("harness", "profile", "cwd", "worktree", "role", "resume",
                "mesh", "workflow", "restore", "attach"):
        assert isinstance(wiz.field(key), wizard.ChoiceField), key
    for key in ("name", "task", "args", "handle", "context", "worktree_name"):
        assert isinstance(wiz.field(key), wizard.TextField), key


def test_an_uninstalled_harness_is_shown_and_unpickable():
    """Hiding it would read as "claunch does not know about codex"."""
    wiz = form()
    codex = [o for o in wiz.field("harness").options if o.value == "codex"][0]
    assert codex.disabled and "not installed" in codex.detail
    focus_on(wiz, "harness")
    wiz.handle("right")
    assert wiz.value("harness") == "claude"


def test_a_missing_workspace_is_shown_and_unpickable():
    wiz = form()
    gone = [o for o in wiz.field("cwd").options if o.value == "/srv/gone"][0]
    assert gone.disabled and "missing" in gone.detail


def test_the_directory_starts_where_the_command_was_typed():
    """The CLI's default is the directory you are standing in; the wizard must
    not quietly move a launch into the workspace registry instead."""
    wiz = form()
    assert wiz.value("cwd") == wizard.os.path.abspath("/work/repo")
    assert wiz.field("cwd").options[0].label == "this directory"


def test_the_claude_harness_defaults_to_a_real_profile():
    wiz = form()
    assert wiz.value("profile") == "work"
    assert wiz.field("profile").options[0].value == ""  # (no profile), on purpose


# --------------------------------------------------------------------------- #
# fields that depend on other fields
# --------------------------------------------------------------------------- #
def test_role_and_resume_belong_to_claude_only():
    wiz = form()
    assert wiz.field("role").selectable
    wiz.field("harness").options.append(wizard.Option("pi", "pi"))
    pick(wiz, "harness", "pi")
    assert not wiz.field("role").selectable
    assert not wiz.field("resume").selectable
    assert not wiz.field("fork_session").selectable
    assert not wiz.field("borrow").selectable
    assert not wiz.field("null_token").selectable


def test_the_borrow_picker_offers_the_profiles():
    """--borrow names a profile, and profiles are a closed set the daemon
    publishes -- so it is a picker, never typed."""
    wiz = form()
    labels = [o.label for o in wiz.field("borrow").options]
    assert labels[0].startswith("(this profile's own token)")
    assert "work" in labels and "ds4" in labels
    assert wiz.value("borrow") == ""  # borrowing is an answer somebody gives


def test_null_takes_the_borrow_with_it():
    """`run` refuses --null --borrow outright; the form never offers the
    pair -- saying yes to null greys the borrow row and resets it."""
    wiz = form()
    pick(wiz, "borrow", "ds4")
    assert wiz.value("borrow") == "ds4"
    pick(wiz, "null_token", "yes")
    assert not wiz.field("borrow").selectable
    assert wiz.value("borrow") == ""
    pick(wiz, "null_token", "no")
    assert wiz.field("borrow").selectable


def test_fork_needs_a_conversation_to_fork():
    wiz = form()
    assert not wiz.field("fork_session").selectable
    pick(wiz, "resume", "old")
    assert wiz.field("fork_session").selectable
    pick(wiz, "fork_session", "yes")
    assert wiz.value("fork_session") is True
    # taking the conversation away takes the fork with it
    pick(wiz, "resume", "(new conversation)")
    assert not wiz.field("fork_session").selectable
    assert wiz.value("fork_session") is False


def test_no_worktree_question_outside_a_repository():
    wiz = form(repo=False)
    assert wiz.field("worktree").hidden
    assert wiz.field("worktree_name").hidden


def test_existing_worktrees_are_offered_alongside_a_new_one():
    wiz = form()
    labels = [o.label for o in wiz.field("worktree").options]
    assert labels[0].startswith("(none)")
    assert "new worktree" in labels
    assert "review" in labels  # already on disk: reuse is the common case


def test_naming_a_worktree_lands_on_the_name():
    wiz = form()
    pick(wiz, "worktree", "new worktree, named")
    assert wiz.current.key == "worktree_name"
    for ch in "feature/x":
        wiz.handle(ch)
    wiz.handle("enter")
    assert wiz.value("worktree_name") == "feature/x"
    assert wiz.problems() == []


def test_an_impossible_worktree_name_is_refused_before_anything_is_built():
    wiz = form()
    pick(wiz, "worktree", "new worktree, named")
    for ch in "../etc":
        wiz.handle(ch)
    wiz.handle("enter")
    assert wiz.handle("submit") is None
    assert "invalid worktree name" in wiz.error
    assert wiz.current.key == "worktree_name"


def test_the_mesh_reveals_the_handle_and_the_roster():
    wiz = form()
    assert wiz.field("handle").hidden and wiz.field("connect").hidden
    pick(wiz, "mesh", "team")
    assert not wiz.field("handle").hidden
    connect = wiz.field("connect")
    assert [o.value for o in connect.options] == ["lead", "api"]
    focus_on(wiz, "connect")
    wiz.handle("enter")          # opens the roster
    wiz.handle("space")          # lead
    wiz.handle("down")
    wiz.handle("enter")          # api, and done
    assert wiz.value("connect") == ["lead", "api"]


def test_workflows_follow_the_directory():
    sources = FakeSources(workflows={"/srv/api": ["ship-it"]})
    wiz = wizard.Wizard(sources, cwd="/work/repo")
    assert [o.value for o in wiz.field("workflow").options] == [""]
    pick(wiz, "cwd", "api")
    assert "ship-it" in [o.value for o in wiz.field("workflow").options]
    pick(wiz, "workflow", "ship-it")
    assert not wiz.field("context").hidden


def test_type_to_jump_in_a_long_picker():
    wiz = form()
    focus_on(wiz, "profile")
    wiz.handle("enter")
    wiz.handle("d")
    assert wiz.field("profile").options[wiz.pick].label == "ds4"
    wiz.handle("enter")
    assert wiz.value("profile") == "ds4"


# --------------------------------------------------------------------------- #
# the answers
# --------------------------------------------------------------------------- #
def test_apply_writes_new_sessions_own_spelling():
    wiz = form()
    focus_on(wiz, "name")
    for ch in "api":
        wiz.handle(ch)
    wiz.handle("enter")
    pick(wiz, "profile", "ds4")
    pick(wiz, "borrow", "work")
    pick(wiz, "worktree", "review")
    pick(wiz, "role", "worker")
    pick(wiz, "resume", "old")
    pick(wiz, "fork_session", "yes")
    pick(wiz, "mesh", "team")
    pick(wiz, "attach", "yes")
    focus_on(wiz, "args")
    for ch in "--verbose":
        wiz.handle(ch)
    wiz.handle("enter")

    args = argparse.Namespace()
    wiz.apply(args)
    assert args.name == "api"
    assert args.harness == "claude"
    assert args.profile == "ds4"
    assert args.borrow == "work"
    assert args.null_token is False
    assert args.worktree == "review"
    assert args.role == "worker"
    assert args.resume == "old"
    assert args.fork_session is True
    assert args.mesh == "team"
    assert args.attach is True
    assert args.args == ["--verbose"]


def test_a_null_launch_travels_as_null_token():
    wiz = form()
    pick(wiz, "null_token", "yes")
    args = argparse.Namespace()
    wiz.apply(args)
    assert args.null_token is True
    assert args.borrow is None


def test_a_new_conversation_leaves_resume_unset():
    """``None`` and ``""`` are different answers to the API: a missing key is
    a new conversation, an empty one opens claude's picker."""
    wiz = form()
    args = argparse.Namespace()
    wiz.apply(args)
    assert args.resume is None
    pick(wiz, "resume", "pick in the harness")
    wiz.apply(args)
    assert args.resume == ""


def test_answering_no_to_the_worktree_is_not_the_same_as_not_answering():
    """ASK would put the old y/N prompt on the screen right after a form that
    just asked -- so the wizard always leaves a decided value."""
    wiz = form()
    args = argparse.Namespace()
    wiz.apply(args)
    assert args.worktree is worktree.NEVER
    wiz2 = form(repo=False)
    wiz2.apply(args)
    assert args.worktree is worktree.NEVER


def test_flags_typed_before_the_wizard_prefill_it():
    defaults = argparse.Namespace(
        name="api", harness="claude", profile="ds4", cwd="/srv/api",
        borrow="work", null_token=False,
        role="worker", resume="old", fork_session=True, mesh="team",
        handle="apibot", task="ship it", args=["--", "--verbose"],
        connect=["lead"], attach=True, restore=True, workflow=None,
        context=None,
    )
    wiz = wizard.Wizard(FakeSources(), cwd="/work/repo", defaults=defaults)
    assert wiz.value("name") == "api"
    assert wiz.value("profile") == "ds4"
    assert wiz.value("borrow") == "work"
    assert wiz.value("cwd") == wizard.os.path.abspath("/srv/api")
    assert wiz.value("role") == "worker"
    assert wiz.value("resume") == "old"
    assert wiz.value("fork_session") is True
    assert wiz.value("mesh") == "team"
    assert wiz.value("handle") == "apibot"
    assert wiz.value("task") == "ship it"
    assert wiz.value("connect") == ["lead"]
    assert wiz.value("attach") is True
    assert wiz.field("args").text == "--verbose"


def test_claude_without_a_profile_is_refused_at_the_profile_field():
    wiz = form()
    pick(wiz, "profile", "(no profile)")
    assert wiz.handle("submit") is None      # nothing created
    assert "needs a profile" in wiz.error
    assert wiz.current.key == "profile"
    pick(wiz, "profile", "work")
    assert wiz.handle("submit") == "create"


# --------------------------------------------------------------------------- #
# what it paints
# --------------------------------------------------------------------------- #
def test_the_form_reads_like_the_web_one():
    wiz = form()
    screen = "\n".join(wiz.render(90, 40))
    for label in ("Name", "Harness", "Profile", "Directory", "Worktree",
                  "Role", "Resume", "Args", "Mesh", "Workflow",
                  "Opening task", "Attach", "Create session"):
        assert label in screen
    assert "START IT WORKING" in screen
    # hidden until they apply
    assert "Handle" not in screen and "Context" not in screen


def test_the_cursor_and_the_hint_say_where_you_are():
    wiz = form()
    lines = wiz.render(90, 40)
    assert any(line.startswith(" > Name") for line in lines)
    assert any("how every other command refers to it" in line for line in lines)


def test_a_short_terminal_says_there_is_more():
    wiz = form()
    screen = "\n".join(wiz.render(90, 14))
    assert len(wiz.render(90, 14)) == 14
    assert "more below" in screen


def test_the_picker_marks_the_answer_that_stands():
    wiz = form()
    focus_on(wiz, "profile")
    wiz.handle("enter")
    screen = "\n".join(wiz.render(90, 20))
    assert "claunch new-session  /  Profile" in screen
    assert "work *" in screen  # the one currently chosen


# --------------------------------------------------------------------------- #
# the loop, and the command
# --------------------------------------------------------------------------- #
def drive(monkeypatch, keys: bytes, args: argparse.Namespace, **kw):
    """Run the real loop with a scripted keyboard and no real terminal."""
    chunks = [keys, b""]
    monkeypatch.setattr(wizard, "available", lambda: True)
    monkeypatch.setattr(attach_mod, "_read_stdin", lambda: chunks.pop(0))
    monkeypatch.setattr(attach_mod, "_RawTerminal", _NoTerminal)
    monkeypatch.setattr(wizard, "_paint", lambda *a, **k: None)
    monkeypatch.setattr(wizard, "_write", lambda *a, **k: None)
    return wizard.run(args, sources=FakeSources(**kw), cwd="/work/repo")


class _NoTerminal:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_escape_creates_nothing(monkeypatch, capsys):
    args = argparse.Namespace()
    assert drive(monkeypatch, b"\x1b", args) is False
    assert "cancelled" in capsys.readouterr().err
    assert not hasattr(args, "harness")


def test_stdin_closing_under_the_form_is_a_cancel(monkeypatch, capsys):
    args = argparse.Namespace()
    assert drive(monkeypatch, b"", args) is False


def test_ctrl_s_creates_and_says_what_it_is_creating(monkeypatch, capsys):
    args = argparse.Namespace()
    assert drive(monkeypatch, b"\x13", args) is True
    assert args.harness == "claude"
    assert args.profile == "work"
    err = capsys.readouterr().err
    assert "creating:" in err and "profile work" in err


def test_the_wizard_is_refused_where_there_is_nobody_to_fill_it_in(monkeypatch):
    monkeypatch.setattr(wizard, "available", lambda: False)
    with pytest.raises(wizard.WizardUnavailable):
        wizard.run(argparse.Namespace(), sources=FakeSources())


def test_a_scripted_wizard_fails_before_it_starts_a_daemon(monkeypatch):
    """The refusal has to come first: auto-starting a daemon on the way to a
    form that cannot be shown leaves a process behind for nothing."""
    monkeypatch.setattr(wizard, "available", lambda: False)
    monkeypatch.setattr(
        cli_sessions.daemon_client, "ensure_running",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("started a daemon")),
    )
    with pytest.raises(wizard.WizardUnavailable):
        cli_sessions._run_wizard(argparse.Namespace(cwd=None))


def test_new_session_runs_the_wizard_before_it_builds_anything(monkeypatch):
    """The flag is a second way to *answer* new-session, not a second way to
    create a session: a cancelled form must not reach the daemon."""
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    monkeypatch.setattr(
        cli_sessions.daemon_client, "ensure_running",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("the daemon must not be asked to build anything")
        ),
    )
    monkeypatch.setattr(cli_sessions, "_run_wizard", lambda args: False)
    parser = argparse.ArgumentParser()
    from claude_launcher import cli

    args = cli.build_parser().parse_args(["new-session", "--wizard"])
    assert args.wizard is True
    assert cli_sessions._cmd_new_session(args) == 1


def test_an_agent_is_sent_to_spawn_before_the_form_opens(monkeypatch, capsys):
    monkeypatch.setenv("CLAUNCH_SESSION", "parent")
    monkeypatch.setattr(
        cli_sessions, "_run_wizard",
        lambda args: (_ for _ in ()).throw(AssertionError("no form for an agent")),
    )
    from claude_launcher import cli

    args = cli.build_parser().parse_args(["new-session", "--wizard"])
    assert cli_sessions._cmd_new_session(args) == 2
    assert "claunch spawn" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# spawn: a child, and only what a child may be asked
# --------------------------------------------------------------------------- #
class FakeSpawnSources(FakeSources):
    """A daemon with one parent session and a policy the test decides."""

    def __init__(self, *, report=None, sessions=None, **kw):
        super().__init__(**kw)
        self._report = report if report is not None else {
            "can_spawn": True, "blocked_by": [], "depth": 0, "max_depth": 3,
            "children_used": 1, "children_remaining": 3,
            # what the shipped policy allows: a workspace from the vouched
            # list, and a checkout of the repository the parent is already in
            "may_choose": ["workspace", "worktree"], "spawnable_harnesses": [],
            "workspaces": [{"name": "api", "path": "/srv/api", "exists": True}],
        }
        self._sessions = sessions if sessions is not None else [
            {"name": "lead", "status": "idle", "harness": "claude",
             "profile": "work", "cwd": "/work/repo"},
            {"name": "old", "status": "exited", "harness": "claude",
             "profile": "work", "cwd": "/work/other", "conversation_id": "u1"},
        ]
        self.report_calls = []

    def sessions(self):
        return self._sessions

    def spawn_report(self, parent):
        self.report_calls.append(parent)
        return self._report

    def mesh_of(self, session):
        return "team" if session == "lead" else ""

    def members(self, mesh):
        return ["lead", "api", "docs"] if mesh == "team" else []


def spawn_form(**kw) -> wizard.SpawnWizard:
    sources = kw.pop("sources", None) or FakeSpawnSources(**kw)
    wiz = wizard.SpawnWizard(sources, cwd="/work/repo")
    wiz.color = False
    return wiz


def test_the_spawn_form_asks_only_what_a_child_may_be_asked():
    """No profile and no directory: a child runs under its parent's login, in
    its parent's directory. A worktree OF that directory is the exception, and
    the only way two children of one parent stop editing each other's files."""
    wiz = spawn_form()
    keys = [f.key for f in wiz.fields]
    assert "parent" in keys
    assert "worktree" in keys
    for absent in ("profile", "cwd", "resume", "fork_session", "restore"):
        assert absent not in keys, absent


def test_the_parent_is_a_picker_of_the_sessions_that_exist():
    wiz = spawn_form()
    assert [o.value for o in wiz.field("parent").options] == ["lead", "old"]
    assert wiz.value("parent") == "lead"
    assert "idle" in wiz.field("parent").options[0].detail


def test_with_no_sessions_there_is_nothing_to_be_a_child_of():
    wiz = spawn_form(sessions=[])
    assert wiz.value("parent") is None
    assert wiz.handle("submit") is None
    assert "no parent to spawn from" in wiz.error


def test_the_parent_row_says_what_it_has_left_to_spend():
    wiz = spawn_form()
    assert "1 running, 3 left" in wiz.field("parent").hint
    assert "depth 0/3" in wiz.field("parent").hint


def test_a_parent_with_no_slots_is_refused_before_anything_is_arranged():
    """The whole reason the form reads the spawn report: a full parent says so
    on its own row, instead of the daemon refusing a filled-in form."""
    wiz = spawn_form(report={
        "can_spawn": False,
        "blocked_by": ["child limit reached (4/4)"],
        "depth": 1, "max_depth": 3, "children_used": 4, "children_remaining": 0,
        "may_choose": [], "spawnable_harnesses": [],
    })
    assert wiz.handle("submit") is None
    assert "child limit reached (4/4)" in wiz.error
    assert wiz.current.key == "parent"


def test_the_policy_decides_which_rows_are_open():
    wiz = spawn_form()
    # allow_harness is empty in the default report: the child runs what its
    # parent runs, and the row says so rather than disappearing.
    assert not wiz.field("harness").selectable
    assert "spawn.allow_harness" in wiz.field("harness").disabled_note
    # allow_workspace is on, so the registry it published is pickable
    assert wiz.field("workspace").selectable
    assert [o.value for o in wiz.field("workspace").options] == ["", "api"]


def test_an_unlocked_harness_becomes_pickable():
    wiz = spawn_form(report={
        "can_spawn": True, "blocked_by": [], "depth": 0, "max_depth": 3,
        "children_used": 0, "children_remaining": 4,
        "may_choose": [], "spawnable_harnesses": ["codex"],
    })
    harness = wiz.field("harness")
    assert harness.selectable
    assert [o.value for o in harness.options] == ["", "codex"]
    assert "the parent's" in harness.options[0].label
    # allow_workspace off means the report carries no workspace list at all
    assert not wiz.field("workspace").selectable


def test_the_form_is_rebuilt_when_the_parent_changes():
    wiz = spawn_form(sessions=[
        {"name": "lead", "status": "idle", "profile": "work", "cwd": "/work/repo"},
        {"name": "solo", "status": "idle", "profile": "work", "cwd": "/work/other"},
    ])
    assert "F:" not in wiz.field("workspace").options[0].label
    assert "/work/repo" in wiz.field("workspace").options[0].label
    pick(wiz, "parent", "solo")
    assert "/work/other" in wiz.field("workspace").options[0].label
    assert wiz.sources.report_calls == ["lead", "solo"]


def test_the_mesh_defaults_to_the_parents_own():
    wiz = spawn_form()
    mesh = wiz.field("mesh")
    assert mesh.options[0].label == "(the parent's: team)"
    assert mesh.value == ""            # "" = inherit, which is what spawn means
    assert mesh.options[1].value == wizard.SpawnWizard.NO_MESH


def test_a_parent_in_no_mesh_says_one_will_be_opened():
    wiz = spawn_form(sessions=[
        {"name": "lead", "status": "idle", "profile": "work", "cwd": "/work/repo"},
        {"name": "solo", "status": "idle", "profile": "work", "cwd": "/work/other"},
    ])
    pick(wiz, "parent", "solo")
    assert "opened for the pair" in wiz.field("mesh").options[0].label


def test_no_mesh_at_all_takes_the_handle_and_the_roster_with_it():
    wiz = spawn_form()
    assert not wiz.field("handle").hidden
    pick(wiz, "mesh", "- no mesh")
    assert wiz.field("handle").hidden
    assert wiz.field("connect").hidden
    args = argparse.Namespace()
    wiz.apply(args)
    assert args.mesh == "-"
    assert args.handle is None and args.connect == []


def test_the_roster_is_the_parents_mesh_minus_the_parent():
    """It can always reach its parent, so offering that as a connection would
    be offering something that is already true."""
    wiz = spawn_form()
    connect = wiz.field("connect")
    assert [o.value for o in connect.options] == ["api", "docs"]


def test_workflows_follow_the_directory_the_child_will_run_in():
    sources = FakeSpawnSources(workflows={"/srv/api": ["ship-it"]})
    wiz = wizard.SpawnWizard(sources, cwd="/work/repo")
    assert [o.value for o in wiz.field("workflow").options] == [""]
    pick(wiz, "workspace", "api")
    assert "ship-it" in [o.value for o in wiz.field("workflow").options]


def test_spawn_apply_writes_the_flags_spawn_reads():
    wiz = spawn_form()
    focus_on(wiz, "name")
    for ch in "helper":
        wiz.handle(ch)
    wiz.handle("enter")
    pick(wiz, "workspace", "api")
    pick(wiz, "role", "worker")
    focus_on(wiz, "handle")
    for ch in "hand":
        wiz.handle(ch)
    wiz.handle("enter")
    focus_on(wiz, "connect")
    wiz.handle("enter")
    wiz.handle("enter")          # the first member, and done
    focus_on(wiz, "task")
    for ch in "ship it":
        wiz.handle(ch)
    wiz.handle("enter")

    args = argparse.Namespace()
    wiz.apply(args)
    assert args.parent == "lead"
    assert args.name == "helper"
    assert args.harness is None          # inherited, and not ours to send
    assert args.workspace == "api"       # the NAME, which is what -w means
    assert args.role == "worker"
    assert args.mesh is None             # "" = the parent's, sent as nothing
    assert args.handle == "hand"
    assert args.connect == ["api"]
    assert args.task == "ship it"
    assert wiz.handle("submit") == "create"


def test_the_spawn_form_says_whose_child_it_is_making():
    wiz = spawn_form()
    assert wiz.summary().startswith("spawning: child of lead")
    assert "mesh team" in wiz.summary()


def test_the_spawn_form_reads_like_the_command():
    wiz = spawn_form()
    screen = "\n".join(wiz.render(90, 30))
    assert "claunch spawn" in screen
    for label in ("Parent", "Name", "Harness", "Workspace", "Mesh", "Role",
                  "Workflow", "Opening task", "Spawn child"):
        assert label in screen


def test_spawn_runs_the_wizard_before_it_asks_the_daemon_for_anything(monkeypatch):
    monkeypatch.setattr(
        cli_sessions.daemon_client, "ensure_running",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("the daemon must not be asked to spawn anything")
        ),
    )
    seen = {}

    def fake(args, *, spawn=False):
        seen["spawn"] = spawn
        return False

    monkeypatch.setattr(cli_sessions, "_run_wizard", fake)
    from claude_launcher import cli

    args = cli.build_parser().parse_args(["spawn", "--wizard"])
    assert args.wizard is True
    assert cli_sessions._cmd_spawn(args) == 1
    assert seen["spawn"] is True


def test_the_spawn_form_is_for_the_person_the_parent_picker_exists_for(monkeypatch):
    """An agent has its parent in $CLAUNCH_SESSION and needs no list; a form
    painted into its PTY would hang the child it was creating."""
    monkeypatch.setenv("CLAUNCH_SESSION", "lead")
    with pytest.raises(wizard.WizardUnavailable):
        cli_sessions._run_wizard(argparse.Namespace(), spawn=True)


# --------------------------------------------------------------------------- #
# reusing a checkout, from either form
# --------------------------------------------------------------------------- #
def test_only_a_reused_worktree_can_be_out_of_date():
    """A fresh one is cut from the repository as it stands, so asking whether
    to update it would be asking about nothing."""
    wiz = form()
    assert wiz.field("update").hidden
    pick(wiz, "worktree", "new worktree")
    assert wiz.field("update").hidden
    pick(wiz, "worktree", "review")
    assert not wiz.field("update").hidden


def test_saying_yes_to_the_update_lands_on_the_branch_to_catch_up_with():
    wiz = form()
    pick(wiz, "worktree", "review")
    pick(wiz, "update", "yes")
    assert wiz.current.key == "rebase_onto"
    assert not wiz.field("rebase_onto").hidden
    # the branch this checkout is cut from leads the list
    assert wiz.field("rebase_onto").options[0].value == "master"
    assert "cut from" in wiz.field("rebase_onto").options[0].detail
    assert [o.value for o in wiz.field("rebase_onto").options] == [
        "master", "topic", "old-thing"
    ]


def test_the_base_is_a_picker_not_a_fixed_branch():
    wiz = form()
    pick(wiz, "worktree", "review")
    pick(wiz, "update", "yes")
    pick(wiz, "rebase_onto", "topic")
    args = argparse.Namespace()
    wiz.apply(args)
    assert args.worktree == "review"
    assert args.rebase_onto == "topic"


def test_no_update_sends_no_base_at_all():
    wiz = form()
    pick(wiz, "worktree", "review")
    args = argparse.Namespace()
    wiz.apply(args)
    assert args.worktree == "review"
    assert args.rebase_onto == ""
    assert "rebased" not in wiz.summary()


def test_the_summary_names_the_branch_it_will_catch_up_with():
    wiz = form()
    pick(wiz, "worktree", "review")
    pick(wiz, "update", "yes")
    assert "worktree review" in wiz.summary()
    assert "rebased onto master" in wiz.summary()


# --------------------------------------------------------------------------- #
# a child's own checkout
# --------------------------------------------------------------------------- #
def test_a_child_can_be_given_a_checkout_of_its_own():
    wiz = spawn_form()
    wt = wiz.field("worktree")
    assert wt.selectable
    labels = [o.label for o in wt.options]
    assert labels[0].startswith("(none)")
    assert "new worktree" in labels
    assert "review" in labels  # already on disk beside the parent's


def test_the_childs_worktree_is_named_here_not_by_the_daemon():
    """`new-session` names an unnamed worktree after the Herdr pane; the
    daemon that cuts a child's has no pane, so the form answers instead."""
    wiz = spawn_form()
    focus_on(wiz, "name")
    for ch in "helper":
        wiz.handle(ch)
    wiz.handle("enter")
    auto = wiz.auto_worktree_name()
    assert auto.startswith("helper-")
    assert "auto-named: helper-" in [
        o.detail for o in wiz.field("worktree").options if o.value == ""
    ][0]
    pick(wiz, "worktree", "new worktree")
    args = argparse.Namespace()
    wiz.apply(args)
    assert args.worktree == auto          # a NAME, never a path
    assert "\\" not in args.worktree and "/" not in args.worktree


def test_without_a_name_the_childs_worktree_is_named_after_its_parent():
    wiz = spawn_form()
    assert wiz.auto_worktree_name().startswith("lead-")


def test_the_policy_can_grey_out_the_childs_worktree():
    wiz = spawn_form(report={
        "can_spawn": True, "blocked_by": [], "depth": 0, "max_depth": 3,
        "children_used": 0, "children_remaining": 4,
        "may_choose": [], "spawnable_harnesses": [],
    })
    wt = wiz.field("worktree")
    assert not wt.selectable
    assert "spawn.allow_worktree" in wt.disabled_note
    args = argparse.Namespace()
    wiz.apply(args)
    assert args.worktree is None


def test_a_childs_update_catches_up_with_its_parents_branch():
    """'rebase onto the parent's branch' is the same rule as new-session's
    'the branch you came from' -- the one the checkout is cut from."""
    wiz = spawn_form()
    pick(wiz, "worktree", "review")
    pick(wiz, "update", "yes")
    assert wiz.field("rebase_onto").options[0].value == "master"
    args = argparse.Namespace()
    wiz.apply(args)
    assert args.rebase_onto == "master"


def test_a_childs_worktree_follows_the_workspace_it_was_sent_to():
    """Cut from where the child will actually run, so the branch to catch up
    with is that repository's, not the parent's."""
    wiz = spawn_form()
    pick(wiz, "workspace", "api")
    pick(wiz, "worktree", "review")
    pick(wiz, "update", "yes")
    assert wiz.field("rebase_onto").options[0].value == "api-main"


def test_a_parent_outside_a_repository_is_offered_no_worktree():
    wiz = spawn_form(repo=False)
    assert wiz.field("worktree").hidden
    assert wiz.field("update").hidden
    assert wiz.field("rebase_onto").hidden


# --------------------------------------------------------------------------- #
# who the Parent picker offers, and in what order
# --------------------------------------------------------------------------- #
FLEET = [
    {"name": "s10", "status": "exited", "profile": "nc", "cwd": "F:/works/gds5"},
    {"name": "s11", "status": "idle", "profile": "nc", "cwd": "F:/works/gds5"},
    {"name": "s9", "status": "busy", "profile": "nc", "cwd": "F:/works/gds5"},
]


def test_a_session_that_cannot_spawn_does_not_lead_the_picker():
    """The daemon refuses a child of an exited session outright, so offering
    one as the default is offering a launch that is already lost."""
    wiz = spawn_form(sessions=FLEET)
    values = [o.value for o in wiz.field("parent").options]
    assert values == ["s9", "s11", "s10"]
    assert wiz.value("parent") == "s9"


def test_names_are_ordered_as_numbers_not_as_strings():
    """Past nine sessions a string sort puts the tenth between the first and
    the second, which reads as no order at all."""
    fleet = [
        {"name": f"s{n}", "status": "idle", "cwd": "/w"} for n in (9, 10, 11, 2)
    ]
    wiz = spawn_form(sessions=fleet)
    assert [o.value for o in wiz.field("parent").options] == \
        ["s2", "s9", "s10", "s11"]


def test_an_exited_session_is_shown_and_unpickable_with_the_way_out():
    wiz = spawn_form(sessions=FLEET)
    exited = [o for o in wiz.field("parent").options if o.value == "s10"][0]
    assert exited.disabled
    # Shown, not hidden: it is a respawn away from being a usable parent, and
    # a session missing from the list reads as gone.
    assert "respawn" in exited.detail


def test_the_picker_skips_past_a_session_that_cannot_spawn():
    wiz = spawn_form(sessions=FLEET)
    focus_on(wiz, "parent")
    wiz.handle("right")
    assert wiz.value("parent") == "s11"
    wiz.handle("right")          # s10 is next, and unpickable
    assert wiz.value("parent") == "s11"


def test_your_own_session_leads_and_says_so(monkeypatch):
    """`spawn` inside a session means *this* session, so the form opens on the
    answer the bare command would have given."""
    monkeypatch.setenv("CLAUNCH_SESSION", "s11")
    wiz = spawn_form(sessions=FLEET)
    assert [o.value for o in wiz.field("parent").options][0] == "s11"
    assert wiz.value("parent") == "s11"
    assert wiz.field("parent").options[0].label == "s11 (you)"


def test_your_own_session_does_not_lead_when_it_cannot_spawn(monkeypatch):
    """An exited caller is still refused, so the form must not open on it."""
    monkeypatch.setenv("CLAUNCH_SESSION", "s10")
    wiz = spawn_form(sessions=FLEET)
    assert wiz.value("parent") == "s9"
    assert wiz.field("parent").options[0].value == "s10"  # still first, greyed
    assert wiz.field("parent").options[0].disabled


def test_an_all_exited_fleet_offers_nothing_pickable():
    wiz = spawn_form(sessions=[
        {"name": "s1", "status": "exited", "cwd": "/w"},
        {"name": "s2", "status": "exited", "cwd": "/w"},
    ])
    assert all(o.disabled for o in wiz.field("parent").options)


def test_an_explicit_parent_still_wins_over_the_ordering():
    wiz = spawn_form(sessions=FLEET)
    assert wiz.field("parent").select("s11")
    assert wiz.value("parent") == "s11"


@pytest.mark.parametrize(
    "names, expected",
    [
        (["s10", "s9"], ["s9", "s10"]),
        (["b", "a"], ["a", "b"]),
        (["w2p10", "w2p9"], ["w2p9", "w2p10"]),
        (["x", "x1"], ["x", "x1"]),
    ],
)
def test_natural_sort_key(names, expected):
    assert sorted(names, key=wizard._natural) == expected
