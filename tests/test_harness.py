"""Session definitions and harness command/env assembly."""

from __future__ import annotations

import sys

import pytest

from claude_launcher import profile, store
from claude_launcher.daemon import harness
from claude_launcher.daemon.harness import HarnessError, SessionDef


def test_sessiondef_roundtrip():
    sdef = SessionDef(
        name="work", harness="claude", profile="p", cwd="/tmp",
        args=("--resume",), env={"A": "1"}, restore=False, cols=80, rows=24,
        conversation_id="11111111-2222-3333-4444-555555555555",
    )
    assert SessionDef.from_dict(sdef.to_dict()) == sdef


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

    store.update(lambda doc: doc.update({"harnesses": {"h": {"command": "h"}}}))
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


def test_generic_harness_from_config(home, tmp_path):
    store.update(
        lambda doc: doc.update(
            {"harnesses": {"codex": {"command": "codex", "args": ["--yolo"], "env": {"K": "V"}}}}
        )
    )
    sdef = harness.normalize(
        SessionDef(name="x", harness="codex", cwd=str(tmp_path), args=("extra",))
    )
    argv, env, _ = harness.build_command(sdef)
    assert argv[0] == "codex"
    assert argv[1] == "--yolo"
    assert argv[-1] == "extra"
    assert env["K"] == "V"


def test_session_env_overrides_harness_env(home, tmp_path):
    store.update(
        lambda doc: doc.update({"harnesses": {"h": {"command": "h", "env": {"K": "harness"}}}})
    )
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
