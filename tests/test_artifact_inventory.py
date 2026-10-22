"""The Artifacts page's daemon side: the listing and the size cache.

``artifact_inventory`` is a reading of what :mod:`artifact_cleanup` would
consider, so the tests hold it to the cleanup module's own rules rather than
to a second copy of them: ownership comes from ``basetemp_reaper``, the
scratch candidate rule from ``artifact_cleanup``, and the two must agree on
which directory belongs to which session.

The size cache is checked for the two failures a cache has: a number that
outlives the tree it measured (``stale``), and keys that accumulate after the
paths are gone (``retain``).
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher.daemon import artifact_cleanup_api
from claude_launcher.daemon import artifact_inventory as inventory
from claude_launcher.daemon import basetemp_reaper


def session(name, cwd, **flags):
    return SimpleNamespace(
        sdef=SimpleNamespace(name=name, cwd=str(cwd)),
        exited=flags.get("exited", False),
        held_at=flags.get("held_at"),
        archived_at=flags.get("archived_at"),
        paused_at=flags.get("paused_at"),
        failed_at=flags.get("failed_at"),
        delayed_at=flags.get("delayed_at"),
    )


@pytest.fixture
def place(tmp_path, monkeypatch):
    """A launcher home's artifact roots under ``tmp_path``, nothing real."""
    root = tmp_path / "basetemp"
    root.mkdir()
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    checkout = tmp_path / "work"
    checkout.mkdir()
    monkeypatch.setattr(basetemp_reaper, "default_root", lambda: root)
    monkeypatch.setattr(inventory.paths, "session_scratch_dir",
                        lambda name: sessions / name / "scratch")
    monkeypatch.setattr(inventory.paths, "session_dir", lambda name: sessions / name)
    monkeypatch.setattr(inventory.cleanup, "_foreign_names", lambda: set())
    monkeypatch.setattr(inventory.cleanup, "_registered_worktrees", lambda repo: set())
    monkeypatch.setattr(inventory, "usage_path",
                        lambda: tmp_path / "artifact-usage.json")
    records = {
        "s1": session("s1", checkout),
        "s10": session("s10", checkout),
        "s2": session("s2", checkout),
    }
    manager = SimpleNamespace(list=lambda: list(records.values()),
                              get=lambda name: records[name])
    return SimpleNamespace(root=root, sessions=sessions, checkout=checkout,
                           manager=manager, records=records, tmp=tmp_path)


def entry(path: Path, size: int = 10) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "payload").write_bytes(b"x" * size)
    return path


def by_name(document: dict, name: str) -> dict:
    return next(row for row in document["sessions"] if row["name"] == name)


def test_owned_basetemps_and_orphans_are_told_apart(place):
    mine = entry(place.root / "s1c1012000000")
    sweep = entry(place.root / "s1w")
    test_dir = entry(place.root / "s1")
    other = entry(place.root / "s10c1012000000")
    orphan = entry(place.root / "nobodyc1012000000")

    document = inventory.inventory(place.manager)

    items = {row["name"]: row for row in by_name(document, "s1")["items"]}
    assert set(items) == {"s1c1012000000", "s1w", "s1"}
    assert items["s1c1012000000"]["kind"] == "run"
    assert items["s1w"]["kind"] == "sweep"
    assert items["s1"]["kind"] == "test-dir"
    assert str(mine) == items["s1c1012000000"]["path"]
    # A name is claimed by the longest session that matches it, so "s10"'s
    # tree is never listed under "s1".
    assert all(row["name"] != "s10c1012000000"
               for row in by_name(document, "s1")["items"])
    assert [row["name"] for row in by_name(document, "s10")["items"]] == ["s10c1012000000"]
    assert str(other) in {row["path"] for row in by_name(document, "s10")["items"]}
    assert [row["path"] for row in document["orphans"]] == [str(orphan)]
    assert document["orphans"][0]["reason"] == "no registry session owns this name"
    # Nothing was measured: the listing never walks a tree.
    assert items["s1c1012000000"].get("bytes") is None
    assert str(sweep) and str(test_dir)


