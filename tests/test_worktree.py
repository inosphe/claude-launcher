"""Worktree launches: who is asked, who is never asked, and what gets made."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from claude_launcher import cli, herdr, worktree

#: Every test here ends up making real worktrees (or real git repos to make
#: them in), which is the expensive part of the whole suite on this machine.
#: `-m "not worktree"` is the fast path for work that cannot touch this code.
#:
#: Kept deliberately small: one test per behaviour, with the variants of a
#: behaviour asserted inside it rather than as a test each. The repository
#: itself comes from a per-worker template (``repo_template`` in conftest),
#: so what a test pays for is the worktree it cuts, not the repo it cuts from.
pytestmark = pytest.mark.worktree


#: Captured before any test patches it. The launcher shells out to both git
#: and claude through this one function, so a test that fakes "the launch"
#: must let git through or it is testing its own mock.
REAL_RUN = subprocess.run

#: Same reason: the autouse fixture below stubs Herdr out for every test, and
#: the one test that checks the stub-free path needs the original back.
REAL_HERDR_RUN = herdr._run


def git(*args, cwd):
    return REAL_RUN(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )


def fake_launch(record: dict):
    """A ``subprocess.run`` that records the claude launch and really runs git."""

    def run(cmd, **kwargs):
        if cmd and cmd[0] == "git":
            return REAL_RUN(cmd, **kwargs)
        record["cmd"] = list(cmd)
        record["cwd"] = kwargs.get("cwd")
        return type("Done", (), {"returncode": 0})()

    return run


@pytest.fixture
def outside_a_repo(tmp_path, monkeypatch):
    """A directory in no repository at all.

    pytest's basetemp can sit *inside* a checkout (this project's does), and
    git's discovery walks upwards until it finds one -- so "not a repo" has to
    be arranged, not assumed. A ceiling stops the walk at the temp root.
    """
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    plain = tmp_path / "plain"
    plain.mkdir()
    return plain


def _build_repo(root):
    git("init", "-q", cwd=root)
    git("config", "user.email", "t@example.com", cwd=root)
    git("config", "user.name", "t", cwd=root)
    (root / "a.txt").write_text("hi\n", encoding="utf-8")
    git("add", "-A", cwd=root)
    git("commit", "-qm", "init", cwd=root)


@pytest.fixture
def repo(tmp_path, repo_template):
    return repo_template("worktree", _build_repo, tmp_path / "repo")


@pytest.fixture(autouse=True)
def no_herdr(monkeypatch):
    """Tests must not talk to a real multiplexer, or read one's env."""
    monkeypatch.delenv(herdr.ENV_FLAG, raising=False)
    monkeypatch.delenv(herdr.PANE_ID_ENV, raising=False)
    monkeypatch.delenv(worktree.SESSION_ENV, raising=False)
    monkeypatch.delenv(worktree.WORKTREE_DIR_ENV, raising=False)
    monkeypatch.setattr(herdr, "_run", lambda args: False)


@pytest.fixture(autouse=True)
def labels_are_machine_independent(monkeypatch, tmp_path):
    """The machine must not leak into labels asserted literally.

    A label collapses home to ``~`` and drops leading segments past
    ``LABEL_LIMIT`` -- and on Windows ``tmp_path`` lives *inside* home
    (``C:\\Users\\<u>\\AppData\\Local\\Temp``) and is long enough to
    truncate, so every test asserting a literal repo path would watch its
    expected value be rewritten on some machines and not others. Home is
    pointed where no test path can be under it and the default limit is
    lifted; each behaviour keeps its own test, which patches home back or
    passes an explicit ``limit`` over this.
    """
    monkeypatch.setattr(
        herdr.Path, "home", classmethod(lambda cls: tmp_path / ".elsewhere")
    )
    real = herdr.launch_label
    monkeypatch.setattr(
        herdr, "launch_label",
        lambda identity, branch="", path="", role="", limit=10_000:
            real(identity, branch, path, role, limit),
    )


# --------------------------------------------------------------------------- #
# naming
# --------------------------------------------------------------------------- #
def test_default_name_is_pane_plus_time(monkeypatch):
    monkeypatch.setenv(herdr.ENV_FLAG, "1")
    monkeypatch.setenv(herdr.PANE_ID_ENV, "w4:p4")
    from datetime import datetime

    name = worktree.default_name(datetime(2026, 8, 18, 17, 30, 5))
    # The colon in a pane id is not a path character anywhere useful.
    assert name == "w4-p4-20260818-173005"


def test_default_name_falls_back_to_session_then_constant(monkeypatch):
    monkeypatch.setenv(worktree.SESSION_ENV, "reviewer-2")
    assert worktree.default_name().startswith("reviewer-2-")
    monkeypatch.delenv(worktree.SESSION_ENV)
    assert worktree.default_name().startswith("wt-")
    # A stale HERDR_PANE_ID without HERDR_ENV=1 is not this pane's.
    monkeypatch.setenv(herdr.PANE_ID_ENV, "w9:p9")
    assert herdr.pane_id() is None
    assert worktree.default_name().startswith("wt-")


