"""Recorded PRs belonging to one session, without network calls during polling."""

from __future__ import annotations

import copy
import json
import re
import threading
from urllib.parse import urlsplit

from .. import atomic
from ..cflow2.store import RunStore, StoreError
from . import paths

_cache: dict = {}
_lock = threading.RLock()


def clean(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    url = value.get("url") or value.get("pr_url")
    if not isinstance(url, str):
        return None
    try:
        parsed = urlsplit(url)
        match = re.fullmatch(r"/([^/]+/[^/]+)/pull/([1-9][0-9]*)/?", parsed.path)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or not match:
            return None
        if parsed.username or parsed.password:
            return None
    except ValueError:
        return None
    out = {"url": url, "number": int(match[2]), "repo": f"{parsed.netloc}/{match[1]}"}
    for field in ("title", "branch", "base", "status", "source"):
        if isinstance(value.get(field), str) and value[field]:
            out[field] = value[field]
    draft = value.get("isDraft", value.get("draft"))
    if isinstance(draft, bool):
        out["draft"] = draft
    if isinstance(value.get("issues"), list):
        out["issues"] = [v for v in value["issues"] if isinstance(v, str)]
    return out


def _read(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def record(name: str, result: dict, request: dict) -> None:
    """Keep successful wizard results even when terminal delivery is disabled."""
    if not result.get("ok"):
        return
    row = clean({**(result.get("pr") or {}), "branch": result.get("branch"),
                 "base": result.get("base"), "title": request.get("title"),
                 "source": "PR wizard"})
    if row is None:
        return
    path = paths.session_dir(name) / "prs.json"
    with _lock:
        old = _read(path)
        rows = old if isinstance(old, list) else []
        rows = [r for r in rows if isinstance(r, dict) and r.get("url") != row["url"]]
        rows.append(row)
        with atomic.scratch(path) as tmp:
            tmp.write_text(json.dumps(rows), encoding="utf-8")
            atomic.replace(tmp, path)


def for_session(cwd: str, name: str) -> list[dict]:
    """Merge the wizard record and this session's own v2 PR collection.

    The signature includes the authoritative checkpoint, not just state.json
    (which is only a projection). Cache only the small public rows.
    """
    path = paths.session_dir(name) / "prs.json"
    st = RunStore(cwd, name) if cwd else None
    files = [path] + ([st.state_path, st.checkpoint_path] if st else [])
    stamps = []
    for file in files:
        try:
            stat = file.stat()
            stamps.append((str(file), stat.st_mtime_ns, stat.st_size))
        except OSError:
            stamps.append((str(file), None, None))
    key = (str(path), cwd, name)
    with _lock:
        hit = _cache.get(key)
        if hit and hit[0] == stamps:
            return copy.deepcopy(hit[1])
    saved = _read(path)
    candidates = saved if isinstance(saved, list) else []
    try:
        state = st.load_state() if st and st.exists() else {}
    except (OSError, ValueError, StoreError):
        state = {}
    data = state.get("data") or {}
    landing = data.get("landing") or {}
    if isinstance(landing, dict):
        candidates.append({**landing, "source": "workflow"})
    collection = (state.get("collections") or {}).get("prs") or {}
    for row in collection.get("items") or []:
        if isinstance(row, dict):
            candidates.append({**row, "source": "workflow"})
    merged = {}
    for candidate in candidates:
        row = clean(candidate)
        if row:
            merged[row["url"]] = {**merged.get(row["url"], {}), **row}
    rows = list(reversed(list(merged.values())))
    with _lock:
        # Bound the cache when sessions are removed from the daemon.
        if len(_cache) >= 1024:
            _cache.clear()
        _cache[key] = (stamps, rows)
    return copy.deepcopy(rows)
