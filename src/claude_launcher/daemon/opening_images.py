"""Images that ride with a new session's opening task (claunch-jmrmc).

The create form (New session / Spawn child) can carry images beside its
opening task. A create request is one JSON body, and an image is neither JSON
nor small, so the browser uploads each image first -- one raw body per image,
the same shape the web session line's paste route takes -- and gets back an
id. The create request then names those ids in ``opening_images``.

Two homes, in order:

* **staged** -- ``<daemon dir>/opening-images/<id><ext>``, written by the
  upload. Nothing owns it yet: the form may still be cancelled. A staged file
  older than :data:`STAGE_TTL_SECONDS` is removed by the next upload, so a
  cancelled form does not leave files behind forever.
* **claimed** -- moved into the session's state directory
  (``<session dir>/opening-images/``) once the create request names it. That
  is where the daemon keeps everything that belongs to one session, and never
  the session's working directory: a repository is not a place to drop a
  screenshot.

How a claimed image reaches the harness is the paste route's way (clipboard +
the harness's declared ``image_paste_keys``); the API composes that, since it
owns the clipboard lock. This module only names, stores and moves files.
"""

from __future__ import annotations

import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List

from . import paths

#: Media types accepted, and the extension each is stored under. The same set
#: the session line's paste route takes (``api.PASTE_IMAGE_TYPES``): a type the
#: clipboard writers do not know cannot be handed over either.
TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}

_EXT_TYPES = {ext: kind for kind, ext in TYPES.items()}

#: How many images one opening may carry. Each one is a clipboard round trip
#: paced by the paste settle window, so the bound keeps a session's first
#: message from taking minutes to type in.
MAX_IMAGES = 10

#: How long an uploaded image waits for a create request to claim it.
STAGE_TTL_SECONDS = 24 * 3600

_ID = re.compile(r"^[0-9a-f]{20}(\.(png|jpg|gif|webp))$")


class OpeningImageError(ValueError):
    """A request named images that cannot be used."""


@dataclass(frozen=True)
class Image:
    path: Path
    media_type: str


def stage_dir() -> Path:
    return paths.daemon_dir() / "opening-images"


def session_dir(name: str) -> Path:
    return paths.session_dir(name) / "opening-images"


def _sweep(folder: Path, now: float) -> None:
    for old in folder.iterdir():
        try:
            if now - old.stat().st_mtime > STAGE_TTL_SECONDS:
                old.unlink()
        except OSError:
            continue  # another upload swept it first, or it is in use


def stage(chunks: Iterable[bytes], media_type: str) -> str:
    """Write one uploaded image to the staging area; returns its id."""
    suffix = TYPES.get(media_type)
    if suffix is None:
        raise OpeningImageError(f"{media_type or 'no type'} is not an accepted image type")
    folder = stage_dir()
    folder.mkdir(parents=True, exist_ok=True)
    _sweep(folder, time.time())
    image_id = secrets.token_hex(10) + suffix
    with (folder / image_id).open("wb") as fh:
        for chunk in chunks:
            fh.write(chunk)
    return image_id


def check_ids(raw, *, field: str = "opening_images",
              what: str = "an opening") -> List[str]:
    """``opening_images`` from a request body, as a list of staged ids.

    Refused when the shape is wrong, the count is over :data:`MAX_IMAGES`, or
    an id names no staged file -- before anything is arranged, so a bad
    request leaves no session behind. A thread message stages its images
    the same way (daemon/thread_images.py) and names its own ``field``.
    """
    if raw in (None, "", []):
        return []
    if not isinstance(raw, list):
        raise OpeningImageError(f"{field} must be a list of uploaded image ids")
    if len(raw) > MAX_IMAGES:
        raise OpeningImageError(
            f"{what} carries at most {MAX_IMAGES} images ({len(raw)} given)"
        )
    ids: List[str] = []
    for item in raw:
        image_id = str(item or "").strip()
        if not _ID.match(image_id):
            raise OpeningImageError(f"{image_id!r} is not an uploaded image id")
        if not (stage_dir() / image_id).is_file():
            raise OpeningImageError(
                f"uploaded image {image_id} is not on this daemon "
                f"(expired or already used); attach it again"
            )
        if image_id not in ids:
            ids.append(image_id)
    return ids


def claim(ids: List[str], name: str) -> List[Image]:
    """Move staged images into session ``name``'s state directory, in order."""
    if not ids:
        return []
    folder = session_dir(name)
    folder.mkdir(parents=True, exist_ok=True)
    out: List[Image] = []
    for n, image_id in enumerate(ids, start=1):
        ext = image_id[image_id.index("."):]
        target = folder / f"{n:02d}-{image_id}"
        (stage_dir() / image_id).replace(target)
        out.append(Image(target, _EXT_TYPES[ext]))
    return out
