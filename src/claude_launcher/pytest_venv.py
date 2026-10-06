"""Whether pytest is running on the interpreter of the tree it collects.

``uv run --no-sync pytest`` in a worktree whose ``.venv`` holds no pytest
does not fail: ``pytest`` is a console script, so uv finds it on PATH -- the
main checkout's -- and that script's shebang starts the main checkout's
interpreter (claunch-3gx, seen by s113, s117, s118 and s156). The source is
still this tree's: ``pythonpath = ["src"]`` puts ``<rootdir>/src`` ahead of
the editable install (``tests/test_tree_isolation.py`` pins that). What is
someone else's is the interpreter, pytest, xdist and every plugin, and
nothing says so.

The gate already refuses that run (``tools/changed_tests.py``
``foreign_pytest``, claunch-7sj). A hand-typed pytest and ``tools/sweep.py``
do not pass through the gate, so ``tests/conftest.py`` asks this module at
session start and prints the answer. It warns and does not refuse: the run
still measures this tree's source.

The test is narrow on purpose. It speaks only when the tree *has* a
``.venv`` and the running interpreter is not that one. A tree with no
``.venv`` -- CI installing into an environment outside the checkout -- is
not the case this is about, and warning there would teach readers to skip
the line.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

#: The project environment's directory, as uv creates it.
VENV_DIR = ".venv"


def _same(a: Path, b: Path) -> bool:
    """Path equality that holds across symlinks and Windows case folding."""
    try:
        a, b = a.resolve(), b.resolve()
    except OSError:
        pass
    return os.path.normcase(str(a)) == os.path.normcase(str(b))


def foreign_interpreter(
    rootdir: Path, prefix: str, executable: str
) -> Optional[str]:
    """A warning when ``<rootdir>/.venv`` exists and ``prefix`` is not it.

    ``prefix`` is ``sys.prefix`` (the environment the interpreter runs in)
    and ``executable`` is ``sys.executable``, quoted in the warning. None
    when the tree has no ``.venv`` or the run is already on it.
    """
    venv = Path(rootdir) / VENV_DIR
    if not venv.is_dir() or _same(Path(prefix), venv):
        return None
    return (
        f"WARNING (claunch-3gx): this pytest runs on another environment's "
        f"interpreter.\n"
        f"  running:  {executable}\n"
        f"  expected: the interpreter in {venv}\n"
        f"The source under test is still {Path(rootdir) / 'src'}, but pytest, "
        f"xdist and the plugins are that other environment's.\n"
        f"Fix: run 'uv sync --extra test' in {rootdir}, then "
        f"'uv run --no-sync pytest ...'."
    )
