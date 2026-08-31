"""``claunch beads``: one board per repository, reached from any worktree.

``br`` discovers its database in the current directory, and a git worktree
has the tracked JSONL but no database — so without help every worktree
would grow a board of its own. These tests pin the three things the
passthrough adds: the root it resolves (git's common dir, so a worktree
answers with the main checkout), the ``--db``/``--actor`` it stamps on, and
the rebuild-from-JSONL it performs when the database is missing.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from claude_launcher import cli_beads, workspaces

REAL_RUN = subprocess.run


def git(*args, cwd):
    return REAL_RUN(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git("init", "-q", cwd=root)
    git("config", "user.email", "t@example.com", cwd=root)
    git("config", "user.name", "t", cwd=root)
    (root / "a.txt").write_text("hi\n", encoding="utf-8")
    git("add", "-A", cwd=root)
    git("commit", "-qm", "init", cwd=root)
    return root


@pytest.fixture
def worktree(repo, tmp_path):
    path = tmp_path / "wt"
    git("worktree", "add", "-q", str(path), "-b", "wt", cwd=repo)
    return path


def _resolved(path: Path) -> Path:
    return Path(os.path.realpath(str(path)))


# --------------------------------------------------------------------------- #
# where the board is
# --------------------------------------------------------------------------- #
@pytest.mark.worktree
def test_a_worktree_resolves_to_the_main_checkout(repo, worktree):
    """The whole reason for the passthrough: from a worktree, the board is
    the main checkout's, not one the worktree would otherwise grow."""
    assert _resolved(cli_beads.repo_root(str(worktree))) == _resolved(repo)
    assert _resolved(cli_beads.repo_root(str(repo))) == _resolved(repo)


