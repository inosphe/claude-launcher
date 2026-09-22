"""Every session is given a directory of its own to write working files in.

``/tmp`` is one directory for the whole machine on this platform, and more
than twenty sessions run here at once. Two of them that pick the same file
name overwrite each other with no error: the file is there, it is readable,
its format is right, and only the values belong to somebody else. The
incident is on ``claunch-shared-tmp-clobber-xjn`` -- one session's list of
changed files became another's between two commands, and the intersection
computed from it named a file that session had never touched. It was caught
because the answer was impossible, not because it was wrong; an answer that
had merely been wrong would have shipped.

Redirecting ``/tmp`` per session is not on the table. Running with ``TMP``
and ``TEMP`` pointed elsewhere still leaves ``cygpath -w /tmp`` at the
machine-wide path, because MSYS mounts it fixed. So what these tests pin is
the alternative: a path that is per session by construction, named to the
session in its own environment, and existing before the session's first
command.

What this cannot reach is the shell command an agent types. That is why the
last group here pins a check over the files this repository itself executes:
a fixed temp path written into one of those collides on every machine and in
every session, and unlike an agent's command it can be refused before it
lands.
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest

from claude_launcher import profile
from claude_launcher.daemon import harness, paths
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.session import Session


ROOT = Path(__file__).resolve().parents[1]
CHECK = ROOT / "tools" / "check_tmp_paths.py"


def _load_check():
    spec = importlib.util.spec_from_file_location("check_tmp_paths_under_test", CHECK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# -- the path itself ------------------------------------------------------


def test_scratch_lives_under_the_session_directory(home):
    """Its lifetime is the session record's, which is what we want.

    ``manager`` removes the whole session directory when the record is
    cleared, so the scratch space goes with it and nothing has to remember to
    sweep it. Putting it anywhere else would mean inventing a second lifetime
    for the same thing.
    """
    assert paths.session_scratch_dir("s7") == paths.session_dir("s7") / "scratch"
    assert paths.session_log("s7").parent == paths.session_scratch_dir("s7").parent


def test_two_sessions_never_share_a_scratch_path(home):
    """The whole property, stated once.

    Everything else here is arrangement; this is the thing the incident was
    about. Sessions are distinguished by name, so deriving the path from the
    name is what makes the collision impossible rather than unlikely.
    """
    seen = {paths.session_scratch_dir(name) for name in ("s1", "s2", "s10", "sweep")}
    assert len(seen) == 4


# -- the session is told where it is --------------------------------------


def test_every_session_is_told_where_its_scratch_is(home, tmp_path):
    """``CLAUNCH_SCRATCH`` is set for every harness, not just claude.

    claude sessions get a scratchpad from their own harness; pi and codex
    sessions, and the ``!`` shells inside any of them, get nothing. Those are
    exactly the ones left writing to the shared directory, so the variable
    cannot be conditional on the harness.
    """
    profile.create("work")
    sdef = harness.normalize(SessionDef(name="sx", profile="work", cwd=str(tmp_path)))
    _, env, _ = harness.build_command(sdef)
    assert env["CLAUNCH_SCRATCH"] == str(paths.session_scratch_dir("sx"))
    assert env["CLAUNCH_SESSION"] == "sx"


def test_the_variable_follows_the_session_name(home, tmp_path):
    """Two sessions are handed two different directories, in the env itself.

    Pinned through ``build_command`` rather than through the helper, because
    the env is what the session actually reads.
    """
    profile.create("work")
    values = []
    for name in ("sa", "sb"):
        sdef = harness.normalize(
            SessionDef(name=name, profile="work", cwd=str(tmp_path))
        )
        _, env, _ = harness.build_command(sdef)
        values.append(env["CLAUNCH_SCRATCH"])
    assert values[0] != values[1]


def test_assembling_the_command_creates_nothing(home, tmp_path):
    """``build_command`` stays pure -- it is called to inspect, not only to run.

    The directory is created where the session is, so that asking what a
    session's command would be does not leave a directory behind for a
    session that was never started.
    """
    profile.create("work")
    sdef = harness.normalize(SessionDef(name="sy", profile="work", cwd=str(tmp_path)))
    harness.build_command(sdef)
    assert not paths.session_scratch_dir("sy").exists()


def test_the_directory_exists_before_the_session_runs_anything(home, tmp_path):
    """A variable naming a directory that is not there is still a trap.

    The first writer would have to notice and create it, which is the same
    kind of "remember to" the exported path exists to remove -- and a session
    whose redirect fails will reach for the shared directory instead.
    """
    async def run():
        # ``Session`` binds the running loop in its constructor, so it is
        # built inside one here for the same reason ``test_activity`` does.
        session = Session(
            SessionDef(name="screated", cwd=str(tmp_path)),
            idle_threshold=2,
            scrollback=20,
        )
        try:
            scratch = paths.session_scratch_dir("screated")
            assert scratch.is_dir()
            probe = scratch / "mine.txt"
            probe.write_text("written by this session", encoding="utf-8")
            assert probe.read_text(encoding="utf-8") == "written by this session"
        finally:
            session._feeder.close()
            session._log.close()

    asyncio.run(run())


# -- the check over the files this repository executes ---------------------


@pytest.mark.parametrize(
    "line,flagged,why",
    [
        ("run('git diff > /tmp/mine.txt')", True, "a fixed name"),
        ("run('git diff > /tmp/$CLAUNCH_SESSION-mine.txt')", False, "session in the name"),
        ("run('git diff > $CLAUNCH_SCRATCH/mine.txt')", False, "the exported directory"),
        ("# never write to /tmp/<a fixed name>", False, "a placeholder in prose"),
        ("STATE = '.claunch/tmp/e2e-roundtrip.json'", False, "project-relative, per checkout"),
        ("url = 'file:///tmp/x'", False, "a path inside a url"),
    ],
)
def test_the_check_separates_a_collision_from_a_mention(line, flagged, why):
    """It has to refuse the form and leave the documentation of it alone.

    ``AGENTS.md`` and the workflow rules quote the incident's real file names,
    and a check that flagged those would be answered by deleting the account
    of the bug. So the verdict is on the shape of the path, not on the string
    ``/tmp``: a name that varies per session passes, a placeholder passes, and
    a directory that merely ends in ``tmp`` is not this problem at all.
    """
    check = _load_check()
    hits = check.findings_in(line, path="x.py", workflow=False)
    assert bool(hits) is flagged, why


def test_only_command_lines_of_a_workflow_are_read():
    """A workflow's prose states the rule and quotes what it forbids.

    What runs in a workflow is the declared command, so that is what is
    checked; reading the prose would report the rule itself as a violation.
    """
    check = _load_check()
    prose = "      `/tmp/mine.txt` 처럼 쓰면 다른 세션이 덮는다"
    command = "    verify: 'sh -c \"git diff > /tmp/mine.txt\"'"
    assert not check.findings_in(prose, path="w.yaml", workflow=True)
    assert check.findings_in(command, path="w.yaml", workflow=True)


def test_this_repository_has_no_fixed_temp_path_in_what_it_executes():
    """The state the check exists to hold, measured on this checkout."""
    check = _load_check()
    assert check.scan(ROOT) == []
