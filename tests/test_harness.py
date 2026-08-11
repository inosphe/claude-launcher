"""Session definitions and harness command/env assembly."""

from __future__ import annotations

import sys

import pytest

from claude_launcher import profile, store
from claude_launcher.daemon import harness
from claude_launcher.daemon.harness import HarnessError, SessionDef


def _declare_harness(name: str, **extra) -> None:
    """Declare a harness in the config file, pointed at a real executable.

    The command has to exist: a declared harness whose program is not on PATH
    is refused up front (that is the state 'pi' ships in), so a placeholder
    like 'h' would fail for the wrong reason.
    """
    entry = {"command": sys.executable, **extra}
    store.update(lambda doc: doc.setdefault("harnesses", {}).update({name: entry}))


def test_sessiondef_roundtrip():
    sdef = SessionDef(
        name="work", harness="claude", profile="p", cwd="/tmp",
        args=("--resume",), env={"A": "1"}, restore=False, cols=80, rows=24,
        conversation_id="11111111-2222-3333-4444-555555555555",
        role="worker", resume="", fork_session=True,
    )
    assert SessionDef.from_dict(sdef.to_dict()) == sdef


def test_resume_field_keeps_the_picker_distinct_from_no_resume():
    """'' (open the picker) and None (a new conversation) are different
    answers; a falsiness test would collapse them into one."""
    assert SessionDef.from_dict({"name": "x", "resume": ""}).resume == ""
    assert SessionDef.from_dict({"name": "x"}).resume is None
    assert SessionDef.from_dict({"name": "x", "resume": None}).resume is None
    assert SessionDef.from_dict({"name": "x", "resume": True}).resume == ""


def test_claude_harness_requires_profile(home):
    with pytest.raises(HarnessError, match="profile"):
        harness.normalize(SessionDef(name="x"))


def test_unknown_harness_rejected(home):
    with pytest.raises(HarnessError, match="unknown harness"):
        harness.normalize(SessionDef(name="x", harness="nope"))


def test_claude_command_uses_profile_env(home, monkeypatch, tmp_path):
    p = profile.create("work")
    from claude_launcher import settings

    settings.set_env(p, {"MY_FLAG": "on"})
    sdef = harness.normalize(
        SessionDef(name="x", profile="work", cwd=str(tmp_path))
    )
    argv, env, cwd = harness.build_command(sdef)
    assert argv[0] == "claude"
    assert env["CLAUDE_CONFIG_DIR"] == str(p.config_dir)
    assert env["MY_FLAG"] == "on"
    assert cwd == str(tmp_path)


def test_session_identity_env_exported(home, tmp_path):
    """CLAUNCH_SESSION (tmux's $TMUX equivalent) marks every session; cflow
    keys run state by it so sessions get 1:1 workflow runs."""
    profile.create("work")
    sdef = harness.normalize(SessionDef(name="sx", profile="work", cwd=str(tmp_path)))
    _, env, _ = harness.build_command(sdef)
    assert env["CLAUNCH_SESSION"] == "sx"

    _declare_harness("h")
    sdef = harness.normalize(SessionDef(name="hx", harness="h", cwd=str(tmp_path)))
    _, env, _ = harness.build_command(sdef)
    assert env["CLAUNCH_SESSION"] == "hx"


def test_claude_fresh_start_pins_conversation_id(home, tmp_path):
    import uuid

    profile.create("work")
    sdef = harness.normalize(SessionDef(name="x", profile="work", cwd=str(tmp_path)))
    assert sdef.conversation_id
    uuid.UUID(sdef.conversation_id)  # a valid UUID for --session-id
    argv, _, _ = harness.build_command(sdef)
    assert argv[argv.index("--session-id") + 1] == sdef.conversation_id


def test_claude_restore_resumes_own_conversation(home, tmp_path):
    """A restore must reopen the session's own conversation — never grab the
    cwd's most recent one (--continue), which can hijack another session."""
    profile.create("work")
    sdef = harness.normalize(SessionDef(name="x", profile="work", cwd=str(tmp_path)))
    argv, _, _ = harness.build_command(sdef, restoring=True)
    assert "--continue" not in argv
    assert argv[argv.index("--resume") + 1] == sdef.conversation_id
    assert "--session-id" not in argv


def test_claude_restore_of_legacy_def_falls_back_to_continue(home, tmp_path):
    profile.create("work")
    legacy = {"name": "x", "profile": "work", "cwd": str(tmp_path)}  # no id recorded
    sdef = harness.normalize(SessionDef.from_dict(legacy), restoring=True)
    assert sdef.conversation_id is None  # never invent an id while restoring
    argv, _, _ = harness.build_command(sdef, restoring=True)
    assert "--continue" in argv
    assert "--resume" not in argv


