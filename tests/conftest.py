"""Shared fixtures: isolate every test in its own launcher home + config file."""

from __future__ import annotations

import contextlib
import os
import shutil
import sys
import time
from pathlib import Path

import pytest

from claude_launcher import pytest_venv, test_window


#: The daemon instance variables a runner or session exports. Cleared once,
#: here, as well as per test by the ``home`` fixture: test modules snapshot
#: them when they are imported (tests/test_instance_manifest.py compares each
#: test's start against that snapshot), so a value still set at import time
#: would differ from the per-test cleared one (claunch-w5c4u: 23 errors).
#: A named instance's daemon also exports its manifest's bookkeeping
#: (applied name, previous values, port); left set, the first ``cli.main()``
#: of a worker undoes it and restores the pre-manifest launcher home over the
#: test's throwaway one (claunch-egr6y).
_DAEMON_INSTANCE_VARS = (
    "CLAUNCH_INSTANCE_BASE", "CLAUNCH_INSTANCE", "CLAUNCH_DAEMON",
    "CLAUNCH_INSTANCE_APPLIED", "CLAUNCH_INSTANCE_PREV", "CLAUNCH_DAEMON_PORT",
)
for _name in _DAEMON_INSTANCE_VARS:
    os.environ.pop(_name, None)

_test_window_grant = None
#: The grant this pytest reports its result into, and when it started
#: (claunch-8kald: the window's run history).
_report_grant_id = None
_session_started = 0.0


def _pytest_window_class(args) -> str:
    """Classify pytest's collected scope before collection starts."""
    selected = [str(arg).replace("\\", "/").rstrip("/") for arg in args]
    if not selected or all(arg in (".", "tests") for arg in selected):
        return test_window.SWEEP
    return test_window.TARGETED


def _spend_only_the_grant(config, grant) -> None:
    """Hold the run to the granted xdist width (claunch-8kald).

    The grant's width used to be advice that only the wrapper tools applied;
    a hand-typed ``-n 16`` spent sixteen workers whatever the arbiter said.
    """
    clamped = test_window.clamp_xdist_width(config.option, grant.advisory_n)
    if clamped:
        before, after = clamped
        print(
            f"claunch window: -n {before} cut to the granted width {after} "
            f"({grant.cls} grant {grant.grant_id})",
            file=sys.stderr,
        )


def pytest_sessionstart(session):
    """Guard every direct pytest entry point, including unwrapped commands."""
    global _test_window_grant, _report_grant_id, _session_started
    if os.environ.get("PYTEST_XDIST_WORKER"):
        return
    _session_started = time.monotonic()
    stranger = pytest_venv.foreign_interpreter(
        session.config.rootpath, sys.prefix, sys.executable
    )
    if stranger:
        print(stranger, file=sys.stderr)
    # Only the direct child of a grant owner reports into that grant; a
    # pytest started by a test inside this run must not overwrite it.
    reporter = os.environ.pop(test_window.WINDOW_REPORT_ENV, None) == "1"
    cls = _pytest_window_class(session.config.args)
    inherited = test_window.inherited_grant()
    if inherited is not None:
        if cls == test_window.SWEEP and inherited.cls != test_window.SWEEP:
            raise pytest.UsageError(
                "a targeted parent grant cannot cover an exclusive sweep"
            )
        _spend_only_the_grant(session.config, inherited)
        _report_grant_id = inherited.grant_id if reporter else None
        return
    label = "pytest " + " ".join(str(arg) for arg in session.config.args)
    try:
        _test_window_grant = test_window.acquire(
            cls,
            label=label,
            workers=test_window.requested_xdist_width(session.config.option),
        )
    except test_window.WindowUnavailable as exc:
        raise pytest.UsageError(str(exc)) from exc
    _test_window_grant.install_environment()
    _spend_only_the_grant(session.config, _test_window_grant)
    _report_grant_id = _test_window_grant.grant_id


