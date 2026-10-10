"""The daemon's stdout/stderr append to daemon.log; they never overwrite it.

``spawn_daemon`` hands the child a handle on ``daemon.log`` for its stdout
and stderr, while the child's logging handler appends through a handle of
its own. Opened with ``open(..., "ab")`` the inherited handle kept writing at
the position the file had at spawn time on Windows, so the child's first
stderr output landed on top of the log lines written since and erased their
start -- a restart's "restoring N session(s)" line went missing for weeks
(claunch-t8bgj.1).
"""

from __future__ import annotations

import os
import subprocess
import sys

from claude_launcher import daemon_client
from claude_launcher.daemon import paths

CHILD = r"""
import logging, sys
logging.basicConfig(level=logging.INFO, format="%(message)s",
    handlers=[logging.FileHandler(sys.argv[1], encoding="utf-8")])
for i in range(3):
    logging.info("logline-%d kept whole", i)
for h in logging.getLogger().handlers:
    h.flush()
sys.stderr.write("stderr after the log lines\n")
sys.stderr.flush()
"""


def test_child_stderr_lands_after_the_log_lines(home):
    paths.daemon_dir().mkdir(parents=True, exist_ok=True)
    log = paths.log_file()
    log.write_text("before the spawn\n", encoding="utf-8")

    fd = daemon_client._open_log_for_child()
    try:
        subprocess.run(
            [sys.executable, "-c", CHILD, str(log)],
            stdin=subprocess.DEVNULL, stdout=fd, stderr=fd, check=True,
        )
    finally:
        os.close(fd)

    assert log.read_text(encoding="utf-8").splitlines() == [
        "before the spawn",
        "logline-0 kept whole",
        "logline-1 kept whole",
        "logline-2 kept whole",
        "stderr after the log lines",
    ]


def test_the_log_is_created_when_missing(home):
    paths.daemon_dir().mkdir(parents=True, exist_ok=True)
    log = paths.log_file()
    assert not log.exists()
    fd = daemon_client._open_log_for_child()
    try:
        os.write(fd, b"first\n")
    finally:
        os.close(fd)
    assert log.read_bytes() == b"first\n"
