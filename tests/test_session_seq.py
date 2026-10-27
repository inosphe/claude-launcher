"""The session-name counter: auto-assignment never rewinds.

``daemon/manager.py`` used to pick the next session name by scanning for the
lowest free ``sN``. Two consequences of that scan are what these tests hold:

* a directory left behind by a failed creation kept its number forever, so the
  numbers a fleet had spent ran far ahead of the sessions it had;
* deleting such a directory made its number available again, so a session
  created *later* could be named lower than one created before it — and a
  recycled name makes two histories share one output log, one report directory
  and one worktree name.

The counter that replaces the scan is ``<launcher home>/daemon/session-seq.json``
(:mod:`claude_launcher.daemon.session_seq`). ``clear-sessions --logs`` deletes
directories and no longer frees names.

The manager is driven directly (no PTY) wherever the question is only about
what name it picks and what it leaves on disk.
"""

from __future__ import annotations

import asyncio
import json
import sys

from claude_launcher import lineage, profile, store
from claude_launcher.daemon import paths, session_seq
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import ManagerError, SessionManager
from claude_launcher.daemon.session import DeadSession

#: The smallest harness that satisfies ``normalize``: one that is not claude,
#: so it needs no profile. The manager only has to *stage* these sessions, so
#: the command is never run.
CHILD = "import sys\nprint('READY')\n"


def _register_py_harness():
    store.update(
        lambda doc: doc.update(
            {"harnesses": {"py": {"command": [sys.executable, "-u", "-c", CHILD]}}}
        )
    )
    if not profile.resolve("py").exists():
        lineage.set_harness(profile.create("py"), "py")


def _manager() -> SessionManager:
    return SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)


def _record(mgr: SessionManager, name: str) -> DeadSession:
    """A record holding ``name``, without a PTY or a directory.

    The shape a restore leaves behind, and the one place a name can be taken
    without the filesystem saying so.
    """
    record = DeadSession(SessionDef(name=name, harness="claude", cwd="."), exit_code=0)
    mgr._sessions[name] = record
    return record


def _seq() -> int:
    return session_seq.read()[0]


def _take(mgr: SessionManager) -> str:
    """The next name, reserved -- what ``stage`` does, without a session.

    ``_auto_name`` only proposes (see ``settle_name``), so a test that wants a
    number *spent* has to reserve it the way the manager does.
    """
    return mgr._reserve(mgr._auto_name())


def test_the_first_allocation_starts_at_zero_on_a_fresh_home(home):
    assert not session_seq.file().exists()
    assert _take(_manager()) == "s0"
    assert json.loads(session_seq.file().read_text()) == {"next": 1}


def test_the_counter_recovers_from_the_highest_directory_plus_one(home):
    """No counter file, but sessions already on disk: the next number is above
    every one of them, not the lowest gap among them.

    This is the case a migrated or hand-cleaned home hits, and the one where a
    "lowest free" answer would collide with a name that is in use.
    """
    for name in ("s0", "s1", "s4", "s1305-rev", "job-3"):
        paths.session_dir(name).mkdir(parents=True)

    number, source = session_seq.read()
    assert number == 5
    assert source.startswith("recovered")

    assert _take(_manager()) == "s5"
    assert _seq() == 6


def test_the_counter_recovers_from_a_record_whose_directory_is_gone(home):
    """Both sources are needed. ``clear-sessions --logs`` takes the directory
    and leaves the record's name in the durable store, so a recovery that read
    only directories would hand that name out again."""
    paths.session_dir("s2").mkdir(parents=True)

    mgr = _manager()
    _record(mgr, "s9")  # a record with no directory at all
    # Nothing on disk says 9; the registry does.
    assert session_seq.recover(records=list(mgr._sessions)) == 10
    assert _take(mgr) == "s10"


def test_a_corrupt_counter_file_recovers_instead_of_failing(home):
    paths.session_dir("s7").mkdir(parents=True)
    session_seq.file().parent.mkdir(parents=True, exist_ok=True)
    session_seq.file().write_text("{not json", encoding="utf-8")

    number, source = session_seq.read()
    assert number == 8
    assert "unusable document" in source
    assert _take(_manager()) == "s8"


def test_an_unusable_value_recovers_rather_than_being_handed_out(home):
    paths.session_dir("s2").mkdir(parents=True)
    session_seq.file().parent.mkdir(parents=True, exist_ok=True)
    for broken in ('{"next": -4}', '{"next": "9"}', '{"next": true}', '{}'):
        session_seq.file().write_text(broken, encoding="utf-8")
        number, source = session_seq.read()
        assert number == 3, broken
        assert source.startswith("recovered"), broken


