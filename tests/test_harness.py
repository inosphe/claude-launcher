"""Session definitions and harness command/env assembly."""

from __future__ import annotations

import json
import re
import sys

import pytest
from dataclasses import replace

from claude_launcher import credentials, lineage, profile, store, transcripts
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


def _profile_for(name: str, harness_name: str):
    p = profile.create(name)
    lineage.set_harness(p, harness_name)
    return p


def _write_transcript(sdef, profile_name: str = "work") -> None:
    """Put a conversation on disk where claude would have written it.

    A restore only resumes what exists, so any test about ``--resume`` has to
    say that the conversation is there — otherwise it is testing the *other*
    branch (a session that died before its first turn landed).
    """
    d = transcripts.project_dir(
        profile.require(profile_name).config_dir, sdef.cwd
    )
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{sdef.conversation_id}.jsonl").write_text("{}", encoding="utf-8")


def test_sessiondef_roundtrip():
    sdef = SessionDef(
        name="work", harness="claude", profile="p", cwd="/tmp",
        args=("--resume",), model="opus", env={"A": "1"}, restore=False,
        cols=80, rows=24,
        conversation_id="11111111-2222-3333-4444-555555555555",
        role="worker", resume="", fork_session=True, borrow="lender",
    )
    assert SessionDef.from_dict(sdef.to_dict()) == sdef
    nulled = SessionDef(name="bare", profile="p", null_token=True)
    assert SessionDef.from_dict(nulled.to_dict()) == nulled


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
    p = profile.create("work")
    store.set_profile_field(p.name, "harness", "nope")
    with pytest.raises(HarnessError, match="unknown harness"):
        harness.normalize(SessionDef(name="x", profile="work"))


def test_qualified_profile_pins_harness_across_restore(home, tmp_path):
    _declare_harness("other", home_env="OTHER_HOME")
    base = profile.create("work")
    lineage.set_harness(base, "claude")

    sdef = harness.normalize(
        SessionDef(name="x", profile="work:other", cwd=str(tmp_path))
    )
    assert sdef.profile == "work:other"
    assert sdef.harness == "other"

    # The explicit selector, not a stale harness field or the mutable profile
    # default, remains the source of truth when a saved definition returns.
    lineage.set_harness(base, "pi")
    restored = harness.normalize(sdef, restoring=True)
    assert restored.profile == "work:other"
    assert restored.harness == "other"
    argv, env, _ = harness.build_command(restored, restoring=True)
    assert argv[0] == sys.executable
    assert env["OTHER_HOME"] == str(base.config_dir / "other")


def test_bare_profile_pins_its_validated_default_across_restore(home, tmp_path):
    _declare_harness("other", home_env="OTHER_HOME")
    base = profile.create("work")  # historical/default Claude

    sdef = harness.normalize(
        SessionDef(name="x", profile="work", cwd=str(tmp_path))
    )
    assert sdef.profile == "work:claude"
    assert sdef.harness == "claude"

    lineage.set_harness(base, "other")
    restored = harness.normalize(sdef, restoring=True)
    assert restored.profile == "work:claude"
    assert restored.harness == "claude"


def test_claude_command_uses_profile_env(home, monkeypatch, tmp_path):
    p = profile.create("work")
    from claude_launcher import settings

    settings.set_env(p, {"MY_FLAG": "on"})
    sdef = harness.normalize(
        SessionDef(name="x", profile="work", cwd=str(tmp_path))
    )
    assert sdef.profile == "work:claude"
    argv, env, cwd = harness.build_command(sdef)
    assert argv[0] == "claude"
    assert env["CLAUDE_CONFIG_DIR"] == str(p.config_dir)
    assert env["MY_FLAG"] == "on"
    assert cwd == str(tmp_path)


def test_selected_claude_model_is_an_argv_alias_and_profile_env_still_maps_it(
    home, tmp_path
):
    p = profile.create("work")
    from claude_launcher import settings

    settings.set_env(p, {"ANTHROPIC_DEFAULT_OPUS_MODEL": "vendor/opus-v2"})
    sdef = harness.normalize(
        SessionDef(
            name="x", profile="work", cwd=str(tmp_path), model="opus"
        )
    )

    argv, env, _ = harness.build_command(sdef)
    assert "--model=opus" in argv
    assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "vendor/opus-v2"


