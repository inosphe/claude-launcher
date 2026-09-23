"""Source-preserving workflow editor used by the dashboard.

The editor never serializes a parsed workflow back to YAML.  That would drop
comments, ordering, and fields introduced after this version of the UI.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from . import model, state


class EditConflict(Exception):
    """The source changed after the browser read it."""


def declarations(cwd: str) -> list[dict]:
    """All visible source files, including declarations shadowed by a layer."""
    rows = []
    for found in state.resolved_workflows(cwd):
        for path in (found.path, *found.shadows):
            rows.append({
                "name": found.name,
                "layer": state.layer_of(path, cwd),
                "path": str(path),
                "active": path == found.path,
            })
    return rows


def source_path(cwd: str, name: str, layer: str, selected_path: str = "") -> Path:
    """Resolve only a declaration found in the requested workflow layers."""
    for row in declarations(cwd):
        if (row["name"] == name and row["layer"] == layer
                and (not selected_path or row["path"] == selected_path)):
            return Path(row["path"])
    raise model.WorkflowError(f"no {layer} declaration named {name!r}")


def revision(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _editor_text(path: Path) -> str:
    # HTML textareas normalize line endings to LF. Return that same spelling so
    # opening a CRLF file does not immediately appear as an unsaved edit.
    return model.read_file(path).replace("\r\n", "\n").replace("\r", "\n")


def read(cwd: str, name: str, layer: str, selected_path: str = "") -> dict:
    path = source_path(cwd, name, layer, selected_path)
    text = _editor_text(path)
    return {"name": name, "layer": layer, "path": str(path),
            "text": text, "revision": revision(text)}


def validate(cwd: str, name: str, layer: str, text: str,
             selected_path: str = "") -> dict:
    """Validate a draft through the same layer resolution and parser as runs."""
    path = source_path(cwd, name, layer, selected_path)
    wf, composed, chain = _compose_draft(cwd, path, path, text)
    # A global base can be valid alone while breaking the active project
    # overlay. Check every declaration whose extends chain uses this file.
    for found in state.resolved_workflows(cwd):
        if found.path == path:
            continue
        try:
            existing = state.compose_located(found, cwd)
        except model.WorkflowError:
            # A pre-existing invalid definition is not caused by this draft.
            continue
        if path.resolve() not in {base.resolve() for base in existing.bases}:
            continue
        try:
            _compose_draft(cwd, found.path, path, text)
        except model.WorkflowError as exc:
            raise model.WorkflowError(f"{found.name} depends on this draft: {exc}") from exc
    return {"name": wf.name, "steps": wf.step_count(), "start": wf.start,
            "warnings": list(wf.warnings), "advice": list(wf.advice),
            "deprecations": list(wf.deprecations),
            "bases": [str(p) for p in chain[1:]],
            "effective_yaml": model.yaml.safe_dump(
                composed, sort_keys=False, allow_unicode=True)}


def _compose_draft(cwd: str, source: Path, edited: Path, text: str):
    docs = []
    chain = []
    seen = set()
    current = source
    while True:
        identity = current.resolve()
        if identity in seen:
            raise model.WorkflowError("extends cycle: " + " -> ".join(map(str, chain + [current])))
        seen.add(identity)
        chain.append(current)
        doc = model.read_doc(text if current == edited else model.read_file(current), where=str(current))
        docs.append(doc)
        ref = model.extends_ref(doc)
        if ref is None:
            break
        if len(chain) > model.MAX_EXTENDS_DEPTH:
            raise model.WorkflowError(f"extends chain deeper than {model.MAX_EXTENDS_DEPTH}")
        current = state.resolve_base(ref, current, cwd)
    composed = model.compose_docs(docs)
    wf = model.parse_doc(composed, default_name=source.stem)
    return wf, composed, chain


def save(cwd: str, name: str, layer: str, text: str, expected_revision: str,
         selected_path: str = "") -> dict:
    path = source_path(cwd, name, layer, selected_path)
    current = _editor_text(path)
    if revision(current) != expected_revision:
        raise EditConflict("workflow changed on disk; reload before saving")
    checked = validate(cwd, name, layer, text, str(path))
    # Keep source text, comments and key order. Replacement is atomic on the
    # same filesystem; a failed validation never writes the source file.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="",
                                         dir=path.parent, prefix=f".{path.stem}-",
                                         suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text)
        os.chmod(temporary, path.stat().st_mode)
        if revision(_editor_text(path)) != expected_revision:
            raise EditConflict("workflow changed on disk; reload before saving")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {**read(cwd, name, layer, str(path)), "validation": checked}