def test_allocation_steps_by_one_and_advances_the_counter(home):
    mgr = _manager()
    assert [_take(mgr) for _ in range(4)] == ["s0", "s1", "s2", "s3"]
    assert _seq() == 4
    # Each name was reserved: the directory exists before the counter moved.
    for name in ("s0", "s1", "s2", "s3"):
        assert paths.session_dir(name).is_dir()


def test_deleting_the_counter_does_not_rewind_the_numbers(home):
    """The whole point of the change: erasing state must not hand a live
    session's name to a new one.

    The counter is rebuilt from the directories, so a deleted counter file
    resumes above them. A deleted *directory* (``clear-sessions --logs``) frees
    no number either, because the counter does not look at directories again
    once it has a value.
    """
    mgr = _manager()
    for _ in range(3):
        _take(mgr)

    session_seq.file().unlink()
    assert _take(mgr) == "s3"

    # Now delete every directory, as `clear-sessions --logs` would, and the
    # counter still stands: numbers already handed out are spent.
    import shutil
    for name in ("s0", "s1", "s2", "s3"):
        shutil.rmtree(paths.session_dir(name))
    assert _take(mgr) == "s4"


def test_a_name_already_on_disk_is_stepped_over_and_the_counter_moves(home):
    """A person's ``s9000`` costs the sequence nothing, but a *number* the
    counter points at that is taken is skipped and the counter moved past it —
    otherwise every allocation would retry the same taken pair."""
    paths.session_dir("s0").mkdir(parents=True)
    paths.session_dir("s1").mkdir(parents=True)
    session_seq.file().parent.mkdir(parents=True, exist_ok=True)
    session_seq.file().write_text('{"next": 0}')

    assert _take(_manager()) == "s2"
    assert _seq() == 3


def test_a_hand_chosen_name_does_not_consume_a_number(home):
    """``settle_name`` with a name that is not in the sequence leaves the
    counter exactly where it was: the numbers are the auto-assignment's, and a
    person typing ``s9000`` or ``s1305-rev`` is not asking it to skip theirs."""
    mgr = _manager()
    assert _take(mgr) == "s0"
    before = _seq()

    assert mgr.settle_name("s9000") == "s9000"
    assert mgr.settle_name("s1305-rev") == "s1305-rev"
    assert mgr.settle_name("job-17") == "job-17"
    assert _seq() == before

    # And the sequence carries on from where it was, not from s9000.
    assert mgr.settle_name("") == "s1"


def test_a_hand_chosen_numeric_name_is_stepped_over_when_reached(home):
    """A person's ``s1`` is a name in the sequence's own shape, so the sequence
    has to step over it when it gets there — without being told what the
    person's number was."""
    session_seq.file().parent.mkdir(parents=True, exist_ok=True)
    session_seq.file().write_text('{"next": 0}')
    paths.session_dir("s1").mkdir(parents=True)

    mgr = _manager()
    assert _take(mgr) == "s0"
    # s1 is taken by a person's session: the sequence steps to s2.
    assert _take(mgr) == "s2"


def test_a_record_without_a_directory_still_holds_its_name(home):
    """A restored record is a name in use even when nobody has made its
    directory yet, so the manager's own registry is consulted too."""
    mgr = _manager()
    _record(mgr, "s0")
    assert _take(mgr) == "s1"


def test_settle_name_refuses_a_name_a_record_holds(home):
    mgr = _manager()
    _record(mgr, "s3")
    try:
        mgr.settle_name("s3")
    except ManagerError as exc:
        assert "already exists" in str(exc)
    else:  # pragma: no cover - the assertion above is the point
        raise AssertionError("settle_name took a name a record holds")


def test_the_name_settled_is_the_name_the_stage_then_reserves(home, tmp_path):
    """The caller cuts a worktree named for the settled name; the session must
    then actually get it. Settling writes nothing, so this checks the pair —
    candidate, then reservation — lands on one name."""
    _register_py_harness()

    async def run():
        mgr = _manager()
        name = mgr.settle_name("")
        assert name == "s0"
        session = mgr.stage(SessionDef(name="", harness="py", cwd=str(tmp_path)))
        assert session.sdef.name == name
        assert paths.session_dir(name).is_dir()
        mgr.discard(name)

    asyncio.run(run())


def test_a_staged_session_does_not_take_a_second_number(home, tmp_path):
    """``stage`` settles a candidate and reserves it: one number, one directory.

    A second reservation would make every session appear to consume two.
    """
    _register_py_harness()

    async def run():
        mgr = _manager()
        session = mgr.stage(SessionDef(name="", harness="py", cwd=str(tmp_path)))
        assert session.sdef.name == "s0"
        assert _seq() == 1
        mgr.discard("s0")

    asyncio.run(run())


