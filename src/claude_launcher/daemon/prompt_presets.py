"""Daemon-local prompt-message presets used by the session footer."""

from __future__ import annotations

import json
import uuid
from typing import List

from .. import atomic
from . import paths


class PromptPresetError(Exception):
    """The prompt preset file could not be read or written."""


def _clean(rows) -> List[dict]:
    """Normalize the persisted prompt-preset representation."""
    if not isinstance(rows, list):
        return []
    clean = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        text = str(row.get("text") or "").strip()
        if name and text:
            clean.append({
                "id": str(row.get("id") or uuid.uuid4()),
                "name": name,
                "text": text,
                "enabled": row.get("enabled", True) is not False,
            })
    return clean


def entries() -> List[dict]:
    """Read prompt-message presets for this daemon instance."""
    path = paths.prompt_presets_json()
    try:
        if not path.is_file():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("the prompt preset file must contain an object")
        return _clean(data.get("presets"))
    except (OSError, ValueError) as exc:
        raise PromptPresetError(f"cannot read prompt presets {path}: {exc}") from exc


def set_entries(rows) -> List[dict]:
    """Persist user-defined prompt-message presets for this daemon instance."""
    clean = _clean(rows)
    path = paths.prompt_presets_json()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with atomic.scratch(path) as tmp:
            tmp.write_text(
                json.dumps({"presets": clean}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            atomic.replace(tmp, path)
    except OSError as exc:
        raise PromptPresetError(f"cannot write prompt presets {path}: {exc}") from exc
    return clean
