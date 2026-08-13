"""The dashboard's client-side logic, run under node.

The dashboard has no build step and no JS test runner, which was fine while
`app.js` was mostly wiring. The clustered diagram is not: it derives a forest
from parent handles, packs it into columns, places boxes on a ring and clips
edges to them, and none of that is checked by anything Python can see. Nor is
the sidebar's own forest, which orders the session list by lineage.

So the harnesses in ``tests/web`` slice the real functions out of the shipped
``app.js`` — not a copy of them — and exercise them: ``layout_check`` on the
maths, ``render_check`` on the SVG the drawing code assembles against a stub
DOM, ``lineage_check`` on the session list's tree ordering, ``owed_check`` on
the Unanswered box and the requests its nudge/dismiss buttons send,
``panel_check`` on where the session detail docks (the rail beside the
terminal, or the page slot on a phone) and what closing it leaves behind,
``sesssend_check`` on the message that panel can put into the mesh — whose
handle it lands on and what it does with a refusal — ``sessrun_check`` on the
run it can fold open in place of a trip to the run page, ``seq_check`` on the
message trace's event list — the order a mesh's traffic is read in, and what
the picture is allowed to claim about where each message got to —
``seqrender_check`` on the sequence that list is drawn into, and the two
``flow*_check`` harnesses on the flow view — one on
the track a workflow becomes, one on the page those tracks are drawn into.
This wrapper is what makes them run with everything else.

Skipped, not failed, where node is unavailable: node is a convenience for
testing this project, never a requirement for using it.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parent / "web"


@pytest.mark.parametrize(
    "script",
    [
        "layout_check.js",
        "render_check.js",
        "lineage_check.js",
        "owed_check.js",
        "panel_check.js",
        "sesssend_check.js",
        "sessrun_check.js",
        "seq_check.js",
        "seqrender_check.js",
        "flowtrack_check.js",
        "flowrender_check.js",
    ],
)
def test_topology_diagram_logic(script):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    proc = subprocess.run(
        [node, str(WEB / script)],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
