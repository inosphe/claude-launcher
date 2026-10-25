"""The daemon raises its open-file soft limit to the hard limit at startup.

The descriptor count grows with the sessions a daemon hosts (one PTY pair
each, plus browser sockets, sweep subprocesses and the files a request walks).
On 2026-10-25 a daemon holding 103 sessions sat at 705 descriptors against a
soft limit of 1024 while its hard limit was 1048576, and ``artifact_cleanup``
then answered ``[Errno 24] Too many open files`` for every item of its plan
(claunch-wo1zj.1).

The measurement happens in a child process: the limit is per process, and the
property that matters — "the daemon that comes up has the whole hard limit,
not what its parent's shell allowed" — only exists in a process that is not
this pytest run. The child lowers its own soft limit first, so a daemon that
did nothing would be visibly left there.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from claude_launcher import config
from claude_launcher.daemon import open_files

REPO = Path(__file__).resolve().parents[1]

#: The soft limit the child starts from. Low enough that a daemon which never
#: looked at the limit is unmistakably still there, high enough for the
#: child's own imports and subprocess pipes.
STARTING_SOFT = 256


def test_supported_matches_the_platform(tmp_path):
    """Windows has no per-process descriptor ceiling of this shape."""
    assert open_files.supported() is (os.name != "nt")


def _run_child(code: str, tmp_path: Path) -> dict:
    """Run ``code`` in a fresh interpreter and return the JSON it printed last.

    The open-file limit is per process, so a raise can only be observed from
    below: the child lowers its own soft limit first. Copied at call time
    because an env built from scratch would drop conftest's isolation
    variables (.claunch/prompts/test-isolation.md rule 3).
    """
    script = tmp_path / "probe.py"
    script.write_text(code, encoding="utf-8")
    child = dict(os.environ)
    child[config.LAUNCHER_HOME_ENV] = str(tmp_path / "home")
    child["CLAUNCH_INSTANCE_BASE"] = ""
    child["CLAUNCH_INSTANCE"] = ""
    child["CLAUNCH_DAEMON"] = ""
    done = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        env=child,
        timeout=180,
        cwd=str(REPO),
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_it_takes_the_whole_hard_limit(tmp_path):
    """The soft limit ends at the hard one, and the record says so.

    Measured in a child that lowers its own soft limit, so the assertion is
    about a change rather than about whatever ceiling this host hands out.
    """
    if not open_files.supported():
        pytest.skip("no RLIMIT_NOFILE on this platform")
    got = _run_child(
        f"""
import json, resource
from claude_launcher.daemon import open_files

soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
resource.setrlimit(resource.RLIMIT_NOFILE, ({STARTING_SOFT}, hard))
before = resource.getrlimit(resource.RLIMIT_NOFILE)
record = open_files.raise_soft_limit()
print(json.dumps({{
    "before": list(before),
    "record": {{"before": list(record["before"]), "after": list(record["after"]),
                "why": record["why"]}} if record else None,
    "after": list(resource.getrlimit(resource.RLIMIT_NOFILE)),
    "line": open_files.describe(record),
}}))
""",
        tmp_path,
    )
    hard = got["before"][1]
    assert got["before"][0] == STARTING_SOFT
    assert got["record"] == {
        "before": [STARTING_SOFT, hard],
        "after": [hard, hard],
        "why": "",
    }
    assert got["after"] == [hard, hard]
    assert f"{STARTING_SOFT} -> {hard}" in got["line"]


def test_it_never_raises_when_the_kernel_refuses(tmp_path):
    """A ceiling this process cannot lift is a log line, not an outage.

    ``setrlimit`` can fail even for a raise — a container may cap it, and a
    host reporting ``RLIM_INFINITY`` as the hard limit need not hand it out.
    A daemon that stopped serving over that would turn a resource ceiling
    into an outage, so the failure is recorded and the process carries on
    with the limit it had.
    """
    if not open_files.supported():
        pytest.skip("no RLIMIT_NOFILE on this platform")

    got = _run_child(
        """
