"""A transient failure while creating a zvec collection must not poison the path.

On Windows the engine's id map renames ``000005.dbtmp`` to ``CURRENT`` in the
new directory, and that rename fails with "access denied" when another process
holds a handle on the file for a moment (claunch-etcs6: 3 failures in 1200
creates with 8 processes on separate paths). The failed create leaves the
directory behind, and the engine then refuses a create (the path exists) and an
open (no version file), so every later test or request on that path failed with
``KeyError: Failed to find the version file`` until the directory was removed.
"""

from __future__ import annotations

from array import array
from pathlib import Path

import pytest

from claude_launcher.daemon import rag


def _put(index: rag.VectorIndex, dims: int = 8) -> None:
    index.put(rag.Doc("d-1", "h1", [], {"title": "t"}), [array("f", [0.1] * dims)])


def _flaky_create(monkeypatch, failures: int):
    """Make ``create_and_open`` fail ``failures`` times the way the engine does."""
    zvec = rag._zvec()
    real = zvec.create_and_open
    calls = {"n": 0}

    def create(path, schema):
        calls["n"] += 1
        if calls["n"] <= failures:
            (Path(path) / "idmap.0").mkdir(parents=True)  # what the failed create leaves
            raise RuntimeError(f"create id map failed, path: {path}/idmap.0")
        return real(path=path, schema=schema)

    monkeypatch.setattr(zvec, "create_and_open", create)
    monkeypatch.setattr(rag, "_CREATE_BACKOFF", 0)
    return calls


def test_a_transient_create_failure_is_retried_from_a_clean_directory(tmp_path, monkeypatch):
    calls = _flaky_create(monkeypatch, failures=2)
    index = rag.VectorIndex(tmp_path / "idx.zvec", model="m", dims=8)
    _put(index)
    assert calls["n"] == 3
    assert index.ids() == {"d-1"}
    index.close()


def test_a_create_that_keeps_failing_raises_and_leaves_no_directory(tmp_path, monkeypatch):
    calls = _flaky_create(monkeypatch, failures=10**6)
    index = rag.VectorIndex(tmp_path / "idx.zvec", model="m", dims=8)
    with pytest.raises(RuntimeError, match="create id map failed"):
        _put(index)
    assert calls["n"] == rag._CREATE_ATTEMPTS
    assert not (tmp_path / "idx.zvec").exists()


def test_a_partial_directory_from_an_earlier_failure_is_replaced_on_open(tmp_path):
    path = tmp_path / "idx.zvec"
    (path / "idmap.0").mkdir(parents=True)
    index = rag.VectorIndex(path, model="m", dims=8)
    _put(index)
    assert index.ids() == {"d-1"}
    index.close()


def test_a_finished_collection_is_not_taken_for_a_partial_one(tmp_path):
    path = tmp_path / "idx.zvec"
    first = rag.VectorIndex(path, model="m", dims=8)
    _put(first)
    first.close()
    assert not rag._is_partial_collection(path)
