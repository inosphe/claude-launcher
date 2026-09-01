"""Daemon-local Y/N status-check presets and agent-reported session values."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import List

from .. import atomic
from . import paths


class StatusCheckError(Exception):
    """The status-check file could not be read or written."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clean_presets(rows) -> List[dict]:
    if not isinstance(rows, list):
        return []
    clean = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        # Older files stored only ``question``.  Keep them useful after the
        # label was introduced by using that sentence as their visible name.
        question = str(row.get("question") or "").strip()
        name = str(row.get("name") or question).strip()
        if name and question:
            clean.append({
                "id": str(row.get("id") or uuid.uuid4()),
                "name": name,
                "question": question,
                "enabled": row.get("enabled", True) is not False,
            })
    return clean


def _clean_reports(rows) -> dict:
    if not isinstance(rows, dict):
        return {}
    clean = {}
    for session, reports in rows.items():
        if not isinstance(reports, dict):
            continue
        session_reports = {}
        for preset_id, report in reports.items():
            if not isinstance(report, dict):
                continue
            answer = str(report.get("answer") or "").lower()
            if answer not in ("yes", "no"):
                continue
            session_reports[str(preset_id)] = {
                "answer": answer,
                "reported_at": str(report.get("reported_at") or ""),
                "source": "agent",
            }
        if session_reports:
            clean[str(session)] = session_reports
    return clean


def _read() -> dict:
    path = paths.status_checks_json()
    try:
        if not path.is_file():
            return {"presets": [], "reports": {}}
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("the status-check file must contain an object")
        return {
            "presets": _clean_presets(data.get("presets")),
            "reports": _clean_reports(data.get("reports")),
        }
    except (OSError, ValueError) as exc:
        raise StatusCheckError(f"cannot read status checks {path}: {exc}") from exc


def _write(data: dict) -> dict:
    clean = {
        "presets": _clean_presets(data.get("presets")),
        "reports": _clean_reports(data.get("reports")),
    }
    path = paths.status_checks_json()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with atomic.scratch(path) as tmp:
            tmp.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
            atomic.replace(tmp, path)
    except OSError as exc:
        raise StatusCheckError(f"cannot write status checks {path}: {exc}") from exc
    return clean


def entries() -> List[dict]:
    return _read()["presets"]


def set_entries(rows) -> List[dict]:
    data = _read()
    data["presets"] = rows
    return _write(data)["presets"]


def session_entries(session: str, *, enabled_only: bool = False) -> List[dict]:
    data = _read()
    reports = data["reports"].get(session, {})
    return [
        {**preset, **({"report": reports[preset["id"]]} if preset["id"] in reports else {})}
        for preset in data["presets"]
        if not enabled_only or preset["enabled"]
    ]


def report(session: str, answers: list) -> List[dict]:
    """Record one agent's current Y/N answers for enabled presets."""
    data = _read()
    enabled = {row["id"] for row in data["presets"] if row["enabled"]}
    incoming = {}
    for item in answers:
        if not isinstance(item, dict):
            raise ValueError("each status-check answer must be an object")
        preset_id = str(item.get("id") or "").strip()
        answer = str(item.get("answer") or "").lower()
        if preset_id not in enabled:
            raise ValueError(f"no enabled status check named {preset_id!r}")
        if answer not in ("yes", "no"):
            raise ValueError("a status-check answer must be 'yes' or 'no'")
        incoming[preset_id] = answer
    if not incoming:
        raise ValueError("at least one status-check answer is required")
    reports = data["reports"].setdefault(session, {})
    at = _now()
    for preset_id, answer in incoming.items():
        reports[preset_id] = {"answer": answer, "reported_at": at, "source": "agent"}
    _write(data)
    return session_entries(session, enabled_only=True)


def digests(sessions: List[str]) -> dict:
    """Compact enabled values for a session-list poll with one file read."""
    data = _read()
    presets = [row for row in data["presets"] if row["enabled"]]
    return {
        session: [
            {
                "id": preset["id"],
                "name": preset["name"],
                "question": preset["question"],
                **data["reports"].get(session, {}).get(preset["id"], {}),
            }
            for preset in presets
        ]
        for session in sessions
    }
