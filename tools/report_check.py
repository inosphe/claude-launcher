"""``report check`` as a gate command -- run from the checkout, not from PATH.

The ``improv-worker`` wrapup step gates on "this round left its HTML report
where ``claunch report path`` said to put it". The check itself lives in
:mod:`claude_launcher.cli_report`; this file exists only so the gate can
*reach* it from the tree being checked.

Two spellings were tried first and both were wrong, in the two different ways
a gate can be wrong:

* ``claunch report check`` -- the obvious one. ``claunch`` on PATH is an
  installed copy of whichever checkout was last installed, not this one, so it
  answered ``invalid choice: 'report'`` and exited 2. Loud, and caught on the
  first attempt to pass the gate.
* ``uv run --no-sync python -m claude_launcher.cli report check`` -- the fix
  for that. It is still wrong, and quieter about it. This project is a ``src``
  layout, so ``-m`` cannot find ``claude_launcher`` under the working
  directory; it has to come from the environment's ``site-packages``. But
  ``--no-sync`` is a promise never to populate the worktree's ``.venv``, and
  ``uv`` ignores the ambient ``VIRTUAL_ENV`` (it says so, then does it). In a
  worktree nobody happened to ``uv sync`` by hand, that is
  ``ModuleNotFoundError`` and exit 1 -- measured in this very worktree. In a
  worktree somebody did, it works. The gate's answer depended on the disk
  state of a directory the gate does not manage.

So the gate names a file in this checkout and that file puts this checkout's
``src`` in front of every installed copy. Neither the ``.venv`` nor PATH can
change the answer any more. The other gates in ``.claunch/workflows/`` reach
their code the same way, and
``tests/test_gates_run_this_checkout.py`` holds all of them to it.
"""

from __future__ import annotations

import sys
from pathlib import Path

# A gate runs the tree it is checking. This checkout's ``src`` goes in front of
# every installed copy, so the import below resolves HERE -- whatever the
# worktree's .venv holds, and whatever else on the path answers to the same
# name. Pinned by tests/test_gates_run_this_checkout.py.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def main(argv: list | None = None) -> int:
    """Run ``claunch report check`` out of this checkout.

    Arguments are passed through, so the gate can still say ``--issue`` or
    ``--session`` exactly as the CLI documents them.

    It enters at :func:`claude_launcher.cli_report.check_main` rather than at
    ``cli.main``, because ``--no-sync`` means the standard library is all the
    gate can count on and building the full parser imports ``yaml``. That
    reason is written out where the entry point is defined.
    """
    from claude_launcher.cli_report import check_main

    return check_main(sys.argv[1:] if argv is None else argv)


if __name__ == "__main__":
    raise SystemExit(main())
