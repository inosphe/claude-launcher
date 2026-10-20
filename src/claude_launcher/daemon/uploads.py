"""Files of any kind the user hands a session from the web UI (claunch-5gpcf).

Images go into a harness's composer through the clipboard
(daemon/opening_images.py, daemon/thread_images.py). Most files cannot: a
performance capture (.utrace, a Chrome trace .json, .pftrace) is neither an
image nor small, and the only thing a harness can do with it is read it off
disk. So a file is stored on the daemon's machine and the session is told
where, in one line per file (:func:`line`):

    [claunch attachment] capture.utrace (1.2 GiB) /abs/path/capture.utrace

That also covers a browser on another PC: the bytes travel with the upload,
and the path is one on the machine the session runs on.

Three homes:

* **staged** -- ``<daemon dir>/uploads/<id>/<name>``, written by
  ``POST /api/uploads`` before the request that uses it (a create form, a
  thread post) exists. Nothing owns it yet; a staged file older than
  :data:`STAGE_TTL_SECONDS` is removed by the next upload.
* **session** -- ``<session dir>/uploads/<id>/<name>``: moved there by a
  create request (``opening_files``), or written there directly by the
  session line's upload. It lives as long as the session's state directory.
  Never the session's working directory: a repository is not a place to
  drop a capture, and an untracked file there shows up in every
  ``git status``.
* **thread** -- ``<daemon dir>/thread-files/<id>/<name>``: moved there by a
  thread post (``files``). A thread outlives its session, so its files are
  not in the session's directory; they are removed :data:`THREAD_TTL_SECONDS`
  after the post (:func:`sweep_thread_files`, hourly and on every post) and
  the thread shows them as expired from then on.

Each file sits in a directory named by its random id, so the original name
(made safe by :func:`safe_name`) can be kept: the agent and the person both
recognise ``capture.utrace``, and two uploads of the same name never meet.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from . import paths

#: A file's default size ceiling, in bytes; ``daemon.upload_max_bytes``
#: overrides it (read on every upload). The body is streamed to disk, so the
#: ceiling bounds disk use, not memory.
DEFAULT_MAX_BYTES = 2 * 1024 ** 3

#: How many files one request (an opening, a thread message) may carry.
MAX_FILES = 10

#: How long an uploaded file waits for a request to claim it.
STAGE_TTL_SECONDS = 24 * 3600

#: How long a file attached to a thread message is kept.
THREAD_TTL_SECONDS = 30 * 24 * 3600

_ID = re.compile(r"^[0-9a-f]{20}$")
_UNSAFE = re.compile(r'[\x00-\x1f<>:"/\\|?*]')
_MAX_NAME = 120


class UploadError(ValueError):
    """A request named files that cannot be used, or an upload was refused."""


@dataclass(frozen=True)
class Stored:
    id: str
    name: str
    path: Path
    bytes: int

    def record(self) -> dict:
        return {"id": self.id, "name": self.name, "bytes": self.bytes}


def max_bytes() -> int:
    """The configured ceiling (``daemon.upload_max_bytes``), or the default."""
    from .. import store
    try:
        value = int(store.daemon_config().get("upload_max_bytes") or DEFAULT_MAX_BYTES)
    except (TypeError, ValueError):
        return DEFAULT_MAX_BYTES
    return value if value > 0 else DEFAULT_MAX_BYTES


def human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"  # unreachable


def safe_name(raw: Optional[str]) -> str:
    """``raw`` as a single file name: no directory part, no characters a
    Windows or POSIX file system refuses, no leading dots, at most
    :data:`_MAX_NAME` characters (the extension kept). ``upload`` when
    nothing usable is left."""
    name = str(raw or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = _UNSAFE.sub("_", name).strip().lstrip(".").strip()
    if len(name) > _MAX_NAME:
        stem, dot, ext = name.rpartition(".")
        if dot and 0 < len(ext) <= 16:
            name = stem[: _MAX_NAME - len(ext) - 1] + "." + ext
        else:
            name = name[:_MAX_NAME]
    return name or "upload"


def stage_dir() -> Path:
    return paths.daemon_dir() / "uploads"


def session_dir(name: str) -> Path:
    return paths.session_dir(name) / "uploads"


def thread_dir() -> Path:
    return paths.daemon_dir() / "thread-files"


def _sweep(folder: Path, ttl: float, now: float) -> List[str]:
    gone: List[str] = []
    if not folder.is_dir():
        return gone
    for old in folder.iterdir():
        try:
            if now - old.stat().st_mtime > ttl:
                shutil.rmtree(old) if old.is_dir() else old.unlink()
                gone.append(old.name)
        except OSError:
            continue  # another upload swept it first, or it is in use
    return gone


class Writer:
    """One upload being written: :meth:`write` each chunk, then
    :meth:`finish` (or :meth:`abort`). The size ceiling is checked as the
    bytes arrive, so an oversized body stops at the ceiling instead of
    filling the disk first."""

    def __init__(self, folder: Path, name: str, limit: int):
        self.id = secrets.token_hex(10)
        self.name = safe_name(name)
        self.limit = limit
        self.dir = folder / self.id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / self.name
        self.bytes = 0
        self._fh = self.path.open("wb")

    def write(self, chunk: bytes) -> None:
        self.bytes += len(chunk)
        if self.bytes > self.limit:
            raise UploadError(f"the file is larger than {human_size(self.limit)} (daemon.upload_max_bytes)")
        self._fh.write(chunk)

    def finish(self) -> Stored:
        self._fh.close()
        if not self.bytes:
            self.abort()
            raise UploadError("the body carried no bytes")
        return Stored(self.id, self.name, self.path, self.bytes)

    def abort(self) -> None:
        try:
            self._fh.close()
        finally:
            shutil.rmtree(self.dir, ignore_errors=True)


def stage_writer(name: str) -> Writer:
    """A writer into the staging area; sweeps stale staged files first."""
    folder = stage_dir()
    folder.mkdir(parents=True, exist_ok=True)
    _sweep(folder, STAGE_TTL_SECONDS, time.time())
    return Writer(folder, name, max_bytes())


def session_writer(session: str, name: str) -> Writer:
    """A writer straight into session ``session``'s uploads (the session line)."""
    return Writer(session_dir(session), name, max_bytes())


