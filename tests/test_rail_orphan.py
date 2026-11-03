"""The rail marks a top-level row whose parent is gone (claunch-ikiys).

``railOrphanOf`` in app.js decides it: a session the daemon recorded as an
orphan (``orphan_of``), or one whose parent is on record but not on the rail
(archived, filtered out), is an orphan rather than a root a human started.
The function is lifted out of app.js and run under node.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / "src/claude_launcher/web/static/app.js"


def _source() -> str:
    text = APP.read_text(encoding="utf-8")
    match = re.search(r"^function railOrphanOf\(.*?^}\n", text, re.S | re.M)
    assert match, "railOrphanOf is missing from app.js"
    return match.group(0)


def _run(cases):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    script = _source() + (
        "const cases = " + json.dumps(cases) + ";\n"
        "console.log(JSON.stringify(cases.map(([s, shown]) =>"
        " railOrphanOf(s, shown ? new Set(shown) : null))));\n"
    )
    out = subprocess.run([node, "-e", script], capture_output=True, text=True,
                         check=True)
    return json.loads(out.stdout)


def test_orphan_rows_name_the_parent_they_lost():
    removed, hidden, root, under, self_parent, unknown_shown = _run([
        [{"name": "kid", "parent": None, "orphan_of": "gone"}, ["kid"]],
        [{"name": "kid", "parent": "archived"}, ["kid"]],
        [{"name": "solo", "parent": None}, ["solo"]],
        [{"name": "w1", "parent": "lead"}, ["lead", "w1"]],
        [{"name": "loop", "parent": "loop"}, ["loop"]],
        [{"name": "kid", "parent": "archived"}, None],
    ])
    assert removed == {"parent": "gone", "title": "orphan: parent gone was removed"}
    assert hidden["parent"] == "archived"
    assert "not among the current sessions" in hidden["title"]
    assert root is None
    assert under is None
    assert self_parent is None
    # without the rail's names only the daemon's record counts
    assert unknown_shown is None


def test_the_row_builder_draws_the_tag_only_at_the_top_level():
    text = APP.read_text(encoding="utf-8")
    assert "const orphan = !depth && typeof railOrphanOf === \"function\"" in text
    # existence is read off every current record, not the filtered rail
    assert "const railKnownNames = new Set(sessionsCache.map((s) => s.name));" in text
    assert "head.append(label, ...(orphanTag ? [orphanTag] : []), keys," in text


def test_the_rail_poll_carries_the_orphan_mark():
    """A field left out of RAIL_FIELDS never reaches the dashboard."""
    from claude_launcher.daemon import api

    assert "orphan_of" in api.RAIL_FIELDS