@pytest.mark.parametrize("bad", ["", "  ", "../evil", "a b", "-lead", "x//y", "a..b"])
def test_invalid_names_are_refused(bad):
    with pytest.raises(worktree.WorktreeError):
        worktree.validate_name(bad)


@pytest.mark.parametrize("good", ["a", "feature/x", "fix-1.2_3", "w4-p4-20260818"])
def test_valid_names_pass(good):
    assert worktree.validate_name(good) == good


# --------------------------------------------------------------------------- #
# creating
# --------------------------------------------------------------------------- #
def test_creates_worktree_on_its_own_branch(repo):
    wt = worktree.resolve(str(repo), "feature-a")
    assert wt is not None and wt.created
    assert wt.path == repo / ".claude" / "worktrees" / "feature-a"
    assert wt.branch == "feature-a"
    assert (wt.path / "a.txt").exists()


def test_a_second_ask_reuses_the_checkout_and_reports_its_real_branch(repo):
    first = worktree.resolve(str(repo), "review")
    (first.path / "wip.txt").write_text("uncommitted\n", encoding="utf-8")
    again = worktree.resolve(str(repo), "review")
    assert again.path == first.path
    assert not again.created
    # Reuse means the work in it survives; a fresh checkout would not have it.
    assert (again.path / "wip.txt").exists()
    assert again.branch == "review"
    # The label follows the checkout, not the name it was cut under.
    git("checkout", "-q", "-b", "other", cwd=first.path)
    moved = worktree.resolve(str(repo), "review")
    assert moved.branch == "other"
    assert worktree.pane_label("api", str(moved.path)) == (
        f"api · other · {moved.path}"
    )


def test_launching_from_inside_a_worktree_makes_a_sibling(repo):
    inner = worktree.resolve(str(repo), "first")
    sibling = worktree.resolve(str(inner.path), "second")
    # Beside it, not nested inside the checkout an agent is already editing.
    assert sibling.path.parent == inner.path.parent
    assert repo in sibling.path.parents


def test_the_directory_is_relocatable_by_env_and_refused_when_taken(
    repo, tmp_path, monkeypatch
):
    (repo / ".claude" / "worktrees" / "taken").mkdir(parents=True)
    with pytest.raises(worktree.WorktreeError):
        worktree.resolve(str(repo), "taken")
    elsewhere = tmp_path / "trees"
    monkeypatch.setenv(worktree.WORKTREE_DIR_ENV, str(elsewhere))
    wt = worktree.resolve(str(repo), "moved")
    assert wt.path == elsewhere / "moved"


# --------------------------------------------------------------------------- #
# inheriting the MCP approval
# --------------------------------------------------------------------------- #
def approve(checkout: Path, doc: dict) -> Path:
    """Write ``doc`` as ``checkout``'s Claude Code local settings."""
    path = checkout / worktree.LOCAL_SETTINGS
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def settings_of(checkout: Path) -> dict:
    return json.loads(
        (checkout / worktree.LOCAL_SETTINGS).read_text(encoding="utf-8")
    )


def test_the_approval_is_inherited_and_nothing_else_is(repo):
    """The person answered the modal once; the worktree must not re-ask.

    `.mcp.json` is resolved from the repository, so a worktree inherits the
    declaration -- but the approval is recorded per directory, so without
    this it is asked again in a checkout no human is watching. Everything
    else in that file stays behind: permissions and env live there too, and
    `enableAllProjectMcpServers` answers for servers nobody has seen yet.
    A new checkout of an EXISTING branch is `created` too, and inherits.
    """
    approve(
        repo,
        {
            worktree.MCP_APPROVAL_KEY: ["claunch"],
            "enableAllProjectMcpServers": True,
            "disabledMcpjsonServers": ["cflow"],
            "permissions": {"deny": ["Bash(rm:*)"]},
            "env": {"SECRET": "1"},
        },
    )
    wt = worktree.resolve(str(repo), "narrow")
    assert settings_of(wt.path) == {worktree.MCP_APPROVAL_KEY: ["claunch"]}

    git("branch", "already", cwd=repo)
    wt = worktree.resolve(str(repo), "already")
    assert wt.created and wt.branch == "already"
    assert settings_of(wt.path) == {worktree.MCP_APPROVAL_KEY: ["claunch"]}


