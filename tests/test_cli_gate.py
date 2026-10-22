"""``claunch gate``: a packaged workflow's gate answers in any repository.

improv-worker-v2's landed-wait probe named ``tools/landed_check.py`` by
relative path. The workflow ships to every repository and that file exists only
in a claude-launcher checkout, so elsewhere python exited 2 on the missing
file -- the gate's own "could not tell" -- and every round ended at the
landed-unclear question (claunch-eyajw, claunch-bxxji). These pin the two
halves of the replacement: outside a checkout the installed package's copy
answers 0/1, inside one the checkout's own script runs.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from claude_launcher import cli, cli_gate
from claude_launcher.cflow2 import layers

ROOT = Path(__file__).resolve().parents[1]


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=str(repo), capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


@pytest.fixture
def plain_repo(tmp_path) -> Path:
    """A repository that is not a claude-launcher checkout: no tools/, no src/.
    ``feature`` has one commit of its own on top of ``master``."""
    repo = tmp_path / "plain"
    repo.mkdir()
    _git(repo, "init", "-b", "master")
    _git(repo, "commit", "--allow-empty", "-m", "base")
    _git(repo, "checkout", "-b", "feature")
    _git(repo, "commit", "--allow-empty", "-m", "work")
    return repo


def test_outside_a_checkout_the_packaged_check_answers(plain_repo, capsys):
    assert cli_gate.checkout_script("landed_check", str(plain_repo)) is None
    args = ["--repo", str(plain_repo)]
    assert cli_gate.run_gate("landed-check", args, cwd=str(plain_repo)) == 1
    assert "not yet" in capsys.readouterr().out

    _git(plain_repo, "checkout", "master")
    _git(plain_repo, "merge", "--no-ff", "feature", "-m", "merge feature")
    _git(plain_repo, "checkout", "feature")
    assert cli_gate.run_gate("landed-check", args, cwd=str(plain_repo)) == 0
    assert "landed" in capsys.readouterr().out


def test_the_cli_passes_flags_through_to_the_gate(plain_repo, monkeypatch):
    monkeypatch.chdir(plain_repo)
    assert cli.main(["gate", "landed-check", "--repo", str(plain_repo), "--target", "master"]) == 1
    _git(plain_repo, "checkout", "master")
    _git(plain_repo, "merge", "--no-ff", "feature", "-m", "merge feature")
    _git(plain_repo, "checkout", "feature")
    assert cli.main(["gate", "landed-check", "--repo", str(plain_repo), "--target", "master"]) == 0


def test_inside_a_checkout_its_own_script_runs(tmp_path):
    """A gate runs the tree it is checking: the checkout's tools/ script, with
    the arguments and exit code passed through."""
    co = tmp_path / "checkout"
    (co / "src" / "claude_launcher").mkdir(parents=True)
    (co / "src" / "claude_launcher" / "__init__.py").write_text("", encoding="utf-8")
    (co / "tools").mkdir()
    marker = tmp_path / "ran.txt"
    (co / "tools" / "landed_check.py").write_text(
        "import sys\n"
        f"open(r'{marker}', 'w').write(' '.join(sys.argv[1:]))\n"
        "sys.exit(7)\n",
        encoding="utf-8",
    )
    _git(co, "init", "-b", "master")

    assert cli_gate.checkout_script("landed_check", str(co)) == co / "tools" / "landed_check.py"
    assert cli_gate.run_gate("landed-check", ["--landing"], cwd=str(co / "tools")) == 7
    assert marker.read_text(encoding="utf-8") == "--landing"


def test_a_repository_with_a_tools_dir_but_no_package_is_not_a_checkout(tmp_path):
    repo = tmp_path / "other"
    (repo / "tools").mkdir(parents=True)
    (repo / "tools" / "landed_check.py").write_text("raise SystemExit(9)\n", encoding="utf-8")
    _git(repo, "init", "-b", "master")
    assert cli_gate.checkout_script("landed_check", str(repo)) is None


def test_this_checkout_resolves_to_its_own_tools_script():
    assert cli_gate.checkout_script("landed_check", str(ROOT)) == ROOT / "tools" / "landed_check.py"


# Probes that still name a tools/ script, each measuring claude-launcher's own
# daemon, so only meaningful in this repository. Moving them into the package
# is claunch-bxxji / claunch-l6aay.1; a new entry here needs the same reason.
_TOOLS_PROBES_STILL_ALLOWED = {
    ("improv-leader-v2.yaml", "tools/deploy_check.py"),
    # admission: the leader's persistent verification queue is claude-launcher's own
    # (the task's verify named the same script before it became a wait, claunch-8qn6h)
    ("improv-leader-v2.yaml", "tools/delayed_sweep.py"),
    ("perf-monitor-v2.yaml", "tools/perf_probe.py"),
}


def test_no_packaged_v2_probe_names_a_tools_script():
    """A v2 definition is read from the package in every repository; a probe
    naming this repository's tools/ cannot run anywhere else."""
    for path in sorted(layers.package_dir().glob("*.yaml")):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip().startswith("probe:"):
                continue
            for script in re.findall(r"tools/[\w.-]+", line):
                assert (path.name, script) in _TOOLS_PROBES_STILL_ALLOWED, \
                    f"{path.name}:{n}: {line.strip()}"


def test_the_landed_wait_probe_uses_claunch_gate():
    text = (layers.package_dir() / "improv-worker-v2.yaml").read_text(encoding="utf-8")
    probes = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("probe:")]
    assert any("claunch gate landed-check --landing" in p for p in probes), probes
