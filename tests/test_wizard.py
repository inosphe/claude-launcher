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

    def worktrees(self, cwd):
        return ["review"] if self._repo else []

    def is_repo(self, cwd):
        return self._repo


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
    assert args.worktree == "review"
    assert args.role == "worker"
    assert args.resume == "old"
    assert args.fork_session is True
    assert args.mesh == "team"
    assert args.attach is True
    assert args.args == ["--verbose"]


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
        role="worker", resume="old", fork_session=True, mesh="team",
        handle="apibot", task="ship it", args=["--", "--verbose"],
        connect=["lead"], attach=True, restore=True, workflow=None,
        context=None,
    )
    wiz = wizard.Wizard(FakeSources(), cwd="/work/repo", defaults=defaults)
    assert wiz.value("name") == "api"
    assert wiz.value("profile") == "ds4"
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