def test_nothing_to_inherit_writes_nothing_and_own_settings_stand(repo):
    """Inventing an approval would be a new decision, not an inherited one;
    an unreadable file is at worst the modal the person was already getting;
    and a reused checkout's answers are its own -- including a later refusal.
    """
    assert not (repo / worktree.LOCAL_SETTINGS).exists()
    wt = worktree.resolve(str(repo), "none")
    assert not (wt.path / worktree.LOCAL_SETTINGS).exists()

    for doc in ({}, {worktree.MCP_APPROVAL_KEY: []}, {"permissions": {}}):
        approve(repo, doc)
        wt = worktree.resolve(str(repo), f"bare{abs(hash(str(doc))) % 1000}")
        assert not (wt.path / worktree.LOCAL_SETTINGS).exists()

    (repo / worktree.LOCAL_SETTINGS).write_text("{not json", encoding="utf-8")
    wt = worktree.resolve(str(repo), "broken")
    assert wt is not None and wt.created
    assert not (wt.path / worktree.LOCAL_SETTINGS).exists()

    approve(repo, {worktree.MCP_APPROVAL_KEY: ["claunch"]})
    wt = worktree.resolve(str(repo), "review")
    assert settings_of(wt.path) == {worktree.MCP_APPROVAL_KEY: ["claunch"]}
    approve(wt.path, {worktree.MCP_APPROVAL_KEY: [], "disabledMcpjsonServers": ["claunch"]})
    again = worktree.resolve(str(repo), "review")
    assert not again.created
    assert settings_of(again.path)["disabledMcpjsonServers"] == ["claunch"]


# --------------------------------------------------------------------------- #
# who gets asked
# --------------------------------------------------------------------------- #
def test_nobody_but_a_person_at_a_tty_is_asked(repo, monkeypatch):
    never = lambda *a: pytest.fail("this launch must not be asked")  # noqa: E731

    # --no-worktree never asks, whoever is there
    monkeypatch.setattr(worktree, "interactive", lambda: True)
    monkeypatch.setattr("builtins.input", never)
    assert worktree.resolve(str(repo), worktree.NEVER) is None

    # an agent's PTY passes isatty(); it must still not be prompted
    monkeypatch.undo()
    monkeypatch.setenv(worktree.SESSION_ENV, "worker-1")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    assert not worktree.interactive()
    monkeypatch.setattr("builtins.input", never)
    assert worktree.resolve(str(repo), worktree.ASK) is None

    # no tty: stays put without a question
    monkeypatch.undo()
    monkeypatch.setattr(worktree, "interactive", lambda: False)
    assert worktree.resolve(str(repo), worktree.ASK) is None

    # a person who declines, or hits EOF at the question, stays put too
    monkeypatch.setattr(worktree, "interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: "n")
    assert worktree.resolve(str(repo), worktree.ASK) is None

    def eof(*_a):
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    assert worktree.resolve(str(repo), worktree.ASK) is None


def test_a_person_who_accepts_names_it_or_takes_the_suggestion(
    repo, monkeypatch, capsys
):
    monkeypatch.setattr(worktree, "interactive", lambda: True)
    answers = iter(["y", "mine"])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    assert worktree.resolve(str(repo), worktree.ASK).name == "mine"

    answers = iter(["y", ""])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    monkeypatch.setattr(worktree, "default_name", lambda now=None: "suggested")
    assert worktree.resolve(str(repo), worktree.ASK).name == "suggested"

    # a rejected name is asked again, and the refusal is said out loud
    answers = iter(["y", "no spaces", "fine"])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    assert worktree.resolve(str(repo), worktree.ASK).name == "fine"
    assert "invalid worktree name" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# not a repository
# --------------------------------------------------------------------------- #
def test_outside_a_repo_nothing_is_asked_and_an_explicit_request_fails(
    outside_a_repo, monkeypatch
):
    monkeypatch.setattr(worktree, "interactive", lambda: True)
    monkeypatch.setattr(
        "builtins.input", lambda *a: pytest.fail("nothing to make a worktree of")
    )
    assert worktree.repo_root(str(outside_a_repo)) is None
    assert worktree.resolve(str(outside_a_repo), worktree.ASK) is None
    with pytest.raises(worktree.WorktreeError):
        worktree.resolve(str(outside_a_repo), "x")


# --------------------------------------------------------------------------- #
# the flags, as the two commands parse them
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "argv, expected, rest",
    [
        (["--worktree=x", "-p", "hi"], "x", ["-p", "hi"]),
        (["--worktree", "-p", "hi"], "", ["-p", "hi"]),
        (["--no-worktree", "-p", "hi"], worktree.NEVER, ["-p", "hi"]),
        (["-p", "hi"], worktree.ASK, ["-p", "hi"]),
        # After `--` it is claude's argument, not ours.
        (["--", "--worktree=x"], worktree.ASK, ["--", "--worktree=x"]),
        # A bare --worktree does not eat the next token: that is claude's prompt.
        (["--worktree", "fix the parser"], "", ["fix the parser"]),
    ],
)
def test_run_extracts_the_flag_from_passthrough(argv, expected, rest):
    assert cli._extract_worktree(argv) == (expected, rest)


@pytest.mark.parametrize(
    "argv, expected",
    [
        ([], worktree.ASK),
        (["--worktree"], ""),
        (["--worktree=demo"], "demo"),
        (["--worktree", "demo"], "demo"),
        (["--no-worktree"], worktree.NEVER),
    ],
)
def test_new_session_parses_the_flag(argv, expected):
    args = cli.build_parser().parse_args(
        ["new-session", "--profile", "nc", *argv]
    )
    assert args.worktree == expected


