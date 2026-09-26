"""Opening a corpus's index must not stop the daemon's event loop.

``RagService._index`` opened the zvec collection and read its fields on the
loop. After a restart that held the loop for 3.8 s (claunch-fh8u1.1: 189
py-spy samples of pid 67196 under ``VectorIndex._open`` > ``zvec.open``), and
every HTTP answer and terminal socket waited for it. zvec releases the GIL
while it opens -- on a copy of the live ``sessions`` collection (1.9 GB) the
open took 0.6-0.9 s in a thread and the main thread's longest gap meanwhile
was 3.3 ms -- so the open moves to a worker thread. What these pin:

- the loop keeps running callbacks while an index loads;
- two callers of one key share one open (the collection's lock is
  exclusive, a second open of the directory is refused);
- a caller cancelled while it waits still leaves the index registered;
- shutdown waits for an open in flight and closes what it opened.
"""

from __future__ import annotations

import asyncio
import threading

from claude_launcher.daemon import rag

CFG = {"base_url": "http://embed.invalid/v1", "api_key": "k", "embedding_model": "emb",
       "dimensions": 0}


class _SlowLoad:
    """Stands in for ``VectorIndex.load``: blocks its thread until released,
    and records which thread ran it and how many times."""

    def __init__(self):
        self.release = threading.Event()
        self.started = threading.Event()
        self.threads = []
        self.closed = []

    def install(self, monkeypatch):
        gate = self

        def load(index):
            gate.threads.append(threading.get_ident())
            gate.started.set()
            gate.release.wait(5)
            index._loaded = True

        def close(index):
            gate.closed.append(index)

        monkeypatch.setattr(rag.VectorIndex, "load", load)
        monkeypatch.setattr(rag.VectorIndex, "close", close)
        return self


def _service(tmp_path):
    return rag.RagService(config=lambda: CFG, root_dir=tmp_path / "rag")


async def _started(gate):
    await asyncio.wait_for(asyncio.to_thread(gate.started.wait, 5), 5)


def test_the_loop_runs_while_an_index_loads(tmp_path, monkeypatch):
    gate = _SlowLoad().install(monkeypatch)

    async def run():
        svc = _service(tmp_path)
        opening = asyncio.ensure_future(svc._index("sessions", None, CFG))
        await _started(gate)
        ticks = 0
        for _ in range(5):
            await asyncio.sleep(0.01)
            ticks += 1
        assert ticks == 5 and not opening.done(), "the loop ran while the load was blocked"
        gate.release.set()
        index = await asyncio.wait_for(opening, 5)
        assert gate.threads == [gate.threads[0]] and gate.threads[0] != threading.get_ident()
        assert svc._indexes["sessions"] is index
        assert await svc._index("sessions", None, CFG) is index, "a loaded index is reused"
        assert len(gate.threads) == 1

    asyncio.run(run())


def test_two_callers_share_one_open(tmp_path, monkeypatch):
    gate = _SlowLoad().install(monkeypatch)

    async def run():
        svc = _service(tmp_path)
        first = asyncio.ensure_future(svc._index("sessions", None, CFG))
        await _started(gate)
        second = asyncio.ensure_future(svc._index("sessions", None, CFG))
        await asyncio.sleep(0.02)
        gate.release.set()
        a, b = await asyncio.wait_for(asyncio.gather(first, second), 5)
        assert a is b
        assert len(gate.threads) == 1, "the directory was opened once"

    asyncio.run(run())


def test_a_cancelled_caller_leaves_the_index_registered(tmp_path, monkeypatch):
    gate = _SlowLoad().install(monkeypatch)

    async def run():
        svc = _service(tmp_path)
        caller = asyncio.ensure_future(svc._index("sessions", None, CFG))
        await _started(gate)
        caller.cancel()
        await asyncio.sleep(0)
        assert caller.cancelled() or caller.done()
        # A caller arriving now joins the open still in flight.
        later = asyncio.ensure_future(svc._index("sessions", None, CFG))
        gate.release.set()
        index = await asyncio.wait_for(later, 5)
        assert svc._indexes["sessions"] is index and not svc._loading
        assert len(gate.threads) == 1

    asyncio.run(run())


def test_shutdown_waits_for_an_open_and_closes_it(tmp_path, monkeypatch):
    gate = _SlowLoad().install(monkeypatch)

    async def run():
        svc = _service(tmp_path)
        caller = asyncio.ensure_future(svc._index("sessions", None, CFG))
        await _started(gate)
        stopping = asyncio.ensure_future(svc.shutdown())
        await asyncio.sleep(0.02)
        assert not stopping.done(), "shutdown waits for the open"
        gate.release.set()
        await asyncio.wait_for(stopping, 5)
        assert not svc._indexes and not svc._loading
        assert len(gate.closed) == 1, "the collection the open made was closed"
        try:
            await caller
        except rag.RagError:
            pass
        else:  # pragma: no cover - the open finished after shutdown began
            raise AssertionError("an open that ends after shutdown is not handed out")

    asyncio.run(run())