def test_claude_restore_respects_explicit_resume(home, tmp_path):
    profile.create("work")
    sdef = harness.normalize(
        SessionDef(name="x", profile="work", cwd=str(tmp_path), args=("--resume", "abc"))
    )
    assert sdef.conversation_id is None  # caller's args steer the conversation
    argv, _, _ = harness.build_command(sdef, restoring=True)
    assert "--continue" not in argv
    assert argv.count("--resume") == 1
    assert argv[argv.index("--resume") + 1] == "abc"


def test_missing_working_directory_is_refused_up_front(home, tmp_path):
    """At spawn time a missing cwd surfaces as "could not spawn 'claude'",
    which reads as a broken install rather than a bad path."""
    profile.create("work")
    with pytest.raises(HarnessError, match="working directory does not exist"):
        harness.normalize(
            SessionDef(name="x", profile="work", cwd=str(tmp_path / "gone"))
        )


def test_role_injects_its_stance_into_the_system_prompt(home, tmp_path):
    """A role is a system-prompt injection, not a label: the session is told
    who it is before its first prompt."""
    profile.create("work")
    sdef = harness.normalize(
        SessionDef(name="x", profile="work", cwd=str(tmp_path), role="reviewer")
    )
    argv, _, _ = harness.build_command(sdef)
    prompt = argv[argv.index("--append-system-prompt") + 1]
    assert "reviewer" in prompt
    assert "ADVERSARY" in prompt  # the packaged reviewer stance, verbatim


def test_role_accepts_an_alias_and_stores_the_canonical_name(home, tmp_path):
    profile.create("work")
    sdef = harness.normalize(
        SessionDef(name="x", profile="work", cwd=str(tmp_path), role="MOD")
    )
    assert sdef.role == "leader"


def test_unknown_role_rejected(home, tmp_path):
    """A typo must not hand the session a blank stance nobody notices."""
    profile.create("work")
    with pytest.raises(HarnessError, match="unknown role"):
        harness.normalize(
            SessionDef(name="x", profile="work", cwd=str(tmp_path), role="architekt")
        )


def test_role_survives_a_restore(home, tmp_path):
    """An appended system prompt lives in the process, not the transcript, so
    a resumed session has to be told its role again."""
    profile.create("work")
    sdef = harness.normalize(
        SessionDef(name="x", profile="work", cwd=str(tmp_path), role="worker")
    )
    argv, _, _ = harness.build_command(sdef, restoring=True)
    assert "--append-system-prompt" in argv


def test_resume_opens_the_named_conversation_and_pins_it(home, tmp_path):
    """Resuming without a fork continues that conversation, so it becomes
    this session's own — and a later restore reopens it."""
    profile.create("work")
    other = "11111111-2222-3333-4444-555555555555"
    sdef = harness.normalize(
        SessionDef(name="x", profile="work", cwd=str(tmp_path), resume=other)
    )
    assert sdef.conversation_id == other
    argv, _, _ = harness.build_command(sdef)
    assert argv[argv.index("--resume") + 1] == other
    assert "--session-id" not in argv  # would collide with --resume
    assert "--fork-session" not in argv

    argv, _, _ = harness.build_command(sdef, restoring=True)
    assert argv[argv.index("--resume") + 1] == other


def test_fork_session_lands_the_copy_on_a_restorable_id(home, tmp_path):
    """claude mints a new conversation for a fork; --session-id decides where,
    so the fork stays restorable and the original stays untouched."""
    import uuid

    profile.create("work")
    other = "11111111-2222-3333-4444-555555555555"
    sdef = harness.normalize(
        SessionDef(
            name="x", profile="work", cwd=str(tmp_path),
            resume=other, fork_session=True,
        )
    )
    assert sdef.conversation_id and sdef.conversation_id != other
    uuid.UUID(sdef.conversation_id)
    argv, _, _ = harness.build_command(sdef)
    assert argv[argv.index("--resume") + 1] == other
    assert "--fork-session" in argv
    assert argv[argv.index("--session-id") + 1] == sdef.conversation_id

    # From the second spawn on it is an ordinary session of its own.
    argv, _, _ = harness.build_command(sdef, restoring=True)
    assert argv[argv.index("--resume") + 1] == sdef.conversation_id
    assert "--fork-session" not in argv


def test_bare_resume_opens_the_picker_and_pins_nothing(home, tmp_path):
    """Nobody knows yet which conversation the user will choose, so there is
    no id to pin — a restore falls back to --continue, as it always has."""
    profile.create("work")
    sdef = harness.normalize(
        SessionDef(name="x", profile="work", cwd=str(tmp_path), resume="")
    )
    assert sdef.conversation_id is None
    argv, _, _ = harness.build_command(sdef)
    assert argv[-1] == "--resume"  # bare: claude prompts for the conversation
    assert "--session-id" not in argv
    argv, _, _ = harness.build_command(sdef, restoring=True)
    assert "--continue" in argv


