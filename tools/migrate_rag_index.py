"""Move the JSON vector indexes under ``<daemon dir>/rag/`` into zvec collections.

The search index used to be one JSON file per corpus, holding every vector
base64-encoded. It is now a zvec collection per corpus (``<key>.zvec/``) plus
a small sidecar (``<key>.meta.json``) carrying the four corpus-level fields.
This script carries the vectors across.

Re-indexing from scratch would do the same job, but it calls the embedding
endpoint once per document: the module docstring records 10-12 minutes for a
778-issue board, and the live daemon's unified corpus is 31k documents. The
vectors in the old file are still the same vectors, so copying them costs no
endpoint calls at all.

**This script does not touch a running daemon's indexes by default.** It
reads, reports and stops unless ``--write`` is given, and it never deletes
the JSON file it read: the old and new layouts sit side by side until
somebody removes the old one by hand. Run it with the daemon stopped --
zvec locks a collection directory, so a daemon holding the new collection
open will make the write fail rather than corrupt anything.

Usage::

    # what would happen, reading the live daemon's directory
    uv run --no-sync python tools/migrate_rag_index.py

    # convert, writing the collections
    uv run --no-sync python tools/migrate_rag_index.py --write

    # a directory other than the live daemon's
    uv run --no-sync python tools/migrate_rag_index.py --dir /path/to/rag
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Iterator, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from claude_launcher.daemon import paths, rag  # noqa: E402


class Source:
    """One JSON index file, read lazily so a 477 MB corpus is not held twice."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.model = ""
        self.dims = 0
        self.signature = ""
        self.updated_at: Optional[str] = None
        self.docs: dict = {}
        self.error: Optional[str] = None

    @property
    def key(self) -> str:
        return self.path.stem

    def read(self) -> bool:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self.error = f"unreadable ({exc.__class__.__name__})"
            return False
        if not isinstance(data, dict):
            self.error = "not an object"
            return False
        if data.get("format") != rag.FORMAT:
            self.error = f"format {data.get('format')!r}, expected {rag.FORMAT}"
            return False
        docs = data.get("docs")
        if not isinstance(docs, dict):
            self.error = "no docs"
            return False
        self.model = str(data.get("model") or "")
        self.dims = int(data.get("dims") or 0)
        self.signature = str(data.get("signature") or "")
        self.updated_at = data.get("updated_at")
        self.docs = docs
        return True

    def entries(self) -> Iterator[Tuple[str, str, list, dict]]:
        """``(doc_id, hash, vectors, meta)`` for each row that decodes."""
        for doc_id, row in self.docs.items():
            try:
                vecs = [rag._decode(v) for v in row["v"]]
            except (KeyError, TypeError, ValueError):
                continue
            if not vecs:
                continue
            yield str(doc_id), str(row.get("h") or ""), vecs, dict(row.get("m") or {})


def plan(directory: Path) -> list:
    """Every JSON index in ``directory``, read far enough to describe it."""
    out = []
    for path in sorted(directory.glob("*.json")):
        if path.name.endswith(".meta.json"):
            continue  # a sidecar this script wrote
        src = Source(path)
        src.read()
        out.append(src)
    return out


def convert(src: Source, *, verbose: bool) -> Tuple[int, int]:
    """Write one source into its collection. Returns (documents, vectors)."""
    target = src.path.with_suffix(".zvec")
    index = rag.VectorIndex(target, model=src.model, dims=src.dims,
                            signature=src.signature)
    docs = vectors = 0
    for doc_id, doc_hash, vecs, meta in src.entries():
        if src.dims and any(len(v) != src.dims for v in vecs):
            continue
        index.put(rag.Doc(doc_id, doc_hash, [], meta), vecs)
        docs += 1
        vectors += len(vecs)
        if verbose and docs % 2000 == 0:
            print(f"    {docs} documents", flush=True)
    index.updated_at = src.updated_at
    index.save()
    index.close()
    return docs, vectors


def verify(src: Source, expected_docs: int) -> Optional[str]:
    """Read the collection back the way the daemon will. None means it matches."""
    target = src.path.with_suffix(".zvec")
    index = rag.VectorIndex(target, model=src.model, dims=src.dims,
                            signature=src.signature)
    try:
        index.load()
        if len(index.entries) != expected_docs:
            return f"read back {len(index.entries)} documents, wrote {expected_docs}"
        for doc_id, doc_hash, vecs, _meta in src.entries():
            entry = index.entries.get(doc_id)
            if entry is None:
                return f"{doc_id} missing"
            if entry.hash != doc_hash:
                return f"{doc_id} hash {entry.hash!r}, expected {doc_hash!r}"
            if entry.chunks != len(vecs):
                return f"{doc_id} has {entry.chunks} chunks, expected {len(vecs)}"
            break  # one document proves the round trip; the count covers the rest
        return None
    finally:
        index.close()


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dir", type=Path, default=None,
                    help="the rag directory (default: the live daemon's)")
    ap.add_argument("--write", action="store_true",
                    help="actually convert; without it the script only reports")
    ap.add_argument("--only", action="append", default=None,
                    help="convert just this corpus key (repeatable): all, fleet, board:...")
    args = ap.parse_args(argv)

    directory = args.dir or paths.rag_dir()
    if not directory.is_dir():
        print(f"no such directory: {directory}")
        return 1
    print(f"rag directory: {directory}")

    sources = plan(directory)
    if args.only:
        wanted = set(args.only)
        sources = [s for s in sources if s.key in wanted]
    if not sources:
        print("no JSON index found -- nothing to migrate")
        return 0

    todo = []
    for src in sources:
        size = src.path.stat().st_size / 1e6
        target = src.path.with_suffix(".zvec")
        if src.error:
            print(f"  {src.path.name:28} {size:8.1f} MB  skipped: {src.error}")
            continue
        state = "target exists" if target.is_dir() else "ready"
        print(f"  {src.path.name:28} {size:8.1f} MB  {len(src.docs):7d} docs  "
              f"dims {src.dims}  {state}")
        if target.is_dir():
            print(f"      {target.name} is already there -- remove it to reconvert")
            continue
        todo.append(src)

    if not args.write:
        print()
        print(f"{len(todo)} corpus/corpora would be converted. "
              f"Re-run with --write to do it.")
        print("Stop the daemon first: zvec locks a collection directory.")
        return 0

    failures = 0
    for src in todo:
        print(f"converting {src.path.name} ...", flush=True)
        started = time.monotonic()
        try:
            docs, vectors = convert(src, verbose=True)
        except Exception as exc:
            print(f"  failed: {exc}")
            failures += 1
            continue
        elapsed = time.monotonic() - started
        print(f"  wrote {docs} documents, {vectors} vectors in {elapsed:.1f}s")
        problem = verify(src, docs)
        if problem:
            print(f"  verification failed: {problem}")
            failures += 1
        else:
            print(f"  verified: {src.path.with_suffix('.zvec').name} reads back")
            print(f"  the old file is left in place: {src.path.name}")

    if failures:
        print(f"\n{failures} corpus/corpora failed.")
        return 1
    print("\nDone. Start the daemon and search once to confirm, then the old "
          "JSON files can be removed by hand.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