def test_new_session_refuses_both_flags_at_once():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(
            ["new-session", "--profile", "nc", "--worktree", "--no-worktree"]
        )


# --------------------------------------------------------------------------- #
# run, end to end
# --------------------------------------------------------------------------- #
def test_run_launches_claude_inside_the_worktree_and_relabels_the_pane(
    repo, monkeypatch, capsys
):
    from claude_launcher import runner

    cli.main(["create", "work", "--no-seed"])
    capsys.readouterr()
    seen = {}
    labels, cleared = [], []
    monkeypatch.setattr(
        herdr, "rename_pane", lambda label, **kw: labels.append(label) or True
    )
    monkeypatch.setattr(herdr, "clear_pane_label", lambda **kw: cleared.append(True))
    monkeypatch.setattr(runner.subprocess, "run", fake_launch(seen))
    monkeypatch.chdir(repo)
    assert cli.main(["run", "work", "--worktree=solo", "-p", "hi"]) == 0
    solo = repo / ".claude" / "worktrees" / "solo"
    assert seen["cwd"] == str(solo)
    # The flag is ours; everything else still reaches claude untouched.
    assert seen["cmd"][1:] == ["-p", "hi"]
    assert "created worktree 'solo'" in capsys.readouterr().err
    # The profile is run's nearest thing to a session name; the branch and the
    # directory are what tell two checkouts of it apart. And the pane goes
    # back to Herdr's own label when claude exits.
    assert labels == [f"work · solo · {solo}"]
    assert cleared == [True]


def test_run_without_the_flag_stays_where_it_was(repo, monkeypatch, capsys):
    from claude_launcher import runner

    cli.main(["create", "work", "--no-seed"])
    capsys.readouterr()
    seen = {}
    monkeypatch.setattr(runner.subprocess, "run", fake_launch(seen))
    monkeypatch.setattr(worktree, "interactive", lambda: False)
    monkeypatch.chdir(repo)
    assert cli.main(["run", "work", "-p", "hi"]) == 0
    # None, not a path: inherit the caller's directory as before.
    assert seen["cwd"] is None


def test_a_failed_worktree_aborts_the_run(repo, monkeypatch, capsys):
    """A worktree that was asked for and could not be made must not silently
    launch in the shared checkout -- that is the collision it was meant to
    prevent."""
    from claude_launcher import runner

    cli.main(["create", "work", "--no-seed"])
    capsys.readouterr()

    def no_launch(cmd, **kwargs):
        if cmd and cmd[0] == "git":
            return REAL_RUN(cmd, **kwargs)
        pytest.fail(f"must not launch: {cmd}")

    monkeypatch.setattr(runner.subprocess, "run", no_launch)
    monkeypatch.chdir(repo)
    (repo / ".claude" / "worktrees" / "taken").mkdir(parents=True)
    assert cli.main(["run", "work", "--worktree=taken"]) == 1
    assert "error:" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# herdr, which is optional everywhere
# --------------------------------------------------------------------------- #
def test_rename_pane_is_a_no_op_outside_herdr():
    assert herdr.rename_pane("anything") is False


def test_rename_pane_survives_a_missing_binary(monkeypatch):
    monkeypatch.setenv(herdr.ENV_FLAG, "1")
    monkeypatch.setenv(herdr.PANE_ID_ENV, "w1:p1")
    monkeypatch.setattr(
        herdr.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("gone"))
    )
    # Best-effort decoration must never take a launch down with it.
    assert herdr.rename_pane("label") is False


def test_rename_pane_sends_the_label_to_the_calling_pane(monkeypatch):
    monkeypatch.setenv(herdr.ENV_FLAG, "1")
    monkeypatch.setenv(herdr.PANE_ID_ENV, "w1:p1")
    monkeypatch.setattr(herdr, "_run", REAL_HERDR_RUN)  # this one test wants it
    sent = {}

    def fake(cmd, **kwargs):
        sent["cmd"] = cmd

        class Done:
            returncode = 0

        return Done()

    monkeypatch.setattr(herdr.subprocess, "run", fake)
    assert herdr.rename_pane("solo [main]") is True
    assert sent["cmd"] == ["herdr", "pane", "rename", "w1:p1", "solo [main]"]


# --------------------------------------------------------------------------- #
# new-session, which hands the daemon an already-decided directory
# --------------------------------------------------------------------------- #
class FakeClient:
    """Enough of the daemon client for ``new-session`` to run against."""

    base_url = "http://127.0.0.1:0"

    def __init__(self):
        self.posted = None

    def post(self, path, body):
        self.posted = (path, body)
        return {
            "name": "s1", "harness": "claude", "profile": "work",
            "model": body.get("model"), "pid": 1,
        }

    def get(self, path):
        return {}


@pytest.fixture
def fake_daemon(monkeypatch):
    from claude_launcher import daemon_client

    client = FakeClient()
    monkeypatch.setattr(daemon_client, "ensure_running", lambda *a, **k: client)
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    return client