def test_declared_non_claude_model_is_inserted_before_free_args(home, tmp_path):
    _declare_harness("other", models=["luna", "terra", "sol"])
    _profile_for("work", "other")
    sdef = harness.normalize(
        SessionDef(
            name="x", profile="work", cwd=str(tmp_path),
            model="terra", args=("--verbose",),
        )
    )

    argv, _, _ = harness.build_command(sdef)
    assert argv[-2:] == ["--model=terra", "--verbose"]


def test_model_must_be_declared_and_not_repeated_in_free_args(home, tmp_path):
    profile.create("work")
    with pytest.raises(HarnessError, match="unknown model"):
        harness.normalize(
            SessionDef(
                name="x", profile="work", cwd=str(tmp_path), model="unknown"
            )
        )
    with pytest.raises(HarnessError, match="already select a model"):
        harness.normalize(
            SessionDef(
                name="x", profile="work", cwd=str(tmp_path), model="opus",
                args=("--model=sonnet",),
            )
        )


def test_session_env_cannot_restore_api_key_beside_claude_auth_token(
    home, tmp_path
):
    p = profile.create("work")
    from claude_launcher import settings

    settings.set_env(p, {"ANTHROPIC_AUTH_TOKEN": "gateway-token"})
    sdef = harness.normalize(
        SessionDef(
            name="x",
            profile="work",
            cwd=str(tmp_path),
            env={"ANTHROPIC_API_KEY": "must-not-win"},
        )
    )

    _, env, _ = harness.build_command(sdef)

    assert env["ANTHROPIC_AUTH_TOKEN"] == "gateway-token"
    assert env["ANTHROPIC_API_KEY"] == ""


def test_session_identity_env_exported(home, tmp_path):
    """CLAUNCH_SESSION (tmux's $TMUX equivalent) marks every session; cflow
    keys run state by it so sessions get 1:1 workflow runs."""
    profile.create("work")
    sdef = harness.normalize(SessionDef(name="sx", profile="work", cwd=str(tmp_path)))
    _, env, _ = harness.build_command(sdef)
    assert env["CLAUNCH_SESSION"] == "sx"

    _declare_harness("h")
    _profile_for("custom", "h")
    sdef = harness.normalize(
        SessionDef(name="hx", profile="custom", cwd=str(tmp_path))
    )
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
    _write_transcript(sdef)
    argv, _, _ = harness.build_command(sdef, restoring=True)
    assert "--continue" not in argv
    assert argv[argv.index("--resume") + 1] == sdef.conversation_id
    assert "--session-id" not in argv


def test_claude_restore_without_a_transcript_starts_the_pinned_id_fresh(
    home, tmp_path
):
    """A session born just before a restart has no conversation on disk yet.

    ``--resume`` of an id claude never wrote is fatal ("No conversation found
    with session ID"), and the session comes back as ``exited(1)`` — alive in
    the record, dead in fact, with whatever it was spawned to do lost. The
    restore takes the pinned id as a *new* conversation instead, which is
    exactly what the first spawn would have done.
    """
    profile.create("work")
    sdef = harness.normalize(SessionDef(name="x", profile="work", cwd=str(tmp_path)))
    argv, _, _ = harness.build_command(sdef, restoring=True)
    assert "--resume" not in argv
    assert "--continue" not in argv  # would hijack another session's history
    assert argv[argv.index("--session-id") + 1] == sdef.conversation_id


def test_claude_restore_resumes_a_transcript_filed_under_another_slug(
    home, tmp_path
):
    """The check is generous on purpose: a conversation found anywhere in the
    config dir counts as resumable. Guessing 'not there' about one that is
    would start a fresh conversation over a live scrollback — so a slug we
    spell differently than claude does costs a failed restore (today's
    behaviour), never a lost history."""
    profile.create("work")
    sdef = harness.normalize(SessionDef(name="x", profile="work", cwd=str(tmp_path)))
    stray = profile.require("work").config_dir / "projects" / "somewhere-else"
    stray.mkdir(parents=True)
    (stray / f"{sdef.conversation_id}.jsonl").write_text("{}", encoding="utf-8")
    argv, _, _ = harness.build_command(sdef, restoring=True)
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