def _staged(file_id: str) -> Stored:
    folder = stage_dir() / file_id
    files = [p for p in folder.iterdir() if p.is_file()] if folder.is_dir() else []
    if len(files) != 1:
        raise UploadError(
            f"uploaded file {file_id} is not on this daemon (expired or already used); attach it again"
        )
    return Stored(file_id, files[0].name, files[0], files[0].stat().st_size)


def check_ids(raw, *, field: str = "opening_files", what: str = "an opening") -> List[str]:
    """``raw`` (a request's list of staged ids) checked: the shape, the count
    and that every id names a staged file -- before anything is arranged, so
    a bad request leaves nothing behind."""
    if raw in (None, "", []):
        return []
    if not isinstance(raw, list):
        raise UploadError(f"{field} must be a list of uploaded file ids")
    if len(raw) > MAX_FILES:
        raise UploadError(f"{what} carries at most {MAX_FILES} files ({len(raw)} given)")
    ids: List[str] = []
    for item in raw:
        file_id = str(item or "").strip()
        if not _ID.match(file_id):
            raise UploadError(f"{file_id!r} is not an uploaded file id")
        _staged(file_id)
        if file_id not in ids:
            ids.append(file_id)
    return ids


def _move(ids: List[str], target: Path) -> List[Stored]:
    target.mkdir(parents=True, exist_ok=True)
    out: List[Stored] = []
    for file_id in ids:
        src = _staged(file_id)
        (stage_dir() / file_id).replace(target / file_id)
        # The sweeps count a folder's age from its mtime: from now on that
        # is the time the file was claimed, not the time it was staged.
        os.utime(target / file_id)
        out.append(Stored(file_id, src.name, target / file_id / src.name, src.bytes))
    return out


def claim(ids: List[str], session: str) -> List[Stored]:
    """Move staged files into session ``session``'s uploads, in order."""
    return _move(ids, session_dir(session)) if ids else []


def keep_for_thread(ids: List[str]) -> List[dict]:
    """Move staged files into the thread folder; the records a thread
    message stores (``[{id, name, bytes}]``)."""
    if not ids:
        return []
    _sweep(thread_dir(), THREAD_TTL_SECONDS, time.time())
    return [s.record() for s in _move(ids, thread_dir())]


def unkeep_thread(records: List[dict]) -> None:
    """Put kept thread files back in the staging area: the post that took
    them was refused, and the browser may send them again."""
    stage_dir().mkdir(parents=True, exist_ok=True)
    for rec in records:
        try:
            (thread_dir() / rec["id"]).replace(stage_dir() / rec["id"])
        except (OSError, KeyError):
            continue


def thread_file(file_id: str, name: str) -> Optional[Path]:
    """The kept thread file ``file_id``/``name``, or None once it is gone
    (expired) or when the pair is not one this module writes."""
    if not _ID.match(str(file_id or "")) or safe_name(name) != name:
        return None
    path = thread_dir() / file_id / name
    return path if path.is_file() else None


def sweep_thread_files(now: Optional[float] = None) -> List[str]:
    """Remove thread files older than :data:`THREAD_TTL_SECONDS`; their ids."""
    return _sweep(thread_dir(), THREAD_TTL_SECONDS, time.time() if now is None else now)


def attach(message: dict) -> dict:
    """Give each file of ``message`` its ``path`` on this machine, or
    ``expired: true`` once the sweep has removed it."""
    for rec in message.get("files") or []:
        path = thread_file(rec.get("id"), rec.get("name"))
        if path is None:
            rec["expired"] = True
            rec.pop("path", None)
        else:
            rec["path"] = str(path)
            rec.pop("expired", None)
    return message


def line(name: str, size: int, path) -> str:
    """The line a session reads a file from."""
    return f"[claunch attachment] {name} ({human_size(size)}) {path}"


def block(files: List[Stored]) -> str:
    """The lines for ``files``, under a one-line note of what they are."""
    if not files:
        return ""
    head = ("The user attached these files; each line ends with its path on this machine "
            "(read it from there; it is not in your working directory):")
    return "\n".join([head, *(line(f.name, f.bytes, f.path) for f in files)])


#: Seconds between two thread-file sweeps of the running daemon.
SWEEP_INTERVAL = 3600.0


async def _sweep_loop() -> None:
    import asyncio
    import logging
    log = logging.getLogger(__name__)
    while True:
        try:
            gone = await asyncio.to_thread(sweep_thread_files)
            if gone:
                log.info("uploads: removed %d thread file(s) past %d days", len(gone),
                         THREAD_TTL_SECONDS // 86400)
        except Exception:  # noqa: BLE001 -- the next tick tries again
            log.exception("uploads: thread-file sweep failed")
        await asyncio.sleep(SWEEP_INTERVAL)


def install(app) -> None:
    """Sweep expired thread files at start and every :data:`SWEEP_INTERVAL`,
    so a file is gone after its 30 days even when nothing new is posted."""
    import asyncio
    import contextlib

    async def start(app_):
        app_["uploads_sweep"] = asyncio.get_running_loop().create_task(_sweep_loop(), name="uploads-sweep")

    async def stop(app_):
        task = app_.get("uploads_sweep")
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app.on_startup.append(start)
    app.on_shutdown.append(stop)
