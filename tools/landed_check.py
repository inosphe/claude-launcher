"""``landed_check`` as a gate command -- run from the checkout, not from PATH.

The check lives in :mod:`claude_launcher.gates.landed_check`, where every
repository reaches it through ``claunch gate landed-check``. This file is
how this repository's own gates (the v1 project layer, and ``claunch gate``
itself when it runs inside a claude-launcher checkout) reach the copy in the
tree being checked rather than the installed one -- the rule
``tests/test_gates_run_this_checkout.py`` holds every gate under ``tools/``
to. Same flags and exit codes: 0 landed, 1 not yet, 2 cannot tell.
"""

from __future__ import annotations

import sys
from pathlib import Path

# A gate runs the tree it is checking. This checkout's ``src`` goes in front of
# every installed copy, so the import below resolves HERE.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from claude_launcher.gates import landed_check as _impl  # noqa: E402

# The module's names, private ones included, so a caller that loads this file
# (tests/test_landed_check.py) reads the same functions and constants.
globals().update(
    {k: v for k, v in vars(_impl).items() if not (k.startswith("__") and k.endswith("__"))}
)
__doc__ = _impl.__doc__

if __name__ == "__main__":
    raise SystemExit(_impl.main())
