"""The topology diagram's client-side logic, run under node.

The dashboard has no build step and no JS test runner, which was fine while
`app.js` was mostly wiring. The clustered diagram is not: it derives a forest
from parent handles, packs it into columns, places boxes on a ring and clips
edges to them, and none of that is checked by anything Python can see.

So the two harnesses in ``tests/web`` slice the real functions out of the
shipped ``app.js`` — not a copy of them — and exercise them: ``layout_check``
on the maths, ``render_check`` on the SVG the drawing code assembles, against
a stub DOM. This wrapper is what makes them run with everything else.

Skipped, not failed, where node is unavailable: node is a convenience for
testing this project, never a requirement for using it.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parent / "web"


@pytest.mark.parametrize("script", ["layout_check.js", "render_check.js"])
def test_topology_diagram_logic(script):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    proc = subprocess.run(
        [node, str(WEB / script)],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