def _report_to_the_window(session, exitstatus) -> None:
    global _report_grant_id
    grant_id, _report_grant_id = _report_grant_id, None
    if not test_window.reportable(grant_id):
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    result = test_window.pytest_result(
        exitstatus,
        getattr(reporter, "stats", {}) or {},
        getattr(session, "testscollected", 0),
        time.monotonic() - _session_started,
    )
    test_window.report_result(grant_id, result)


def _release_test_window() -> None:
    global _test_window_grant
    grant = _test_window_grant
    if grant is None:
        return
    _test_window_grant = None
    grant.release()
    if os.environ.get(test_window.WINDOW_GRANT_ENV) == grant.grant_id:
        os.environ.pop(test_window.WINDOW_GRANT_ENV, None)
        os.environ.pop(test_window.WINDOW_CLASS_ENV, None)
        os.environ.pop(test_window.WINDOW_WORKERS_ENV, None)


def pytest_sessionfinish(session, exitstatus):
    if not os.environ.get("PYTEST_XDIST_WORKER"):
        _report_to_the_window(session, exitstatus)
    _release_test_window()


def pytest_unconfigure(config):
    # Sessionfinish is skipped by some early pytest failures. Release is
    # idempotent, so this is the process-exit backstop.
    _release_test_window()


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """Point the launcher at a throwaway home, config file and (empty) seed.

    Returns the launcher home directory. ``CLAUDE_LAUNCHER_SEED`` is an empty
    directory so seeding copies nothing (tests never touch the real ~/.claude).

    Autouse, because forgetting it is not a test failure — it is a write into
    the developer's real ``~/.claude-launcher``. ``claunch install --global``
    seeds the global workflow layer there, so a test that installs without
    this fixture quietly edits the machine it is running on, and then passes.
    ``CLAUDE_CONFIG_DIR`` is pointed away for the same reason: the global
    install writes skills and the user-scope ``.claude.json`` under it.
    """
    h = tmp_path / ".home"
    h.mkdir()
    # Dotted names, because this now runs for every test: a fixture that
    # claims `tmp_path/home` or `tmp_path/seed` collides with the tests that
    # build directories of those names themselves.
    seed = tmp_path / ".seed"
    seed.mkdir()
    monkeypatch.setenv("CLAUDE_LAUNCHER_HOME", str(h))
    monkeypatch.setenv("CLAUDE_LAUNCHER_SYNC_FILE", str(h / ".claunch.yaml"))
    monkeypatch.setenv("CLAUDE_LAUNCHER_SEED", str(seed))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude-config"))
    # Devin is the one harness whose install target is not under any of the
    # paths above: its config home is machine-wide (see install.devin_home).
    # Left unset, a test that installs a devin profile would write the MCP
    # registration and five skill trees into the developer's real devin
    # config — and pass. Same reasoning as the two lines above it.
    monkeypatch.setenv("CLAUNCH_DEVIN_HOME", str(tmp_path / ".devin-home"))
    # Cursor Agent reads MCP servers and skills from ~/.cursor regardless of
    # its profile home (see install.cursor_home) -- the same exposure.
    monkeypatch.setenv("CLAUNCH_CURSOR_HOME", str(tmp_path / ".cursor-home"))
    # Metering fronts every API-key provider with a real proxy process; the
    # env-assembly tests would each start one against a made-up upstream.
    # Off by default here — the tests of the feature set it back to "1".
    monkeypatch.setenv("CLAUNCH_METERING", "0")
    # The delayed-sweep runner (and any session) exports the live daemon's
    # instance variables. CLAUNCH_INSTANCE_BASE wins over the launcher home in
    # paths.base_home(), so left set it points every instance path back at the
    # developer's real ~/.claude-launcher and overrides the home above
    # (claunch-llmi1: 24 failures in the delayed sweep, none in a shell).
    for name in _DAEMON_INSTANCE_VARS:
        monkeypatch.delenv(name, raising=False)
    # Workflow check commands say `python`. A runner that starts pytest by
    # absolute interpreter path has no `python` on PATH unless the venv's bin
    # directory is (the runner now adds it; this keeps a bare shell the same).
    monkeypatch.setenv(
        "PATH", str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""))
    # The basetemp root is C:/t on Windows whatever the launcher home is, and
    # the daemon's reaper deletes ended sessions' trees under it. A test that
    # boots the real daemon over a registry of made-up names would otherwise
    # delete real sessions' directories of the same names (claunch-4zc51.2).
    monkeypatch.setattr(
        "claude_launcher.daemon.basetemp_reaper.default_root",
        lambda: tmp_path / ".basetemp-root",
    )
    return h