def test_new_session_is_created_in_the_worktree_of_the_named_repository(
    repo, fake_daemon, tmp_path, monkeypatch
):
    monkeypatch.chdir(repo)
    # It runs in the daemon's PTY, not in this pane -- and nothing would ever
    # take a label back off, so the pane is left alone.
    monkeypatch.setattr(
        herdr, "rename_pane", lambda *a, **k: pytest.fail("not this pane's session")
    )
    assert cli.main(["new-session", "--profile", "work", "--worktree=solo"]) == 0
    path, body = fake_daemon.posted
    assert path == "/api/sessions"
    assert body["cwd"] == str(repo / ".claude" / "worktrees" / "solo")

    # -c names the repository; the worktree belongs to *that* one.
    monkeypatch.chdir(tmp_path)
    assert cli.main(
        ["new-session", "--profile", "work", "-c", str(repo), "--worktree=aimed"]
    ) == 0
    _, body = fake_daemon.posted
    assert body["cwd"] == str(repo / ".claude" / "worktrees" / "aimed")


def test_new_session_without_a_worktree_uses_the_directory_itself(
    repo, fake_daemon, monkeypatch
):
    monkeypatch.chdir(repo)
    monkeypatch.setattr(worktree, "interactive", lambda: False)
    assert cli.main([
        "new-session", "--profile", "work", "--model", "opus",
        "--effort", "high",
    ]) == 0
    _, body = fake_daemon.posted
    assert body["cwd"] == str(repo)
    assert body["model"] == "opus"
    assert body["effort"] == "high"


def test_new_session_refusals_leave_nothing_made(
    repo, fake_daemon, monkeypatch, capsys
):
    """Inside a managed session, new-session is refused before anything is
    made -- and even --detached must not open a prompt nobody can answer.
    A worktree that cannot be made creates no session either."""
    monkeypatch.chdir(repo)
    monkeypatch.setenv(worktree.SESSION_ENV, "worker-1")
    monkeypatch.setattr(
        "builtins.input", lambda *a: pytest.fail("a session must not be asked")
    )
    assert cli.main(["new-session", "--profile", "work"]) == 2
    assert fake_daemon.posted is None
    assert cli.main(["new-session", "--profile", "work", "--detached"]) == 0
    _, body = fake_daemon.posted
    assert body["cwd"] == str(repo)

    fake_daemon.posted = None
    monkeypatch.delenv(worktree.SESSION_ENV)
    (repo / ".claude" / "worktrees" / "taken").mkdir(parents=True)
    assert cli.main(["new-session", "--profile", "work", "--worktree=taken"]) == 1
    assert fake_daemon.posted is None
    assert "error:" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# resume: the conversation decides the directory
# --------------------------------------------------------------------------- #
def test_a_resume_only_ever_enters_an_existing_checkout(repo, monkeypatch):
    """`claunch run nc --resume` means "carry on where I was", and where it
    was is this directory -- claude keeps transcripts per cwd. So the
    question is not asked, a new worktree is refused (a bare --worktree names
    a checkout after the second, so it is new by definition), and only going
    back to a checkout that exists is allowed."""
    monkeypatch.setattr(worktree, "interactive", lambda: True)
    monkeypatch.setattr(
        "builtins.input", lambda *a: pytest.fail("a resume must not be asked")
    )
    assert worktree.resolve(str(repo), worktree.ASK, resuming=True) is None
    assert worktree.resolve(str(repo), worktree.NEVER, resuming=True) is None

    with pytest.raises(worktree.WorktreeError, match="cannot be opened in a new one"):
        worktree.resolve(str(repo), "fresh", resuming=True)
    # ...and nothing was left behind by the refusal.
    assert not (repo / ".claude" / "worktrees" / "fresh").exists()
    with pytest.raises(worktree.WorktreeError):
        worktree.resolve(str(repo), "", resuming=True)

    made = worktree.resolve(str(repo), "review")
    again = worktree.resolve(str(repo), "review", resuming=True)
    assert again.path == made.path and not again.created


@pytest.mark.parametrize(
    "flags", [["--resume"], ["-c"], ["--session-id", "x"], ["-p", "hi", "--continue"]]
)
def test_run_reads_every_conversation_flag_as_a_resume(repo, monkeypatch, flags):
    from claude_launcher import runner

    cli.main(["create", "work", "--no-seed"])
    seen = {}
    monkeypatch.setattr(runner.subprocess, "run", fake_launch(seen))
    monkeypatch.setattr(worktree, "interactive", lambda: True)
    monkeypatch.setattr(
        "builtins.input", lambda *a: pytest.fail("a resume must not be asked")
    )
    monkeypatch.chdir(repo)
    assert cli.main(["run", "work", *flags]) == 0
    assert seen["cwd"] is None  # stayed in the checkout the conversation is in


