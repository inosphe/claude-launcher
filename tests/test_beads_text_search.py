"""The board page's text-search fallback, over its route.

The Beads page's search box works in two states: with the rag configured it
ranks by meaning, and without it — the state this file pins — it substring-
matches id/title/description through ``br search`` (/api/beads/text-search).
A daemon with nothing configured beyond ``br`` must still answer a search.
The answers are cached for the listing caches' TTL so a typing burst forks
``br`` once, and they never invalidate the board's listing cache, because
``search`` is a read.

``br`` never runs: the board is :class:`tests.test_beads_daemon.FakeBr`.
"""

from __future__ import annotations

import asyncio
import time

from aiohttp.test_utils import TestClient, TestServer

from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.mesh import MeshManager

from test_beads_daemon import BEARER, FakeBr, _board, repo  # noqa: F401


async def _serve(tmp_path, board):
    mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
    mm = MeshManager(mgr, root=tmp_path / "mesh")
    app = build_app(mgr, "sekrit", started_at=time.monotonic(), mesh=mm, beads=board)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


def _run(coro_factory):
    asyncio.run(coro_factory())


def _seed(fake: FakeBr) -> None:
    fake.add(id="cl-1", title="pagination is slow", status="open",
             description="the board page takes seconds")
    fake.add(id="cl-2", title="unrelated work", status="in_progress")
    fake.add(id="cl-3", title="a closed PAGINATION note", status="closed")


def test_substring_search_over_id_title_description(tmp_path, repo):
    """Every field a row renders is searchable, closed issues included."""
    fake = FakeBr()
    _seed(fake)
    board = _board(fake, repo)

    async def run():
        client = await _serve(tmp_path, board)
        try:
            resp = await client.get(
                "/api/beads/text-search",
                params={"q": "pagination", "cwd": str(repo)},
                headers=BEARER,
            )
            doc = await resp.json()
            assert resp.status == 200, doc
            assert [r["id"] for r in doc["issues"]] == ["cl-1", "cl-3"]
            assert doc["root"] == str(repo)
            # Full rows, so the page renders them with its usual row.
            assert doc["issues"][0]["title"] == "pagination is slow"
        finally:
            await client.close()

    _run(run)


def test_search_never_invalidates_the_listing_cache(tmp_path, repo):
    """A read must not throw away what the board's own polling just paid for."""
    fake = FakeBr()
    _seed(fake)
    board = _board(fake, repo)

    async def run():
        await board.issue_page(repo, offset=0, limit=10)
        assert board._page_cache, "the listing page was cached"
        client = await _serve(tmp_path, board)
        try:
            resp = await client.get(
                "/api/beads/text-search",
                params={"q": "pagination", "cwd": str(repo)},
                headers=BEARER,
            )
            assert resp.status == 200, await resp.json()
            assert board._page_cache, "a read dropped the listing cache"
        finally:
            await client.close()

    _run(run)


def test_a_typing_burst_forks_br_once(tmp_path, repo):
    """The short TTL dedups a debounced box's repeated identical asks."""
    fake = FakeBr()
    _seed(fake)
    board = _board(fake, repo)

    async def run():
        client = await _serve(tmp_path, board)
        try:
            for _ in range(3):
                resp = await client.get(
                    "/api/beads/text-search",
                    params={"q": "pagination", "cwd": str(repo)},
                    headers=BEARER,
                )
                assert resp.status == 200, await resp.json()
            searches = [c for c in fake.calls if "search" in c]
            assert len(searches) == 1, searches
        finally:
            await client.close()

    _run(run)


def test_refusals(tmp_path, repo):
    """The route's own contract: a query is required and bounded."""
    fake = FakeBr()
    _seed(fake)
    board = _board(fake, repo)

    async def run():
        client = await _serve(tmp_path, board)
        try:
            for params, why in (
                ({"cwd": str(repo)}, "q is required"),
                ({"q": "x" * 201, "cwd": str(repo)}, "q must be at most 200 characters"),
                ({"q": "ok", "limit": "0", "cwd": str(repo)}, "limit must be 1..100"),
                ({"q": "ok", "limit": "many", "cwd": str(repo)}, "limit must be an integer"),
            ):
                resp = await client.get("/api/beads/text-search", params=params, headers=BEARER)
                doc = await resp.json()
                assert resp.status == 400, (params, resp.status, doc)
                assert why in doc["error"], (params, doc)
        finally:
            await client.close()

    _run(run)


def test_no_board_for_the_cwd(tmp_path, repo):
    fake = FakeBr()
    # A root with no .beads in it: whatever directory is asked about, there
    # is no board behind it.
    board = _board(fake, tmp_path / "elsewhere")

    async def run():
        client = await _serve(tmp_path, board)
        try:
            resp = await client.get(
                "/api/beads/text-search",
                params={"q": "x", "cwd": str(tmp_path / "elsewhere")},
                headers=BEARER,
            )
            assert resp.status == 404, await resp.json()
        finally:
            await client.close()

    _run(run)
