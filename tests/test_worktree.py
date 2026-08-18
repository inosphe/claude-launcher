"""Worktree launches: who is asked, who is never asked, and what gets made."""

from __future__ import annotations

import subprocess

import pytest

from claude_launcher import cli, herdr, worktree


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


@pytest.fixture(autouse=True)
def no_herdr(monkeypatch):
    """Tests must not talk to a real multiplexer, or read one's env."""
    monkeypatch.delenv(herdr.ENV_FLAG, raising=False)
    monkeypatch.delenv(herdr.PANE_ID_ENV, raising=False)
    monkeypatch.delenv(worktree.SESSION_ENV, raising=False)
    monkeypatch.delenv(worktree.WORKTREE_DIR_ENV, raising=False)
    monkeypatch.setattr(herdr, "_run", lambda args: False)


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


def test_herdr_env_gates_the_pane_id(monkeypatch):
    """A stale HERDR_PANE_ID without HERDR_ENV=1 is not this pane's."""
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


def test_second_ask_for_the_same_name_reuses_the_checkout(repo):
    first = worktree.resolve(str(repo), "review")
    (first.path / "wip.txt").write_text("uncommitted\n", encoding="utf-8")
    again = worktree.resolve(str(repo), "review")
    assert again.path == first.path
    assert not again.created
    # Reuse means the work in it survives; a fresh checkout would not have it.
    assert (again.path / "wip.txt").exists()


def test_existing_branch_is_checked_out_not_recut(repo):
    git("branch", "already", cwd=repo)
    wt = worktree.resolve(str(repo), "already")
    assert wt.created and wt.branch == "already"


def test_launching_from_inside_a_worktree_makes_a_sibling(repo):
    inner = worktree.resolve(str(repo), "first")
    sibling = worktree.resolve(str(inner.path), "second")
    # Beside it, not nested inside the checkout an agent is already editing.
    assert sibling.path.parent == inner.path.parent
    assert repo in sibling.path.parents


def test_directory_in_the_way_is_refused(repo):
    (repo / ".claude" / "worktrees" / "taken").mkdir(parents=True)
    with pytest.raises(worktree.WorktreeError):
        worktree.resolve(str(repo), "taken")


def test_worktree_dir_env_relocates_them(repo, tmp_path, monkeypatch):
    elsewhere = tmp_path / "trees"
    monkeypatch.setenv(worktree.WORKTREE_DIR_ENV, str(elsewhere))
    wt = worktree.resolve(str(repo), "moved")
    assert wt.path == elsewhere / "moved"


def test_label_names_both_when_branch_diverges(repo):
    wt = worktree.resolve(str(repo), "review")
    git("checkout", "-q", "-b", "other", cwd=wt.path)
    again = worktree.resolve(str(repo), "review")
    assert again.branch == "other"
    assert again.label == "review [other]"
    # ...and only once when they agree, which is the usual case.
    assert wt.label == "review"


# --------------------------------------------------------------------------- #
# who gets asked
# --------------------------------------------------------------------------- #
def test_no_worktree_never_asks_and_never_makes_one(repo, monkeypatch):
    monkeypatch.setattr(worktree, "interactive", lambda: True)
    monkeypatch.setattr(
        "builtins.input", lambda *a: pytest.fail("--no-worktree must not ask")
    )
    assert worktree.resolve(str(repo), worktree.NEVER) is None


def test_a_managed_session_is_not_a_person(repo, monkeypatch):
    """An agent's PTY passes isatty(); it must still not be prompted."""
    monkeypatch.setenv(worktree.SESSION_ENV, "worker-1")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    assert not worktree.interactive()
    monkeypatch.setattr(
        "builtins.input", lambda *a: pytest.fail("a session must not be asked")
    )
    assert worktree.resolve(str(repo), worktree.ASK) is None


def test_no_tty_stays_put(repo, monkeypatch):
    monkeypatch.setattr(worktree, "interactive", lambda: False)
    assert worktree.resolve(str(repo), worktree.ASK) is None