def test_run_with_a_resume_refuses_a_new_worktree_and_enters_an_existing_one(
    repo, monkeypatch, capsys
):
    from claude_launcher import runner

    cli.main(["create", "work", "--no-seed"])
    capsys.readouterr()

    def no_launch(cmd, **kwargs):
        if cmd and cmd[0] == "git":
            return REAL_RUN(cmd, **kwargs)
        pytest.fail(f"must not launch: {cmd}")

    monkeypatch.setattr(runner.subprocess, "run", no_launch)
    monkeypatch.chdir(repo)
    assert cli.main(["run", "work", "--worktree=fresh", "--resume"]) == 1
    assert "cannot be opened in a new one" in capsys.readouterr().err

    worktree.resolve(str(repo), "review")
    seen = {}
    monkeypatch.setattr(runner.subprocess, "run", fake_launch(seen))
    assert cli.main(["run", "work", "--worktree=review", "--resume"]) == 0
    assert seen["cwd"] == str(repo / ".claude" / "worktrees" / "review")
    assert seen["cmd"][1:] == ["--resume"]


def test_new_session_treats_a_resume_the_same_way(
    repo, fake_daemon, monkeypatch, capsys
):
    """--resume is claunch's spelling; `-- --continue` is the harness's."""
    monkeypatch.chdir(repo)
    monkeypatch.setattr(worktree, "interactive", lambda: True)
    monkeypatch.setattr(
        "builtins.input", lambda *a: pytest.fail("a resume must not be asked")
    )
    assert cli.main(["new-session", "--profile", "work", "--resume"]) == 0
    assert fake_daemon.posted[1]["cwd"] == str(repo)
    assert cli.main(["new-session", "--profile", "work", "--", "--continue"]) == 0
    assert fake_daemon.posted[1]["cwd"] == str(repo)

    fake_daemon.posted = None
    assert cli.main(
        ["new-session", "--profile", "work", "--worktree=fresh", "--resume"]
    ) == 1
    assert fake_daemon.posted is None
    assert "cannot be opened in a new one" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# the pane label: who is running here, and where
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "args, expected",
    [
        (("api", "review", "/w/repo"), "api · review · /w/repo"),
        (("api", "", "/w/repo"), "api · /w/repo"),
        (("api", "master", ""), "api · master"),
        (("", "master", "/w/repo"), "master · /w/repo"),
        (("api", "", ""), "api"),
        # The role rides with the identity, never as a segment of its own —
        # a bare word between separators would read as a branch.
        (("s22", "review", "/w/repo", "worker"), "s22 (worker) · review · /w/repo"),
        (("s22", "", "", "worker"), "s22 (worker)"),
        (("", "master", "/w/repo", "worker"), "(worker) · master · /w/repo"),
    ],
)
def test_launch_label_composition(args, expected):
    assert herdr.launch_label(*args) == expected


def test_launch_label_writes_home_as_tilde(monkeypatch, tmp_path):
    monkeypatch.setattr(herdr.Path, "home", classmethod(lambda cls: tmp_path))
    label = herdr.launch_label("api", "main", str(tmp_path / "works" / "repo"))
    assert label == "api · main · " + str(Path("~/works/repo")).replace("\\", os.sep)


def test_launch_label_drops_leading_path_segments_but_never_the_identity():
    """The tail tells two worktrees of one repository apart; the road to the
    workspace is the same on every pane. The identity -- role included -- is
    the fixed part the path budget is measured against."""
    deep = "/w/" + "/".join(f"level{i}" for i in range(20)) + "/worktrees/review"
    label = herdr.launch_label("api", "review", deep, limit=60)
    assert len(label) <= 60
    assert label.startswith("api · review · …")
    assert label.endswith("worktrees/review")
    # The ellipsis lands on a segment boundary, not mid-word.
    assert "…level" in label or "…worktrees" in label

    assert herdr.launch_label("api", "review", "/w/repo", limit=12).startswith(
        "api · review"
    )
    assert herdr.launch_label(
        "api", "review", "/w/repo", "worker", limit=12
    ).startswith("api (worker) · review")


def test_pane_label_reads_branch_and_directory_wherever_it_stands(
    repo, outside_a_repo, tmp_path
):
    """A worktree, the main checkout, a plain directory, a directory that is
    not there: the directory is always the answer to "which of the agents in
    this repo is this one", and the branch and role join it when known."""
    made = worktree.resolve(str(repo), "review")
    assert worktree.pane_label("api", str(made.path)) == (
        f"api · review · {made.path}"
    )
    assert worktree.pane_label("s22", str(made.path), "worker") == (
        f"s22 (worker) · review · {made.path}"
    )
    branch = worktree.current_branch(repo)
    assert worktree.pane_label("api", str(repo)) == f"api · {branch} · {repo}"
    assert worktree.pane_label("api", str(outside_a_repo)) == (
        f"api · {outside_a_repo}"
    )
    gone = tmp_path / "gone"
    assert worktree.pane_label("api", str(gone)) == f"api · {gone}"


