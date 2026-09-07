"""Session listings are ordered by when the sessions were made, not by name.

Names are auto-generated with a counter (``s9``, ``s10``, ``s100``), so the
string sort the registry used to do filed the hundredth session between the
tenth and the eleventh — the order a fleet past nine reads as no order at
all. Every listing surface (``claunch ls``, the web dashboard, the
agent-facing tree) takes its order straight from
:meth:`SessionManager.list`, so both halves of the fix live here: the
ordering itself, and the creation stamp surviving every relaunch that keeps
the session's name. A stamp that got reset by a daemon restart would leave
the listing sorted by "when the daemon last came up", which is the same
non-order wearing a different hat.
"""

from __future__ import annotations

import asyncio
import json
import sys

from claude_launcher import lineage, profile, store
from claude_launcher.daemon import db, paths
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager

CHILD = "import sys\nprint('READY')\nsys.stdin.readline()\n"


def _manager() -> SessionManager:
    return SessionManager(idle_threshold=0.5, scrollback=100, restore_default=True)


def _register_py_harness() -> None:
    """A harness that exists so ``stage`` can normalize a definition."""
    store.update(
        lambda doc: doc.update(
            {"harnesses": {"py": {"command": [sys.executable, "-u", "-c", CHILD]}}}
        )
    )
    if not profile.resolve("py").exists():
        lineage.set_harness(profile.create("py"), "py")


class _NoLaunch(SessionManager):
    """A manager that stages sessions for real but never spawns a child.

    The creation stamp is set in ``Session.__init__``, so the object under
    test has to be a genuine :class:`Session` — only the PTY is faked away.
    """

    def launch(self, session, *, restoring: bool = False, opening: str = ""):
        self.persist()
        return session


def _record(mgr: SessionManager, name: str, at: str, *, parent: str = "") -> None:
    """Register an exited record made at ``at`` — the cheap way to hold a
    creation stamp without a process behind it."""
    mgr._retire(
        SessionDef(name=name, harness="py", parent=parent), {"created_at": at}
    )


# --------------------------------------------------------------------------- #
# the ordering
# --------------------------------------------------------------------------- #
def test_list_is_ordered_by_creation_and_not_by_name(home):
    """The reported symptom, in one assertion: s100 is the newest session, so
    it comes last — under a string sort it came second."""
    mgr = _manager()
    _record(mgr, "s9", "2026-08-25T09:00:00+00:00")
    _record(mgr, "s100", "2026-08-25T12:00:00+00:00")
    _record(mgr, "s10", "2026-08-25T10:00:00+00:00")
    _record(mgr, "s45", "2026-08-25T11:00:00+00:00")

    assert [s.sdef.name for s in mgr.list()] == ["s9", "s10", "s45", "s100"]


def test_same_second_sessions_keep_registry_order(home):
    """``created_at`` has second resolution, so a fast spawn ties. The sort is
    stable, so a tie falls back to insertion order — which is creation order
    too, never the name."""
    mgr = _manager()
    at = "2026-08-25T09:00:00+00:00"
    for name in ("s7", "s70", "s8"):
        _record(mgr, name, at)

    assert [s.sdef.name for s in mgr.list()] == ["s7", "s70", "s8"]


def test_a_record_with_no_stamp_sorts_first(home):
    """A legacy ``sessions.json`` entry has no ``created_at``. It must not
    crash the sort and must not be dropped: oldest is the honest guess for a
    record written before the field existed."""
    mgr = _manager()
    _record(mgr, "s2", "2026-08-25T09:00:00+00:00")
    _record(mgr, "ancient", "2026-08-25T09:00:00+00:00")
    # What a record whose stamp never made it out of the file looks like:
    # ``_retire`` fills a missing one in with *now*, so the only way to hold
    # the empty case is to put it back.
    mgr._sessions["ancient"].created_at = None

    assert [s.sdef.name for s in mgr.list()] == ["ancient", "s2"]


def test_children_and_live_children_follow_the_same_order(home):
    """Siblings are indented under their parent, so name order would put a
    lead's tenth child ahead of its second just as visibly as at the top."""
    mgr = _manager()
    _record(mgr, "lead", "2026-08-25T09:00:00+00:00")
    _record(mgr, "s101", "2026-08-25T11:00:00+00:00", parent="lead")
    _record(mgr, "s9", "2026-08-25T10:00:00+00:00", parent="lead")

    assert mgr.children("lead") == ["s9", "s101"]
    # live_children answers with the same order and the same exclusion it
    # always had: these records are exited, so it answers with nothing.
    assert mgr.live_children("lead") == []


