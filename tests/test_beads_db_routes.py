"""The Settings page's Beads boards card, over its routes.

Which database a board reads is the one decision this card makes, and it is
made in three calls: read the boards, point one at a ``.db`` file, drop the
override again. A fourth creates the database now instead of on first use.
These tests pin what each answers and what each refuses — the refusals are
the point of the card, because a path that is almost right (a directory, a
relative path, another suffix) produces an empty board rather than an error
if it is stored.

``br`` never runs: the board is :class:`tests.test_beads_daemon.FakeBr`.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from claude_launcher import beads_db, workspaces
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.mesh import MeshManager

from test_beads_daemon import BEARER, FakeBr, _board, repo  # noqa: F401


@pytest.fixture
def ws(tmp_path, monkeypatch):
    """One registered workspace, and nothing else on this machine."""
    root = tmp_path / "trees" / "alpha"
    root.mkdir(parents=True)
    row = workspaces.Workspace(name="alpha", path=str(root))
    monkeypatch.setattr(workspaces, "list_all", lambda doc=None: [row])
    monkeypatch.setattr(workspaces, "get", lambda name, doc=None: row if name == "alpha" else None)
    monkeypatch.setattr(workspaces, "owning", lambda path, doc=None: None)
    return row


async def _serve(tmp_path, board):
    from aiohttp.test_utils import TestClient, TestServer

    mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
    mm = MeshManager(mgr, root=tmp_path / "mesh")
    app = build_app(mgr, "sekrit", started_at=time.monotonic(), mesh=mm, beads=board)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


def _run(coro_factory):
    asyncio.run(coro_factory())


# --------------------------------------------------------------------------- #
# reading
# --------------------------------------------------------------------------- #
def test_every_workspace_is_a_board_the_card_can_point(tmp_path, ws, repo):
    board = _board(FakeBr(), repo)

    async def run():
        client = await _serve(tmp_path, board)
        try:
            resp = await client.get("/api/beads/settings", headers=BEARER)
            doc = await resp.json()
            assert resp.status == 200, doc
            rows = {r["board"]: r for r in doc["boards"]}
            assert "alpha" in rows
            assert doc["default_board"] == beads_db.DEFAULT_BOARD
            alpha = rows["alpha"]
            assert Path(alpha["db"]) == Path(ws.path) / ".beads" / "beads.db"
            assert alpha["configured"] is False
            # The card offers "reset" without recomputing the rule itself.
            assert Path(alpha["default_db"]) == Path(alpha["db"])
            # Nothing has been created, and the row says so rather than
            # reporting zero issues on a board that does not exist.
            assert alpha["exists"] is False
            assert alpha["issues"] is None
        finally:
            await client.close()

    _run(run)


def test_a_database_that_is_there_is_counted(tmp_path, ws, repo):
    """A board about to be repointed can be seen to have work on it first."""
    import sqlite3

    db_path = Path(ws.path) / ".beads" / "beads.db"
    db_path.parent.mkdir(parents=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE issues (id TEXT)")
    conn.executemany("INSERT INTO issues VALUES (?)", [("a",), ("b",)])
    conn.commit()
    conn.close()
    board = _board(FakeBr(), repo)

    async def run():
        client = await _serve(tmp_path, board)
        try:
            resp = await client.get("/api/beads/settings", headers=BEARER)
            rows = {r["board"]: r for r in (await resp.json())["boards"]}
            assert rows["alpha"]["exists"] is True
            assert rows["alpha"]["issues"] == 2
        finally:
            await client.close()

    _run(run)


# --------------------------------------------------------------------------- #
# writing
# --------------------------------------------------------------------------- #
def test_a_board_can_be_pointed_at_another_db_file(tmp_path, ws, repo):
    board = _board(FakeBr(), repo)
    elsewhere = tmp_path / "boards"
    elsewhere.mkdir()
    target = elsewhere / "alpha.db"

    async def run():
        client = await _serve(tmp_path, board)
        try:
            resp = await client.put(
                "/api/beads/settings/alpha",
                json={"db": str(target)}, headers=BEARER,
            )
            doc = await resp.json()
            assert resp.status == 200, doc
            assert Path(doc["board"]["db"]) == target
            assert doc["board"]["configured"] is True
            # And it is what the next read reports, not just this answer.
            again = await (await client.get("/api/beads/settings", headers=BEARER)).json()
            rows = {r["board"]: r for r in again["boards"]}
            assert Path(rows["alpha"]["db"]) == target
        finally:
            await client.close()

    _run(run)


def test_clearing_puts_the_board_back_on_its_default(tmp_path, ws, repo):
    board = _board(FakeBr(), repo)
    beads_db.set_db("alpha", str(tmp_path / "x.db"))

    async def run():
        client = await _serve(tmp_path, board)
        try:
            resp = await client.put(
                "/api/beads/settings/alpha", json={"db": None}, headers=BEARER,
            )
            doc = await resp.json()
            assert resp.status == 200, doc
            assert doc["board"]["configured"] is False
            assert Path(doc["board"]["db"]) == Path(ws.path) / ".beads" / "beads.db"
        finally:
            await client.close()

    _run(run)


@pytest.mark.parametrize(
    "bad, says",
    [
        ("boards/alpha.db", "absolute"),
        ("relative.db", "absolute"),
    ],
)
def test_a_path_that_is_not_an_absolute_db_file_is_refused(tmp_path, ws, repo, bad, says):
    board = _board(FakeBr(), repo)

    async def run():
        client = await _serve(tmp_path, board)
        try:
            resp = await client.put(
                "/api/beads/settings/alpha", json={"db": bad}, headers=BEARER,
            )
            doc = await resp.json()
            assert resp.status == 400, doc
            assert says in doc["error"]
            # And nothing was stored: a refused write leaves the board where
            # it was rather than half-applying.
            assert beads_db.configured("alpha") is None
        finally:
            await client.close()

    _run(run)


def test_a_directory_named_db_is_refused(tmp_path, ws, repo):
    board = _board(FakeBr(), repo)
    looks = tmp_path / "looks.db"
    looks.mkdir()

    async def run():
        client = await _serve(tmp_path, board)
        try:
            resp = await client.put(
                "/api/beads/settings/alpha", json={"db": str(looks)}, headers=BEARER,
            )
            doc = await resp.json()
            assert resp.status == 400, doc
            assert "is a directory" in doc["error"]
        finally:
            await client.close()

    _run(run)


def test_a_board_nobody_registered_is_a_404(tmp_path, ws, repo):
    board = _board(FakeBr(), repo)

    async def run():
        client = await _serve(tmp_path, board)
        try:
            resp = await client.put(
                "/api/beads/settings/ghost",
                json={"db": str(tmp_path / "g.db")}, headers=BEARER,
            )
            doc = await resp.json()
            assert resp.status == 404, doc
            assert beads_db.DEFAULT_BOARD in doc["error"]
        finally:
            await client.close()

    _run(run)


# --------------------------------------------------------------------------- #
# creating the database now
# --------------------------------------------------------------------------- #
def test_create_runs_init_under_the_boards_own_name(tmp_path, ws, repo):
    br = FakeBr()
    board = _board(br, repo)

    async def run():
        client = await _serve(tmp_path, board)
        try:
            resp = await client.post("/api/beads/settings/alpha/init", headers=BEARER)
            doc = await resp.json()
            assert resp.status == 200, doc
            assert doc["created"] is True
            assert [i["prefix"] for i in br.inits] == ["alpha"]
            # br init writes where it stands, so it is run in the board's own
            # root and the file lands at the board's own path with no move.
            assert Path(br.inits[0]["cwd"]) == Path(ws.path)
            assert Path(br.inits[0]["made"]) == Path(ws.path) / ".beads" / "beads.db"
            # The row that comes back says the board is there now.
            assert doc["board"]["exists"] is True
        finally:
            await client.close()

    _run(run)


def test_create_on_a_board_that_exists_changes_nothing(tmp_path, ws, repo):
    br = FakeBr()
    board = _board(br, repo)
    db_path = Path(ws.path) / ".beads" / "beads.db"
    db_path.parent.mkdir(parents=True)
    db_path.write_bytes(b"")

    async def run():
        client = await _serve(tmp_path, board)
        try:
            resp = await client.post("/api/beads/settings/alpha/init", headers=BEARER)
            doc = await resp.json()
            assert resp.status == 200, doc
            assert doc["created"] is False
            assert br.inits == []
        finally:
            await client.close()

    _run(run)


def test_a_relocated_board_is_created_and_moved_onto_its_path(tmp_path, ws, repo):
    """``br init`` ignores ``--db``, so a board set to another path is
    initialised where ``br`` will accept it and the file it wrote is moved
    onto the path the board is set to."""
    br = FakeBr()
    board = _board(br, repo)
    elsewhere = tmp_path / "boards"
    elsewhere.mkdir()
    target = elsewhere / "alpha.db"
    beads_db.set_db("alpha", str(target))

    async def run():
        client = await _serve(tmp_path, board)
        try:
            resp = await client.post("/api/beads/settings/alpha/init", headers=BEARER)
            doc = await resp.json()
            assert resp.status == 200, doc
            assert doc["created"] is True
            assert target.is_file()
            # br was run in the relocated board's own directory, and what it
            # wrote there was moved to the name the board is set to.
            assert Path(br.inits[0]["cwd"]) == elsewhere
            assert not Path(br.inits[0]["made"]).exists()
            # The .beads directory br makes stays: every other subcommand
            # needs one discoverable from the directory it runs in.
            assert (elsewhere / ".beads").is_dir()
            assert doc["board"]["exists"] is True
        finally:
            await client.close()

    _run(run)