def test_a_directory_git_does_not_claim_falls_back_to_its_workspace(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.setattr(
        cli_beads.workspaces, "owning",
        lambda path, doc=None: workspaces.Workspace(name="p", path=str(plain)),
    )
    assert cli_beads.repo_root(str(plain)) == plain


def test_a_directory_nobody_claims_has_no_board(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.setattr(cli_beads.workspaces, "owning", lambda path, doc=None: None)
    assert cli_beads.repo_root(str(plain)) is None


# --------------------------------------------------------------------------- #
# what gets run
# --------------------------------------------------------------------------- #
def test_every_call_names_the_root_database(tmp_path):
    root = tmp_path / "r"
    (cmd,) = cli_beads.plan(
        ["list", "--json"], root, actor=None, db_exists=True, jsonl_exists=True
    )
    assert cmd[:3] == ["br", "--db", str(root / ".beads" / "beads.db")]
    assert cmd[3:] == ["list", "--json"]


def test_the_session_becomes_the_actor_unless_the_caller_set_one(tmp_path):
    root = tmp_path / "r"
    (cmd,) = cli_beads.plan(["create", "x"], root, "s45", True, True)
    assert cmd[3:5] == ["--actor", "s45"]
    (cmd,) = cli_beads.plan(
        ["create", "x", "--actor", "me"], root, "s45", True, True
    )
    assert cmd.count("--actor") == 1 and "s45" not in cmd


def test_init_passes_straight_through(tmp_path):
    """``init`` is how a board is first made; it must not trip the rebuild."""
    root = tmp_path / "r"
    cmds = cli_beads.plan(
        ["init", "--prefix", "x"], root, "s45", db_exists=False, jsonl_exists=False
    )
    assert len(cmds) == 1 and cmds[0][-3:] == ["init", "--prefix", "x"]


def test_a_missing_database_is_rebuilt_from_the_tracked_jsonl(tmp_path):
    """A fresh clone, or the main checkout right after the board's first
    merge: the JSONL is there, the (ignored) database is not. The caller's
    command runs third, after init and import, under the recorded prefix."""
    root = tmp_path / "r"
    beads = root / ".beads"
    beads.mkdir(parents=True)
    (beads / "config.yaml").write_text(
        "# Beads Project Configuration\nissue_prefix: claunch\n", encoding="utf-8"
    )
    cmds = cli_beads.plan(["ready", "--json"], root, "s1", False, True)
    db = str(beads / "beads.db")
    assert cmds == [
        ["br", "--db", db, "init", "--prefix", "claunch"],
        ["br", "--db", db, "sync", "--import-only"],
        ["br", "--db", db, "--actor", "s1", "ready", "--json"],
    ]


def test_the_rebuild_prefix_falls_back_to_the_root_name(tmp_path):
    root = tmp_path / "My Repo"
    (root / ".beads").mkdir(parents=True)
    cmds = cli_beads.plan(["list"], root, None, False, True)
    assert cmds[0][-1] == "my-repo"


def test_nothing_to_rebuild_from_is_a_refusal_not_a_new_board(tmp_path):
    with pytest.raises(cli_beads.BeadsError, match="no board"):
        cli_beads.plan(["list"], tmp_path / "r", None, False, False)


# --------------------------------------------------------------------------- #
# end to end, with br faked
# --------------------------------------------------------------------------- #
@pytest.fixture
def fake_br(monkeypatch):
    """Record every ``br`` launch; let git through so root resolution is real."""
    seen: list = []

    def run(cmd, **kwargs):
        if cmd and cmd[0] == "git":
            return REAL_RUN(cmd, **kwargs)
        seen.append({"cmd": list(cmd), "cwd": kwargs.get("cwd")})
        return type("Done", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(cli_beads.subprocess, "run", run)
    monkeypatch.setattr(cli_beads.shutil, "which", lambda name: r"C:\bin\br.exe")
    return seen


@pytest.mark.worktree
def test_from_a_worktree_the_command_runs_against_the_main_board(
    repo, worktree, fake_br, monkeypatch
):
    beads = repo / ".beads"
    beads.mkdir()
    (beads / "beads.db").write_bytes(b"")
    monkeypatch.setenv(cli_beads.SESSION_ENV, "s7")
    assert cli_beads.run(["show", "claunch-1"], cwd=str(worktree)) == 0
    (launch,) = fake_br
    db = Path(launch["cmd"][2])
    assert _resolved(db) == _resolved(beads / "beads.db")
    assert launch["cmd"][3:] == ["--actor", "s7", "show", "claunch-1"]
    assert launch["cwd"] == str(worktree)


def test_without_br_installed_the_answer_is_an_error_not_a_stack(monkeypatch, capsys):
    monkeypatch.setattr(cli_beads.shutil, "which", lambda name: None)
    ns = type("NS", (), {"args": ["list"]})()
    assert cli_beads._cmd(ns) == 2
    assert "not installed" in capsys.readouterr().err


def test_a_failed_rebuild_step_stops_before_the_callers_command(
    repo, monkeypatch, capsys
):
    beads = repo / ".beads"
    beads.mkdir()
    (beads / "issues.jsonl").write_text("", encoding="utf-8")
    seen = []

    def run(cmd, **kwargs):
        if cmd and cmd[0] == "git":
            return REAL_RUN(cmd, **kwargs)
        seen.append(list(cmd))
        code = 1 if "init" in cmd else 0
        return type("Done", (), {"returncode": code, "stdout": "", "stderr": "boom"})()

    monkeypatch.setattr(cli_beads.subprocess, "run", run)
    monkeypatch.setattr(cli_beads.shutil, "which", lambda name: r"C:\bin\br.exe")
    assert cli_beads.run(["list"], cwd=str(repo)) == 1
    assert len(seen) == 1 and "init" in seen[0]
    assert "rebuilding the board" in capsys.readouterr().err


def test_the_cli_registers_the_subcommand():
    from claude_launcher import cli

    ns = cli.build_parser().parse_args(["beads", "ready", "--json"])
    assert ns.args == ["ready", "--json"]
    assert ns.func is cli_beads._cmd


# --------------------------------------------------------------------------- #
# --status: the one value the passthrough checks
#
# ``br`` matches a ``--status`` value literally without comparing it to any
# vocabulary, so an unknown one is not an error: the filter answers
# ``total: 0`` with exit 0, and a write stores it and drops the issue out
# of every status filter and out of ``ready``. "I could not read your
# question" and "there is nothing" arrive identical (claunch-6s1h).
# --------------------------------------------------------------------------- #
STATUS_ARG_FORMS = [
    (["list", "--status", "in_ready"], ["in_ready"]),
    (["list", "--status", "in_review"], ["in_review"]),
    (["list", "--status=in_review"], ["in_review"]),
    (["list", "-s", "in_review"], ["in_review"]),
    (["list", "-sin_review"], ["in_review"]),
    (["list", "-s=in_review"], ["in_review"]),
    (["list", "--status", "open", "--status", "blocked"], ["open", "blocked"]),
    (["list", "--json"], []),
    # after a bare ``--`` everything is a positional, not a flag
    (["search", "--", "--status", "open"], []),
    # a value that merely contains the word is not a flag
    (["list", "--desc-contains", "--status open"], []),
]


@pytest.mark.parametrize("args,expected", STATUS_ARG_FORMS)
def test_the_status_values_are_read_out_of_every_spelling(args, expected):
    assert cli_beads.status_values(args) == expected


def test_a_comma_list_is_refused_and_the_message_names_the_working_form():
    """The shape the workflows kept writing. ``br`` reads it as one status
    nothing is in, so the board's own orphan check answered 'no orphans'
    when it had 2 (claunch-6s1h)."""
    with pytest.raises(cli_beads.BeadsError) as exc:
        cli_beads.check_statuses(["list", "--status", "in_progress,in_review"])
    message = str(exc.value)
    assert "in_progress,in_review" in message
    assert "repeated flag" in message
    assert "--status open --status in_progress" in message


def test_a_space_separated_list_is_refused_the_same_way():
    with pytest.raises(cli_beads.BeadsError, match="repeated flag"):
        cli_beads.check_statuses(["list", "--status", "in_progress in_review"])


def test_an_unknown_status_is_refused_and_the_valid_ones_are_named():
    """The control that established the mechanism: a name that is not a
    status at all, with no comma to blame."""
    with pytest.raises(cli_beads.BeadsError) as exc:
        cli_beads.check_statuses(["list", "--status", "bogus_status"])
    message = str(exc.value)
    assert "bogus_status" in message
    for status in cli_beads.STATUSES:
        assert status in message


def test_the_repeated_flag_form_is_what_passes(tmp_path):
    """The only multi-status spelling ``br`` actually implements."""
    root = tmp_path / "r"
    (cmd,) = cli_beads.plan(
        ["list", "--status", "in_progress", "--status", "in_review", "--json"],
        root, actor=None, db_exists=True, jsonl_exists=True,
    )
    assert cmd[3:] == [
        "list", "--status", "in_progress", "--status", "in_review", "--json"
    ]


def test_a_typo_on_the_write_path_never_reaches_br(tmp_path):
    """The expensive half: ``br update --status in_reviw`` returns 0 and
    stores the typo, after which the issue is in no status filter and in
    no ``ready`` list. The refusal happens in ``plan``, so nothing runs."""
    with pytest.raises(cli_beads.BeadsError, match="in_reviw"):
        cli_beads.plan(
            ["update", "claunch-1", "--status", "in_reviw"],
            tmp_path / "r", "s1", db_exists=True, jsonl_exists=True,
        )


def test_the_refusal_is_exit_2_with_the_message_on_stderr(monkeypatch, capsys):
    monkeypatch.setattr(cli_beads.shutil, "which", lambda name: r"C:\bin\br.exe")
    ns = type("NS", (), {"args": ["list", "--status", "in_progress,in_review"]})()
    assert cli_beads._cmd(ns) == 2
    err = capsys.readouterr().err
    assert "unknown status" in err and "repeated flag" in err


# --------------------------------------------------------------------------- #
# the vocabulary's two sources — a hardcoded list that goes stale silently
# would be the same defect this check exists to stop
# --------------------------------------------------------------------------- #
def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def test_every_status_on_the_tracked_board_is_in_the_vocabulary():
    """Source one: the board itself. ``.beads/issues.jsonl`` is tracked, so
    a status somebody wrote onto the board is visible here."""
    import json

    jsonl = _repo_root() / ".beads" / "issues.jsonl"
    if not jsonl.is_file():
        pytest.skip("no tracked board in this checkout")
    seen = set()
    for line in jsonl.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            status = json.loads(line).get("status")
        except ValueError:
            continue
        if status:
            seen.add(status)
    assert seen <= set(cli_beads.STATUSES), (
        f"the board holds statuses the wrapper would refuse: "
        f"{sorted(seen - set(cli_beads.STATUSES))}"
    )


def test_every_status_the_canon_workflows_name_is_in_the_vocabulary():
    """Source two: the transition rules. A workflow that prescribes a
    ``--status`` the wrapper refuses would make the prescription fail at
    exit 2 — the loud half of the same defect, but still a defect."""
    import re

    canon = _repo_root() / "src" / "claude_launcher" / "workflows"
    if not canon.is_dir():
        pytest.skip("no packaged workflows in this checkout")
    pattern = re.compile(r"--status[ =]([A-Za-z_,]+)")
    seen = set()
    for path in sorted(canon.glob("*.yaml")):
        for value in pattern.findall(path.read_text(encoding="utf-8")):
            seen.add(value)
    unknown = sorted(v for v in seen if v not in cli_beads.STATUSES)
    assert not unknown, (
        f"the canon workflows prescribe --status values the wrapper "
        f"refuses: {unknown}"
    )