def test_fork_without_resume_rejected(home, tmp_path):
    profile.create("work")
    with pytest.raises(HarnessError, match="fork-session"):
        harness.normalize(
            SessionDef(
                name="x", profile="work", cwd=str(tmp_path), fork_session=True
            )
        )


def test_resume_alongside_conversation_steering_args_rejected(home, tmp_path):
    """Two sources for one decision: refuse rather than pick a winner."""
    profile.create("work")
    with pytest.raises(HarnessError, match="already steer"):
        harness.normalize(
            SessionDef(
                name="x", profile="work", cwd=str(tmp_path),
                args=("--continue",), resume="abc",
            )
        )


def test_role_and_resume_rejected_on_a_non_claude_harness(home, tmp_path):
    """Both are spelled in claude's own flags; accepting and dropping them
    would only be discovered later, by the session's behaviour."""
    _declare_harness("h")
    with pytest.raises(HarnessError, match="claude harness"):
        harness.normalize(
            SessionDef(name="x", harness="h", cwd=str(tmp_path), role="worker")
        )
    with pytest.raises(HarnessError, match="claude harness"):
        harness.normalize(
            SessionDef(name="x", harness="h", cwd=str(tmp_path), resume="")
        )


def test_generic_harness_from_config(home, tmp_path):
    _declare_harness("codex", args=["--yolo"], env={"K": "V"})
    sdef = harness.normalize(
        SessionDef(name="x", harness="codex", cwd=str(tmp_path), args=("extra",))
    )
    argv, env, _ = harness.build_command(sdef)
    assert argv[0] == sys.executable
    assert argv[1] == "--yolo"
    assert argv[-1] == "extra"
    assert env["K"] == "V"


def test_session_env_overrides_harness_env(home, tmp_path):
    _declare_harness("h", env={"K": "harness"})
    sdef = harness.normalize(
        SessionDef(name="x", harness="h", cwd=str(tmp_path), env={"K": "session"})
    )
    _, env, _ = harness.build_command(sdef)
    assert env["K"] == "session"


@pytest.mark.skipif(sys.platform == "win32", reason="TERM defaulting is Unix-only")
def test_unix_gets_term_default(home, tmp_path):
    profile.create("work")
    sdef = harness.normalize(SessionDef(name="x", profile="work", cwd=str(tmp_path)))
    _, env, _ = harness.build_command(sdef)
    assert "TERM" in env


def test_the_opening_message_rides_in_as_the_positional_prompt(home, tmp_path):
    """Where a new session's assignment actually goes.

    Typed into the terminal it can be lost: a just-started Claude Code reads
    idle for a few seconds before its input is live, and a paste plus its
    separately-written Enter written into that gap come back out of one read
    with the Enter folded into the text. On the command line there is no such
    window -- the message is the process's first turn.
    """
    profile.create("work")
    sdef = harness.normalize(
        SessionDef(name="x", profile="work", cwd=str(tmp_path), args=["--verbose"])
    )
    block = "---\n# claunch mesh: join briefing\n---\n\ntake the API"
    argv, _, _ = harness.build_command(sdef, opening=block)
    assert argv[-1] == block
    # behind the end-of-options marker: an opening block routinely starts with
    # a fence of dashes, which claude's option parser would refuse to start on
    assert argv[-2] == "--"
    assert argv[-3] == "--verbose"  # after the flags, as a prompt must be


def test_a_restore_does_not_repeat_the_opening_message(home, tmp_path):
    """It is true once. The conversation already contains it, and claude comes
    back into that same conversation -- sending it again would be the session
    receiving its opening instruction twice."""
    profile.create("work")
    sdef = harness.normalize(SessionDef(name="x", profile="work", cwd=str(tmp_path)))
    argv, _, _ = harness.build_command(sdef, restoring=True, opening="take the API")
    assert "take the API" not in argv


def test_a_harness_with_no_prompt_argument_is_not_given_one(home, tmp_path):
    """Only claude documents 'claude [options] [prompt]'. Anything else would
    be handed a stray argument, so those are typed into instead."""
    _declare_harness("h")
    sdef = harness.normalize(SessionDef(name="x", harness="h", cwd=str(tmp_path)))
    argv, _, _ = harness.build_command(sdef, opening="take the API")
    assert "take the API" not in argv
    assert harness.takes_opening_argv("h") is False
    assert harness.takes_opening_argv(harness.CLAUDE_HARNESS) is True