def test_legacy_role_does_not_inject_a_system_prompt(home, tmp_path):
    """Role stance now arrives through mesh opening and session reminder."""
    profile.create("work")
    sdef = harness.normalize(
        SessionDef(name="x", profile="work", cwd=str(tmp_path), role="reviewer")
    )
    argv, _, _ = harness.build_command(sdef)
    assert "--append-system-prompt" not in argv


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


def test_legacy_role_record_survives_without_restore_time_injection(home, tmp_path):
    profile.create("work")
    sdef = harness.normalize(
        SessionDef(name="x", profile="work", cwd=str(tmp_path), role="worker")
    )
    argv, _, _ = harness.build_command(sdef, restoring=True)
    assert sdef.role == "worker"
    assert "--append-system-prompt" not in argv


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

    _write_transcript(sdef)
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
    _write_transcript(sdef)
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


def test_legacy_role_is_harness_neutral_but_resume_remains_claude_only(
    home, tmp_path
):
    _declare_harness("h")
    _profile_for("custom", "h")
    sdef = harness.normalize(
        SessionDef(name="x", profile="custom", cwd=str(tmp_path), role="worker")
    )
    assert sdef.role == "worker"
    with pytest.raises(HarnessError, match="claude harness"):
        harness.normalize(
            SessionDef(name="x", profile="custom", cwd=str(tmp_path), resume="")
        )


def test_borrow_and_null_rejected_on_a_non_claude_harness(home, tmp_path):
    """A harness with no token route cannot borrow; --null remains Claude-only."""
    _declare_harness("h")
    _profile_for("custom", "h")
    with pytest.raises(HarnessError, match="own storage"):
        harness.normalize(
            SessionDef(name="x", profile="custom", cwd=str(tmp_path), borrow="lender")
        )
    with pytest.raises(HarnessError, match="claude harness"):
        harness.normalize(
            SessionDef(name="x", profile="custom", cwd=str(tmp_path), null_token=True)
        )


def test_api_key_session_borrows_lender_token_not_lender_harness(home, tmp_path):
    _declare_harness(
        "keyed", auth="api-key", token_env="KEYED_API_KEY", home_env="KEYED_HOME"
    )
    runtime = _profile_for("work", "keyed")
    lender = _profile_for("lender", "claude")
    credentials.save_token(runtime, "own-secret")
    credentials.save_token(lender, "lender-secret")

    sdef = harness.normalize(
        SessionDef(name="x", profile="work", cwd=str(tmp_path), borrow="lender")
    )
    _, env, _ = harness.build_command(sdef)

    assert sdef.harness == "keyed"
    assert sdef.borrow == "lender"
    assert env["KEYED_API_KEY"] == "lender-secret"
    assert env["KEYED_HOME"] == str(runtime.config_dir / "keyed")


def test_daemon_pi_adapter_projects_provider_model_and_token(home, tmp_path):
    _declare_harness(
        "keyed",
        auth="api-key",
        token_env="KEYED_API_KEY",
        home_env="KEYED_HOME",
        provider_adapter="pi",
    )
    runtime = _profile_for("work", "keyed")
    credentials.save_token(runtime, "provider-secret")
    store.update(
        lambda doc: doc.setdefault("providers", {}).update(
            {
                "omlx": {
                    "env": {
                        "ANTHROPIC_BASE_URL": "https://omlx.example/",
                        "ANTHROPIC_MODEL": "solar-main",
                    }
                }
            }
        )
    )
    store.set_profile_field(runtime.name, "provider", "omlx")
    sdef = harness.normalize(
        SessionDef(name="x", profile="work:keyed", cwd=str(tmp_path))
    )

    argv, env, _ = harness.build_command(sdef)

    assert argv[argv.index("--provider") + 1] == "claunch-profile"
    assert argv[argv.index("--model") + 1] == "solar-main"
    assert "--extension" in argv
    assert env["KEYED_API_KEY"] == "provider-secret"
    assert env["CLAUNCH_PI_TOKEN_ENV"] == "KEYED_API_KEY"


