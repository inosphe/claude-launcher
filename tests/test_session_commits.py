"""A session's commits, read back off the stamps ``commit-stamp`` leaves.

Four surfaces, and each group below owns one of them:

* :mod:`claude_launcher.session_commits` — whose commits a commit is, which
  is a question ``--grep`` alone answers wrongly (``s19`` would collect
  ``s191``'s work), and where the walk has to start from for a worker's
  feature branch to be visible at all.
* ``claunch commits`` — the terminal's handle on the same read.
* the daemon — ``/api/sessions/<name>/meta`` carries it, so the detail panel
  and the terminal cannot drift apart.
* ``improv-worker`` — the workflow that must actually demand the stamp and
  the per-commit issue comment, in both layers.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from claude_launcher import cli, session_commits


def git(*args, cwd) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )


@pytest.fixture
def repo(tmp_path, monkeypatch, repo_template):
    """A real repository, because the whole module is a git query.

    ``GIT_CEILING_DIRECTORIES`` is set for the same reason the ctxsize tests
    set it: pytest's basetemp can sit inside a checkout, and git's discovery
    walks upward — without it a "not a repository" case quietly becomes a
    query against the repository under test.
    """
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    return repo_template("session-commits", _build_repo, tmp_path / "repo")


def _build_repo(r: Path) -> None:
    git("init", "-q", "-b", "master", cwd=r)
    git("config", "user.email", "t@example.com", cwd=r)
    git("config", "user.name", "t", cwd=r)


def commit(repo: Path, subject: str, *, session=None, worktree=None, body="") -> str:
    """One commit, stamped the way the skill teaches — or not stamped at all."""
    n = len(list(repo.glob("f*.txt"))) + 1
    (repo / f"f{n}.txt").write_text(subject, encoding="utf-8")
    git("add", "-A", cwd=repo)
    message = subject
    if body:
        message += "\n\n" + body
    trailers = []
    if session:
        trailers.append(f"{session_commits.SESSION_TRAILER}: {session}")
    if worktree:
        trailers.append(f"{session_commits.WORKTREE_TRAILER}: {worktree}")
    if trailers:
        message += "\n\n" + "\n".join(trailers)
    git("commit", "-q", "-m", message, cwd=repo)
    return git("rev-parse", "--short", "HEAD", cwd=repo).stdout.strip()


# --------------------------------------------------------------------------- #
# whose commit is it
# --------------------------------------------------------------------------- #
def test_a_stamped_commit_is_attributed_and_an_unstamped_one_is_not(repo):
    """The trailer is the whole record. A commit without one is not "missing
    from the list" — it is not this session's commit, which is exactly what
    ``commit-stamp`` says an omitted stamp means."""
    mine = commit(repo, "feat: stamped", session="s191")
    commit(repo, "chore: unstamped")
    found = session_commits.for_session(repo, "s191")
    assert [c["short"] for c in found] == [mine]


def test_a_shorter_session_name_does_not_collect_a_longer_ones_work(repo):
    """``--grep`` is a substring filter on the walk and would hand ``s19``
    every commit ``s191`` made. The exact trailer comparison is what stops
    it, so this is the test that fails if that check is ever dropped."""
    long_one = commit(repo, "feat: for s191", session="s191")
    short_one = commit(repo, "feat: for s19", session="s19")
    assert [c["short"] for c in session_commits.for_session(repo, "s19")] == [short_one]
    assert [c["short"] for c in session_commits.for_session(repo, "s191")] == [long_one]


def test_the_worktree_is_carried_when_stamped_and_absent_when_not(repo):
    commit(repo, "feat: in a worktree", session="s1", worktree="s1-thing")
    commit(repo, "feat: in the main checkout", session="s1")
    main, linked = session_commits.for_session(repo, "s1")
    assert "worktree" not in main
    assert linked["worktree"] == "s1-thing"


def test_the_list_is_newest_first_and_honours_the_limit(repo):
    first = commit(repo, "feat: one", session="s1")
    commit(repo, "feat: two", session="s1")
    third = commit(repo, "feat: three", session="s1")
    assert [c["short"] for c in session_commits.for_session(repo, "s1", limit=1)] == [third]
    assert len(session_commits.for_session(repo, "s1")) == 3
    assert session_commits.for_session(repo, "s1")[-1]["short"] == first


def test_a_commit_on_a_branch_that_is_not_checked_out_still_counts(repo):
    """A worker's commits live on its feature branch, and the directory the
    daemon reads is very often not on it. Walking ``HEAD`` would report a busy
    session as having committed nothing."""
    commit(repo, "chore: base", session="s1")
    git("checkout", "-qb", "s1-feature", cwd=repo)
    on_branch = commit(repo, "feat: on the feature branch", session="s1")
    git("checkout", "-q", "master", cwd=repo)
    assert on_branch in [c["short"] for c in session_commits.for_session(repo, "s1")]


def test_a_trailer_in_the_body_rather_than_the_last_paragraph_is_not_a_stamp(repo):
    """Git reads trailers out of the final paragraph only, and this module
    reads what git reads. A session name quoted mid-message — in a commit
    that talks *about* stamping, which this repository's own history has —
    must not be collected as authorship."""
    commit(
        repo,
        "docs: describe the stamp",
        body=f"we write {session_commits.SESSION_TRAILER}: s1 at the end.\n\nmore prose",
    )
    assert session_commits.for_session(repo, "s1") == []


def test_the_subject_survives_whatever_is_in_it(repo):
    """Records are split on control characters, so a subject full of the
    punctuation a real commit message uses must come back whole."""
    subject = "fix(gate): the tree key — 'a: b' | c, e"
    commit(repo, subject, session="s1")
    assert session_commits.for_session(repo, "s1")[0]["subject"] == subject


@pytest.mark.parametrize("session", ["", "   "])
def test_no_session_name_is_an_empty_list_not_every_commit(repo, session):
    commit(repo, "feat: stamped", session="s1")
    assert session_commits.for_session(repo, session) == []


def test_a_directory_that_is_no_repository_answers_empty_rather_than_raising(
    tmp_path, monkeypatch
):
    """What this pins is the *contract*: describing a session must not be able
    to raise, because a pruned directory would then take out the whole detail
    panel.

    What it does NOT pin, and must not be read as blessing, is that an empty
    list is a good ANSWER here. It is the same value a readable empty
    repository gives, so callers may only say no commit was found -- see
    ``for_session``'s docstring, the two callers' wording, and
    ``claunch-j5kp`` for making the two tellable apart."""
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    plain = tmp_path / "plain"
    plain.mkdir()
    assert session_commits.for_session(plain, "s1") == []
    assert session_commits.for_session(tmp_path / "gone", "s1") == []
    assert session_commits.for_session(None, "s1") == []


def test_summary_is_the_shape_the_panel_reads(repo):
    commit(repo, "feat: one", session="s1", worktree="s1-thing")
    newest = commit(repo, "feat: two", session="s1", worktree="s1-thing")
    s = session_commits.summary(session_commits.for_session(repo, "s1"))
    assert s["count"] == 2
    assert s["latest"] == newest
    assert s["worktrees"] == ["s1-thing"]      # distinct, not one per commit
    assert [c["short"] for c in s["commits"]][0] == newest


def test_summary_of_nothing_is_still_a_shape(repo):
    """The block is drawn for a session that has not committed yet, so the
    empty case must be a whole object rather than something the caller has to
    guard around."""
    s = session_commits.summary([])
    assert s == {"commits": [], "count": 0, "latest": None, "worktrees": []}


# --------------------------------------------------------------------------- #
# claunch commits
# --------------------------------------------------------------------------- #
def run_cli(*argv) -> int:
    return cli.main(list(argv))


def test_the_cli_prints_a_row_per_commit(home, repo, capsys):
    short = commit(repo, "feat: something", session="s1", worktree="s1-thing")
    assert run_cli("commits", "--session", "s1", "--repo", str(repo)) == 0
    out = capsys.readouterr().out
    assert short in out and "feat: something" in out and "s1-thing" in out


def test_the_cli_json_is_the_object_the_api_serves(home, repo, capsys):
    commit(repo, "feat: something", session="s1")
    assert run_cli("commits", "--session", "s1", "--repo", str(repo), "--json") == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc == session_commits.summary(session_commits.for_session(repo, "s1"))


def test_nothing_found_is_said_about_the_search_not_about_the_session(home, repo, capsys):
    """Not an error, and not a claim either. An unreadable repository comes
    back from ``for_session`` exactly the way a readable empty one does, so
    "this session committed nothing" would be a sentence this command has no
    way to check. "found" is the word that keeps it about the reading."""
    assert run_cli("commits", "--session", "s1", "--repo", str(repo)) == 0
    out = capsys.readouterr().out
    assert "no stamped commit found" in out
    assert "this session" not in out


def test_the_unreadable_case_gets_the_same_careful_sentence(home, tmp_path, monkeypatch, capsys):
    """The case the wording exists for: a directory that is not a checkout
    (a pruned worktree reaches the same branch). A session that committed
    twenty times would print this, so the sentence must not deny them."""
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    plain = tmp_path / "plain"
    plain.mkdir()
    assert run_cli("commits", "--session", "s1", "--repo", str(plain)) == 0
    assert "no stamped commit found" in capsys.readouterr().out


def test_the_session_comes_from_the_environment_when_unnamed(home, repo, monkeypatch, capsys):
    short = commit(repo, "feat: something", session="s7")
    monkeypatch.setenv("CLAUNCH_SESSION", "s7")
    assert run_cli("commits", "--repo", str(repo)) == 0
    assert short in capsys.readouterr().out


def test_with_no_session_anywhere_it_says_so_rather_than_guessing(
    home, repo, monkeypatch, capsys
):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    assert run_cli("commits", "--repo", str(repo)) == 2
    assert "no session" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# the daemon: the same read, inside /meta
# --------------------------------------------------------------------------- #
CHILD = "import time\nprint('READY')\ntime.sleep(60)\n"
CID = "cafe0000-0000-0000-0000-0000000000c0"
BEARER = {"Authorization": "Bearer sekrit"}


async def _serve(mgr):
    from aiohttp.test_utils import TestClient, TestServer

    from claude_launcher.daemon.api import build_app
    from claude_launcher.daemon.mesh import MeshManager

    app = build_app(mgr, "sekrit", started_at=time.monotonic(), mesh=MeshManager(mgr))
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


def test_the_meta_call_carries_the_sessions_commits(home, repo, monkeypatch):
    """The detail panel's read. The session here runs python rather than a
    real harness — what is under test is that ``/meta`` answers with the same
    list the module produces, keyed by the session's NAME rather than by
    whatever its checkout happens to have checked out."""
    from claude_launcher import store
    from claude_launcher.daemon.manager import SessionManager
    from claude_launcher.daemon.session import SessionDef

    store.update(
        lambda doc: doc.update(
            {"harnesses": {"py": {"command": [sys.executable, "-u", "-c", CHILD]}}}
        )
    )
    mine = commit(repo, "feat: mine", session="s191", worktree="s191-thing")
    commit(repo, "feat: another session's", session="s192")

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=50, restore_default=False)
        client = await _serve(mgr)
        try:
            mgr.create(SessionDef(name="s191", harness="py", cwd=str(repo),
                                  conversation_id=CID))
            resp = await client.get("/api/sessions/s191/meta", headers=BEARER)
            assert resp.status == 200
            block = (await resp.json())["commits"]
            assert [c["short"] for c in block["commits"]] == [mine]
            assert block["count"] == 1
            assert block["latest"] == mine
            assert block["worktrees"] == ["s191-thing"]
        finally:
            await client.close()
            await mgr.shutdown_all()

    asyncio.run(run())


def test_a_session_with_no_directory_gets_null_rather_than_an_empty_summary(
    home, repo, monkeypatch
):
    """The difference between "committed nothing" and "there was nowhere to
    look", at the one place the daemon can tell them apart for free.

    An empty summary here reaches the panel as a fact about the SESSION, and
    for a session with no directory that fact was never established. ``None``
    is the honest answer, and it is what keeps ``sessCommits``'s early return
    reachable at all -- the block was written for a null the daemon did not
    actually send (found in review: s181 on ``claunch-t65p``).

    ``_session_cwd`` is stubbed rather than a session created with an empty
    ``cwd``: :class:`SessionManager` fills a missing directory in with the
    daemon's own, so the empty case cannot be reached from the outside here.
    What is under test is the handler's branch on it, which is where the
    decision lives."""
    from claude_launcher import store
    from claude_launcher.daemon import api as api_mod
    from claude_launcher.daemon.manager import SessionManager
    from claude_launcher.daemon.session import SessionDef

    store.update(
        lambda doc: doc.update(
            {"harnesses": {"py": {"command": [sys.executable, "-u", "-c", CHILD]}}}
        )
    )
    monkeypatch.setattr(api_mod, "_session_cwd", lambda s: "")

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=50, restore_default=False)
        client = await _serve(mgr)
        try:
            mgr.create(SessionDef(name="nowhere", harness="py", cwd=str(repo),
                                  conversation_id=CID))
            resp = await client.get("/api/sessions/nowhere/meta", headers=BEARER)
            assert resp.status == 200
            body = await resp.json()
            assert "commits" in body        # the key is served, so the panel can read it
            assert body["commits"] is None  # and it is not an empty summary
        finally:
            await client.close()
            await mgr.shutdown_all()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# improv-worker: the workflow that must ask for it
# --------------------------------------------------------------------------- #
BUNDLED = Path("src/claude_launcher/workflows/improv-worker.yaml")
OVERRIDE = Path(".claunch/workflows/improv-worker.yaml")


def commit_step_of(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["steps"]["commit"]


@pytest.mark.parametrize("path", [BUNDLED, OVERRIDE], ids=["bundled", "override"])
def test_the_commit_step_asks_for_the_stamp_in_both_layers(path):
    """The stamp is the only thing that puts a commit into the session's
    metadata, so a commit step that does not ask for it produces rounds whose
    work is invisible the moment the terminal closes."""
    step = commit_step_of(path)
    text = step["instructions"] + step["done_when"]
    assert session_commits.SESSION_TRAILER in text
    assert session_commits.WORKTREE_TRAILER in step["instructions"]
    assert "claunch commits" in text


@pytest.mark.parametrize("path", [BUNDLED, OVERRIDE], ids=["bundled", "override"])
def test_the_commit_step_asks_for_a_comment_per_commit_in_both_layers(path):
    step = commit_step_of(path)
    assert "claunch beads comments add" in step["instructions"]
    assert "COMMIT <해시>" in step["instructions"]
    assert "COMMIT <해시>" in step["done_when"]


def test_the_commit_step_prose_is_identical_in_both_layers():
    """The project layer exists to carry this repository's verify commands,
    not a second version of the instructions. A drifted copy is how a rule
    ends up true in one checkout and not the other."""
    bundled, override = commit_step_of(BUNDLED), commit_step_of(OVERRIDE)
    assert bundled["instructions"] == override["instructions"]
    assert bundled["done_when"] == override["done_when"]


@pytest.mark.parametrize("path", [BUNDLED, OVERRIDE], ids=["bundled", "override"])
def test_the_round_report_is_told_where_the_hashes_already_are(path):
    """The HTML page is written from memory unless it is pointed at the list,
    and a hash typed from memory is the one fact in a report nobody can catch
    being wrong."""
    wrapup = yaml.safe_load(path.read_text(encoding="utf-8"))["steps"]["wrapup"]
    assert "claunch commits" in wrapup["instructions"]