def test_a_named_staged_session_leaves_the_counter_alone(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        session = mgr.stage(SessionDef(name="mine", harness="py", cwd=str(tmp_path)))
        assert session.sdef.name == "mine"
        assert not session_seq.file().exists()
        mgr.discard("mine")

    asyncio.run(run())


def test_a_discarded_session_does_not_give_its_number_back(home, tmp_path):
    """The counter never rewinds. An attempt that fails after taking a name
    costs that number, which is cheaper than a rule that decides case by case
    whether a failed attempt may safely return one — that rule is where a
    double allocation would come from."""
    _register_py_harness()

    async def run():
        mgr = _manager()
        mgr.stage(SessionDef(name="", harness="py", cwd=str(tmp_path)))
        mgr.discard("s0")
        assert _take(mgr) == "s1"

    asyncio.run(run())


def test_within_process_allocation_hands_out_no_duplicate(home):
    """Two creators that read the counter at the same moment must not both
    write ``N + 1``: the read-decide-write is one critical section, and the
    directory is the reservation for anything outside it."""
    import threading

    mgr = _manager()
    names: list = []
    errors: list = []
    barrier = threading.Barrier(8)

    def take():
        try:
            barrier.wait()
            names.append(_take(mgr))
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=take) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(names) == 8
    assert len(set(names)) == 8, names
    assert _seq() == 8


def test_two_managers_in_one_home_do_not_collide(home):
    """The directory is the reservation, so a second manager — another
    daemon's view of the same home — steps over a name the first one made
    even when both read the same counter value."""
    first, second = _manager(), _manager()
    assert _take(first) == "s0"
    # Rewind the counter as if the second manager had read it before the first
    # one wrote: _reserve finds the candidate taken and takes the next number.
    session_seq.file().write_text('{"next": 0}')
    assert _take(second) == "s1"
    assert _seq() == 2


def test_settle_name_spends_nothing_when_the_request_is_refused(home, tmp_path):
    """The regression that made this split necessary.

    ``POST /api/sessions`` settles a name so the worktree can be named after
    it, and then checks the worktree, the harness, the profile and the kind.
    Every one of those can still refuse the request. A ``settle_name`` that
    reserved the name would therefore leave one spent number and one empty
    directory for every rejected form — 3,824 of those directories is the
    measurement that started this issue.

    So: settling a name must leave the counter file untouched and no directory
    behind, and only staging (which happens after the last refusal) may spend.
    """
    _register_py_harness()
    mgr = _manager()

    name = mgr.settle_name("")
    assert name == "s0"
    # Nothing spent, nothing on disk.
    assert not session_seq.file().exists()
    assert not paths.session_dir("s0").is_dir()

    # Settling again answers the same candidate: it was never reserved.
    assert mgr.settle_name("") == "s0"

    # Only the stage -- past every check -- spends the number.
    async def run():
        session = mgr.stage(SessionDef(name="", harness="py", cwd=str(tmp_path)))
        assert session.sdef.name == "s0"
        assert _seq() == 1
        assert paths.session_dir("s0").is_dir()
        mgr.discard("s0")

    asyncio.run(run())


def test_a_candidate_taken_meanwhile_is_replaced_at_stage(home, tmp_path):
    """A settled name is a proposal. Another creator can take it before the
    stage reserves it, and the stage then takes the next number instead of
    failing a request that is otherwise ready to run."""
    _register_py_harness()

    async def run():
        mgr = _manager()
        candidate = mgr.settle_name("")
        assert candidate == "s0"
        # Another creator wins the race.
        paths.session_dir("s0").mkdir(parents=True)

        session = mgr.stage(SessionDef(name="", harness="py", cwd=str(tmp_path)))
        assert session.sdef.name == "s1"
        mgr.discard("s1")

    asyncio.run(run())


def test_a_person_may_reuse_a_name_whose_directory_outlived_its_record(home, tmp_path):
    """``clear-sessions --logs`` can leave a directory with no record (or the
    reverse). A hand-given name is the person's to reuse — master let them, and
    this change does not take that away. Only the auto sequence must never
    reuse a number."""
    _register_py_harness()

    async def run():
        mgr = _manager()
        paths.session_dir("mine").mkdir(parents=True)
        session = mgr.stage(SessionDef(name="mine", harness="py", cwd=str(tmp_path)))
        assert session.sdef.name == "mine"
        mgr.discard("mine")

    asyncio.run(run())


def test_clear_sessions_does_not_free_a_number(home):
    """``clear --logs`` drops the record and the directory; the counter keeps
    the number spent. The CLI says so in its help."""
    mgr = _manager()
    _record(mgr, "s0")
    paths.session_dir("s0").mkdir(parents=True)
    session_seq.file().parent.mkdir(parents=True, exist_ok=True)
    session_seq.file().write_text('{"next": 1}')

    removed = mgr.clear(logs=True)
    assert removed == ["s0"]
    assert not paths.session_dir("s0").is_dir()

    assert mgr._auto_name() == "s1"