def test_borrow_selector_is_rejected_even_when_its_harness_matches(home, tmp_path):
    _declare_harness("keyed", auth="api-key", token_env="KEYED_API_KEY")
    _profile_for("work", "keyed")
    _profile_for("lender", "keyed")

    with pytest.raises(HarnessError, match="base profile"):
        harness.normalize(
            SessionDef(
                name="x", profile="work", cwd=str(tmp_path),
                borrow="lender:keyed",
            )
        )


def test_null_cannot_be_combined_with_borrow(home, tmp_path):
    """The same refusal `run --null --borrow` gives: the two flags answer
    "whose token" with opposite answers."""
    profile.create("work")
    with pytest.raises(HarnessError, match="cannot be combined"):
        harness.normalize(
            SessionDef(
                name="x", profile="work", cwd=str(tmp_path),
                borrow="lender", null_token=True,
            )
        )


def test_a_borrowing_session_launches_with_the_lenders_token(home, tmp_path):
    p = profile.create("work")
    credentials.save_token(p, "sk-ant-oat01-own")
    lender = profile.create("lender")
    credentials.save_token(lender, "sk-ant-oat01-lender")
    sdef = harness.normalize(
        SessionDef(name="x", profile="work", cwd=str(tmp_path), borrow="lender")
    )
    _, env, _ = harness.build_command(sdef)
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-lender"
    # config stays the session's own: borrow swaps auth, not identity
    assert env["CLAUDE_CONFIG_DIR"] == str(p.config_dir)


def test_a_null_session_launches_with_no_token_at_all(home, tmp_path, monkeypatch):
    p = profile.create("work")
    credentials.save_token(p, "sk-ant-oat01-own")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "stale-shell-token")
    sdef = harness.normalize(
        SessionDef(name="x", profile="work", cwd=str(tmp_path), null_token=True)
    )
    _, env, _ = harness.build_command(sdef)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env


def test_generic_harness_from_config(home, tmp_path):
    _declare_harness("codex", args=["--yolo"], env={"K": "V"})
    _profile_for("custom", "codex")
    sdef = harness.normalize(
        SessionDef(name="x", profile="custom", cwd=str(tmp_path), args=("extra",))
    )
    argv, env, _ = harness.build_command(sdef)
    assert argv[0] == sys.executable
    assert argv[1] == "--yolo"
    assert argv[-1] == "extra"
    assert env["K"] == "V"


def test_declared_runtime_mode_replaces_conflicting_harness_default(
    home, tmp_path
):
    _declare_harness(
        "codex",
        args=["--dangerously-bypass-approvals-and-sandbox"],
        full_access_args=["--sandbox", "danger-full-access"],
        full_access_off_args=["--sandbox", "workspace-write"],
        mode_conflict_args=["--dangerously-bypass-approvals-and-sandbox"],
    )
    _profile_for("custom", "codex")
    for mode in ("danger-full-access", "workspace-write"):
        sdef = harness.normalize(SessionDef(
            name="x", profile="custom", cwd=str(tmp_path),
            args=("--sandbox", mode),
        ))
        argv, _, _ = harness.build_command(sdef)
        assert "--dangerously-bypass-approvals-and-sandbox" not in argv
        assert argv[-2:] == ["--sandbox", mode]


def test_an_explicit_yolo_mode_replaces_the_same_harness_default_once(
    home, tmp_path
):
    flag = "--dangerously-bypass-approvals-and-sandbox"
    _declare_harness(
        "codex",
        args=[flag],
        mode_conflict_args=[flag],
    )
    _profile_for("custom", "codex")
    sdef = harness.normalize(SessionDef(
        name="x", profile="custom", cwd=str(tmp_path), args=(flag,),
    ))
    argv, _, _ = harness.build_command(sdef)
    assert argv.count(flag) == 1


def test_codex_restore_resumes_its_pinned_conversation_id(
    home, tmp_path
):
    _profile_for("work", "codex")
    _declare_harness("codex", restore_args=["resume", "--last"])
    sdef = harness.normalize(SessionDef(
        name="x", profile="work", cwd=str(tmp_path), conversation_id="thread-1"
    ))

    fresh, _, _ = harness.build_command(sdef)
    restored, _, _ = harness.build_command(sdef, restoring=True)

    assert fresh[-2:] != ["resume", "--last"]
    assert restored[-2:] == ["resume", "thread-1"]