def test_live_children_still_excludes_the_exited(home):
    """The ordering change must not touch what the spawn budget counts.

    The recorded stamps are deliberately long past: ``live-kid`` is staged
    for real and stamps itself with the clock, so it must be the newest.
    """
    _register_py_harness()

    async def run():
        mgr = _NoLaunch(idle_threshold=0.5, scrollback=100, restore_default=True)
        _record(mgr, "lead", "2020-01-01T09:00:00+00:00")
        _record(mgr, "dead-kid", "2020-01-01T10:00:00+00:00", parent="lead")
        mgr.stage(SessionDef(name="live-kid", harness="py", parent="lead"))

        assert mgr.children("lead") == ["dead-kid", "live-kid"]
        assert mgr.live_children("lead") == ["live-kid"]

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# the stamp survives every relaunch that keeps the name
# --------------------------------------------------------------------------- #
def _write_sessions_json(entries: list) -> None:
    # Seed the durable registry directly (was a sessions.json write before
    # the store moved to SQLite); allow_empty lets an empty list clear it.
    db.open_default().save(entries, prune=True, allow_empty=True)


def test_a_restored_session_keeps_its_original_creation_time(home):
    """A daemon restart relaunches into a new ``Session`` object. If that
    object stamped itself, every restored session would read as created at
    the restart and the fleet's order would be gone for good."""
    _register_py_harness()
    made = "2026-08-24T08:30:00+00:00"
    _write_sessions_json([
        {
            "def": SessionDef(name="s5", harness="py", restore=True).to_dict(),
            "was_running": True,
            "created_at": made,
        }
    ])

    async def run():
        mgr = _NoLaunch(idle_threshold=0.5, scrollback=100, restore_default=True)
        assert mgr.restore_all() == []
        assert mgr.get("s5").created_at == made
        # and it is written back out, so the next restart reads the same thing
        entries = db.open_default().load_all()
        assert entries[0]["created_at"] == made

    asyncio.run(run())


def test_restore_orders_a_restarted_fleet_the_way_it_was_made(home):
    """The end-to-end shape of the bug: names out of order, stamps in order,
    and the listing follows the stamps across the restart."""
    _register_py_harness()
    _write_sessions_json([
        {
            "def": SessionDef(name=name, harness="py", restore=True).to_dict(),
            "was_running": True,
            "created_at": at,
        }
        for name, at in (
            ("s100", "2026-08-24T12:00:00+00:00"),
            ("s20", "2026-08-24T09:00:00+00:00"),
            ("s45", "2026-08-24T10:00:00+00:00"),
        )
    ])

    async def run():
        mgr = _NoLaunch(idle_threshold=0.5, scrollback=100, restore_default=True)
        mgr.restore_all()
        assert [s.sdef.name for s in mgr.list()] == ["s20", "s45", "s100"]

    asyncio.run(run())


def test_a_respawn_keeps_the_session_where_it_was(home):
    """``respawn`` is the same session continuing — same name, same pinned
    conversation, same mesh memberships. Its place in the listing is one more
    thing that must not move."""
    _register_py_harness()
    made = "2026-08-24T08:30:00+00:00"

    async def run():
        mgr = _NoLaunch(idle_threshold=0.5, scrollback=100, restore_default=True)
        _record(mgr, "old", made)
        _record(mgr, "new", "2026-08-25T09:00:00+00:00")

        revived = mgr.respawn("old")
        assert revived.created_at == made
        assert [s.sdef.name for s in mgr.list()] == ["old", "new"]

    asyncio.run(run())


def test_a_redefine_keeps_the_session_where_it_was(home, tmp_path):
    """Same for the relaunch-under-a-changed-definition path (migrate,
    reborrow and the rest all end in ``_recreate``)."""
    _register_py_harness()
    made = "2026-08-24T08:30:00+00:00"

    async def run():
        mgr = _NoLaunch(idle_threshold=0.5, scrollback=100, restore_default=True)
        _record(mgr, "old", made)
        old = mgr.get("old")
        moved = mgr._recreate(
            "old", old, SessionDef(name="old", harness="py", cwd=str(tmp_path))
        )
        assert moved.created_at == made

    asyncio.run(run())