def test_scratch_lists_only_the_candidates_cleanup_would_take(place):
    scratch = place.sessions / "s1" / "scratch"
    entry(scratch / "pytest-of-me")
    entry(scratch / "browser")
    entry(scratch / "s1-preview-r2")
    entry(scratch / "notes")
    entry(scratch / "s1-other")

    document = inventory.inventory(place.manager)

    rows = [row for row in by_name(document, "s1")["items"] if row["area"] == "scratch"]
    assert sorted(row["name"] for row in rows) == ["browser", "pytest-of-me", "s1-preview-r2"]
    kinds = {row["name"]: row["kind"] for row in rows}
    assert kinds["browser"] == "browser"
    assert kinds["pytest-of-me"] == "pytest"
    assert kinds["s1-preview-r2"] == "preview"


def test_a_live_session_inside_an_artifact_marks_it_excluded(place):
    # A preview worktree a live session stands in: cleanup skips it, and the
    # page has to say so rather than offer a size that implies it can go.
    inside = entry(place.sessions / "s1" / "scratch" / "s1-preview-r2")
    place.records["s1"] = session("s1", inside)

    document = inventory.inventory(place.manager)
    row = next(row for row in by_name(document, "s1")["items"]
               if row["path"] == str(inside))
    assert row["reason"] == "a live or held session uses this directory"


def test_foreign_reservation_is_reported_on_the_owned_tree(place, monkeypatch):
    entry(place.root / "s1w")
    monkeypatch.setattr(inventory.cleanup, "_foreign_names", lambda: {"s1"})
    document = inventory.inventory(place.manager)
    row = next(row for row in by_name(document, "s1")["items"] if row["name"] == "s1w")
    assert row["reason"] == "other daemon owns matching name"


def test_cleanup_schedule_and_receipts_are_read(place):
    folder = place.sessions / "s1" / "cleanup"
    folder.mkdir(parents=True)
    (folder / "scheduled.json").write_text(json.dumps({
        "session": "s1", "created_at": 1000.0, "expires_at": 2000.0,
        "attempts": 3, "last_reason": "measurement window unavailable",
    }), encoding="utf-8")
    (folder / "plan1.json").write_text(json.dumps({
        "plan_id": "plan1", "session": "s1", "created_at": 1500.0,
        "items": [
            {"path": "/x", "status": "removed", "bytes": 2048},
            {"path": "/y", "status": "cleanup_pending", "reason": "changed"},
        ],
    }), encoding="utf-8")

    state = by_name(inventory.inventory(place.manager), "s1")["cleanup"]
    assert state["scheduled"]["attempts"] == 3
    assert state["scheduled"]["last_reason"] == "measurement window unavailable"
    assert state["scheduled"]["created_at"].startswith("1970-01-01")
    assert state["receipts"][0]["plan_id"] == "plan1"
    assert (state["receipts"][0]["removed"], state["receipts"][0]["cleanup_pending"]) == (1, 1)
    assert state["receipts"][0]["bytes"] == 2048


def test_measure_fills_the_cache_and_a_later_write_reads_stale(place):
    tree = entry(place.root / "s1w", size=100)
    entry(tree / "nested", size=23)
    document = inventory.inventory(place.manager)
    assert str(tree) in document["measurable"]

    assert inventory.measure([str(tree)]) == 1
    row = next(r for s in inventory.inventory(place.manager)["sessions"]
               for r in s["items"] if r["path"] == str(tree))
    assert row["bytes"] == 123 and row["files"] == 2
    assert row["stale"] is False

    entry(tree / "later", size=1)
    os.utime(tree, None)
    row = next(r for s in inventory.inventory(place.manager)["sessions"]
               for r in s["items"] if r["path"] == str(tree))
    assert row["stale"] is True


def test_measure_skips_cached_rows_unless_forced(place):
    tree = entry(place.root / "s1w", size=5)
    inventory.measure([str(tree)])
    assert inventory.measure([str(tree)]) == 0
    assert inventory.measure([str(tree)], force=True) == 1


def test_retain_drops_the_paths_the_listing_no_longer_has(place):
    kept = entry(place.root / "s1w")
    gone = entry(place.root / "s2w")
    inventory.measure([str(kept), str(gone)])
    inventory.retain([str(kept)])
    assert set(inventory.load()) == {str(kept)}