def test_run_from_inside_a_worktree_is_labelled_by_where_it_already_is(
    repo, monkeypatch, capsys
):
    """The label says where the agent works, not only where one was moved to."""
    from claude_launcher import runner

    made = worktree.resolve(str(repo), "review")
    cli.main(["create", "work", "--no-seed"])
    capsys.readouterr()
    labels = []
    monkeypatch.setattr(
        herdr, "rename_pane", lambda label, **kw: labels.append(label) or True
    )
    monkeypatch.setattr(herdr, "clear_pane_label", lambda **kw: True)
    monkeypatch.setattr(runner.subprocess, "run", fake_launch({}))
    monkeypatch.setattr(worktree, "interactive", lambda: False)
    monkeypatch.chdir(made.path)
    here = os.getcwd()
    assert cli.main(["run", "work"]) == 0
    assert labels == [f"work · review · {here}"]


def test_run_in_the_main_checkout_labels_the_pane_and_clears_it_even_on_failure(
    repo, monkeypatch, capsys
):
    """No worktree is not "nowhere" -- the directory is still the answer to
    "which of the agents in this repo is this one". And the label comes off
    however claude ends."""
    from claude_launcher import runner

    cli.main(["create", "work", "--no-seed"])
    capsys.readouterr()
    labels, cleared = [], []
    monkeypatch.setattr(
        herdr, "rename_pane", lambda label, **kw: labels.append(label) or True
    )
    monkeypatch.setattr(herdr, "clear_pane_label", lambda **kw: cleared.append(True))

    def boom(cmd, **kwargs):
        if cmd and cmd[0] == "git":
            return REAL_RUN(cmd, **kwargs)
        raise OSError("no claude here")

    monkeypatch.setattr(runner.subprocess, "run", boom)
    monkeypatch.setattr(worktree, "interactive", lambda: False)
    monkeypatch.chdir(repo)
    here = os.getcwd()
    branch = worktree.current_branch(repo)
    assert cli.main(["run", "work"]) == 1
    assert labels == [f"work · {branch} · {here}"]
    assert cleared == [True]


class _NoRawTerminal:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def attachable(monkeypatch):
    """Stub out everything an attach touches except the labelling."""
    from claude_launcher import attach as attach_mod

    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True, raising=False)
    monkeypatch.setattr(attach_mod, "_RawTerminal", _NoRawTerminal)
    monkeypatch.setattr(attach_mod, "_write_text", lambda text: None)

    async def detached(*a, **k):
        return {"reason": "detach"}

    monkeypatch.setattr(attach_mod, "_attach_async", detached)
    return attach_mod


class AttachClient:
    base_url = "http://127.0.0.1:0"
    token = "t"

    def __init__(self, cwd):
        self._cwd = cwd

    def get(self, path):
        return {"name": "api", "status": "idle", "cwd": self._cwd}


def test_attach_labels_the_pane_for_as_long_as_it_lasts(repo, attachable, monkeypatch):
    """The one place a pane and a session genuinely coincide."""
    made = worktree.resolve(str(repo), "review")
    labels, cleared = [], []
    monkeypatch.setattr(
        herdr, "rename_pane", lambda label, **kw: labels.append(label) or True
    )
    monkeypatch.setattr(herdr, "clear_pane_label", lambda **kw: cleared.append(True))
    assert attachable.attach(AttachClient(str(made.path)), "api") == 0
    assert labels == [f"api · review · {made.path}"]
    # Detaching hands the pane back: a label for a session you are no longer
    # watching still reads as true.
    assert cleared == [True]


def test_attach_outside_herdr_clears_nothing(repo, attachable, monkeypatch):
    """rename_pane says False off-Herdr, and a clear that was never set would
    take away a label somebody else put there."""
    monkeypatch.setattr(herdr, "rename_pane", lambda *a, **k: False)
    monkeypatch.setattr(
        herdr, "clear_pane_label", lambda **kw: pytest.fail("nothing was set")
    )
    assert attachable.attach(AttachClient(str(repo)), "api") == 0


# --------------------------------------------------------------------------- #
# reusing a checkout, and catching it up
# --------------------------------------------------------------------------- #
def base_branch(repo) -> str:
    """Whatever this machine's git calls the first branch (master/main)."""
    return worktree.current_branch(repo)


def commit(path, name, text="x\n"):
    (path / name).write_text(text, encoding="utf-8")
    git("add", "-A", cwd=path)
    git("commit", "-qm", name, cwd=path)


def test_info_reads_the_repository_in_one_answer(repo):
    worktree.resolve(str(repo), "review")
    info = worktree.info(str(repo))
    assert info["repo"] is True
    assert info["branch"] == base_branch(repo)
    assert set(info["branches"]) >= {base_branch(repo), "review"}
    assert info["worktrees"] == ["review"]
    # each name carries its path, which is what joins a session's cwd to the
    # checkout it sits in -- read from inside that checkout the answer is the
    # same, since the list is the repository's, not the directory's
    wt_path = worktree.worktrees_dir(repo) / "review"
    assert list(info["paths"]) == ["review"]
    assert os.path.normcase(info["paths"]["review"]) == os.path.normcase(
        str(wt_path.resolve()))
    assert worktree.info(str(wt_path))["paths"] == info["paths"]
    # a directory that is no repository says so rather than half-answering
    assert worktree.info(str(repo.parent))["repo"] is False
    assert worktree.info(str(repo.parent))["paths"] == {}


