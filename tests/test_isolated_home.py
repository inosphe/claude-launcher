"""``tools/isolated_home.py``: the isolation a hand-typed reproduction gets.

pytest isolates itself through ``conftest.home``; a script typed by hand gets
nothing. That gap is where ``~/.claunch.yaml`` was overwritten with a fixture
(claunch-wo1zj), so the helper is pinned here: what it sets, what it clears,
and that a process started through it resolves both ``store.path()`` and
``daemon.paths.base_home()`` under the throwaway home -- and fails loudly when
either resolves to a real one.

The subprocess tests never name a real home as their target. They assert on
paths inside pytest's own ``tmp_path``, and the one test that does ask for a
real path expects the guard to refuse before anything is written.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from claude_launcher import config
from claude_launcher.daemon import paths as daemon_paths

REPO = Path(__file__).resolve().parents[1]
TOOL = REPO / "tools" / "isolated_home.py"


def _load():
    # tools/ is not on pythonpath (pyproject sets src/ only) and it is a script
    # rather than a package -- load it by path, the way this repository's other
    # tools tests do.
    spec = importlib.util.spec_from_file_location("isolated_home", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_tool(code: str, base: Path, env: dict = None):
    """Run ``code`` through the helper with its throwaway home under ``base``."""
    child = dict(os.environ if env is None else env)
    child["CLAUNCH_ISOLATED_HOME"] = str(base)
    return subprocess.run(
        [sys.executable, str(TOOL), "--", "python", "-c", code],
        capture_output=True, text=True, env=child, timeout=120,
    )


def test_every_isolation_variable_is_set_and_every_instance_var_is_cleared(tmp_path):
    """The environment the child gets, checked without starting one.

    ``paths.base_home()`` reads ``CLAUNCH_INSTANCE_BASE`` before the launcher
    home (claunch-llmi1), and ``cli.main`` un-applies an instance manifest from
    ``CLAUNCH_INSTANCE_APPLIED``/``PREV``, restoring the pre-manifest home over
    the throwaway one (claunch-egr6y). Cleared rather than pointed elsewhere, so
    the reproduction starts where a fresh shell starts.
    """
    mod = _load()
    base = tmp_path / "iso"
    caller = {name: "leftover" for name in mod.INSTANCE_VARS}
    caller["CLAUNCH_DAEMON_PORT"] = "9333"

    env = mod.environment(base, caller)

    assert env[config.LAUNCHER_HOME_ENV] == str(base / "home")
    assert env[config.LAUNCHER_SEED_ENV] == str(base / "seed")
    assert env[config.CLAUDE_CONFIG_DIR_ENV] == str(base / ".claude-config")
    assert env[config.TEST_REAL_HOMES_ENV]
    assert env["CLAUNCH_ISOLATED_HOME"] == str(base / "home")
    for name in mod.INSTANCE_VARS:
        assert name not in env, f"{name} survived into the child's environment"


def test_the_guard_lists_the_caller_homes_not_the_throwaway_one(tmp_path):
    """``CLAUNCH_TEST_REAL_HOMES`` is read before the isolation is applied.

    Read after, it would list the throwaway home and the guard would refuse the
    very paths the helper is about to hand the child -- the isolation would
    fail on the first ``launcher_home()`` call.
    """
    mod = _load()
    base = tmp_path / "iso"
    caller = {
        "HOME": str(tmp_path / "user-home"),
        config.LAUNCHER_HOME_ENV: str(tmp_path / "caller-home"),
    }

    homes = mod.real_homes(caller)
    env = mod.environment(base, caller)

    guarded = env[config.TEST_REAL_HOMES_ENV].split(os.pathsep)
    assert str(Path(tmp_path / "caller-home")) in guarded
    assert str(Path(tmp_path / "user-home")) in guarded
    # The default home is guarded whether or not the caller overrode anything:
    # a reproduction that unsets the override still must not reach it.
    assert str(config._DEFAULT_HOME) in guarded
    assert not any(str(base) in entry for entry in guarded)
    assert str(base / "home") not in homes


def test_a_process_started_through_it_resolves_everything_under_the_throwaway_home(tmp_path):
    """``store.path()`` and ``paths.base_home()`` -- the issue's first criterion.

    Measured in a separate process because that is the case the accident was:
    a hand-typed script, not a test.
    """
    code = (
        "import json, os\n"
        "from claude_launcher import store, config\n"
        "from claude_launcher.daemon import paths\n"
        "print(json.dumps({\n"
        "    'store': str(store.path()),\n"
        "    'config': str(config.sync_file()),\n"
        "    'home': str(config.launcher_home()),\n"
        "    'base_home': str(paths.base_home()),\n"
        "    'shown': os.environ['CLAUNCH_ISOLATED_HOME'],\n"
        "}))\n"
    )
    base = tmp_path / "iso"
    done = _run_tool(code, base)

    assert done.returncode == 0, done.stderr
    import json

    got = json.loads(done.stdout.strip().splitlines()[-1])
    home = base / "home"
    assert got["store"] == str(home / config.CONFIG_NAME)
    assert got["config"] == str(home / config.CONFIG_NAME)
    assert got["home"] == str(home)
    assert got["base_home"] == str(home)
    assert got["shown"] == str(home)
    # And it says where it went, so the run can be read back afterwards.
    assert f"isolated home: {home}" in done.stdout


def test_a_save_through_it_writes_only_inside_the_throwaway_home(tmp_path):
    """``store.save()`` is the call that did the damage; pin where it lands."""
    code = (
        "from claude_launcher import store\n"
        "store.save({'profiles': {'probe': {'harness': 'claude'}}})\n"
        "print(store.path())\n"
    )
    base = tmp_path / "iso"
    done = _run_tool(code, base)

    assert done.returncode == 0, done.stderr
    written = Path(done.stdout.strip().splitlines()[-1])
    assert written == base / "home" / config.CONFIG_NAME
    assert written.is_file()
    assert base in written.parents


def test_resolving_a_real_home_inside_it_fails_immediately(tmp_path):
    """The issue's second criterion, and claunch-gziru's guard.

    The reproduction breaks its own isolation the way the accident did -- by
    pointing the home at the machine's default -- and the guard refuses at the
    point of resolution, before ``save`` runs.
    """
    code = (
        f"import os\n"
        f"os.environ[{config.LAUNCHER_HOME_ENV!r}] = {str(config._DEFAULT_HOME)!r}\n"
        "from claude_launcher import store\n"
        "try:\n"
        "    store.path()\n"
        "except Exception as exc:\n"
        "    print(type(exc).__name__)\n"
        "else:\n"
        "    print('NO ERROR')\n"
    )
    base = tmp_path / "iso"
    done = _run_tool(code, base)

    assert done.returncode == 0, done.stderr
    assert done.stdout.strip().splitlines()[-1] == config.RealHomeInTest.__name__
    # Nothing was written: the refusal happens at resolution, before save runs.
    assert not (base / "home" / config.CONFIG_NAME).exists()


def test_the_child_checks_before_the_reproduction_body_runs(tmp_path):
    """The prelude runs ``check()`` first, so a script cannot assert nothing.

    A script started any other way has no ``CLAUNCH_ISOLATED_HOME``, and
    ``check()`` says so rather than assuming the environment is fine.
    """
    mod = _load()
    with pytest.raises(RuntimeError, match="was not started through"):
        mod.check()


def test_the_child_refuses_a_home_that_does_not_contain_the_resolved_paths(tmp_path, monkeypatch):
    """``verify`` is the assertion both halves share, and it is not decorative.

    Run with a home that is *not* where the environment points, it must raise --
    otherwise the check would be a print statement wearing an assert's clothes.
    """
    mod = _load()
    monkeypatch.setenv(config.LAUNCHER_HOME_ENV, str(tmp_path / "elsewhere"))
    monkeypatch.delenv(daemon_paths.INSTANCE_BASE_ENV, raising=False)

    with pytest.raises(RuntimeError, match="isolation did not hold"):
        mod.verify(tmp_path / "iso" / "home")


def test_verify_accepts_the_home_the_isolation_actually_points_at(tmp_path, monkeypatch):
    """The same call, with the environment the helper sets, passes.

    Without this the test above would also pass on a ``verify`` that raised
    unconditionally.
    """
    mod = _load()
    base = tmp_path / "iso"
    monkeypatch.setenv(config.LAUNCHER_HOME_ENV, str(base / "home"))
    monkeypatch.delenv(daemon_paths.INSTANCE_BASE_ENV, raising=False)

    mod.verify(base / "home")


def test_the_helper_does_not_pick_a_fixed_temp_name(tmp_path, monkeypatch):
    """``$CLAUNCH_SCRATCH`` when it is set, a pid-carrying name otherwise.

    A fixed name under the machine-wide temp directory is shared by every
    session on this box (claunch-shared-tmp-clobber-xjn): the second writer
    overwrites the first silently, and a verdict built on the overwritten
    values reads as reasonable while being wrong.
    """
    mod = _load()
    scratch = tmp_path / "scratch"
    monkeypatch.setenv(mod.SCRATCH_ENV, str(scratch))
    monkeypatch.setenv(mod.HOME_ENV, "")

    one = mod.make_base(dict(os.environ))
    two = mod.make_base(dict(os.environ))

    assert one.parent == scratch
    assert one != two
    assert str(os.getpid()) in one.name
    assert one.is_dir() and two.is_dir()


def test_a_named_home_is_used_as_given(tmp_path, monkeypatch):
    """``--home``/``CLAUNCH_ISOLATED_HOME`` wins, so a reproduction can keep one."""
    mod = _load()
    named = tmp_path / "chosen"
    monkeypatch.setenv(mod.HOME_ENV, str(named))

    assert mod.make_base(dict(os.environ)) == named
    assert named.is_dir()


def test_a_bare_python_in_the_command_is_dropped(tmp_path):
    """``-- python repro.py`` is what a person types; both halves are python.

    Kept as ``sys.executable python repro.py``, the interpreter would take
    ``python`` as the script name and fail with a confusing ENOENT.
    """
    script = tmp_path / "repro.py"
    script.write_text(
        "from claude_launcher import store\nprint(store.path())\n", encoding="utf-8"
    )
    base = tmp_path / "iso"
    child = dict(os.environ)
    child["CLAUNCH_ISOLATED_HOME"] = str(base)

    done = subprocess.run(
        [sys.executable, str(TOOL), "--", "python", str(script)],
        capture_output=True, text=True, env=child, timeout=120,
    )

    assert done.returncode == 0, done.stderr
    assert done.stdout.strip().splitlines()[-1] == str(base / "home" / config.CONFIG_NAME)


def test_the_script_runs_with_its_own_argv(tmp_path):
    """A reproduction that reads ``sys.argv`` keeps what the caller passed.

    ``runpy`` is used so the isolation check still runs first; getting argv
    wrong there is the quiet way to make a script read the wrong file.
    """
    script = tmp_path / "argv.py"
    script.write_text(
        "import sys\nprint('|'.join(sys.argv[1:]))\n", encoding="utf-8"
    )
    base = tmp_path / "iso"
    child = dict(os.environ)
    child["CLAUNCH_ISOLATED_HOME"] = str(base)

    done = subprocess.run(
        [sys.executable, str(TOOL), "--", str(script), "alpha", "beta"],
        capture_output=True, text=True, env=child, timeout=120,
    )

    assert done.returncode == 0, done.stderr
    assert done.stdout.strip().splitlines()[-1] == "alpha|beta"


def test_the_c_form_the_help_advertises_actually_runs(tmp_path):
    """``-c`` is advertised in the docstring, the epilog and the error message.

    It was not an option at all: argparse read it as an unknown flag and the
    invocation returned exit 2 with "unrecognized arguments: -c", so the one
    usage this file recommends to a person racing to reproduce something was
    the one that did not work. Found in review of claunch-wo1zj, not by any of
    the tests here -- none of them called the advertised form.

    Pinned against the help text too, so the two cannot drift apart again.
    """
    child = dict(os.environ)
    child["CLAUNCH_ISOLATED_HOME"] = str(tmp_path / "iso")

    done = subprocess.run(
        [sys.executable, str(TOOL), "-c", "from claude_launcher import store; print(store.path())"],
        capture_output=True, text=True, env=child, timeout=120,
    )

    assert done.returncode == 0, done.stderr
    assert done.stdout.strip().splitlines()[-1] == str(tmp_path / "iso" / "home" / config.CONFIG_NAME)
    help_text = subprocess.run(
        [sys.executable, str(TOOL), "--help"], capture_output=True, text=True, timeout=60,
    ).stdout
    assert "-c PYTHON" in help_text


def test_c_and_a_command_together_are_refused(tmp_path):
    """Two answers to "what should run" is a mistake, not a preference."""
    done = subprocess.run(
        [sys.executable, str(TOOL), "-c", "print(1)", "--", "echo", "hi"],
        capture_output=True, text=True, timeout=60,
    )
    assert done.returncode != 0
    assert "not both" in (done.stderr + done.stdout)


def test_no_command_is_refused_rather_than_running_an_interactive_shell(tmp_path):
    """An empty invocation must not fall through to something that waits."""
    done = subprocess.run(
        [sys.executable, str(TOOL)],
        capture_output=True, text=True, timeout=60,
    )
    assert done.returncode != 0
    assert "no command" in (done.stderr + done.stdout)


@pytest.mark.parametrize("name", ["AGENTS.md", "CLAUDE.md", ".claunch/prompts/test-isolation.md"])
def test_the_prose_points_at_a_tool_that_exists(name):
    """The issue's fourth criterion, and the only guard the prose gets.

    ``tools/`` is not importable and a markdown file has no module, so neither
    rule of ``tools/changed_tests.py`` can select a test for either side. Naming
    the path here means a rename of the helper breaks a test instead of leaving
    three documents pointing at a file that is gone.
    """
    text = (REPO / name).read_text(encoding="utf-8")
    assert "tools/isolated_home.py" in text, f"{name} no longer names the helper"
    assert TOOL.is_file()
