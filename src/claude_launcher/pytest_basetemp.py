"""Where the gate tools root pytest's ``--basetemp`` on this host.

``tools/changed_tests.py``, ``tools/sweep.py`` and ``tools/gate_bench.py``
each hand pytest an explicit basetemp under one root, so that a single
directory listing is the timeline of every run on the machine (start from
CreationTime, width from the ``popen-gw*`` count -- the reasons are in
``changed_tests.session_basetemp``). This module is that root, once.

The root is also where every session's general test directory lives:
``root()/<session>``, handed to the session as ``$CLAUNCH_TEST_DIR`` for
artifacts pytest's basetemp does not cover (browser profiles, playwright
reports, node test output). The exit reaper (``daemon/basetemp_reaper.py``)
reaps the whole per-session subtree when the session ends.

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
* **Configured** -- the ``daemon.test_output_dir`` setting
  (``store.DAEMON_DEFAULTS``) names the root explicitly instead of the
  built-in per-platform default. Empty keeps the default; a relative value
  is anchored at the launcher home so it cannot silently resolve against
  whatever checkout a session happens to stand in.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from . import config, store

log = logging.getLogger(__name__)

#: The Windows root. Unchanged from before this module: tests pin its length.
WINDOWS_ROOT = "C:/t"

#: The POSIX root's name under :func:`config.launcher_home`.
POSIX_DIR = "basetemp"


def _windows() -> bool:
    """The host test, alone so a test can take either branch on any host."""
    return os.name == "nt"


def _default_root() -> Path:
    if _windows():
        return Path(WINDOWS_ROOT)
    return config.launcher_home() / POSIX_DIR


def root() -> Path:
    """The directory every session's test outputs are created in.

    ``daemon.test_output_dir`` overrides the built-in per-platform root
    when set; a relative value is anchored at the launcher home, never at
    a session's cwd, so a misconfigured relative path cannot put test
    trees inside a checkout the way the POSIX ``C:/t`` accident did
    (claunch-wt12o.5). Read live: the setting applies to the next run.
    """
    try:
        configured = store.daemon_config().get("test_output_dir")
    except Exception as exc:
        # Housekeeping never decides a gate: an unreadable config falls back
        # to the built-in root, the same spirit as ensure() below.
        log.warning("test_output_dir unreadable (%s); using the default root", exc)
        configured = None
    if not isinstance(configured, str) or not configured.strip():
        return _default_root()
    path = Path(configured.strip()).expanduser()
    if not path.is_absolute():
        path = config.launcher_home() / path
    return path


def session_test_dir(name: str) -> Path:
    """``name``'s general test directory: ``<root>/<name>``.

    The directory a session writes its non-pytest test artifacts into, as
    named in ``$CLAUNCH_TEST_DIR`` (exported by ``daemon.harness`` and
    created by ``Session.__init__`` alongside the scratch directory). The
    bare session name is one of the shapes ``daemon/basetemp_reaper``
    reaps at the session's exit, so kill and archive end with the tree
    gone without any session remembering to clean it.
    """
    return root() / name


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