import json
from claude_launcher.daemon import open_files


def refuse(which, values):
    raise OSError(1, "Operation not permitted")


open_files._resource.setrlimit = refuse
open_files._resource.getrlimit = lambda which: (256, 1024)
record = open_files.raise_soft_limit()
print(json.dumps({"record": record, "line": open_files.describe(record)}))
""",
        tmp_path,
    )

    assert "setrlimit failed" in got["record"]["why"]
    assert got["record"]["after"] == got["record"]["before"] == [256, 1024]
    assert "unchanged" in got["line"]


def test_an_already_maximal_limit_is_reported_as_such(tmp_path):
    """Nothing to do is not the same as a failure, and the log says which."""
    if not open_files.supported():
        pytest.skip("no RLIMIT_NOFILE on this platform")

    got = _run_child(
        """
import json
from claude_launcher.daemon import open_files

open_files._resource.getrlimit = lambda which: (4096, 4096)
record = open_files.raise_soft_limit()
print(json.dumps({"record": record, "line": open_files.describe(record)}))
""",
        tmp_path,
    )

    assert got["record"]["why"] == ""
    assert got["record"]["before"] == got["record"]["after"] == [4096, 4096]
    assert "already at its maximum" in got["line"]


def test_describe_is_silent_where_there_is_no_limit():
    """``None`` — no such limit — must not print a line about a limit."""
    assert open_files.describe(None) == ""


def _write_config(home: Path) -> None:
    """The daemon refuses to start without its config file (:func:`missing_config_error`)."""
    home.mkdir(parents=True, exist_ok=True)
    (home / config.CONFIG_NAME).write_text("{}\n", encoding="utf-8")


def test_the_daemon_comes_up_holding_the_hard_limit(tmp_path):
    """The issue's criterion, measured in the process that has the limit.

    The child lowers its own soft limit, then runs ``daemon.__main__.main``
    with the serving coroutine stubbed out, and reports the limit it sees
    *inside* that coroutine — the earliest point at which the daemon is
    actually serving and would be opening the files that exhausted it.
    """
    if not open_files.supported():
        pytest.skip("no RLIMIT_NOFILE on this platform")

    _write_config(tmp_path / "home")
    got = _run_child(
        f"""
import json, resource
from claude_launcher.daemon import __main__ as daemon_main

soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
resource.setrlimit(resource.RLIMIT_NOFILE, ({STARTING_SOFT}, hard))
started = resource.getrlimit(resource.RLIMIT_NOFILE)

seen = {{}}

async def fake_serve(host, port, cfg, bound):
    seen["serving"] = resource.getrlimit(resource.RLIMIT_NOFILE)
    return 0

daemon_main._serve = fake_serve
code = daemon_main.main([])
print(json.dumps({{
    "started": list(started),
    "serving": list(seen.get("serving") or ()),
    "final": list(resource.getrlimit(resource.RLIMIT_NOFILE)),
    "code": code,
}}))
""",
        tmp_path,
    )

    assert got["code"] == 0
    # The child really was constrained before it started, so the value below
    # is a change and not this host's default.
    assert got["started"][0] == STARTING_SOFT
    hard = got["started"][1]
    assert hard > STARTING_SOFT
    assert got["serving"] == [hard, hard]
    assert got["final"] == [hard, hard]


def test_the_module_is_reachable_from_the_startup_path():
    """A guard against the helper existing but never being called.

    The measurement above stubs ``_serve``, so it would still pass if
    ``main`` lost the call; this pins the call itself.
    """
    from claude_launcher.daemon import __main__ as daemon_main

    source = Path(daemon_main.__file__).read_text(encoding="utf-8")
    assert "open_files.raise_soft_limit()" in source
    body = source.split("def main(argv=None)", 1)[1]
    # Called before the singleton lock is taken: the daemon must not be
    # measuring a limit it has already been opening files under.
    assert body.index("open_files.raise_soft_limit()") < body.index("SingletonLock")
