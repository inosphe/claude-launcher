"""The suite proves, from inside itself, which source tree it just measured.

Every worker runs in its own worktree, and a measurement is only about that
worktree if ``import claude_launcher`` resolved there. The convention has
always said to check that by hand. The hand check was
``python -c "import claude_launcher"`` -- and that command answers a
different question than the suite does:

* the venv carries an editable install, so ``_editable_impl_claude_launcher``
  puts the MAIN checkout's ``src`` on ``sys.path`` for any bare interpreter;
* ``pyproject.toml`` sets ``pythonpath = ["src"]``, and pytest resolves that
  against the directory holding the ini file
  (``_pytest/config/__init__.py``, ``_decode_value``: ``dp = self.inipath.parent``)
  then inserts it at the FRONT of ``sys.path``
  (``_configure_python_path``: ``sys.path.insert(0, str(path))``) -- in the
  session and in every xdist worker.

Front insertion is why pytest wins and the bare interpreter does not: both
paths are present, but the ini one is ahead of the ``.pth`` one.

So in a worktree whose ``.venv`` is empty, the bare probe reports the main
checkout while pytest is reading the worktree. The probe says "contaminated"
about a run that was clean. That false positive is not hypothetical: it sent
two sessions to re-sync venvs they did not need to re-sync, and it briefly
invalidated a green measurement that was fine.

The fix is not a better sentence in the convention -- a probe has to travel
the path it is making a claim about. These two run inside the suite, so they
cannot drift from it, and a new session inherits them without being told
anything.

``test_multi_daemon_mesh`` already carries the second one's idiom in prose
(``the subprocess must import the same source tree the test runs against``).
It is one line, in one file, and every other subprocess in the suite happens
to be a stub that never imports this package -- so nothing today would catch
its removal. This does.
"""

import os
import subprocess
import sys
from pathlib import Path

import claude_launcher

#: What a child is asked to print: the file the name resolved to, nothing else.
ECHO = "import claude_launcher, sys; sys.stdout.write(claude_launcher.__file__)"


def _imported_tree() -> Path:
    """The directory that would be on ``sys.path`` for this import."""
    return Path(claude_launcher.__file__).resolve().parents[1]


def test_the_suite_imports_the_tree_it_is_run_from(pytestconfig):
    """The package under test comes from this rootdir, not another checkout.

    Failing here means the run measured somebody else's source. An empty
    ``.venv`` is NOT a cause -- ``pythonpath`` still points pytest at this
    tree, and reading that case as contamination is the false positive this
    file exists to retire. What actually breaks it:

    * ``pythonpath`` gone from ``pyproject.toml``, or overridden on the
      command line (``-o pythonpath=``);
    * ``PYTHONPATH`` naming another checkout -- the environment is searched
      before the ini value's own fallback, so it wins;
    * pytest pointed at a tree other than the one it is collecting from.
    """
    root = Path(pytestconfig.rootpath).resolve()
    got = _imported_tree()
    assert got == root / "src", (
        f"this run measured {got}, not {root / 'src'}. Every number it "
        f"produces is about that other tree. Check `pythonpath` in "
        f"pyproject.toml and PYTHONPATH in this environment -- one of them "
        f"is pointing away from here. Re-running 'uv sync' does not fix "
        f"this and never did."
    )


def test_a_child_process_is_handed_the_tree_the_suite_is_reading():
    """The PYTHONPATH idiom that carries this tree into a subprocess works.

    ``pythonpath`` is a pytest setting: it edits this process's ``sys.path``
    and nothing else. A child gets the venv's view instead -- the editable
    ``.pth``, i.e. the main checkout -- unless it is handed the tree
    explicitly. Deriving it from ``claude_launcher.__file__`` rather than
    from a guessed path is what keeps the child on whatever the parent
    actually imported.
    """
    env = os.environ.copy()
    env["PYTHONPATH"] = (
        str(_imported_tree()) + os.pathsep + env.get("PYTHONPATH", "")
    )
    done = subprocess.run(
        [sys.executable, "-c", ECHO],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    assert (
        Path(done.stdout.strip()).resolve()
        == Path(claude_launcher.__file__).resolve()
    )
