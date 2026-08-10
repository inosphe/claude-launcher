"""Versioned document storage for the sync server.

One YAML file per namespace plus a small JSON sidecar holding the revision and
who last wrote it. Writes are atomic (temp file + replace), so a crash mid-write
can never leave a half-written config document — the previous revision survives
intact.

Concurrency is optimistic: a writer states the revision it read, and a write
built on a stale revision is rejected with :class:`RevisionMismatch` rather than
silently overwriting whatever arrived in between.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

#: Namespaces name a file on disk, so keep them boring and path-traversal-proof.
_NAMESPACE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class SyncServerError(Exception):
    """Raised for invalid namespaces and unreadable stored documents."""


class RevisionMismatch(SyncServerError):
    """A write was based on a revision the document has since moved past."""

    def __init__(self, expected: int, actual: int) -> None:
        super().__init__(
            f"document is at revision {actual}, write was based on {expected}"
        )
        self.expected = expected
        self.actual = actual


@dataclass(frozen=True)
class StoredDoc:
    """A namespace's document plus its bookkeeping."""

    namespace: str
    revision: int
    doc: Dict[str, Any]
    updated_at: Optional[str] = None
    updated_by: Optional[str] = None

    def payload(self) -> dict:
        return {
            "namespace": self.namespace,
            "revision": self.revision,
            "doc": self.doc,
            "updated_at": self.updated_at,
            "updated_by": self.updated_by,
        }


def validate_namespace(name: str) -> str:
    """Return ``name`` if it is a legal namespace, else raise.

    ``..`` and separators are rejected by the pattern, so a namespace can never
    escape the data directory.
    """
    name = (name or "").strip()
    if not _NAMESPACE_RE.match(name):
        raise SyncServerError(
            f"invalid namespace {name!r}: use letters, digits, '.', '_' or '-' "
            "(1-64 chars, starting with a letter or digit)"
        )
    return name


class DocStore:
    """Documents on disk under ``<root>/docs``."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.docs_dir = self.root / "docs"

    # ------------------------------------------------------------------ #
    # paths
    # ------------------------------------------------------------------ #
    def _doc_path(self, namespace: str) -> Path:
        return self.docs_dir / f"{namespace}.yaml"

    def _meta_path(self, namespace: str) -> Path:
        return self.docs_dir / f"{namespace}.meta.json"

    # ------------------------------------------------------------------ #
    # reads
    # ------------------------------------------------------------------ #
    def read(self, namespace: str) -> StoredDoc:
        """The namespace's document; revision 0 with an empty doc if it has none."""
        namespace = validate_namespace(namespace)
        path = self._doc_path(namespace)
        if not path.is_file():
            return StoredDoc(namespace=namespace, revision=0, doc={})
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            # Never fall back to "empty": that would invite the next write to
            # clobber a document that is merely unreadable right now.
            raise SyncServerError(f"cannot read document {namespace!r}: {exc}") from None
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise SyncServerError(f"document {namespace!r} is not a mapping")
        meta = self._read_meta(namespace)
        return StoredDoc(
            namespace=namespace,
            revision=int(meta.get("revision") or 0),
            doc=data,
            updated_at=meta.get("updated_at"),
            updated_by=meta.get("updated_by"),
        )

    def _read_meta(self, namespace: str) -> dict:
        path = self._meta_path(namespace)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # A document with no readable sidecar is treated as revision 1: it
            # exists, so revision 0 ("absent") would be a lie.
            return {"revision": 1}
        return data if isinstance(data, dict) else {"revision": 1}

    def namespaces(self) -> List[str]:
        if not self.docs_dir.is_dir():
            return []
        return sorted(p.stem for p in self.docs_dir.glob("*.yaml"))

    # ------------------------------------------------------------------ #
    # writes
    # ------------------------------------------------------------------ #
    def write(
        self,
        namespace: str,
        doc: Dict[str, Any],
        expected_revision: int,
        *,
        by: Optional[str] = None,
    ) -> StoredDoc:
        """Store ``doc`` as the next revision, if the caller read the current one."""
        namespace = validate_namespace(namespace)
        if not isinstance(doc, dict):
            raise SyncServerError("document must be a mapping")
        current = self.read(namespace)
        if int(expected_revision) != current.revision:
            raise RevisionMismatch(int(expected_revision), current.revision)

        revision = current.revision + 1
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.docs_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(
            self._doc_path(namespace),
            yaml.safe_dump(doc, sort_keys=True, allow_unicode=True, default_flow_style=False),
        )
        _atomic_write(
            self._meta_path(namespace),
            json.dumps(
                {"revision": revision, "updated_at": stamp, "updated_by": by},
                indent=2,
            )
            + "\n",
        )
        return StoredDoc(
            namespace=namespace,
            revision=revision,
            doc=doc,
            updated_at=stamp,
            updated_by=by,
        )

    def delete(self, namespace: str) -> bool:
        """Remove a namespace's document. Returns whether anything was there."""
        namespace = validate_namespace(namespace)
        found = False
        for path in (self._doc_path(namespace), self._meta_path(namespace)):
            if path.is_file():
                path.unlink()
                found = True
        return found


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
