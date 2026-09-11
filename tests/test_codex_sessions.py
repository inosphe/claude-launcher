from __future__ import annotations

import asyncio
import json
import os

from claude_launcher.daemon import codex_sessions


def _rollout(root, day, session_id, cwd):
    path = root / "sessions" / day / f"rollout-{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "type": "session_meta",
        "payload": {"id": session_id, "cwd": str(cwd)},
    }) + "\n", encoding="utf-8")


def test_claim_new_uses_snapshot_and_cwd(tmp_path):
    profile_home = tmp_path / "codex"
    ours = tmp_path / "ours"
    other = tmp_path / "other"
    _rollout(profile_home, "2026/08/27", "old", ours)
    known = codex_sessions.snapshot(profile_home)

    _rollout(profile_home, "2026/08/27", "wrong-cwd", other)
    _rollout(profile_home, "2026/08/27", "ours", ours)

    assert codex_sessions.claim_new(profile_home, str(ours), known, timeout=0) == "ours"


def test_latest_supports_legacy_unpinned_definitions(tmp_path):
    profile_home = tmp_path / "codex"
    cwd = tmp_path / "work"
    _rollout(profile_home, "2026/08/26", "older", cwd)
    older = next((profile_home / "sessions").glob("**/*older.jsonl"))
    _rollout(profile_home, "2026/08/27", "newer", cwd)
    older.touch()

    # Write time, not directory/filename ordering, is Codex's definition of
    # the conversation most recently active in this cwd.
    assert codex_sessions.latest(profile_home, str(cwd)) == "older"


def test_find_returns_the_rollout_whose_metadata_owns_the_id(tmp_path):
    profile_home = tmp_path / "codex"
    cwd = tmp_path / "work"
    _rollout(profile_home, "2026/08/27", "wanted", cwd)
    expected = next((profile_home / "sessions").glob("**/*wanted.jsonl"))

    assert codex_sessions.find(profile_home, "wanted") == expected
    assert codex_sessions.find(profile_home, "missing") is None


def test_latest_skips_a_conversation_another_session_holds(tmp_path):
    """A cwd is not an identity: several codex sessions stand in one checkout.

    Measured 2026-09-11: s507, s514 and s516 all stood in ``F:/works/gds6``,
    which held exactly one rollout, and the unpinned restore gave all three
    that same conversation.
    """
    profile_home = tmp_path / "codex"
    cwd = tmp_path / "work"
    _rollout(profile_home, "2026/09/11", "taken-by-s507", cwd)

    assert codex_sessions.latest(profile_home, str(cwd)) == "taken-by-s507"
    assert codex_sessions.latest(
        profile_home, str(cwd), taken=["taken-by-s507"]
    ) is None


def test_latest_still_answers_when_an_older_rollout_is_free(tmp_path):
    profile_home = tmp_path / "codex"
    cwd = tmp_path / "work"
    _rollout(profile_home, "2026/09/10", "free", cwd)
    free = next((profile_home / "sessions").glob("**/*free.jsonl"))
    _rollout(profile_home, "2026/09/11", "held", cwd)
    free.touch()
    held = next((profile_home / "sessions").glob("**/*held.jsonl"))
    os.utime(held, (free.stat().st_atime - 60, free.stat().st_mtime - 60))

    assert codex_sessions.latest(profile_home, str(cwd)) == "free"
    assert codex_sessions.latest(profile_home, str(cwd), taken=["free"]) == "held"


def test_names_conversation_reads_the_uuid_the_args_name():
    wanted = "01a08e3a-8b90-7b01-9ab0-6aaab5704bb5"

    assert codex_sessions.names_conversation(["resume", wanted]) == wanted
    assert codex_sessions.names_conversation(
        ["--dangerously-bypass-approvals-and-sandbox", "resume", wanted]
    ) == wanted
    # A bare subcommand opens codex's picker, and a session *name* is not
    # something claunch can pin — rollouts are addressed by their uuid.
    assert codex_sessions.names_conversation(["resume"]) is None
    assert codex_sessions.names_conversation(["resume", "my-session"]) is None
    assert codex_sessions.names_conversation([]) is None


def test_resumes_existing_sees_the_subcommand_a_flag_list_misses():
    """``harness.steers_conversation`` answers this for claude's flags.

    Codex steers with a positional subcommand, so the flag list returns False
    for the very args that do attach to an existing conversation — which is
    why claim_new was started for s507 at all (daemon.log 12:20:59) and then
    retried forever for a rollout codex never writes.
    """
    from claude_launcher.daemon import harness

    assert codex_sessions.resumes_existing(["resume"]) is True
    assert codex_sessions.resumes_existing(["resume", "--last"]) is True
    assert codex_sessions.resumes_existing(
        ["--dangerously-bypass-approvals-and-sandbox"]
    ) is False
    assert harness.steers_conversation(["resume"]) is False


def test_pinned_conversations_is_every_other_session_s_claim(home, tmp_path):
    """What ``latest`` must not hand out twice.

    Three codex sessions in one cwd, two of them already pinned: the set the
    unpinned one's restore has to skip is the other two, never its own.
    """
    from claude_launcher.daemon.harness import SessionDef
    from claude_launcher.daemon.manager import SessionManager

    async def run():
        # Session construction binds the running loop, so the manager is built
        # inside one even though nothing here is awaited.
        mgr = SessionManager(
            idle_threshold=0.5, scrollback=200, restore_default=True
        )
        for name, cid in (("s507", "thread-a"), ("s514", "thread-b"), ("s513", "")):
            mgr.stage(SessionDef(
                name=name, harness="codex", cwd=str(tmp_path),
                conversation_id=cid,
            ))
        return (
            mgr._pinned_conversations(),
            mgr._pinned_conversations(except_for="s507"),
        )

    everything, without_s507 = asyncio.run(run())

    assert everything == {"thread-a", "thread-b"}
    assert without_s507 == {"thread-b"}
