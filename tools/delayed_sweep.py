"""``tools/delayed_sweep.py``: the queue tool's old spelling, kept as a wrapper.

The implementation now lives in the package
(:mod:`claude_launcher.delayed_sweep_cli`), so a repository other than this one
reaches the queue through ``claunch delayed-sweep <action>`` without carrying a
copy of this file (claunch-sr6ex). This wrapper stays because the workflows and
the daemon's notices still spell the checkout-relative command, and the tests
run the tool by path; it passes its arguments unchanged and returns the same
exit code.

Only the wrapper is checkout-specific: the package module is the copy that runs,
and the sweep module a job records is resolved from the verified repository
(``<repo>/tools/sweep.py``), never from this file's directory.
"""
from __future__ import annotations

import sys
from pathlib import Path

# A checkout runs this with an interpreter that need not have the package
# installed (``uv run --no-sync python tools/delayed_sweep.py``): the
# checkout's ``src`` goes in front, the same way ``tools/sweep.py`` does it.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from claude_launcher.delayed_sweep_cli import main  # noqa: E402

raise SystemExit(main())