def test_session_env_overrides_harness_env(home, tmp_path):
    _declare_harness("h", env={"K": "harness"})
    _profile_for("custom", "h")
    sdef = harness.normalize(
        SessionDef(name="x", profile="custom", cwd=str(tmp_path), env={"K": "session"})
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
    # dated like every deliver()ed message: the argv handoff is the one
    # delivery that skips deliver(), so the stamp is prefixed here instead
    assert argv[-1].endswith("\n" + block)
    assert re.fullmatch(
        r"\[claunch delivered \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [+-]\d{4}\]",
        argv[-1].split("\n", 1)[0],
    )
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
    _profile_for("custom", "h")
    sdef = harness.normalize(
        SessionDef(name="x", profile="custom", cwd=str(tmp_path))
    )
    argv, _, _ = harness.build_command(sdef, opening="take the API")
    assert "take the API" not in argv
    assert harness.takes_opening_argv("h") is False
    assert harness.takes_opening_argv(harness.CLAUDE_HARNESS) is True


def test_a_declared_argv_opening_strategy_gets_the_positional_prompt(home, tmp_path):
    _declare_harness("h", opening_transport="argv")
    _profile_for("custom", "h")
    sdef = harness.normalize(
        SessionDef(name="x", profile="custom", cwd=str(tmp_path))
    )
    argv, _, _ = harness.build_command(sdef, opening="take the API")
    assert argv[-2] == "--"
    assert argv[-1].endswith("\ntake the API")
    assert harness.takes_opening_argv("h") is True


def test_windows_codex_npm_shim_preserves_the_multiline_opening(
    home, tmp_path, monkeypatch
):
    """The npm CMD shim treats a newline as a new batch command.

    claunch therefore starts the same JavaScript entry point through Node.
    The full opening remains one argv element through the executable boundary.
    """
    from claude_launcher import harnesses

    bin_dir = tmp_path / "npm"
    shim = bin_dir / "codex.CMD"
    entry = bin_dir / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    entry.parent.mkdir(parents=True)
    shim.write_text("@node codex.js %*", encoding="utf-8")
    entry.write_text("", encoding="utf-8")
    node = tmp_path / "node.exe"
    _profile_for("work", "codex")

    original_which = harnesses.shutil.which

    def which(program):
        if program == "codex":
            return str(shim)
        if program == "node":
            return str(node)
        return original_which(program)

    monkeypatch.setattr(harnesses.sys, "platform", "win32")
    monkeypatch.setattr(harnesses.shutil, "which", which)
    sdef = harness.normalize(
        SessionDef(name="x", profile="work", cwd=str(tmp_path))
    )
    block = "---\nmesh: team\n---\n\nworkflow: review"

    argv, _, _ = harness.build_command(sdef, opening=block)

    assert argv[:2] == [str(node), str(entry)]
    assert argv[-2] == "--"
    assert argv[-1].endswith("\n" + block)


def test_the_packaged_codex_strategies_name_its_tui_contract(home):
    from claude_launcher import harnesses

    codex = harnesses.get("codex")
    assert codex is not None
    assert codex.opening_transport == "argv"
    assert codex.input_readiness == "bracketed-paste"
    assert codex.submit_strategy == "screen"


def test_task_is_a_recorded_field(home):
    """The opening task is kept on the definition — the one piece of a
    session's setup a re-briefing could not otherwise reconstruct."""
    sdef = SessionDef.from_dict({"name": "x", "task": "do the thing"})
    assert sdef.task == "do the thing"
    assert SessionDef.from_dict(sdef.to_dict()).task == "do the thing"
    assert SessionDef.from_dict({"name": "x", "task": "  "}).task is None
    assert SessionDef.from_dict({"name": "x"}).task is None


def test_rebrief_hook_rides_on_every_claude_spawn(home, tmp_path):
    """The SessionStart hook lives in the process like the system prompt, so
    it is re-injected on restores too — and it fires only on the two events
    that lose the transcript-carried briefing."""
    profile.create("work")
    sdef = harness.normalize(SessionDef(name="x", profile="work", cwd=str(tmp_path)))
    for restoring in (False, True):
        argv, _, _ = harness.build_command(sdef, restoring=restoring)
        settings = json.loads(argv[argv.index("--settings") + 1])
        (entry,) = settings["hooks"]["SessionStart"]
        assert entry["matcher"] == "compact|clear"
        assert entry["hooks"] == [{"type": "command", "command": "claunch rebrief"}]


def test_callers_own_settings_suppress_the_hook(home, tmp_path):
    """Two --settings on one command line leaves claude to pick one; the
    caller's must win, so ours stays home."""
    profile.create("work")
    sdef = harness.normalize(
        SessionDef(
            name="x", profile="work", cwd=str(tmp_path),
            args=("--settings", "{}"),
        )
    )
    argv, _, _ = harness.build_command(sdef)
    assert argv.count("--settings") == 1


def test_non_claude_harness_gets_no_settings_flag(home, tmp_path):
    """--settings is claude's flag; handed to another harness it is a stray
    argument, exactly like the opening prompt would be."""
    _declare_harness("h")
    _profile_for("custom", "h")
    sdef = harness.normalize(
        SessionDef(name="x", profile="custom", cwd=str(tmp_path))
    )
    argv, _, _ = harness.build_command(sdef)
    assert "--settings" not in argv


# --------------------------------------------------------------------------- #
# restores_blank: the one restore that comes back empty, named once
# --------------------------------------------------------------------------- #
def test_restores_blank_is_true_exactly_for_the_missing_transcript(home, tmp_path):
    """The predicate and the argv branch are the same rule, not two.

    ``build_command`` asks it, and so does the daemon on the way up (to decide
    who is owed the blank-restore block). Two spellings of "is the transcript
    there" would drift, and the drift is silent: the session comes back empty
    and is told its history is intact.
    """
    profile.create("work")
    sdef = harness.normalize(SessionDef(name="x", profile="work", cwd=str(tmp_path)))

    assert harness.restores_blank(sdef) is True
    argv, _, _ = harness.build_command(sdef, restoring=True)
    assert argv[argv.index("--session-id") + 1] == sdef.conversation_id

    _write_transcript(sdef)
    assert harness.restores_blank(sdef) is False
    argv, _, _ = harness.build_command(sdef, restoring=True)
    assert argv[argv.index("--resume") + 1] == sdef.conversation_id


def test_a_transcript_under_another_slug_is_not_a_blank_restore(home, tmp_path):
    """Generous in the same direction ``transcripts.exists`` is: a conversation
    found anywhere in the config dir is resumed, so it is not blank either."""
    profile.create("work")
    sdef = harness.normalize(SessionDef(name="x", profile="work", cwd=str(tmp_path)))
    stray = profile.require("work").config_dir / "projects" / "somewhere-else"
    stray.mkdir(parents=True)
    (stray / f"{sdef.conversation_id}.jsonl").write_text("{}", encoding="utf-8")
    assert harness.restores_blank(sdef) is False


def test_only_a_pinned_claude_conversation_can_restore_blank(home, tmp_path):
    """The other restore branches are not this one.

    An id-less definition falls back to ``--continue`` (some conversation, not
    an empty one), a caller steering the conversation owns the outcome, and a
    non-claude harness has no transcript to be missing. A definition whose
    profile cannot be resolved is not blank either: that restore fails on its
    own terms, loudly, and calling it blank would send a re-briefing to a
    session that never came back.
    """
    profile.create("work")
    pinned = harness.normalize(SessionDef(name="x", profile="work", cwd=str(tmp_path)))

    legacy = replace(pinned, conversation_id=None)
    assert harness.restores_blank(legacy) is False

    steered = replace(pinned, args=("--resume", "abc"))
    assert harness.restores_blank(steered) is False

    other = replace(pinned, harness="py")
    assert harness.restores_blank(other) is False

    unknown = replace(pinned, profile="no-such-profile")
    assert harness.restores_blank(unknown) is False
