"""The daemon's own prompt: one text this daemon instance gives every session.

It is not source code and not a workspace file -- it lives in the instance's
state directory (:func:`paths.daemon_dir`: ``~/.claude-launcher/daemon`` for
the default instance, ``daemons/<name>`` for ``-L <name>``), so each daemon
reads and writes only its own and two daemons on one machine never see each
other's. An empty file (or none) means the daemon has no prompt.

A session gets it as a prompt pack (:data:`PACK_ID`, source
:data:`prompt_packs.DAEMON`) whatever its role, unless the request turns it
off (``daemon_prompt: false``; the create form's box starts ticked).

Edits come from Settings and from agents through the MCP tool alike, so each
write keeps the text it replaced (:data:`PREV_FILE`) and appends who wrote
what size to :data:`LOG_FILE` -- one agent's bad edit reaches every session
created after it, and the way back has to be on disk.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

from .. import atomic, prompt_packs
from . import paths

#: The pack id a session's record and opening carry the prompt under. The
#: ``@`` keeps it out of the pack-file namespace (``prompt_packs.check_id``
#: refuses it), so a workspace's own ``daemon.md`` is a different pack, not a
#: second record under the same id; and nobody can ask for it by id -- it
#: comes in through ``daemon_prompt``, not ``prompt_packs``.
PACK_ID = "@daemon"
FILE = "daemon-prompt.md"
PREV_FILE = "daemon-prompt.prev.md"
LOG_FILE = "daemon-prompt.log.jsonl"
#: A prompt every session reads in full; past this it is a document.
MAX_CHARS = 32_000


class DaemonPromptError(Exception):
    """A text that may not be saved (too long, not a string)."""


def _write(target: Path, body: str) -> None:
    """Replace ``target`` in one rename, LF line endings kept."""
    with atomic.scratch(target) as tmp:
        tmp.write_text(body, encoding="utf-8", newline="\n")
        atomic.replace(tmp, target)


def _dir(base: Optional[Path]) -> Path:
    return Path(base) if base is not None else paths.daemon_dir()


def path(base: Optional[Path] = None) -> Path:
    return _dir(base) / FILE


def instance_label() -> str:
    """How this daemon is named in a title: its ``-L`` name, else default."""
    return paths.instance() or "default"


def text(base: Optional[Path] = None) -> str:
    try:
        return path(base).read_text(encoding="utf-8").strip()
    except (FileNotFoundError, NotADirectoryError):
        return ""
    except UnicodeDecodeError:
        return ""


def read(base: Optional[Path] = None) -> dict:
    """What Settings and the MCP tool show: where it is and what it says."""
    p = path(base)
    body = text(base)
    try:
        updated = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(p.stat().st_mtime)
        ) if p.exists() else None
    except OSError:
        updated = None
    return {
        "instance": instance_label(),
        "path": str(p),
        "text": body,
        "chars": len(body),
        "enabled": bool(body),
        "updated_at": updated,
        "previous_path": str(_dir(base) / PREV_FILE),
        "log_path": str(_dir(base) / LOG_FILE),
    }


def write(new: str, *, by: str, base: Optional[Path] = None) -> dict:
    """Replace the prompt, keeping the text it replaces; empty disables it."""
    if not isinstance(new, str):
        raise DaemonPromptError("'text' must be a string")
    new = new.replace("\r\n", "\n").strip()
    if len(new) > MAX_CHARS:
        raise DaemonPromptError(
            f"the daemon prompt is {len(new)} characters; the limit is "
            f"{MAX_CHARS} -- every session reads it in full"
        )
    d = _dir(base)
    d.mkdir(parents=True, exist_ok=True)
    old = text(base)
    if new != old:
        if old:
            _write(d / PREV_FILE, old + "\n")
        _write(d / FILE, (new + "\n") if new else "")
        entry = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "by": str(by or "unknown"),
            "chars": len(new),
            "previous_chars": len(old),
        }
        with open(d / LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    return read(base)


def pack(base: Optional[Path] = None) -> Optional[prompt_packs.Pack]:
    """The prompt as a pack, or None when this daemon has none."""
    body = text(base)
    if not body:
        return None
    title = f"Daemon prompt ({instance_label()})"
    return prompt_packs.Pack(
        id=PACK_ID, title=title,
        description="This daemon's own prompt, given to every session unless unticked.",
        body=body, raw=body, source=prompt_packs.DAEMON,
    )


def offer(base: Optional[Path] = None) -> Optional[dict]:
    """The create form's row for it: enough to draw a ticked box."""
    p = pack(base)
    if p is None:
        return None
    return {**p.to_dict(), "chars": len(p.body), "path": str(path(base))}


def wanted(body: dict) -> bool:
    """Whether a create/spawn request takes the prompt: yes unless it says no."""
    raw = body.get("daemon_prompt", True)
    if isinstance(raw, str):
        return raw.strip().lower() not in ("0", "false", "no", "off", "")
    return raw is not False and raw is not None and raw != 0