def test_asked_and_declined_stays_put(repo, monkeypatch):
    monkeypatch.setattr(worktree, "interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: "n")
    assert worktree.resolve(str(repo), worktree.ASK) is None


def test_asked_and_accepted_with_a_name(repo, monkeypatch):
    answers = iter(["y", "mine"])
    monkeypatch.setattr(worktree, "interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    wt = worktree.resolve(str(repo), worktree.ASK)
    assert wt.name == "mine"


def test_asked_and_accepted_with_a_blank_name_takes_the_suggestion(repo, monkeypatch):
    answers = iter(["y", ""])
    monkeypatch.setattr(worktree, "interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    monkeypatch.setattr(worktree, "default_name", lambda now=None: "suggested")
    wt = worktree.resolve(str(repo), worktree.ASK)
    assert wt.name == "suggested"


def test_a_rejected_name_is_asked_again(repo, monkeypatch, capsys):
    answers = iter(["y", "no spaces", "fine"])
    monkeypatch.setattr(worktree, "interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    assert worktree.resolve(str(repo), worktree.ASK).name == "fine"
    assert "invalid worktree name" in capsys.readouterr().err


def test_eof_at_the_question_stays_put(repo, monkeypatch):
    def eof(*_a):
        raise EOFError

    monkeypatch.setattr(worktree, "interactive", lambda: True)
    monkeypatch.setattr("builtins.input", eof)
    assert worktree.resolve(str(repo), worktree.ASK) is None


# --------------------------------------------------------------------------- #
# not a repository
# --------------------------------------------------------------------------- #
def test_outside_a_repo_the_question_is_not_asked(outside_a_repo, monkeypatch):
    monkeypatch.setattr(worktree, "interactive", lambda: True)
    monkeypatch.setattr(
        "builtins.input", lambda *a: pytest.fail("nothing to make a worktree of")
    )
    assert worktree.repo_root(str(outside_a_repo)) is None
    assert worktree.resolve(str(outside_a_repo), worktree.ASK) is None


def test_outside_a_repo_an_explicit_request_fails_loudly(outside_a_repo):
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
def test_run_launches_claude_inside_the_worktree(repo, monkeypatch, capsys):
    from claude_launcher import runner

    cli.main(["create", "work", "--no-seed"])
    capsys.readouterr()
    seen = {}
    monkeypatch.setattr(runner.subprocess, "run", fake_launch(seen))
    monkeypatch.chdir(repo)
    assert cli.main(["run", "work", "--worktree=solo", "-p", "hi"]) == 0
    assert seen["cwd"] == str(repo / ".claude" / "worktrees" / "solo")
    # The flag is ours; everything else still reaches claude untouched.
    assert seen["cmd"][1:] == ["-p", "hi"]
    assert "created worktree 'solo'" in capsys.readouterr().err


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


def test_run_relabels_the_herdr_pane_with_profile_and_worktree(
    repo, monkeypatch, capsys
):
    from claude_launcher import runner

    cli.main(["create", "work", "--no-seed"])
    capsys.readouterr()
    labels, cleared = [], []
    monkeypatch.setattr(
        herdr, "rename_pane", lambda label, **kw: labels.append(label) or True
    )
    monkeypatch.setattr(herdr, "clear_pane_label", lambda **kw: cleared.append(True))
    monkeypatch.setattr(runner.subprocess, "run", fake_launch({}))
    monkeypatch.chdir(repo)
    assert cli.main(["run", "work", "--worktree=solo"]) == 0
    # The profile is run's nearest thing to a session name.
    assert labels == ["work · solo"]
    # And the pane goes back to Herdr's own label when claude exits.
    assert cleared == [True]


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
        return {"name": "s1", "harness": "claude", "profile": "work", "pid": 1}

    def get(self, path):
        return {}


@pytest.fixture
def fake_daemon(monkeypatch):
    from claude_launcher import daemon_client

    client = FakeClient()
    monkeypatch.setattr(daemon_client, "ensure_running", lambda *a, **k: client)
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    return client


def test_new_session_is_created_in_the_worktree(repo, fake_daemon, monkeypatch):
    monkeypatch.chdir(repo)
    assert cli.main(["new-session", "--profile", "work", "--worktree=solo"]) == 0
    path, body = fake_daemon.posted
    assert path == "/api/sessions"
    assert body["cwd"] == str(repo / ".claude" / "worktrees" / "solo")


def test_new_session_without_a_worktree_uses_the_directory_itself(
    repo, fake_daemon, monkeypatch
):
    monkeypatch.chdir(repo)
    monkeypatch.setattr(worktree, "interactive", lambda: False)
    assert cli.main(["new-session", "--profile", "work"]) == 0
    _, body = fake_daemon.posted
    assert body["cwd"] == str(repo)


def test_new_session_worktree_is_cut_from_the_c_flag_not_the_shell(
    repo, fake_daemon, tmp_path, monkeypatch
):
    """-c names the repository; the worktree belongs to *that* one."""
    monkeypatch.chdir(tmp_path)
    assert cli.main(
        ["new-session", "--profile", "work", "-c", str(repo), "--worktree=aimed"]
    ) == 0
    _, body = fake_daemon.posted
    assert body["cwd"] == str(repo / ".claude" / "worktrees" / "aimed")


def test_a_session_creating_a_session_is_never_asked(repo, fake_daemon, monkeypatch):
    """Inside a managed session, new-session is refused before anything is
    made -- and even --detached must not open a prompt nobody can answer."""
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


def test_a_failed_worktree_creates_no_session(repo, fake_daemon, monkeypatch, capsys):
    monkeypatch.chdir(repo)
    (repo / ".claude" / "worktrees" / "taken").mkdir(parents=True)
    assert cli.main(["new-session", "--profile", "work", "--worktree=taken"]) == 1
    assert fake_daemon.posted is None
    assert "error:" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# resume: the conversation decides the directory
# --------------------------------------------------------------------------- #
def test_a_resume_is_not_asked_the_question(repo, monkeypatch):
    """`claunch run nc --resume` means "carry on where I was", and where it
    was is this directory -- claude keeps transcripts per cwd."""
    monkeypatch.setattr(worktree, "interactive", lambda: True)
    monkeypatch.setattr(
        "builtins.input", lambda *a: pytest.fail("a resume must not be asked")
    )
    assert worktree.resolve(str(repo), worktree.ASK, resuming=True) is None


def test_a_resume_refuses_a_new_worktree(repo):
    with pytest.raises(worktree.WorktreeError, match="cannot be opened in a new one"):
        worktree.resolve(str(repo), "fresh", resuming=True)
    # ...and nothing was left behind by the refusal.
    assert not (repo / ".claude" / "worktrees" / "fresh").exists()


def test_a_resume_refuses_a_generated_name(repo):
    """Bare --worktree names a checkout after the second, so by definition it
    is new -- and a new one has no conversation to resume."""
    with pytest.raises(worktree.WorktreeError):
        worktree.resolve(str(repo), "", resuming=True)


def test_a_resume_into_an_existing_worktree_is_allowed(repo):
    """The useful case: go back to that checkout and carry on the work there."""
    made = worktree.resolve(str(repo), "review")
    again = worktree.resolve(str(repo), "review", resuming=True)
    assert again.path == made.path and not again.created


def test_no_worktree_with_a_resume_is_still_just_no(repo):
    assert worktree.resolve(str(repo), worktree.NEVER, resuming=True) is None


@pytest.mark.parametrize(
    "flags", [["--resume"], ["-r"], ["--continue"], ["-c"], ["--resume=abc"],
              ["--session-id", "x"], ["-p", "hi", "--continue"]]
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


def test_run_refuses_a_new_worktree_with_a_resume(repo, monkeypatch, capsys):
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


def test_run_allows_a_resume_into_an_existing_worktree(repo, monkeypatch, capsys):
    from claude_launcher import runner

    cli.main(["create", "work", "--no-seed"])
    capsys.readouterr()
    worktree.resolve(str(repo), "review")
    seen = {}
    monkeypatch.setattr(runner.subprocess, "run", fake_launch(seen))
    monkeypatch.chdir(repo)
    assert cli.main(["run", "work", "--worktree=review", "--resume"]) == 0
    assert seen["cwd"] == str(repo / ".claude" / "worktrees" / "review")
    assert seen["cmd"][1:] == ["--resume"]


def test_new_session_resume_is_not_asked_the_question(repo, fake_daemon, monkeypatch):
    monkeypatch.chdir(repo)
    monkeypatch.setattr(worktree, "interactive", lambda: True)
    monkeypatch.setattr(
        "builtins.input", lambda *a: pytest.fail("a resume must not be asked")
    )
    assert cli.main(["new-session", "--profile", "work", "--resume"]) == 0
    _, body = fake_daemon.posted
    assert body["cwd"] == str(repo)


def test_new_session_reads_a_harness_side_conversation_flag_too(
    repo, fake_daemon, monkeypatch
):
    """--resume is claunch's spelling; `-- --continue` is the harness's."""
    monkeypatch.chdir(repo)
    monkeypatch.setattr(worktree, "interactive", lambda: True)
    monkeypatch.setattr(
        "builtins.input", lambda *a: pytest.fail("a resume must not be asked")
    )
    assert cli.main(["new-session", "--profile", "work", "--", "--continue"]) == 0
    _, body = fake_daemon.posted
    assert body["cwd"] == str(repo)


def test_new_session_refuses_a_new_worktree_with_a_resume(
    repo, fake_daemon, monkeypatch, capsys
):
    monkeypatch.chdir(repo)
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
        (("api", "review", "review"), "api · review"),
        (("api", "review", "other"), "api · review [other]"),
        # No worktree: a session in the main checkout is just itself, and the
        # repository is the one fact every pane would repeat.
        (("api", "", ""), "api"),
        (("", "review", "review"), "review"),
        (("api", "review", ""), "api · review"),
    ],
)
def test_launch_label_composition(args, expected):
    assert herdr.launch_label(*args) == expected


def test_inspect_names_the_worktree_a_directory_is(repo):
    made = worktree.resolve(str(repo), "review")
    seen = worktree.inspect(str(made.path))
    assert seen is not None
    assert (seen.name, seen.branch) == ("review", "review")
    assert not seen.created


def test_inspect_ignores_the_main_checkout(repo):
    """The main checkout is not a place worth naming beside the session."""
    assert worktree.inspect(str(repo)) is None


def test_inspect_ignores_a_plain_directory(outside_a_repo):
    assert worktree.inspect(str(outside_a_repo)) is None


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
    assert cli.main(["run", "work"]) == 0
    assert labels == ["work · review"]


def test_run_in_a_plain_checkout_is_labelled_by_the_profile_alone(
    repo, monkeypatch, capsys
):
    from claude_launcher import runner

    cli.main(["create", "work", "--no-seed"])
    capsys.readouterr()
    labels = []
    monkeypatch.setattr(
        herdr, "rename_pane", lambda label, **kw: labels.append(label) or True
    )
    monkeypatch.setattr(herdr, "clear_pane_label", lambda **kw: True)
    monkeypatch.setattr(runner.subprocess, "run", fake_launch({}))
    monkeypatch.setattr(worktree, "interactive", lambda: False)
    monkeypatch.chdir(repo)
    assert cli.main(["run", "work"]) == 0
    assert labels == ["work"]


def test_run_clears_the_label_even_when_claude_fails(repo, monkeypatch, capsys):
    from claude_launcher import runner

    cli.main(["create", "work", "--no-seed"])
    capsys.readouterr()
    cleared = []
    monkeypatch.setattr(herdr, "rename_pane", lambda label, **kw: True)
    monkeypatch.setattr(herdr, "clear_pane_label", lambda **kw: cleared.append(True))

    def boom(cmd, **kwargs):
        if cmd and cmd[0] == "git":
            return REAL_RUN(cmd, **kwargs)
        raise OSError("no claude here")

    monkeypatch.setattr(runner.subprocess, "run", boom)
    monkeypatch.setattr(worktree, "interactive", lambda: False)
    monkeypatch.chdir(repo)
    assert cli.main(["run", "work"]) == 1
    assert cleared == [True]


def test_an_unattached_new_session_leaves_the_pane_alone(
    repo, fake_daemon, monkeypatch
):
    """It runs in the daemon's PTY, not in this pane -- and nothing would ever
    take the label back off."""
    monkeypatch.setattr(
        herdr, "rename_pane", lambda *a, **k: pytest.fail("not this pane's session")
    )
    monkeypatch.chdir(repo)
    assert cli.main(["new-session", "--profile", "work", "--worktree=solo"]) == 0
    assert fake_daemon.posted[1]["cwd"].endswith("solo")


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
    assert labels == ["api · review"]
    # Detaching hands the pane back: a label for a session you are no longer
    # watching still reads as true.
    assert cleared == [True]


def test_attach_to_the_main_checkout_is_labelled_by_the_session_alone(
    repo, attachable, monkeypatch
):
    labels = []
    monkeypatch.setattr(
        herdr, "rename_pane", lambda label, **kw: labels.append(label) or True
    )
    monkeypatch.setattr(herdr, "clear_pane_label", lambda **kw: True)
    assert attachable.attach(AttachClient(str(repo)), "api") == 0
    assert labels == ["api"]


def test_attach_outside_herdr_clears_nothing(repo, attachable, monkeypatch):
    """rename_pane says False off-Herdr, and a clear that was never set would
    take away a label somebody else put there."""
    monkeypatch.setattr(herdr, "rename_pane", lambda *a, **k: False)
    monkeypatch.setattr(
        herdr, "clear_pane_label", lambda **kw: pytest.fail("nothing was set")
    )
    assert attachable.attach(AttachClient(str(repo)), "api") == 0