def test_sizes_total_without_counting_scratch_entries_twice(place):
    scratch = place.sessions / "s1" / "scratch"
    entry(scratch / "pytest-one", size=40)
    entry(place.root / "s1w", size=100)
    inventory.measure([str(scratch), str(place.root / "s1w")])

    totals = by_name(inventory.inventory(place.manager), "s1")["totals"]
    # 100 (basetemp) + 40 (the scratch directory as a whole); the pytest
    # entry inside it is a row of its own and is not added again.
    assert totals["bytes"] == 140
    assert totals["measured"] == 2


def test_a_linked_worktree_shares_the_main_checkouts_git_reading(place):
    """One ``git worktree list`` per repository, not per worktree.

    The page's own listing is where this was measured: 417 subprocesses
    (6.6s) when every session with a scratch directory ran its own, because
    a launcher worktree is a linked checkout of the same repository.
    """
    main = place.tmp / "repo"
    (main / ".git").mkdir(parents=True)
    linked = place.tmp / "checks" / "s1"
    linked.mkdir(parents=True)
    (linked / ".git").write_text(
        f"gitdir: {main / '.git' / 'worktrees' / 's1'}\n", encoding="utf-8")

    assert inventory._checkout_root(str(main)) == str(main)
    assert inventory._checkout_root(str(linked)) == str(main)
    assert inventory._checkout_root(str(place.tmp / "nowhere")) is None
    # An unrecognised gitdir layout falls back to the checkout itself rather
    # than inventing a root.
    odd = place.tmp / "checks" / "s2"
    odd.mkdir()
    (odd / ".git").write_text(f"gitdir: {place.tmp / 'elsewhere'}\n", encoding="utf-8")
    assert inventory._checkout_root(str(odd)) == str(odd)


def test_scratch_entry_count_rides_with_the_scratch_row(place):
    scratch = place.sessions / "s1" / "scratch"
    entry(scratch / "pytest-one")
    entry(scratch / "notes")
    row = by_name(inventory.inventory(place.manager), "s1")["scratch"]
    assert row["exists"] is True
    assert row["entries"] == 2


def test_the_api_lists_and_refuses_client_supplied_paths(place):
    tree = entry(place.root / "s1w", size=8)

    async def scenario():
        app = web.Application()
        app["manager"] = place.manager
        artifact_cleanup_api.install(app)
        async with TestClient(TestServer(app)) as client:
            response = await client.get("/api/artifacts")
            assert response.status == 200
            document = await response.json()
            assert {row["name"] for row in document["sessions"]} == {"s1", "s10", "s2"}
            assert str(tree) in document["measurable"]

            refused = await client.post("/api/artifacts/measure",
                                        json={"paths": ["/etc"]})
            assert refused.status == 400
            assert "paths are not accepted" in (await refused.json())["error"]

            accepted = await client.post("/api/artifacts/measure", json={})
            assert accepted.status == 202
            assert (await accepted.json())["paths"] == len(document["measurable"])
            # The walk is the daemon's; wait for it so the cached answer is
            # there when the page asks next.
            job = app["artifact_usage"]["task"]
            if job is not None:
                await job
            document = await (await client.get("/api/artifacts")).json()
            row = next(r for s in document["sessions"] for r in s["items"]
                       if r["path"] == str(tree))
            assert row["bytes"] == 8

            unknown = await client.post("/api/artifacts/measure",
                                        json={"session": "nope"})
            assert unknown.status == 400
            bad_force = await client.post("/api/artifacts/measure",
                                          json={"force": "yes"})
            assert bad_force.status == 400

    asyncio.run(scenario())


def test_the_page_is_wired_into_the_dashboard():
    """The nav link, the section, the script and all three route hooks."""
    web_dir = Path(__file__).resolve().parent.parent / "src" / "claude_launcher" / "web" / "static"
    html = (web_dir / "index.html").read_text(encoding="utf-8")
    app_js = (web_dir / "app.js").read_text(encoding="utf-8")
    assert 'href="#/artifacts" data-page="artifacts"' in html
    assert 'id="artifacts-view"' in html
    assert 'src="static/artifacts-page.js"' in html
    # Next to the Worktrees button, not somewhere else in the rail.
    assert html.index('data-page="worktrees"') < html.index('data-page="artifacts"')
    assert 'artifacts: "artifacts-view"' in app_js
    assert 'if (parts[0] === "artifacts") return { page: "artifacts" };' in app_js
    assert 'globalThis.ArtifactsPage.open()' in app_js
    assert 'globalThis.ArtifactsPage?.stop()' in app_js
