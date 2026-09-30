"""Where the gate tools root pytest's ``--basetemp`` on this host.

``tools/changed_tests.py``, ``tools/sweep.py`` and ``tools/gate_bench.py``
each hand pytest an explicit basetemp under one root, so that a single
directory listing is the timeline of every run on the machine (start from
CreationTime, width from the ``popen-gw*`` count -- the reasons are in
``changed_tests.session_basetemp``). This module is that root, once.

* **Windows** -- ``C:/t``. Short because MAX_PATH is a hard limit there:
  xdist nests ``popen-gwN/`` under the basetemp and the transcript tests
  fold an absolute cwd back into a filename, so every character of the
  root is spent twice against 260.
* **POSIX** -- ``C:/t`` is not an absolute path there
  (``PurePosixPath("C:/t").is_absolute()`` is False), so pytest resolved it
  against the cwd: inside the checkout under test it either created a
  ``C:/t/...`` tree or, with no ``C:`` directory to put it in, died in
  INTERNALERROR before collecting anything (claunch-wt12o.5). There is no
  260-character ceiling to budget against, so the root is a directory of
  the launcher home instead: per user, outside every checkout, and kept
  across reboots the way ``C:/t`` is.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import config

#: The Windows root. Unchanged from before this module: tests pin its length.
WINDOWS_ROOT = "C:/t"

#: The POSIX root's name under :func:`config.launcher_home`.
POSIX_DIR = "basetemp"


def _windows() -> bool:
    """The host test, alone so a test can take either branch on any host."""
    return os.name == "nt"


def root() -> Path:
    """The directory every gate run's basetemp is created in."""
    if _windows():
        return Path(WINDOWS_ROOT)
    return config.launcher_home() / POSIX_DIR


def ensure(path: Path) -> Path:
    """``path``, created with its parents if it is missing.

    pytest makes the basetemp itself but not its parent
    (``_pytest/tmpdir.py`` ``getbasetemp``: ``mkdir`` without ``parents``),
    so the first run on a host without the root dies in INTERNALERROR with
    ``FileNotFoundError``. A root that cannot be made is left for that same
    error to report: pytest names the path, and housekeeping never decides
    a gate.
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return path
