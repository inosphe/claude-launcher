"""Images the user attaches to a thread message (claunch-shl2r).

A thread post is one JSON body, so an image travels the way an opening
task's does (daemon/opening_images.py): the browser uploads each one first
as a raw body and gets back a staged id, and the post names those ids in
``images``. This module takes them from there.

* **kept** -- moved out of the staging area into
  ``<daemon dir>/thread-images/<id><ext>``. The thread record names the file
  (threads/store.py ``messages.images``), and the conversation is the record
  of what was said, so the file stays as long as the thread database does:
  the threads pane and the threads page show it again on every read
  (``GET /api/threads/{id}/images/{name}``). Not in the session's state
  directory -- a thread outlives the session it was with.
* **handed over** -- the route types the message into the session with each
  file's path in the block, and on a live session whose harness takes
  images it first pastes them into the composer the way the session line's
  image paste does (api._deliver_pasted_image).
"""

from __future__ import annotations

from pathlib import Path
from typing import List

from . import opening_images, paths

TYPES = opening_images.TYPES
_EXT_TYPES = {ext: kind for kind, ext in TYPES.items()}


class ThreadImageError(ValueError):
    """A post named images that cannot be used."""


def folder() -> Path:
    return paths.daemon_dir() / "thread-images"


def path_of(name: str) -> Path:
    """The kept file ``name``; raises when the name is not one this module
    writes (so a request cannot walk out of the folder)."""
    if not opening_images._ID.match(str(name or "")):
        raise ThreadImageError(f"{name!r} is not a thread image")
    return folder() / name


def media_type(name: str) -> str:
    return _EXT_TYPES.get(Path(name).suffix, "application/octet-stream")


def check(raw) -> List[str]:
    """``images`` of a post, as staged ids (see opening_images.check_ids)."""
    try:
        return opening_images.check_ids(raw, field="images", what="a thread message")
    except opening_images.OpeningImageError as exc:
        raise ThreadImageError(str(exc)) from None


def keep(ids: List[str]) -> List[dict]:
    """Move staged images into the kept folder, in order, as the records the
    thread store takes: ``[{name, type, bytes}]``."""
    if not ids:
        return []
    target = folder()
    target.mkdir(parents=True, exist_ok=True)
    out: List[dict] = []
    for image_id in ids:
        dest = target / image_id
        (opening_images.stage_dir() / image_id).replace(dest)
        out.append({"name": image_id, "type": media_type(image_id), "bytes": dest.stat().st_size})
    return out


def unkeep(images: List[dict]) -> None:
    """Put kept images back in the staging area: the post that took them was
    refused, and the browser may send them again (the stage sweep removes
    them if it does not)."""
    for img in images:
        try:
            (folder() / img["name"]).replace(opening_images.stage_dir() / img["name"])
        except OSError:
            continue


def attach(message: dict) -> dict:
    """Give each image of ``message`` its file ``path`` on this machine, so
    the agent reading the thread (thread_read) can open it."""
    for img in message.get("images") or []:
        img["path"] = str(folder() / img["name"])
    return message