#: This test process's mark on every PTY host started under it, directly or
#: by a daemon subprocess (``ptyhost.OWNER_ENV``, inherited through the
#: environment): the leak check below ends only hosts that carry it.
PTYHOST_OWNER = f"pytest-{os.getpid()}-{os.urandom(4).hex()}"
os.environ["CLAUNCH_PTYHOST_OWNER"] = PTYHOST_OWNER


def _owned_host_pids(root: Path) -> list:
    """Live PTY hosts under ``root`` (by their pid files) that this test
    process owns -- found by the owner mark in their environment, so a host a
    daemon subprocess started is caught too."""
    found = []
    try:
        pid_files = list(root.rglob("ptyhost.pid"))
    except OSError:
        return found
    for pid_file in pid_files:
        try:
            pid = int(pid_file.read_text(encoding="ascii").strip())
            with open(f"/proc/{pid}/environ", "rb") as fh:
                env = fh.read().split(b"\0")
        except (OSError, ValueError):
            continue
        if f"CLAUNCH_PTYHOST_OWNER={PTYHOST_OWNER}".encode() in env:
            found.append(pid)
    return found


def _end_pid(pid: int) -> None:
    import signal

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.05)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


@pytest.fixture(autouse=True)
def ptyhost_leak_check(request):
    """Fail a test that leaves a PTY host running (claunch-xnnct).

    A host outlives the process that started it by design, and it is the
    parent of the session children; one left behind keeps running children
    nobody watches. Two records find them: the hosts this process started
    (``ptyhost.client.start_host`` keeps them), and the pid files under the
    test's ``tmp_path`` whose process carries this run's owner mark -- the
    one that also sees a host a daemon *subprocess* started. A leftover host
    is ended (SIGTERM ends its sessions first) and the test fails.
    """
    yield
    mod = sys.modules.get("claude_launcher.daemon.ptyhost.client")
    if mod is None:
        return
    started = list(mod._started)
    mod._started.clear()
    leaked = []
    for proc in started:
        # A host that was told to stop is a moment away from gone.
        try:
            proc.wait(timeout=3)
        except Exception:
            leaked.append(proc)
    for proc in leaked:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
            proc.wait(timeout=5)
    leaked_pids = [p.pid for p in leaked]
    tmp = request.node.funcargs.get("tmp_path") if hasattr(request.node, "funcargs") else None
    if tmp is not None:
        owned = _owned_host_pids(Path(tmp))
        # Same grace as above: a host told to stop removes its pid file last.
        deadline = time.monotonic() + 3
        while owned and time.monotonic() < deadline:
            time.sleep(0.1)
            owned = _owned_host_pids(Path(tmp))
        for pid in owned:
            _end_pid(pid)
        leaked_pids += [pid for pid in owned if pid not in leaked_pids]
    if leaked_pids:
        pytest.fail(f"PTY host(s) left running: {leaked_pids}")


