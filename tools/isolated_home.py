"""Run a hand-typed reproduction in a throwaway launcher home.

``tests/conftest.py`` isolates every pytest process: an autouse fixture points
``CLAUDE_LAUNCHER_HOME``, ``CLAUDE_LAUNCHER_SEED`` and ``CLAUDE_CONFIG_DIR`` at
``tmp_path``, clears the daemon instance variables, and exports
``CLAUNCH_TEST_REAL_HOMES`` so that resolving the machine's real home or config
file raises ``config.RealHomeInTest`` (claunch-gziru).

A script typed by hand has none of that. On 2026-10-25 a session isolated only
``CLAUDE_LAUNCHER_HOME`` and then called ``store.save()``; at that time
``store.path()`` was ``config.sync_file()``, which read its own override
(``CLAUDE_LAUNCHER_SYNC_FILE``) and replaced the real ``~/.claude.yaml`` --
7140 bytes, 43 profiles -- with a 211-byte fixture (claunch-wo1zj). The config
file now lives inside the launcher home, so isolating the home isolates it too,
but the missing second half of that accident is what this file supplies: the
same guard pytest has, plus the instance variables, applied in one step before
the reproduction runs.

Usage
-----
    python tools/isolated_home.py -- python my-repro.py
    python tools/isolated_home.py -c 'from claude_launcher import store; ...'

The child inherits the caller's environment with these changes, and is started
in the caller's working directory. The throwaway home is printed on the first
line (``isolated home: <path>``) so a reproduction can be re-read afterwards;
set ``CLAUNCH_ISOLATED_HOME`` to choose the directory instead of letting one be
made under ``$CLAUNCH_SCRATCH``.

What "isolated" covers, and why each variable is here
-----------------------------------------------------
``CLAUDE_LAUNCHER_HOME``
    The launcher home. ``config.launcher_home()`` reads it first; profiles,
    daemons and the config file (``<home>/.claunch.yaml``) all hang off it.
``CLAUDE_LAUNCHER_SEED``
    The config directory a newly created profile is seeded from. Left at its
    default, ``profile.create`` copies the developer's real ``~/.claude``.
``CLAUDE_CONFIG_DIR``
    Claude Code's own config directory, read by ``config.default_config_dir``
    and by the seeder. Same reason as the line above.
``CLAUNCH_INSTANCE_BASE`` and its five siblings
    ``daemon.paths.base_home()`` reads ``CLAUNCH_INSTANCE_BASE`` *before* the
    launcher home, so a leftover value puts ``daemons/<name>`` in the real
    home however well the home itself is isolated (claunch-llmi1).
    ``CLAUNCH_INSTANCE_APPLIED``/``PREV`` are worse: ``cli.main`` uses them to
    undo an instance manifest, which restores the pre-manifest home over the
    throwaway one (claunch-egr6y). All six are removed, not overridden.
``CLAUNCH_TEST_REAL_HOMES``
    The guard itself. ``config.guard_home``/``guard_config`` refuse a path in
    this list, so a reproduction that loses its isolation fails at the point of
    resolution rather than after writing.

The list is read from the environment *before* any of it is replaced -- it is
the caller's real homes that get guarded, not the throwaway ones this file
hands the child.

Exit codes: the child's own status. The child asserts its own isolation before
the reproduction body, so a lost isolation shows up as that assertion's failure
in the child rather than as this file's -- which is the point: the assertion
runs in the process that would have done the writing.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from claude_launcher import config  # noqa: E402

#: Environment variables an instance manifest sets, and that a leftover value
#: turns into a write to the real home. Removed rather than pointed at the
#: throwaway one: a reproduction should start where a fresh shell starts.
INSTANCE_VARS = (
    "CLAUNCH_INSTANCE_BASE", "CLAUNCH_INSTANCE", "CLAUNCH_DAEMON",
    "CLAUNCH_INSTANCE_APPLIED", "CLAUNCH_INSTANCE_PREV", "CLAUNCH_DAEMON_PORT",
)

#: Where the throwaway home goes when the caller does not name one. Inside
#: ``$CLAUNCH_SCRATCH`` when the daemon exported it -- that directory belongs to
#: this session alone, so two sessions running a reproduction cannot land on the
#: same name. ``/tmp`` is one directory for the whole machine here and a fixed
#: name under it is overwritten without an error (claunch-shared-tmp-clobber-xjn).
SCRATCH_ENV = "CLAUNCH_SCRATCH"

#: Names the directory the caller wants used, instead of a generated one.
HOME_ENV = "CLAUNCH_ISOLATED_HOME"

#: The home the reproduction was given, handed to the child so a script can
#: write its fixtures under it rather than guessing.
CHILD_HOME_ENV = "CLAUNCH_ISOLATED_HOME"


def real_homes(env: dict) -> list:
    """The machine's real homes, as the guard should list them.

    Read from ``env`` before anything is replaced: the built-in default home
    (``~/.claude-launcher``), the user's home directory (where the pre-32c18e7e
    ``~/.claunch.yaml`` lived), and whatever ``CLAUDE_LAUNCHER_HOME`` or
    ``CLAUNCH_INSTANCE_BASE`` the caller already had set. Duplicates collapse --
    with no overrides the first two entries are the same shape under different
    names only when ``$HOME`` itself is ``~/.claude-launcher``, which is rare
    but would otherwise make the guard's message ambiguous.
    """
    seen = {}
    for value in (
        str(config._DEFAULT_HOME),
        str(Path(env.get("HOME") or Path.home())),
        env.get(config.LAUNCHER_HOME_ENV, ""),
        env.get("CLAUNCH_INSTANCE_BASE", ""),
    ):
        value = value.strip()
        if not value:
            continue
        # normcase+abspath is the comparison config._real_homes() does; storing
        # the normalised form here means the two agree without re-deriving.
        key = os.path.normcase(os.path.abspath(Path(value).expanduser()))
        seen.setdefault(key, str(Path(value).expanduser()))
    return list(seen.values())


def environment(base: Path, env: dict) -> dict:
    """``env`` with every isolation variable set for a run under ``base``.

    Returns a new dict; the caller's environment is not touched. The guard list
    comes from the caller's environment, so call this before writing ``base``
    over ``CLAUDE_LAUNCHER_HOME`` -- it does that itself, and re-reading the
    list afterwards would guard the throwaway home instead of the real ones.
    """
    base = Path(base)
    child = dict(env)
    homes = real_homes(env)
    child[config.LAUNCHER_HOME_ENV] = str(base / "home")
    child[config.LAUNCHER_SEED_ENV] = str(base / "seed")
    child[config.CLAUDE_CONFIG_DIR_ENV] = str(base / ".claude-config")
    child[config.TEST_REAL_HOMES_ENV] = os.pathsep.join(homes)
    child[CHILD_HOME_ENV] = str(base / "home")
    for name in INSTANCE_VARS:
        child.pop(name, None)
    return child


def verify(home: Path) -> None:
    """Refuse to continue unless this process resolves everything under ``home``.

    ``config.sync_file()`` is ``<home>/.claunch.yaml`` and
    ``daemon.paths.base_home()`` is the home itself (unless an instance
    variable says otherwise, which :func:`environment` clears), so all three
    answers must sit at or below the home the caller named.

    Called in the child, before the reproduction body runs: the child is the
    process whose environment actually matters, and checking there also catches
    a module that resolved the home at import time from something other than
    the environment.

    Reads ``os.environ``, so the isolation has to be in place -- the child gets
    it from :func:`environment` via its own environment, and a caller running
    :func:`apply` sets it explicitly.
    """
    home = Path(os.path.abspath(home))
    resolved = {
        "launcher home": config.launcher_home(),
        "config file": config.sync_file(),
    }
    try:
        from claude_launcher.daemon import paths as daemon_paths

        resolved["daemon base home"] = daemon_paths.base_home()
    except Exception as exc:  # pragma: no cover - import guard only
        raise RuntimeError(f"cannot import daemon.paths to verify isolation: {exc}")
    outside = {}
    for name, path in resolved.items():
        resolved_path = Path(os.path.abspath(path))
        if resolved_path != home and home not in resolved_path.parents:
            outside[name] = str(resolved_path)
    if outside:
        raise RuntimeError(
            "isolation did not hold: "
            + "; ".join(f"{name} -> {path}" for name, path in outside.items())
            + f" (expected everything under {home})"
        )


def make_base(env: dict) -> Path:
    """The throwaway home: ``$CLAUNCH_ISOLATED_HOME`` or a generated one.

    A generated name carries this process's pid, so two reproductions started
    at the same moment cannot share one, and it is printed by :func:`main` so
    the run can be inspected afterwards.

    ``--print-env`` makes one too: the paths it prints are then the ones a real
    run would use, rather than a name that only looks like one.
    """
    named = env.get(HOME_ENV, "").strip()
    if named:
        base = Path(named).expanduser()
        base.mkdir(parents=True, exist_ok=True)
        return base
    scratch = env.get(SCRATCH_ENV, "").strip()
    parent = Path(scratch).expanduser() if scratch else None
    if parent is not None:
        parent.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f"isolated-home-{os.getpid()}-", dir=str(parent) if parent else None))


def apply(base: Path, env: dict) -> dict:
    """Set ``env``'s isolation into this process's environment, and verify it."""
    os.environ.update(environment(base, env))
    verify(Path(base) / "home")
    return os.environ


def check() -> None:
    """The child-side half of :func:`verify`, for a reproduction to call first.

    Not needed in the common case -- :func:`run` puts it in front of whatever
    the caller asked for -- but a script started by other means can call it to
    make the same assertion.
    """
    home = os.environ.get(CHILD_HOME_ENV, "").strip()
    if not home:
        raise RuntimeError(
            f"{CHILD_HOME_ENV} is not set: this process was not started through "
            "tools/isolated_home.py, so nothing guarantees its home is throwaway"
        )
    verify(home)


def run(argv: list, base: Path, env: dict, cwd: str = "") -> int:
    """Start ``argv`` with the isolation applied, and return its exit status.

    ``env`` is copied at call time -- the rule tests/conftest.py sets for
    subprocesses too: a copy made at import time or a dict built from scratch
    would drop the guard variables, and the child would then write wherever its
    own defaults point.
    """
    child = environment(base, env)
    # The child asserts its own isolation before the reproduction body, and the
    # assert is a separate process so a script that replaces ``config`` at
    # import time cannot hide behind the parent's check.
    prelude = (
        "import sys;"
        f"sys.path[:0] = [{str(Path(__file__).resolve().parents[1] / 'src')!r},"
        f" {str(Path(__file__).resolve().parent)!r}];"
        "from isolated_home import check;check()"
    )
    argv = list(argv)
    # ``-- python repro.py`` names the interpreter the caller types out of habit.
    # Both halves run on this interpreter anyway, and running ``sys.executable
    # python repro.py`` would hand the script name to python as its first
    # argument, so drop the leading one.
    if argv[:1] and Path(argv[0]).name in ("python", "python3", "py"):
        argv = argv[1:]
    if argv[:1] == ["-c"]:
        code = argv[1] if len(argv) > 1 else ""
        argv = ["-c", f"{prelude}\n{code}"]
    elif argv and not argv[0].startswith("-"):
        # A script path: run it through runpy so the prelude above still runs
        # first. ``python script.py`` would skip it, and a reproduction script
        # is exactly the case this file exists for. Its directory goes on
        # ``sys.path`` the way ``python script.py`` would put it there -- a
        # reproduction that imports a sibling module is ordinary, and losing
        # that would look like a bug in the reproduction.
        script = argv[0]
        rest = argv[1:]
        argv = [
            "-c",
            f"{prelude}\n"
            "import os, runpy, sys;"
            f"sys.argv = [{script!r}, *{rest!r}];"
            f"sys.path.insert(0, os.path.dirname(os.path.abspath({script!r})));"
            f"runpy.run_path({script!r}, run_name='__main__')",
        ]
    return subprocess.call(
        [sys.executable, *argv],
        env=child,
        cwd=cwd or os.getcwd(),
    )


def main(argv: list = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        description="Run a reproduction in a throwaway launcher home.",
        epilog=(
            "examples:\n"
            "  python tools/isolated_home.py -- python my-repro.py\n"
            "  python tools/isolated_home.py -c 'from claude_launcher import store; print(store.path())'"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--home", metavar="DIR",
        help=f"throwaway home to use (default: a new directory under ${SCRATCH_ENV})",
    )
    parser.add_argument(
        "--print-env", action="store_true",
        help="print the isolation as KEY=VALUE lines and exit, without starting anything",
    )
    parser.add_argument(
        "command", nargs=argparse.REMAINDER,
        help="the command to run (a leading '--' is dropped)",
    )
    args = parser.parse_args(argv)

    env = dict(os.environ)
    if args.home:
        env[HOME_ENV] = args.home
    base = make_base(env)
    if args.print_env:
        for name, value in sorted(environment(base, env).items()):
            if name in (
                config.LAUNCHER_HOME_ENV, config.LAUNCHER_SEED_ENV,
                config.CLAUDE_CONFIG_DIR_ENV, config.TEST_REAL_HOMES_ENV,
                CHILD_HOME_ENV,
            ):
                print(f"{name}={value}")
        return 0

    command = list(args.command)
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        parser.error("no command: pass one after '--', or use -c '<python>'")

    print(f"isolated home: {base / 'home'}")
    # Flushed before the child starts so its output cannot precede the line
    # that says where the run went.
    sys.stdout.flush()
    return run(command, base, env)


if __name__ == "__main__":
    sys.exit(main())