def test_a_reused_worktree_is_brought_up_to_date(repo, capsys):
    """The point of the option: a checkout you come back to is as far behind
    as the day you left it. An untracked file is not uncommitted work (a
    rebase does not care about it, so neither does this), and the update is
    announced -- it is the step that could have failed and did not."""
    wt = worktree.resolve(str(repo), "review")
    commit(wt.path, "side.txt")
    (wt.path / "build.log").write_text("noise\n", encoding="utf-8")
    commit(repo, "moved-on.txt")

    again = worktree.resolve(str(repo), "review", rebase_onto=base_branch(repo))
    assert again.created is False
    assert again.rebased == base_branch(repo)
    # master's commit is now under the worktree's own, and its file is there
    assert (again.path / "moved-on.txt").exists()
    assert (again.path / "side.txt").exists()
    assert (again.path / "build.log").exists()
    log = git("log", "--oneline", cwd=again.path).stdout
    assert log.index("side.txt") < log.index("moved-on.txt")
    worktree.announce(again)
    assert f"rebased onto {base_branch(repo)}" in capsys.readouterr().err


def test_a_fresh_worktree_is_cut_from_the_branch_it_is_put_on_and_never_rebased(repo):
    """``rebase_onto`` says where the checkout ends up, and for a NEW branch
    that means where it is cut from: a nested worker's branch cut from its
    parent's branch begins on the stack, not on the trunk. Nothing is
    replayed, so ``rebased`` stays empty -- there was no rebase, and a rebase
    of an empty branch would only be a chance to fail for no reason."""
    parent = worktree.resolve(str(repo), "parent")
    commit(parent.path, "parent-only.txt")
    trunk_tip = git("rev-parse", "HEAD", cwd=repo).stdout.strip()

    child = worktree.resolve(str(repo), "child", rebase_onto="parent")
    assert child.created is True and child.rebased == ""
    assert child.branch == "child"
    assert (child.path / "parent-only.txt").exists()
    assert (
        git("rev-parse", "HEAD", cwd=child.path).stdout.strip()
        == git("rev-parse", "parent", cwd=repo).stdout.strip()
    )
    # the trunk did not move, and a cut that names the trunk starts there
    assert git("rev-parse", "HEAD", cwd=repo).stdout.strip() == trunk_tip
    plain = worktree.resolve(str(repo), "plain", rebase_onto=base_branch(repo))
    assert plain.created is True and plain.rebased == ""
    assert not (plain.path / "parent-only.txt").exists()
    # a branch that already exists is checked out as it stands -- the start
    # point is for new branches only
    git("branch", "old", trunk_tip, cwd=repo)
    old = worktree.resolve(str(repo), "old", rebase_onto="parent")
    assert old.created is True and not (old.path / "parent-only.txt").exists()


def test_a_catch_up_that_cannot_run_is_refused_before_touching_anything(repo):
    """There is no safe automatic answer to somebody's uncommitted work, so
    it is refused rather than stashed, moved or committed for them; a base
    that is no branch is refused by name."""
    wt = worktree.resolve(str(repo), "review")
    with pytest.raises(worktree.WorktreeError) as exc:
        worktree.resolve(str(repo), "review", rebase_onto="nosuch")
    assert "no branch 'nosuch'" in str(exc.value)

    (wt.path / "a.txt").write_text("mine, uncommitted\n", encoding="utf-8")
    with pytest.raises(worktree.WorktreeError) as exc:
        worktree.resolve(str(repo), "review", rebase_onto=base_branch(repo))
    assert "uncommitted changes" in str(exc.value)
    # untouched: still theirs, still there
    assert (wt.path / "a.txt").read_text(encoding="utf-8") == "mine, uncommitted\n"


def test_a_conflicting_rebase_refuses_and_leaves_nothing_half_done(repo):
    """An agent started in a half-rebased checkout would spend its first turn
    on a mess it did not make, so the rebase is aborted and the launch dies."""
    wt = worktree.resolve(str(repo), "review")
    (wt.path / "a.txt").write_text("theirs\n", encoding="utf-8")
    git("add", "-A", cwd=wt.path)
    git("commit", "-qm", "theirs", cwd=wt.path)
    (repo / "a.txt").write_text("ours\n", encoding="utf-8")
    git("add", "-A", cwd=repo)
    git("commit", "-qm", "ours", cwd=repo)

    with pytest.raises(worktree.WorktreeError) as exc:
        worktree.resolve(str(repo), "review", rebase_onto=base_branch(repo))
    assert "was aborted" in str(exc.value)
    # the checkout is exactly as it was found: on its branch, not mid-rebase
    assert worktree.current_branch(wt.path) == "review"
    assert (wt.path / "a.txt").read_text(encoding="utf-8") == "theirs\n"
    assert git("status", "--porcelain", cwd=wt.path).stdout.strip() == ""