@contextlib.contextmanager
def restored_cwd():
    """Put the process's directory back as it was when the block began.

    The daemon's boot moves the process to ``daemon_client.daemon_cwd()``
    (claunch-2yiij.1), and a test that runs ``daemon.__main__.main()`` in
    process takes that move with it. Left in place, every later test on the
    same worker starts in that directory instead of the one pytest gave it.
    """
    before = os.getcwd()
    try:
        yield before
    finally:
        try:
            moved = os.getcwd() != before
        except OSError:  # the directory the test moved to was removed
            moved = True
        if moved:
            os.chdir(before)


@pytest.fixture(autouse=True)
def cwd_restore():
    """No test leaves the worker in another directory (claunch-2yiij.1)."""
    with restored_cwd():
        yield


@pytest.fixture(autouse=True)
def no_linux_clipboard_reads(monkeypatch):
    """Keep the daemon's clipboard history off the developer's clipboard on Linux.

    Every app ``build_app`` makes samples the host clipboard once a second
    while it is served. On Linux a sample is a real process: powershell.exe on
    the Windows side under WSL (``daemon/clipboard.PowerShellBridge``), or
    wl-paste/xclip/xsel on a machine with a display. A hundred and more tests
    serve an app; each would start one and read the developer's clipboard.
    ``_read_linux`` is the one name every such sample goes through, so it
    reads nothing here. The bridge lookup answers None as well, for the tests
    that put the real ``_read_linux`` back to pin which route it takes.
    Windows reads in-process and is left as it was (claunch-lxwwf).
    """
    monkeypatch.setattr("claude_launcher.daemon.clipboard._read_linux", lambda: "")
    monkeypatch.setattr("claude_launcher.daemon.clipboard._wsl_bridge", lambda: None)


@pytest.fixture(scope="session")
def repo_template(tmp_path_factory):
    """Build a git repository once per worker, then copy it per test.

    ``git init`` + identity + one commit is five git processes, and on this
    machine each is ~100ms -- so a per-test ``repo`` fixture spent ~0.6s
    before the test began, across several hundred tests. A ``shutil``
    copy of the same tiny tree is a few milliseconds and the result is
    indistinguishable to git: nothing in a plain repository's ``.git`` is
    an absolute path (linked worktrees are, so never put one in a build).

    Usage, from a module's own fixture::

        @pytest.fixture
        def repo(tmp_path, repo_template):
            return repo_template("sweep", _build, tmp_path / "repo")

    ``key`` names the template (one per distinct ``build``); ``build(path)``
    fills an existing empty directory the first time only; ``dest`` is
    where this test's copy lands and is returned. Identity goes into the
    template's own config so a copy can commit without ``-c`` flags.
    """
    root = tmp_path_factory.mktemp("repo-templates")
    built: dict = {}

    def make(key: str, build, dest: Path) -> Path:
        src = built.get(key)
        if src is None:
            src = root / key
            src.mkdir()
            build(src)
            built[key] = src
        shutil.copytree(src, dest)
        return dest

    return make


@pytest.fixture
def config_file(home):
    """Path to the live config file (``~/.claunch.yaml`` equivalent)."""
    return home / ".claunch.yaml"


@pytest.fixture(scope="session", autouse=True)
def repo_history_guard(pytestconfig):
    """Refuse, for the whole session, any read of THIS repository's history.

    Autouse and session-scoped for the same reason ``home`` is autouse: the
    thing it prevents is not a test failure, it is a *silent* one. Both
    ``tools/sweep.py`` and ``tools/changed_tests.py`` hand a green receipt
    recorded for one commit to another commit with the same tree, and that is
    only correct while nothing in this suite can tell the two apart. See
    ``tests/_repo_history_guard.py`` for what counts as telling them apart --
    naming an object by its hash does not, and one test does that on purpose.

    Yielded so a test can prove the patch is really installed, and so a test
    that genuinely needs the real ``Popen`` can call ``uninstall()``.
    """
    from _repo_history_guard import Guard

    guard = Guard(Path(pytestconfig.rootpath)).install()
    try:
        yield guard
    finally:
        guard.uninstall()
