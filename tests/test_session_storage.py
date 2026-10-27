"""A session's scratch storage: the walk, its cache, and the endpoint.

The dashboard asks for this number on a rail that polls once a second, so the
tests that matter are the ones about *not* measuring: a second request inside
the TTL must not walk again, two simultaneous refreshes must walk once, and a
link inside the tree must not be followed -- following one would count another
tree's bytes as this session's, which is a wrong number rather than a slow one.
"""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import time
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher.daemon import api, storage as storage_mod
from claude_launcher.daemon.manager import ManagerError


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A scratch directory per session name, rooted in ``tmp_path``."""
    sessions = tmp_path / "sessions"
    monkeypatch.setattr(
        storage_mod.paths, "session_scratch_dir",
        lambda name: sessions / name / "scratch",
    )
    storage_mod.STORAGE.reset()
    yield sessions
    storage_mod.STORAGE.reset()


def make_app(tree):
    for name in ("s1", "s2"):
        (tree / name / "scratch").mkdir(parents=True, exist_ok=True)

    def get(name):
        if name not in ("s1", "s2"):
            raise ManagerError(f"no session named {name!r}")
        return SimpleNamespace(name=name)

    app = web.Application()
    app["manager"] = SimpleNamespace(get=get)
    app.router.add_get("/api/sessions/{name}/storage", api.h_session_storage)
    return app


def storage_json(client, name, *, refresh=False):
    async def call():
        query = "?refresh=1" if refresh else ""
        resp = await client.get(f"/api/sessions/{name}/storage{query}")
        assert resp.status == 200, await resp.text()
        return await resp.json()
    return call


def write(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def count_walks(monkeypatch):
    """Wrap ``storage.scan`` so a test can count how often it really ran."""
    seen: list[str] = []
    real = storage_mod.scan

    def counting(name):
        seen.append(name)
        return real(name)

    monkeypatch.setattr(storage_mod, "scan", counting)
    return seen


# -- the walk ---------------------------------------------------------------

def test_scan_totals_and_top_level_entries(tree):
    scratch = tree / "s1" / "scratch"
    write(scratch / "a.txt", 10)
    write(scratch / "big" / "one.bin", 1000)
    write(scratch / "big" / "nested" / "two.bin", 500)
    write(scratch / "small" / "three.bin", 7)

    doc = storage_mod.scan("s1")

    assert doc["bytes"] == 1517
    assert doc["files"] == 4
    assert doc["dirs"] == 3          # big, big/nested, small
    assert doc["error"] is None
    assert doc["truncated"] is False
    assert [row["path"] for row in doc["entries"]] == ["big", "a.txt", "small"]
    assert doc["entries"][0] == {"path": "big", "bytes": 1500, "files": 2}
    assert doc["entries"][1] == {"path": "a.txt", "bytes": 10, "files": 1}


def test_scan_does_not_follow_links(tree, tmp_path):
    outside = tmp_path / "elsewhere"
    write(outside / "huge.bin", 5000)
    scratch = tree / "s1" / "scratch"
    write(scratch / "inside.bin", 3)
    os.symlink(outside, scratch / "linked-dir")
    os.symlink(outside / "huge.bin", scratch / "linked-file")

    doc = storage_mod.scan("s1")

    # Only what is really inside the tree counts; the links and their targets
    # are outside this session's bytes.
    assert doc["bytes"] == 3
    assert doc["files"] == 1
    assert [row["path"] for row in doc["entries"]] == ["inside.bin"]


def test_scan_of_a_missing_scratch_is_zeroes(tree):
    doc = storage_mod.scan("never-wrote-anything")
    assert (doc["bytes"], doc["files"], doc["dirs"]) == (0, 0, 0)
    assert doc["entries"] == []
    assert doc["error"] is None


def test_scan_of_a_linked_scratch_is_refused(tree, tmp_path):
    real = tmp_path / "not-mine"
    write(real / "data.bin", 100)
    (tree / "s1").mkdir(parents=True)
    os.symlink(real, tree / "s1" / "scratch")

    doc = storage_mod.scan("s1")

    assert doc["bytes"] == 0
    assert doc["error"]


# -- the endpoint -----------------------------------------------------------

def test_endpoint_caches_within_ttl(tree, monkeypatch):
    walks = count_walks(monkeypatch)
    write(tree / "s1" / "scratch" / "a.bin", 12)

    async def scenario():
        async with TestClient(TestServer(make_app(tree))) as client:
            first = await storage_json(client, "s1")()
            second = await storage_json(client, "s1")()
            assert first["bytes"] == 12 == second["bytes"]
            assert first["stale"] is False and second["stale"] is False
    asyncio.run(scenario())
    assert walks == ["s1"], "the second request must not walk again"


def test_refresh_measures_again(tree, monkeypatch):
    walks = count_walks(monkeypatch)
    scratch = tree / "s1" / "scratch"
    write(scratch / "a.bin", 12)

    async def scenario():
        async with TestClient(TestServer(make_app(tree))) as client:
            await storage_json(client, "s1")()
            write(scratch / "b.bin", 30)
            doc = await storage_json(client, "s1", refresh=True)()
            assert doc["bytes"] == 42, "refresh must re-walk, not replay the cache"
            assert doc["stale"] is False
    asyncio.run(scenario())
    assert walks == ["s1", "s1"]


def test_a_stale_measurement_answers_at_once_and_says_so(tree, monkeypatch):
    monkeypatch.setattr(storage_mod.STORAGE, "ttl", 0.0)
    walks = count_walks(monkeypatch)
    write(tree / "s1" / "scratch" / "a.bin", 5)

    async def scenario():
        async with TestClient(TestServer(make_app(tree))) as client:
            await storage_json(client, "s1")()
            await asyncio.sleep(0.02)
            doc = await storage_json(client, "s1")()
            assert doc["stale"] is True
            assert doc["bytes"] == 5, "the standing number is returned, not a fresh walk"
            # ...and the refresh it queued runs behind the answer.
            for _ in range(100):
                if len(walks) > 1:
                    break
                await asyncio.sleep(0.02)
            assert len(walks) == 2
    asyncio.run(scenario())


def test_simultaneous_refreshes_walk_once(tree, monkeypatch):
    real = storage_mod.scan
    calls = []

    def counting(name):
        # Slow enough that the second request is really inside the walk when
        # it arrives; a walk that finishes first is not an overlap, and would
        # make this test measure the scheduler instead of the single flight.
        calls.append(name)
        time.sleep(0.15)
        return real(name)

    monkeypatch.setattr(storage_mod, "scan", counting)
    write(tree / "s1" / "scratch" / "a.bin", 9)

    async def scenario():
        async with TestClient(TestServer(make_app(tree))) as client:
            first = asyncio.ensure_future(storage_json(client, "s1", refresh=True)())
            second = asyncio.ensure_future(storage_json(client, "s1", refresh=True)())
            bodies = await asyncio.gather(first, second)
            assert [b["bytes"] for b in bodies] == [9, 9]
    asyncio.run(scenario())
    assert calls == ["s1"], "overlapping asks share one walk"


def test_unknown_session_is_404(tree):
    async def scenario():
        async with TestClient(TestServer(make_app(tree))) as client:
            resp = await client.get("/api/sessions/nope/storage")
            assert resp.status == 404
            assert "no session named" in (await resp.json())["error"]
    asyncio.run(scenario())


def test_session_without_scratch_answers_zeroes(tree):
    async def scenario():
        async with TestClient(TestServer(make_app(tree))) as client:
            (tree / "s2" / "scratch").rmdir()
            resp = await client.get("/api/sessions/s2/storage")
            doc = await resp.json()
            assert doc["session"] == "s2"
            assert doc["root"] == str(tree / "s2" / "scratch")
            assert (doc["bytes"], doc["files"], doc["dirs"]) == (0, 0, 0)
            assert doc["entries"] == [] and doc["error"] is None
            assert doc["stale"] is False and doc["truncated"] is False
            assert doc["scanned_at"]
            json.dumps(doc)   # the body is JSON, not a Python repr
    asyncio.run(scenario())


def test_attach_reads_the_cache_and_never_walks(tree, monkeypatch):
    walks = count_walks(monkeypatch)
    write(tree / "s1" / "scratch" / "a.bin", 64)

    row: dict = {}
    assert storage_mod.attach(row, "s1") is True      # nothing measured yet
    assert "storage" not in row and walks == []
    asyncio.run(storage_mod.STORAGE.measure("s1"))

    row = {}
    assert storage_mod.attach(row, "s1") is False
    assert row["storage"]["bytes"] == 64
    assert row["storage"]["files"] == 1
    assert walks == ["s1"]


def test_attach_marks_an_old_measurement_for_a_new_walk(tree, monkeypatch):
    write(tree / "s1" / "scratch" / "a.bin", 8)
    asyncio.run(storage_mod.STORAGE.measure("s1"))
    row: dict = {}
    assert storage_mod.attach(row, "s1") is False
    monkeypatch.setattr(storage_mod.STORAGE, "ttl", 0.0)
    row = {}
    assert storage_mod.attach(row, "s1") is True, "an old number still rides, and says so"
    assert row["storage"]["bytes"] == 8


def test_a_walk_that_hits_its_depth_budget_says_truncated(tree, monkeypatch):
    write(tree / "s1" / "scratch" / "a" / "b" / "c" / "deep.bin", 40)
    write(tree / "s1" / "scratch" / "top.bin", 4)
    monkeypatch.setattr(storage_mod, "MAX_DEPTH", 1)

    doc = storage_mod.scan("s1")

    assert doc["truncated"] is True
    assert doc["bytes"] == 4, "only what the walk reached is counted"
    assert [row["path"] for row in doc["entries"]] == ["top.bin"]


# -- the rail's short form --------------------------------------------------

def test_storage_size_renders_1024_units():
    src = (Path(__file__).resolve().parents[1] / "src" / "claude_launcher"
           / "web" / "static" / "app.js")
    js = f"""
const src = require("fs").readFileSync({json.dumps(str(src))}, "utf8");
function slice(name) {{
  const start = src.indexOf(`function ${{name}}(`);
  let depth = 0;
  for (let j = src.indexOf("{{", start); j < src.length; j++) {{
    if (src[j] === "{{") depth++;
    else if (src[j] === "}}") {{ depth--; if (!depth) return src.slice(start, j + 1); }}
  }}
  throw new Error("cannot locate " + name);
}}
eval(slice("storageSize"));
const cases = [[0, ""], [1, "1 B"], [1023, "1023 B"], [1024, "1.0 KB"],
               [1536, "1.5 KB"], [82 * 1024 * 1024, "82 MB"],
               [12 * 1024 * 1024 * 1024, "12 GB"], [-5, ""]];
for (const [n, want] of cases) {{
  const got = storageSize(n);
  if (got !== want) {{ console.error(n, "->", JSON.stringify(got), "want", JSON.stringify(want)); process.exit(1); }}
}}
console.log("ok");
"""
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr or out.stdout
