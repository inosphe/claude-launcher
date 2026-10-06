"""The cross-machine commit check (claunch-yq80p.7, ``tools/commit_check.py``).

In a remote formation the leader and a worker share no object store, so "I
have the commit you named" has to be checked on both ends instead of assumed.
These tests stand up the shape that check runs in: one bare repository as the
shared remote, a *sender* clone that pushes, and a *receiver* clone that has
never seen the pushed commit until it fetches. The exit codes are the
interface the workflow probes read, so each one is pinned by a case that
produces it for real -- a fetch that fails, a SHA nobody pushed, a ref that has
moved past the SHA, a merge notice whose commit does not merge the worker's tip.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

CHECK = Path(__file__).resolve().parents[1] / "tools" / "commit_check.py"


def _load():
    """``tools/`` is not a package -- load the script the way a script is."""
    spec = importlib.util.spec_from_file_location("commit_check", CHECK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


commit_check = _load()


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=str(repo), capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _commit(repo: Path, name: str) -> str:
    (repo / name).write_text(name, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-m", name)
    return _git(repo, "rev-parse", "HEAD")


def _build(root: Path) -> None:
    """``origin.git`` (bare) with master@base; ``sender`` and ``receiver`` clones of it."""
    seed = root / "seed"
    seed.mkdir()
    _git(seed, "init", "-b", "master")
    _commit(seed, "base")
    _git(root, "clone", "--bare", str(seed), "origin.git")
    _git(root, "clone", "origin.git", "sender")
    _git(root, "clone", "origin.git", "receiver")


@pytest.fixture
def mesh(tmp_path, repo_template):
    root = repo_template("commit-check", _build, tmp_path / "mesh")
    # Clones record the template's absolute path; point them at this copy.
    for side in ("sender", "receiver"):
        _git(root / side, "remote", "set-url", "origin", str(root / "origin.git"))
    return root


def _run(repo: Path, *args: str, capsys):
    code = commit_check.main(["--repo", str(repo), "--remote", "origin", *args])
    out = capsys.readouterr()
    return code, out.out, out.err


def _push_feature(mesh: Path) -> str:
    sender = mesh / "sender"
    _git(sender, "checkout", "-b", "feature")
    sha = _commit(sender, "work")
    _git(sender, "push", "origin", "HEAD:refs/heads/feature")
    return sha


# --- sending side -----------------------------------------------------------


def test_pushed_confirms_what_the_remote_holds(mesh, capsys):
    sha = _push_feature(mesh)
    code, out, _ = _run(mesh / "sender", "--ref", "feature", "--sha", sha, "--pushed", capsys=capsys)
    assert code == commit_check.CONFIRMED
    assert out.strip() == f"OK {sha} @ origin/feature (pushed)"


def test_pushed_refuses_an_unpushed_commit(mesh, capsys):
    """A local commit made after the push: the remote still holds the old tip."""
    pushed = _push_feature(mesh)
    local = _commit(mesh / "sender", "later")
    code, out, _ = _run(mesh / "sender", "--ref", "feature", "--sha", local, "--pushed", capsys=capsys)
    assert code == commit_check.REF_ELSEWHERE
    assert out.splitlines()[0] == f"NOT FOUND {local} @ origin/feature (fetched {pushed})"


def test_pushed_reports_a_ref_the_remote_does_not_have(mesh, capsys):
    sha = _git(mesh / "sender", "rev-parse", "HEAD")
    code, out, _ = _run(mesh / "sender", "--ref", "nope", "--sha", sha, "--pushed", capsys=capsys)
    assert code == commit_check.SHA_MISSING
    assert out.startswith(f"NOT FOUND {sha} @ origin/nope (fetched -)")


# --- receiving side ---------------------------------------------------------


def test_receiver_fetches_and_confirms_exact(mesh, capsys):
    sha = _push_feature(mesh)
    receiver = mesh / "receiver"
    assert subprocess.run(["git", "-C", str(receiver), "cat-file", "-e", f"{sha}^{{commit}}"],
                          capture_output=True).returncode != 0, "receiver must not have it before the check"
    code, out, _ = _run(receiver, "--ref", "feature", "--sha", sha, "--exact", capsys=capsys)
    assert code == commit_check.CONFIRMED
    assert out.strip() == f"OK {sha} @ origin/feature (exact)"
    assert _git(receiver, "rev-parse", "origin/feature") == sha


def test_exact_fails_when_the_ref_moved_on(mesh, capsys):
    """Exit 1: the SHA is here, the ref is somewhere else -- no nearest-commit guess."""
    first = _push_feature(mesh)
    second = _commit(mesh / "sender", "more")
    _git(mesh / "sender", "push", "origin", "HEAD:refs/heads/feature")
    code, out, _ = _run(mesh / "receiver", "--ref", "feature", "--sha", first, "--exact", capsys=capsys)
    assert code == commit_check.REF_ELSEWHERE
    assert out.splitlines()[0] == f"NOT FOUND {first} @ origin/feature (fetched {second})"


def test_contains_accepts_an_ancestor(mesh, capsys):
    first = _push_feature(mesh)
    _commit(mesh / "sender", "more")
    _git(mesh / "sender", "push", "origin", "HEAD:refs/heads/feature")
    code, out, _ = _run(mesh / "receiver", "--ref", "feature", "--sha", first, "--contains", capsys=capsys)
    assert code == commit_check.CONFIRMED
    assert out.strip() == f"OK {first} @ origin/feature (contains)"


def test_contains_rejects_a_commit_on_another_branch(mesh, capsys):
    """The SHA reached the receiver via another ref, but ``master`` does not hold it."""
    sha = _push_feature(mesh)
    receiver = mesh / "receiver"
    _git(receiver, "fetch", "origin")
    master = _git(receiver, "rev-parse", "origin/master")
    code, out, _ = _run(receiver, "--ref", "master", "--sha", sha, "--contains", capsys=capsys)
    assert code == commit_check.REF_ELSEWHERE
    assert out.splitlines()[0] == f"NOT FOUND {sha} @ origin/master (fetched {master})"


def test_a_sha_nobody_pushed_is_missing(mesh, capsys):
    """Exit 2: committed on the sender, never pushed; the receiver cannot have it."""
    _push_feature(mesh)
    local = _commit(mesh / "sender", "unpushed")
    pushed = _git(mesh / "receiver", "ls-remote", "origin", "refs/heads/feature").split()[0]
    code, out, _ = _run(mesh / "receiver", "--ref", "feature", "--sha", local, "--exact", capsys=capsys)
    assert code == commit_check.SHA_MISSING
    assert out.splitlines()[0] == f"NOT FOUND {local} @ origin/feature (fetched {pushed})"


def test_fetch_failure_is_its_own_code(mesh, capsys):
    sha = _push_feature(mesh)
    code, out, err = _run(mesh / "receiver", "--ref", "no-such-branch", "--sha", sha, "--exact", capsys=capsys)
    assert code == commit_check.FETCH_FAILED
    assert out == "" and "fetch origin refs/heads/no-such-branch failed" in err


def test_merge_notice_requires_the_worker_tip_as_parent(mesh, capsys):
    """Principle 5: the leader's merge commit on master must list the worker's tip as a parent."""
    tip = _push_feature(mesh)
    leader = mesh / "sender"
    _git(leader, "checkout", "master")
    _git(leader, "merge", "--no-ff", "-m", "merge feature", "feature")
    merge = _git(leader, "rev-parse", "HEAD")
    _git(leader, "push", "origin", "master")
    receiver = mesh / "receiver"

    code, out, _ = _run(receiver, "--ref", "master", "--sha", merge, "--contains",
                        "--parent-of", tip, capsys=capsys)
    assert code == commit_check.CONFIRMED
    assert out.strip() == f"OK {merge} @ origin/master (contains, parent {tip})"

    base = _git(receiver, "rev-parse", f"{merge}^1")
    code, out, _ = _run(receiver, "--ref", "master", "--sha", base, "--contains",
                        "--parent-of", tip, capsys=capsys)
    assert code == commit_check.REF_ELSEWHERE
    assert f"does not have {tip} as a parent" in out


# --- arguments ---------------------------------------------------------------


@pytest.mark.parametrize("args", [
    ["--ref", "feature", "--sha", "1a7b9dd", "--exact"],          # short hash
    ["--ref", "HEAD", "--sha", "a" * 40, "--exact"],               # relative ref
    ["--ref", "master~1", "--sha", "a" * 40, "--exact"],
    ["--ref", "@{upstream}", "--sha", "a" * 40, "--contains"],
    ["--ref", "refs/tags/v1", "--sha", "a" * 40, "--exact"],
    ["--ref", "feature", "--sha", "a" * 40],                       # no mode
    ["--ref", "feature", "--sha", "a" * 40, "--exact", "--contains"],
    ["--ref", "feature", "--sha", "a" * 40, "--pushed", "--parent-of", "b" * 40],
    ["--ref", "feature", "--sha", "a" * 40, "--exact", "--parent-of", "bbb"],
])
def test_invalid_references_are_refused_not_reported_missing(mesh, capsys, args):
    with pytest.raises(SystemExit) as exc:
        _run(mesh / "receiver", *args, capsys=capsys)
    assert exc.value.code == commit_check.BAD_ARGS


def test_ref_normalisation():
    assert commit_check.normalize_ref("refs/heads/s83/x") == "s83/x"
    assert commit_check.normalize_ref("feature") == "feature"
    assert commit_check.normalize_ref("HEAD") is None


def test_script_runs_as_a_command(mesh):
    """The probes call it as a file, not an import: exit code and stdout over a real process."""
    sha = _push_feature(mesh)
    import sys
    proc = subprocess.run(
        [sys.executable, str(CHECK), "--repo", str(mesh / "receiver"), "--remote", "origin",
         "--ref", "refs/heads/feature", "--sha", sha.upper(), "--exact"],
        capture_output=True, text=True, encoding="utf-8",
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == f"OK {sha} @ origin/feature (exact)"
