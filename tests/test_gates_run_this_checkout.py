"""A cflow gate must run the tree it is checking.

The engine runs a step's ``verify`` synchronously as the run leaves the step
(``cflow/engine.py`` ``_run_verify``, ``shell=True``, ``cwd`` = the run's
directory). Whatever that command resolves to is what "the gate passed" means.
Three different spellings have now been shipped in this repository, and each
was wrong in a way the previous check could not see:

1. ``claunch report check``. ``claunch`` on PATH is an installed copy of
   whichever checkout was last installed -- not the branch under test. It
   answered ``invalid choice: 'report'`` and exited 2, so the gate could not
   pass in any session until the branch landed and was reinstalled.
   :func:`test_no_gate_command_names_a_bare_executable` is this one.

2. ``uv run --no-sync python -m claude_launcher.cli report check`` -- the fix
   for (1), and quieter. This project is a ``src`` layout, so ``-m`` cannot
   find the package under the working directory and has to take it from
   ``site-packages``; but ``--no-sync`` is a promise never to populate the
   worktree's ``.venv``, and ``uv`` ignores the ambient ``VIRTUAL_ENV`` (it
   prints that it is doing so, then does it). Measured in a worker worktree:
   ``ModuleNotFoundError: No module named 'claude_launcher'``, exit 1.

3. ``uv run --no-sync python tools/<script>.py`` -- the form the other three
   gates already used, and the one (2) was replaced by. It fixes *which file
   runs* and not *which package that file imports*. ``tools/sweep.py`` and
   ``tools/deploy_check.py`` both import ``claude_launcher`` inside their
   functions, and both died the same way on the same bare ``.venv``. Those two
   gates had been passing only in checkouts where somebody happened to have
   run ``uv sync`` by hand; at the time of writing roughly half the worktrees
   on this machine had no ``.venv`` or an empty one.

(2) and (3) share a shape worth naming, because it is the dangerous one. The
gate's answer depended on the disk state of a directory the gate does not
manage. Here it failed loudly, which is luck: a *synced* worktree's editable
install points at that worktree's own ``src`` (checked, three of them), so the
same gate was correct there. Had the resolved copy merely been a different
version of the same code rather than absent, the gate would have gone green
against the wrong tree and said nothing at all.
:func:`test_a_gate_ignores_an_installed_copy_of_the_same_name` is the test for
that silent green specifically: it plants a decoy that would pass, and demands
the gate ignore it.

So the rule these pin, in one sentence: **a gate names a file inside this
checkout, and that file puts this checkout's ``src`` in front of every
installed copy before importing anything.** Neither PATH nor any ``.venv`` can
change a gate's answer.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from claude_launcher.cflow import model, state as state_mod

ROOT = Path(__file__).resolve().parents[1]
OVERRIDES = ROOT / ".claunch" / "workflows"


def _load(path: Path) -> model.Workflow:
    """A project-layer file as the workflow it composes to.

    A file here may be a layer (``extends:``) over the project copy of
    its base -- improv-worker-remote over improv-worker -- and its gates
    are then the base's plus its own. ``model.load`` refuses such a file;
    resolving the base the way a run in this repository would is the
    only reading under which "every gate in the project layer" is true.
    """
    return model.compose(path, resolve=state_mod.base_resolver(str(ROOT))).workflow
SRC = ROOT / "src"

#: The prefix every gate command must carry. ``--no-sync`` is not decoration:
#: a live daemon holds ``claunch.exe`` open, and a sync inside a gate has been
#: measured failing with os error 5 (see the NO_SYNC note in
#: tests/test_project_layer_override.py, which pins the same string).
NO_SYNC = "uv run --no-sync python "


def _gates():
    """Every armed gate in this repository's project layer: (file, step, cmd).

    ``awaits`` probes count. They are the same kind of thing as a ``verify``
    -- a command this repository names, whose exit code is taken as the fact
    -- and the project layer grafts both fields for exactly that reason
    (``tools/sync_project_layer.py``, ``GRAFT_FIELDS``). The difference is
    who runs it: the daemon's reminder clock, on a run standing still, rather
    than the engine on the way out of a step. That makes a probe *worse* to
    get wrong, not better. A ``verify`` that resolves to the wrong tree fails
    where somebody is waiting for it; a probe that does answers nobody, on a
    clock, in a session that is idle by design -- and the run's whole reason
    for having one is that nobody is watching. ``awaits: verify`` is the same
    command as the step's own and is not counted twice.
    """
    found = []
    for path in sorted(OVERRIDES.glob("*.yaml")):
        wf = _load(path)
        for step_id, step in wf.steps.items():
            seen = set()
            for cmd in (
                step.verify.command if step.verify else None,
                step.awaits.command(step) if step.awaits else None,
            ):
                if cmd and cmd not in seen:
                    seen.add(cmd)
                    found.append((path.name, step_id, cmd))
    return found


def _gate_scripts():
    """The files the gates name, for the tests that ask what those files do.

    Only targets that are actually files. A command naming a bare executable
    or a ``-m`` module has no file to examine, and that is not this axis's
    complaint anyway -- :func:`test_no_gate_command_names_a_bare_executable`
    owns it, and owning it alone is what keeps a broken gate to one red test
    with the right name on it. An absolute path *is* a file and stays in:
    resolving to a file in another checkout is exactly what the next test
    catches.
    """
    scripts = []
    for _file, _step, cmd in _gates():
        rest = cmd[len(NO_SYNC):] if cmd.startswith(NO_SYNC) else cmd
        target = ROOT / rest.split()[0]
        if target.is_file() and target not in scripts:
            scripts.append(target)
    return scripts


def _clean_env():
    """The ambient environment minus the knobs these tests set themselves."""
    return {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}


def test_the_project_layer_actually_arms_some_gates():
    """Guard the guards: an empty sweep would make every test below vacuous.

    Every other test here iterates over :func:`_gates`. A yaml edit that
    dropped the ``verify`` fields -- which is exactly what claunch-ybf is
    about, a mistyped field name the parser discards without a word -- would
    turn them all green by finding nothing to check.
    """
    assert len(_gates()) >= 3


@pytest.mark.parametrize("field", ["verify", "awaits"])
def test_the_parser_sees_every_gate_field_the_files_spell(field):
    """A ``verify:`` in the text is a ``verify`` in the model, or it is a typo.

    claunch-ybf: the workflow parser drops a field whose name it does not know,
    silently. A gate spelled ``verfiy:`` therefore does not exist, and nothing
    anywhere says so -- the step simply stops being gated. Counting the lines
    against the parsed model is the cheapest thing that notices.

    ``awaits`` is held to it for the same reason and needs it more: a dropped
    ``verify`` at least stops gating something a person is waiting on, while a
    dropped ``awaits`` removes a signal nobody is waiting for by construction
    -- the run sits idle exactly as it would have, and the silence it was
    added to break is indistinguishable from the silence of it working.
    """
    for path in sorted(OVERRIDES.glob("*.yaml")):
        spelled = sum(
            1
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip().startswith(f"{field}:")
        )
        doc = model.read_doc(path.read_text(encoding="utf-8"), where=str(path))
        if model.extends_ref(doc) is not None:
            # A layer spells only what it adds; the base's fields are not
            # in this file. So the count is taken against the layer's own
            # steps: every field it spells must survive into the composed
            # workflow on that step, which is the same typo check.
            wf = _load(path)
            parsed = sum(
                1
                for step_id, raw in (doc.get("steps") or {}).items()
                if isinstance(raw, dict) and raw.get(field) is not None
                and getattr(wf.steps[step_id], field) is not None
            )
        else:
            parsed = sum(
                1 for s in _load(path).steps.values() if getattr(s, field) is not None
            )
        assert spelled == parsed, (
            f"{path.name}: {spelled} {field!r} line(s) in the file but {parsed} "
            "in the parsed workflow -- the parser discarded one. A gate the "
            "parser does not see is not a gate, and nothing else reports it."
        )


@pytest.mark.parametrize("wf_file,step,cmd", _gates(), ids=lambda v: str(v)[:40])
def test_no_gate_command_names_a_bare_executable(wf_file, step, cmd):
    """Failure mode (1): the gate runs whatever PATH happens to hold.

    The command must reach into this checkout by a *relative path to a file
    that exists here*. ``claunch ...``, ``pytest ...`` and ``python -m pkg``
    all fail this, and so does an absolute path into a different checkout --
    the spelling that would pass a prefix check while running someone else's
    tree.
    """
    assert cmd.startswith(NO_SYNC), (
        f"{wf_file}:{step} must reach this checkout with '{NO_SYNC}...'; got {cmd!r}"
    )
    target = cmd[len(NO_SYNC):].split()[0]
    assert not target.startswith("-"), (
        f"{wf_file}:{step} runs 'python {target}' -- a flag, not a file in this "
        f"checkout. '-m pkg' resolves out of site-packages, which --no-sync "
        f"guarantees is empty in a worktree. Name a tools/ script instead. "
        f"Got {cmd!r}"
    )
    assert not Path(target).is_absolute(), (
        f"{wf_file}:{step} names an absolute path ({target}). An absolute path "
        f"pins the gate to one checkout, so a worktree would be checked by "
        f"another tree's code and never know."
    )
    resolved = (ROOT / target).resolve()
    assert resolved.is_file(), (
        f"{wf_file}:{step} names {target}, which is not a file in this checkout"
    )
    assert str(resolved).startswith(str(ROOT)), (
        f"{wf_file}:{step} escapes the checkout: {target}"
    )


@pytest.mark.parametrize("script", _gate_scripts(), ids=lambda p: p.name)
def test_a_gate_script_puts_this_checkout_first(script):
    """Failure mode (3): the right file, importing the wrong package.

    Runs the script's module level in a subprocess whose working directory is
    elsewhere and whose ``PYTHONPATH`` points somewhere else again, then asks
    where ``sys.path`` now starts. The answer has to be this checkout's
    ``src`` -- that is the whole mechanism, and it is what makes a gate's
    answer independent of the ``.venv`` it happens to run under.
    """
    probe = textwrap.dedent(
        """
        import importlib.util, sys
        spec = importlib.util.spec_from_file_location("gate_under_test", sys.argv[1])
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        print(sys.path[0])
        """
    )
    out = subprocess.run(
        [sys.executable, "-c", probe, str(script)],
        cwd=str(ROOT.parent),
        env={**_clean_env(), "PYTHONPATH": str(ROOT.parent)},
        capture_output=True,
        text=True,
        # The engine decodes a gate's output exactly this way (_run_verify).
        # Without it these calls inherit the console's cp949 on this machine
        # and a gate that prints an em dash raises UnicodeDecodeError in the
        # reader thread -- the test would fail on the message, not the rule.
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert out.returncode == 0, f"{script.name} failed to import: {out.stderr[-1500:]}"
    first = Path(out.stdout.strip())
    assert first == SRC, (
        f"{script.name} left sys.path[0] = {first}, not this checkout's {SRC}. "
        f"Without that line the script imports claude_launcher from whatever "
        f"the environment supplies -- which is nothing at all in a worktree "
        f"--no-sync never populated (measured: ModuleNotFoundError, exit 1), "
        f"or another checkout's copy if one happens to be installed."
    )


@pytest.mark.parametrize("script", _gate_scripts(), ids=lambda p: p.name)
def test_a_gate_runs_without_anything_installed(script):
    """The gate holds on the standard library alone.

    ``python -S`` drops ``site-packages`` -- the closest deterministic stand-in
    for the bare ``.venv`` that ``uv run --no-sync`` leaves in a fresh
    worktree. The gate is allowed to fail *its own check* here (exit 1 is a
    verdict, not a breakage); what it may not do is fail to load. This is the
    test that would have caught routing the wrapup gate through ``cli.main``,
    which builds every subparser and so imports ``yaml``.
    """
    out = subprocess.run(
        [sys.executable, "-S", "-E", str(script), "--help"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        # The engine decodes a gate's output exactly this way (_run_verify).
        # Without it these calls inherit the console's cp949 on this machine
        # and a gate that prints an em dash raises UnicodeDecodeError in the
        # reader thread -- the test would fail on the message, not the rule.
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert "ModuleNotFoundError" not in out.stderr, (
        f"{script.name} cannot load without site-packages:\n{out.stderr[-1500:]}\n"
        f"A gate runs under 'uv run --no-sync', which never populates the "
        f"worktree's .venv. Reach only the standard library and this "
        f"checkout's own src."
    )


def test_a_gate_ignores_an_installed_copy_of_the_same_name(tmp_path):
    """The silent green, staged: a decoy that would pass, and must not be used.

    The measured failures were all loud -- a missing module, a bad subcommand.
    The dangerous version of the same defect is not loud: an installed
    ``claude_launcher`` that answers to the same names with *different
    behaviour* makes the gate exit 0 while looking at nothing. That case
    carries no error message anywhere, so it needs a test rather than a report.

    So: plant a ``claude_launcher`` earlier on ``PYTHONPATH`` whose
    ``cli_report.check_main`` unconditionally succeeds and leaves a marker.
    Run the real gate. If the marker exists, a gate in this repository can be
    satisfied by a package it did not come with.
    """
    decoy = tmp_path / "decoy"
    pkg = decoy / "claude_launcher"
    pkg.mkdir(parents=True)
    marker = tmp_path / "decoy-was-used"
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "cli_report.py").write_text(
        "def check_main(argv=None):\n"
        f"    open(r'{marker}', 'w').write('used')\n"
        "    print('report ok: (decoy)')\n"
        "    return 0\n",
        encoding="utf-8",
    )

    gate = ROOT / "tools" / "report_check.py"
    out = subprocess.run(
        [sys.executable, str(gate), "--session", "no-such-session-for-a-test"],
        cwd=str(ROOT),
        env={**_clean_env(), "PYTHONPATH": str(decoy)},
        capture_output=True,
        text=True,
        # The engine decodes a gate's output exactly this way (_run_verify).
        # Without it these calls inherit the console's cp949 on this machine
        # and a gate that prints an em dash raises UnicodeDecodeError in the
        # reader thread -- the test would fail on the message, not the rule.
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )

    assert not marker.exists(), (
        "the gate imported the decoy claude_launcher off PYTHONPATH instead of "
        "the one in this checkout. The decoy exits 0, so this is the silent "
        "green: the gate would report a healthy round while never looking at "
        f"this tree.\nstdout: {out.stdout!r}\nstderr: {out.stderr[-800:]!r}"
    )
    assert "(decoy)" not in out.stdout
