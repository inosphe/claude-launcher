"""``claunch gate <name>``: a workflow's gate check, reachable from any repository.

A packaged workflow is the same file in every repository, so the command a
probe or a verify names has to exist in every repository. ``python
tools/<x>.py`` exists only in a claude-launcher checkout: elsewhere python
exits 2 on the missing file, and 2 is also the gate's own "could not tell", so
the run waits out three undecided probes and then asks a person
(claunch-eyajw, claunch-bxxji).

Which copy runs:

* Inside a claude-launcher checkout -- a git toplevel holding
  ``src/claude_launcher`` and ``tools/<module>.py`` -- that checkout's script,
  under this interpreter. A gate runs the tree it is checking
  (``tests/test_gates_run_this_checkout.py``): a branch that changes the check
  is measured by its own change, not by whichever copy was installed last.
* Anywhere else, the installed package's module
  (:mod:`claude_launcher.gates`). That is the copy the workflow came with: a
  v2 definition is read from the package unless a layer overrides it.

Arguments after the name pass through unchanged, and so does the exit code.
"""

from __future__ import annotations

import argparse
import importlib
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

#: ``claunch gate`` name -> module under :mod:`claude_launcher.gates`, which is
#: also the file name under a checkout's ``tools/``.
GATES = {
    "landed-check": "landed_check",
}


def checkout_script(module: str, cwd: Optional[str] = None) -> Optional[Path]:
    """``tools/<module>.py`` of the claude-launcher checkout ``cwd`` is in, or
    None when ``cwd`` is not in one (or git cannot say)."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], cwd=cwd, capture_output=True,
            text=True, encoding="utf-8", errors="replace",
        )
    except OSError:
        return None
    top = proc.stdout.strip()
    if proc.returncode != 0 or not top:
        return None
    root = Path(top)
    script = root / "tools" / f"{module}.py"
    if (root / "src" / "claude_launcher" / "__init__.py").is_file() and script.is_file():
        return script
    return None


def run_gate(name: str, args: List[str], cwd: Optional[str] = None) -> int:
    module = GATES[name]
    script = checkout_script(module, cwd)
    if script is not None:
        return subprocess.run([sys.executable, str(script), *args], cwd=cwd).returncode
    impl = importlib.import_module(f"claude_launcher.gates.{module}")
    return int(impl.main(args) or 0)


def _cmd_gate(args: argparse.Namespace) -> int:
    rest = list(args.gate_args or [])
    if rest[:1] == ["--"]:
        rest = rest[1:]
    return run_gate(args.gate_name, rest)


def register(sub) -> None:
    p = sub.add_parser(
        "gate",
        help="run a workflow gate check (exit code is the verdict) in any repository",
        description=(
            "Run a packaged gate check with the arguments that follow its name. "
            "Inside a claude-launcher checkout the checkout's tools/<gate>.py "
            "runs, so a branch is checked by its own code; elsewhere the "
            "installed package's copy runs. Gates: "
            + ", ".join(sorted(GATES))
        ),
    )
    p.add_argument("gate_name", choices=sorted(GATES), metavar="gate")
    p.add_argument("gate_args", nargs=argparse.REMAINDER,
                   help="passed to the gate unchanged")
    p.set_defaults(func=_cmd_gate)
