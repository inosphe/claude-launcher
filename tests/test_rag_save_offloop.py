"""Writing the vector index must not stop the daemon's event loop.

``VectorIndex.save`` serialises the whole index -- every document, every
vector, base64-encoded -- and writes it. ``_sync`` called it directly, on
the loop, after every embed group, and the group size is ``batch`` chunks
(16 by default), so one pass over a changed corpus rewrote the whole file
hundreds of times without ever yielding.

The file it rewrites is not small. Measured on the live daemon (s586,
2026-09-21): ``~/.claude-launcher/daemon/rag/all.json`` is 376,519,999
bytes. A py-spy sample of that daemon put 35.4% of the event loop thread's
time under ``_sync -> _embed_group -> save``, 19.0% of it inside
``json.dumps`` and 13.3% inside ``write_text``. Over the same window a
0.5 KB endpoint answered with a p99 of 2507 ms and a maximum of 6551 ms,
and ``daemon_client.ensure_running`` reported a live daemon as not running.

Terminal input rides that loop. So these pin two rules: the serialisation
and the write happen off the loop, and a pass does not rewrite the file
once per group.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from array import array

import pytest

from claude_launcher.daemon import rag

from test_rag import (
    Endpoint,
    FakeBr,
    _board,
    _cfg,
    _issues,
    _start_endpoint,
    repo,  # noqa: F401 -- fixture
)


def _index(tmp_path, *, docs: int = 8, dims: int = 64) -> rag.VectorIndex:
    """An index big enough that serialising it is measurable work."""
    index = rag.VectorIndex(tmp_path / "idx.json", model="m", dims=dims)
    for i in range(docs):
        index.entries[f"d-{i}"] = rag.Entry(
            f"h{i}", [array("f", [0.1] * dims) for _ in range(4)], {"title": f"t{i}"}
        )
    return index


def test_the_write_happens_off_the_event_loop(tmp_path):
    """The thread that serialises and writes is not the loop's thread."""

    async def run():
        index = _index(tmp_path)
        here = threading.current_thread()
        seen = []
        real = rag.VectorIndex.save

        def watched(self):
            seen.append(threading.current_thread())
            return real(self)

        rag.VectorIndex.save = watched
        try:
            await index.save_soon()
        finally:
            rag.VectorIndex.save = real
        assert seen, "the index was never written"
        assert seen[0] is not here, "the write ran on the event loop"

    asyncio.run(run())


def test_the_loop_keeps_answering_while_the_index_is_written(tmp_path):
    """Said as the property: a write in flight does not cost the loop its
    turn. The heartbeat is what a terminal socket would be."""

    async def run():
        index = _index(tmp_path, docs=200, dims=256)
        ticks = []
        stop = asyncio.Event()

        async def heartbeat():
            while not stop.is_set():
                t = time.perf_counter()
                await asyncio.sleep(0.005)
                ticks.append((time.perf_counter() - t) * 1000)

        beat = asyncio.ensure_future(heartbeat())
        await index.save_soon()
        stop.set()
        await beat
        assert ticks, "the heartbeat never ran: the loop was held for the whole write"
        assert max(ticks) < 250, f"the loop stalled for {max(ticks):.0f} ms"

    asyncio.run(run())


def test_the_file_written_is_the_one_a_fresh_index_reads_back(tmp_path):
    """Off the loop is not a different file: the round trip is unchanged."""

    async def run():
        index = _index(tmp_path, docs=3, dims=8)
        await index.save_soon()
        again = rag.VectorIndex(tmp_path / "idx.json", model="m", dims=8)
        again.load()
        assert set(again.entries) == set(index.entries)
        assert again.entries["d-1"].hash == "h1"
        assert len(again.entries["d-1"].vecs) == 4

    asyncio.run(run())


def test_a_sync_does_not_rewrite_the_file_for_every_group(tmp_path, repo):  # noqa: F811
    """Three issues, one chunk each, ``batch`` of 1: three groups. The file
    is written once for the pass, not once per group.

    Off the loop is not enough by itself -- a 376 MB serialisation still
    holds the interpreter while it runs, and hundreds of them in a row cost
    the loop its share of it.
    """
    ep = Endpoint()
    br = FakeBr(_issues())

    async def run():
        server = await _start_endpoint(ep)
        try:
            cfg = _cfg(server, batch=1)
            svc = rag.RagService(
                board=_board(br, repo), config=lambda: cfg, root_dir=tmp_path / "rag"
            )
            writes = []
            real = rag.VectorIndex.save

            def counted(self):
                writes.append(str(self.path))
                return real(self)

            rag.VectorIndex.save = counted
            try:
                prog = svc.ensure_sync("beads", repo)
                await svc.wait_sync(prog, 10)
            finally:
                rag.VectorIndex.save = real
            assert prog.indexed == 3 and prog.pending == 0
            assert len(writes) <= 1, f"the index was rewritten {len(writes)} times"
        finally:
            await server.close()

    asyncio.run(run())


def test_what_the_pass_embedded_is_on_disk_when_it_ends(tmp_path, repo):  # noqa: F811
    """Saving less often may not mean saving less: the pass ends with the
    file carrying every document it embedded."""
    ep = Endpoint()
    br = FakeBr(_issues())

    async def run():
        server = await _start_endpoint(ep)
        try:
            cfg = _cfg(server, batch=1)
            svc = rag.RagService(
                board=_board(br, repo), config=lambda: cfg, root_dir=tmp_path / "rag"
            )
            prog = svc.ensure_sync("beads", repo)
            await svc.wait_sync(prog, 10)
            files = list((tmp_path / "rag").glob("beads-*.json"))
            assert len(files) == 1
            data = json.loads(files[0].read_text(encoding="utf-8"))
            assert set(data["docs"]) == {"x-1", "x-2", "x-3"}
        finally:
            await server.close()

    asyncio.run(run())


def test_a_pass_that_fails_midway_keeps_what_it_embedded(tmp_path, repo):  # noqa: F811
    """The reason the old code saved after every group: an interrupted sync
    must not throw away its work. Saving once per pass keeps that, because
    the failure path saves too."""
    ep = Endpoint()
    br = FakeBr(_issues())

    async def run():
        server = await _start_endpoint(ep)
        try:
            cfg = _cfg(server, batch=1)
            svc = rag.RagService(
                board=_board(br, repo), config=lambda: cfg, root_dir=tmp_path / "rag"
            )
            groups = {"n": 0}
            real_embed = rag.RagClient.embed

            async def fail_on_the_third(self, texts):
                groups["n"] += 1
                if groups["n"] >= 3:
                    raise rag.RagError("the endpoint went away")
                return await real_embed(self, texts)

            rag.RagClient.embed = fail_on_the_third
            try:
                prog = svc.ensure_sync("beads", repo)
                await svc.wait_sync(prog, 10)
            finally:
                rag.RagClient.embed = real_embed
            assert prog.error, "the premise: this pass failed"
            files = list((tmp_path / "rag").glob("beads-*.json"))
            assert len(files) == 1
            data = json.loads(files[0].read_text(encoding="utf-8"))
            assert data["docs"], "the pass threw away the documents it had embedded"
        finally:
            await server.close()

    asyncio.run(run())
