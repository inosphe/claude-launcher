"""Record an install's file writes instead of making them — the dry run.

``claunch install`` and ``claunch cflow update`` touch a spread of files
(``.mcp.json``, ``.claude.json``, ``settings.json``, a handful of skills, the
global workflow layer and its seed record) through a dozen small writers in
as many modules. A dry run that re-derived "what would change" beside those
writers would be a second implementation of every one of them, and the first
edit to a writer that forgot its twin would make the preview lie.

So the writers themselves go through this module. Outside :func:`dry_run`
each helper is exactly the write it replaced. Inside it, nothing reaches the
disk: every write is recorded as a :class:`Change` (the path, the bytes there
now, the bytes the write would leave), and reads of a path already written in
the same plan are served from the recording — the install merges its deny and
allow rules into one ``settings.json`` in two passes, and the second pass has
to see the first or the preview would show half the change.

The plan is held in a :class:`contextvars.ContextVar`, so a dry run on one
thread (the daemon runs installs off its event loop) never turns a real
install on another thread into a preview.
"""

from __future__ import annotations

import contextvars
import os
import shutil
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, List, Optional

#: Change kinds, as the preview names them.
CREATE = "create"  #: nothing is there; the write creates the file
UPDATE = "update"  #: a file is there and the write changes its bytes
UNCHANGED = "unchanged"  #: a file is there and the write leaves the same bytes
DELETE = "delete"  #: a file is there and the plan removes it


@dataclass
class Change:
    """One file the plan would write, with the bytes before and after."""

    path: Path
    before: Optional[bytes]
    #: None when the plan removes the file.
    after: Optional[bytes]

    @property
    def kind(self) -> str:
        if self.after is None:
            return DELETE
        if self.before is None:
            return CREATE
        return UNCHANGED if self.before == self.after else UPDATE

    def to_dict(self, with_text: bool = False) -> dict:
        out = {
            "path": str(self.path),
            "kind": self.kind,
            "bytes_before": None if self.before is None else len(self.before),
            "bytes_after": None if self.after is None else len(self.after),
        }
        if with_text:
            out["before"] = _text(self.before)
            out["after"] = _text(self.after)
        return out


def _text(data: Optional[bytes]) -> Optional[str]:
    if data is None:
        return None
    return data.decode("utf-8", errors="replace")


@dataclass
class Plan:
    """Every write recorded under one :func:`dry_run`, in first-write order."""

    _changes: Dict[str, Change] = field(default_factory=dict)
    #: Leave Windows Defender out of the plan entirely. Reading its exclusion
    #: list spawns PowerShell (seconds on a cold start), which an overview that
    #: plans every target at once cannot afford per target.
    skip_defender: bool = False

    def record(self, path: Path, data: Optional[bytes]) -> None:
        key = _key(path)
        prior = self._changes.get(key)
        if prior is not None:
            # A second write to the same file keeps the ORIGINAL before-bytes:
            # the preview compares the disk as it is with the disk as the whole
            # install would leave it, not with an intermediate state.
            prior.after = data
            return
        self._changes[key] = Change(Path(path), _read_disk(path), data)

    def overlay(self, path: Path) -> Optional[bytes]:
        change = self._changes.get(_key(path))
        return None if change is None else change.after

    def removed(self, path: Path) -> bool:
        """Whether this plan's last word on ``path`` is a removal."""
        change = self._changes.get(_key(path))
        return change is not None and change.after is None

    @property
    def changes(self) -> List[Change]:
        return list(self._changes.values())


_PLAN: "contextvars.ContextVar[Optional[Plan]]" = contextvars.ContextVar(
    "claunch_fsplan", default=None
)


def _key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _read_disk(path: Path) -> Optional[bytes]:
    try:
        return Path(path).read_bytes() if Path(path).is_file() else None
    except OSError:
        return None


def active() -> Optional[Plan]:
    """The plan being recorded on this context, or None for a real run."""
    return _PLAN.get()


@contextmanager
def dry_run(skip_defender: bool = False) -> Iterator[Plan]:
    """Record every write made through this module instead of making it."""
    plan = Plan(skip_defender=skip_defender)
    token = _PLAN.set(plan)
    try:
        yield plan
    finally:
        _PLAN.reset(token)


def _encode_text(text: str, encoding: str) -> bytes:
    # ``Path.write_text`` opens in text mode with universal newlines, so on
    # Windows every "\n" lands as "\r\n". The preview compares bytes, and has
    # to compare the bytes the real write would leave.
    return text.replace("\n", os.linesep).encode(encoding)


def write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """``path.write_text`` (parents created), or its record in a dry run."""
    plan = active()
    if plan is not None:
        plan.record(path, _encode_text(text, encoding))
        return
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(text, encoding=encoding)


def copyfile(src: Path, dest: Path) -> None:
    """``shutil.copyfile`` (parents created), or its record in a dry run."""
    plan = active()
    if plan is not None:
        plan.record(dest, read_bytes(src) or b"")
        return
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)


def remove(path: Path) -> None:
    """``unlink`` (a missing file is not an error), or its record in a dry run."""
    plan = active()
    if plan is not None:
        if is_file(path):
            plan.record(path, None)
        return
    try:
        Path(path).unlink()
    except FileNotFoundError:
        pass


def mkdir(path: Path) -> None:
    """``mkdir -p``, skipped in a dry run (a directory is not a change)."""
    if active() is None:
        Path(path).mkdir(parents=True, exist_ok=True)


def read_bytes(path: Path) -> Optional[bytes]:
    """The file's bytes as this plan would leave them; None when absent."""
    plan = active()
    if plan is not None:
        if plan.removed(path):
            return None
        data = plan.overlay(path)
        if data is not None:
            return data
    return _read_disk(path)


def read_text(path: Path, encoding: str = "utf-8") -> Optional[str]:
    """Text-mode read through the plan; None when the file is absent."""
    data = read_bytes(path)
    if data is None:
        return None
    # Text mode would have folded "\r\n" back to "\n"; do the same.
    return data.decode(encoding).replace("\r\n", "\n")


def is_file(path: Path) -> bool:
    """Whether the file exists, counting files this plan would create."""
    plan = active()
    if plan is not None:
        if plan.removed(path):
            return False
        if plan.overlay(path) is not None:
            return True
    return Path(path).is_file()
