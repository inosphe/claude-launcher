"""``claunch mesh owed`` must not ask for a ledger over every member.

Building one member's row walks the message log twice, so the unnarrowed
report is members times messages: mesh-0826 answered it in 4.2s over 251
members and 27708 messages (2026-09-20). The walk is synchronous, so those
seconds are not the caller's alone -- the daemon answers nothing else while
it runs, and every terminal it is pumping stops with it.

The daemon route has taken ``?state=`` since the dashboard stopped asking
for the lot; the CLI still defaulted to ``all``, which is the call agents
make routinely. These pin the default, the by-name form (which must not be
narrowed, or a killed member reads as no such member), and that the two
copies of the partition vocabulary stay equal.
"""

from __future__ import annotations

import asyncio

from claude_launcher import cli_mesh
from claude_launcher.daemon import mesh as mesh_mod
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.mesh import MeshManager

from test_mesh import _manager, _register_py_harness, _wait_exited


class _Client:
    """A daemon client that records the path instead of calling one."""

    def __init__(self, answer):
        self.answer = answer
        self.paths = []

    def get(self, path):
        self.paths.append(path)
        return self.answer


def _cli(monkeypatch, argv, answer=None):
    """Run ``claunch mesh owed ...`` against a recorded client."""
    from claude_launcher import cli

    client = _Client(
        answer if answer is not None
        else {"members": [], "owed": 0, "member_counts": {"all": 0}}
    )
    monkeypatch.setattr(
        cli_mesh.daemon_client, "ensure_running", lambda *a, **k: client
    )
    assert cli.main(argv) == 0
    return client


def test_the_default_call_leaves_out_the_sessions_that_have_ended(
    home, monkeypatch, capsys
):
    client = _cli(monkeypatch, ["mesh", "owed", "m"])
    assert client.paths == ["/api/mesh/m/owed?state=current"]


def test_the_whole_ledger_is_still_reachable(home, monkeypatch, capsys):
    client = _cli(monkeypatch, ["mesh", "owed", "m", "--state", "all"])
    assert client.paths == ["/api/mesh/m/owed?state=all"]


def test_a_handle_is_asked_for_by_name(home, monkeypatch, capsys):
    """Not state plus name: the name alone, so the state filter cannot hide
    the member that was asked about."""
    client = _cli(
        monkeypatch,
        ["mesh", "owed", "m", "--handle", "bob"],
        answer={
            "members": [{"handle": "bob", "role": "worker", "owed": 0, "local": True}],
            "owed": 0,
            "member_counts": {"all": 9},
        },
    )
    assert client.paths == ["/api/mesh/m/owed?handle=bob"]


def test_what_was_left_out_is_said(home, monkeypatch, capsys):
    """A narrowed list that does not say it is narrowed reads as the whole
    mesh being quiet."""
    _cli(
        monkeypatch,
        ["mesh", "owed", "m"],
        answer={
            "members": [{"handle": "alice", "role": "worker", "owed": 0, "local": True}],
            "owed": 0,
            "member_counts": {"all": 12},
        },
    )
    out = capsys.readouterr().out
    assert "1 of 12 members" in out
    assert "--state all" in out


def test_the_partition_words_are_the_same_on_both_sides():
    """The CLI spells them out rather than importing them, to keep aiohttp
    off the path of every ``claunch mesh`` call. This is what keeps that
    copy honest."""
    assert cli_mesh.MEMBER_STATES == mesh_mod.MEMBER_STATES


def test_a_named_member_is_read_whatever_state_it_is_in(home, tmp_path):
    """The narrowing that ``--handle`` sends is by name, and it overrides
    the state filter: a member asked for by name and left out would read as
    a member that does not exist."""
    _register_py_harness()
    mgr = _manager()
    mm = MeshManager(mgr)
    mm.create("m")

    async def run():
        for name in ("live", "gone"):
            mgr.create(SessionDef(name=name, harness="py", cwd=str(tmp_path)))
        await mm.join("m", "live", handle="alice")
        await mm.join("m", "gone", handle="bob")
        mgr.kill("gone")
        await _wait_exited(mgr.get("gone"))
        mesh = mm.get("m")

        # state=current would drop bob; the name is what is being asked.
        by_name = mm.owed_report(mesh, state="current", handle="bob")
        assert [r["handle"] for r in by_name["members"]] == ["bob"]
        assert by_name["member_handle"] == "bob"
        assert by_name["member_state"] == "one"
        # Still counted over everybody, so the report says what it left out.
        assert by_name["member_counts"]["all"] == 2

        missing = mm.owed_report(mesh, handle="nobody")
        assert missing["members"] == []

        await mgr.shutdown_all()

    asyncio.run(run())


def test_the_rows_a_named_read_builds_match_the_unnarrowed_one(home, tmp_path):
    """The cheap form must be the same answer, not a different one."""
    _register_py_harness()
    mgr = _manager()
    mm = MeshManager(mgr)
    mm.create("m")

    async def run():
        for name in ("a", "b"):
            mgr.create(SessionDef(name=name, harness="py", cwd=str(tmp_path)))
        await mm.join("m", "a", handle="alice")
        await mm.join("m", "b", handle="bob")
        mesh = mm.get("m")
        await mm.send("m", "alice", "bob", "what do you make of this?")

        whole = mm.owed_report(mesh)
        one = mm.owed_report(mesh, handle="bob")
        row_whole = [r for r in whole["members"] if r["handle"] == "bob"][0]
        row_one = one["members"][0]
        for key in ("handle", "role", "owed", "pending", "local", "source"):
            assert row_one[key] == row_whole[key], key
        assert [m["id"] for m in row_one["messages"]] == [
            m["id"] for m in row_whole["messages"]
        ]

        await mgr.shutdown_all()

    asyncio.run(run())
