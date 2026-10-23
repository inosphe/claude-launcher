"""One VectorIndex under parallel readers and a writer (claunch-d18v8).

The daemon writes an index from a worker thread while searches rank and scan
it from others. On a GIL build the threads interleave; on a free-threaded one
they run at the same time, so a read of ``entries`` beside a ``put`` or
``drop`` has to hold up by itself. This drives that mix and expects no
exception and a final state that matches what was written.
"""

from __future__ import annotations

import threading

import pytest

pytest.importorskip("zvec")

from claude_launcher.daemon import rag  # noqa: E402


def _vec(i: int, dims: int = 8):
    v = [0.0] * dims
    v[i % dims] = 1.0
    v[(i * 3 + 1) % dims] += 0.5
    return rag.unit(v)


def test_readers_and_a_writer_share_one_index(tmp_path):
    index = rag.VectorIndex(tmp_path / "idx.zvec", model="m", dims=8)
    index.load()
    index.put(rag.Doc("seed", "h", ["seed"], {"title": "seed"}), [_vec(0)])

    docs = 120
    errors: list = []
    stop = threading.Event()

    def guarded(fn):
        def run():
            try:
                while not stop.is_set():
                    fn()
            except BaseException as exc:  # noqa: BLE001 - the test reports any
                errors.append(exc)
                stop.set()
        return run

    def read_rank():
        index.rank(_vec(2), 5)

    def read_lexical():
        index.lexical("doc", 10)
        index.ids()
        index.items()

    def read_diff():
        index.diff([rag.Doc("seed", "h", ["seed"], {}), rag.Doc("zzz", "z", ["z"], {})])

    def read_neighbors():
        index.neighbors("seed", 3)

    readers = [threading.Thread(target=guarded(f), daemon=True)
               for f in (read_rank, read_rank, read_lexical, read_lexical, read_diff, read_neighbors)]
    for t in readers:
        t.start()
    try:
        for i in range(docs):
            index.put(rag.Doc(f"d{i}", f"h{i}", [f"doc {i}"], {"title": f"doc {i}"}), [_vec(i)])
            if i % 7 == 0:
                index.refresh_meta(rag.Doc(f"d{i}", f"h{i}", [f"doc {i}"], {"title": f"doc {i} renamed"}))
            if i % 5 == 4:
                index.drop(f"d{i - 2}")
    finally:
        stop.set()
        for t in readers:
            t.join(10)
    assert not errors, errors[:3]
    assert not any(t.is_alive() for t in readers)

    dropped = {f"d{i - 2}" for i in range(docs) if i % 5 == 4}
    assert index.ids() == {"seed"} | {f"d{i}" for i in range(docs)} - dropped
    ranked = index.rank(_vec(3), 5)
    assert ranked and all(doc_id in index.ids() for doc_id, _ in ranked)
    index.save()
    index.close()
