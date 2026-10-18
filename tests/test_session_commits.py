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
# the rail card's chips (claunch-h5m6f)
# --------------------------------------------------------------------------- #
@pytest.fixture
def fresh_rail(monkeypatch):
    """Each test starts with empty repository caches; the module keeps them
    across calls on purpose (one walk per repository per refs state)."""
    monkeypatch.setattr(session_commits, "_rail_dirs", {})
    monkeypatch.setattr(session_commits, "_rail_snaps", {})


@pytest.fixture
def remote(tmp_path, repo):
    """A bare remote the repository pushes to, so "pushed" has a meaning."""
    bare = tmp_path / "remote.git"
    git("init", "-q", "--bare", str(bare), cwd=tmp_path)
    git("remote", "add", "origin", str(bare), cwd=repo)
    return bare


def rail_rows(repo, session, prs=None):
    found = session_commits.rail(str(repo), session, prs)
    return found and [(c["short"], c["state"], c.get("pr")) for c in found["commits"]]


def test_rail_marks_merged_pushed_and_local(repo, remote, fresh_rail):
    merged = commit(repo, "feat: on master", session="s1")
    git("push", "-q", "origin", "master", cwd=repo)
    git("checkout", "-qb", "s1-feature", cwd=repo)
    pushed = commit(repo, "feat: pushed", session="s1")
    git("push", "-q", "origin", "s1-feature", cwd=repo)
    local = commit(repo, "feat: not pushed", session="s1")
    assert rail_rows(repo, "s1") == [
        (local, "local", None), (pushed, "pushed", None), (merged, "merged", None),
    ]


def test_rail_counts_local_master_as_merged_without_a_remote(repo, fresh_rail):
    """A local landing (the leader's --no-ff merge) is in master before any
    push; with no remote at all, every unmerged commit is local."""
    on_master = commit(repo, "feat: landed", session="s1")
    git("checkout", "-qb", "s1-feature", cwd=repo)
    off = commit(repo, "feat: off master", session="s1")
    assert rail_rows(repo, "s1") == [(off, "local", None), (on_master, "merged", None)]


def test_rail_frames_an_open_prs_commits_and_not_the_base_under_it(repo, remote, fresh_rail):
    """The head branch also reaches everything master had when it was cut;
    those commits are master's, not the PR's."""
    old = commit(repo, "feat: older work", session="s1")
    git("push", "-q", "origin", "master", cwd=repo)
    git("checkout", "-qb", "s1-feature", cwd=repo)
    a = commit(repo, "feat: a", session="s1")
    b = commit(repo, "feat: b", session="s1")
    git("push", "-q", "origin", "s1-feature", cwd=repo)
    pr = {"number": 7, "branch": "s1-feature", "base": "master"}
    assert rail_rows(repo, "s1", [pr]) == [
        (b, "pushed", 7), (a, "pushed", 7), (old, "merged", None),
    ]


def test_rail_keeps_a_merged_prs_frame_through_its_merge_commit(repo, remote, fresh_rail):
    old = commit(repo, "feat: older work", session="s1")
    git("checkout", "-qb", "s1-feature", cwd=repo)
    a = commit(repo, "feat: a", session="s1")
    git("checkout", "-q", "master", cwd=repo)
    git("merge", "-q", "--no-ff", "-m", "Merge pull request #7", "s1-feature", cwd=repo)
    git("push", "-q", "origin", "master", "s1-feature", cwd=repo)
    pr = {"number": 7, "branch": "s1-feature", "base": "master"}
    assert rail_rows(repo, "s1", [pr]) == [(a, "merged", 7), (old, "merged", None)]


def test_rail_gives_a_stacked_commit_to_the_lower_pr(repo, fresh_rail):
    """Two stacked PRs: the upper branch reaches the lower one's commits too.
    Each commit belongs to the PR whose base does not already have it."""
    commit(repo, "chore: base")
    git("checkout", "-qb", "s1-low", cwd=repo)
    low = commit(repo, "feat: low", session="s1")
    git("checkout", "-qb", "s1-high", cwd=repo)
    high = commit(repo, "feat: high", session="s1")
    prs = [{"number": 8, "branch": "s1-high", "base": "s1-low"},
           {"number": 7, "branch": "s1-low", "base": "master"}]
    assert rail_rows(repo, "s1", prs) == [(high, "local", 8), (low, "local", 7)]


def test_rail_leaves_a_pr_without_a_known_branch_empty(repo, fresh_rail):
    commit(repo, "chore: base")
    mine = commit(repo, "feat: x", session="s1")
    prs = [{"number": 7, "branch": "deleted-after-merge", "base": "master"},
           {"number": 8}]
    assert rail_rows(repo, "s1", prs) == [(mine, "merged", None)]


def test_rail_shows_the_newest_and_counts_the_rest(repo, fresh_rail, monkeypatch):
    monkeypatch.setattr(session_commits, "RAIL_LIMIT", 2)
    for n in range(4):
        commit(repo, f"feat: {n}", session="s1")
    found = session_commits.rail(str(repo), "s1")
    assert [c["subject"] for c in found["commits"]] == ["feat: 3", "feat: 2"]
    assert found["more"] == 2


def test_rail_gives_the_selected_card_every_commit_with_its_state(repo, fresh_rail, monkeypatch):
    """The selected card unfolds its +N (claunch-7myrv): every commit comes
    back, each with a state, and nothing is left to count."""
    monkeypatch.setattr(session_commits, "RAIL_LIMIT", 2)
    oldest = commit(repo, "feat: 0", session="s1")
    for n in range(1, 3):
        commit(repo, f"feat: {n}", session="s1")
    git("checkout", "-qb", "s1-feature", cwd=repo)
    newest = commit(repo, "feat: 3", session="s1")
    assert session_commits.rail(str(repo), "s1")["more"] == 2
    found = session_commits.rail(str(repo), "s1", everything=True)
    assert [c["subject"] for c in found["commits"]] == ["feat: 3", "feat: 2", "feat: 1", "feat: 0"]
    assert found["more"] == 0
    assert found["commits"][0]["short"] == newest and found["commits"][0]["state"] == "local"
    assert found["commits"][-1]["short"] == oldest and found["commits"][-1]["state"] == "merged"
    # The list poll after it still carries the newest few.
    assert len(session_commits.rail(str(repo), "s1")["commits"]) == 2


def test_the_selected_card_reads_every_commit_from_its_own_route(repo, fresh_rail, monkeypatch):
    from claude_launcher.daemon import api, session_prs, work_checkout

    monkeypatch.setattr(session_commits, "RAIL_LIMIT", 1)
    # A session that moved into a worktree: the commits come off that
    # worktree, the PRs off the directory it was started in -- each where
    # the list poll reads it, so selecting the card changes no PR frame.
    started = repo.parent / "started-here"
    started.mkdir()
    monkeypatch.setattr(work_checkout, "of", lambda sdef: str(repo))
    pr_reads = []
    monkeypatch.setattr(session_prs, "for_session",
                        lambda cwd, name: pr_reads.append(cwd) or [])
    for n in range(3):
        commit(repo, f"feat: {n}", session="s1")
    session = type("S", (), {"sdef": type("D", (), {"name": "s1", "cwd": str(started)})()})()
    manager = type("M", (), {"get": lambda self, name: session})()
    request = type("R", (), {"app": {"manager": manager}, "match_info": {"name": "s1"}})()
    body = json.loads(asyncio.run(api.h_session_rail_commits(request)).text)
    assert body["session"] == "s1"
    assert [c["subject"] for c in body["rail_commits"]["commits"]] == ["feat: 2", "feat: 1", "feat: 0"]
    assert body["rail_commits"]["more"] == 0
    assert pr_reads == [api._session_cwd(session)]
    src = (Path(__file__).resolve().parent.parent / "src/claude_launcher/daemon/api.py").read_text(encoding="utf-8")
    assert 'r.add_get("/api/sessions/{name}/rail-commits", h_session_rail_commits)' in src


def test_rail_is_none_when_nothing_was_found(repo, tmp_path, fresh_rail):
    """None, never an empty list: an empty reading is not evidence of no
    commits, and the card draws no line for it."""
    commit(repo, "feat: someone else", session="s191")
    assert session_commits.rail(str(repo), "s19") is None
    assert session_commits.rail(str(tmp_path / "gone"), "s1") is None
    assert session_commits.rail(None, "s1") is None
    assert session_commits.rail(str(repo), "") is None


def test_rail_reuses_one_walk_until_the_refs_move(repo, fresh_rail, monkeypatch):
    """The list poll calls this for every row; one walk per repository per
    refs state is the whole budget."""
    commit(repo, "feat: one", session="s1")
    commit(repo, "feat: theirs", session="s2")
    walks = []
    real = session_commits._git

    def counting(cwd, *args, **kw):
        if args and args[0] == "log":
            walks.append(args)
        return real(cwd, *args, **kw)

    monkeypatch.setattr(session_commits, "_git", counting)
    session_commits.rail(str(repo), "s1")
    session_commits.rail(str(repo), "s2")
    session_commits.rail(str(repo), "s1")
    assert len(walks) == 1
    newer = commit(repo, "feat: two", session="s1")
    assert session_commits.rail(str(repo), "s1")["commits"][0]["short"] == newer
    assert len(walks) == 2


# --------------------------------------------------------------------------- #
# an exited session's chips, kept (claunch-ork3b)
# --------------------------------------------------------------------------- #
@pytest.fixture
def fresh_kept(fresh_rail, monkeypatch):
    monkeypatch.setattr(session_commits, "_kept_refs", {})


def kept(record, cwds, *, exited_at="e1", created_at="c1", prs=None, read=True):
    return session_commits.kept_rail(
        record, "s1", created_at=created_at, exited_at=exited_at,
        cwds=[str(c) for c in cwds], prs=prs, read=read)


def kept_rows(record):
    view = session_commits.kept_view(record)
    return view and [(c["short"], c["state"], c.get("pr")) for c in view["commits"]]


def count_git(monkeypatch):
    calls = []
    real = session_commits._git

    def counting(cwd, *args, **kw):
        calls.append(args[0])
        return real(cwd, *args, **kw)

    monkeypatch.setattr(session_commits, "_git", counting)
    return calls


def test_kept_rail_reads_once_and_an_all_merged_record_runs_no_git(repo, fresh_kept, monkeypatch):
    a = commit(repo, "feat: a", session="s1")
    record, changed = kept(None, [repo])
    assert changed and kept_rows(record) == [(a, "merged", None)]
    calls = count_git(monkeypatch)
    again, changed = kept(record, [repo])
    assert (again, changed) == (record, False)
    assert calls == []


def test_kept_rail_does_not_look_again_while_the_refs_hold(repo, fresh_kept, monkeypatch):
    commit(repo, "chore: base")
    git("checkout", "-qb", "s1-feature", cwd=repo)
    b = commit(repo, "feat: b", session="s1")
    record, _ = kept(None, [repo])
    assert kept_rows(record) == [(b, "local", None)]
    monkeypatch.setattr(session_commits, "_kept_refs", {})
    calls = count_git(monkeypatch)
    assert kept(record, [repo]) == (record, False)
    # One for-each-ref to see the refs held; no walk, no re-mark.
    assert calls == ["for-each-ref"]
    calls.clear()
    assert kept(record, [repo]) == (record, False)
    assert calls == []  # the refs themselves are shared for RAIL_TTL


def test_kept_rail_marks_a_commit_merged_after_its_worktree_is_gone(
        repo, tmp_path, fresh_kept, monkeypatch):
    commit(repo, "chore: base")
    wt = tmp_path / "wt"
    git("worktree", "add", "-q", "-b", "s1-feature", str(wt), cwd=repo)
    b = commit(wt, "feat: b", session="s1")
    pr = {"number": 7, "branch": "s1-feature", "base": "master"}
    record, _ = kept(None, [wt], prs=[pr])
    assert kept_rows(record) == [(b, "local", 7)]
    git("worktree", "remove", "--force", str(wt), cwd=repo)
    git("merge", "-q", "--no-ff", "-m", "Merge pull request #7", "s1-feature", cwd=repo)
    monkeypatch.setattr(session_commits, "RAIL_TTL", 0)
    settled, changed = kept(record, [wt], prs=[pr])
    assert changed and kept_rows(settled) == [(b, "merged", 7)]
    # Merged is final: nothing is asked of git again, even as refs move.
    commit(repo, "chore: later")
    calls = count_git(monkeypatch)
    assert kept(settled, [wt], prs=[pr]) == (settled, False)
    assert calls == []


def test_kept_rail_finds_a_pr_recorded_after_the_exit(repo, fresh_kept):
    commit(repo, "chore: base")
    git("checkout", "-qb", "s1-feature", cwd=repo)
    b = commit(repo, "feat: b", session="s1")
    record, _ = kept(None, [repo])
    pr = {"number": 9, "branch": "s1-feature", "base": "master"}
    settled, changed = kept(record, [repo], prs=[pr])
    assert changed and kept_rows(settled) == [(b, "local", 9)]


def test_kept_rail_only_adds_when_the_session_ran_again(repo, fresh_kept):
    a = commit(repo, "feat: a", session="s1")
    record, _ = kept(None, [repo])
    gone = {"sha": "f" * 40, "short": "fffffff", "subject": "rewritten away",
            "state": "merged"}
    record = dict(record, rows=record["rows"] + [gone])
    b = commit(repo, "feat: b", session="s1")
    again, changed = kept(record, [repo], exited_at="e2")
    assert changed and again["exited_at"] == "e2"
    assert kept_rows(again) == [(b, "merged", None), (a, "merged", None),
                                ("fffffff", "merged", None)]


def test_kept_rail_reads_a_reused_name_again(repo, fresh_kept):
    commit(repo, "feat: a", session="s1")
    record, _ = kept(None, [repo], created_at="c1")
    assert kept(record, [repo], created_at="c2", read=False) == (None, False)


def test_kept_rail_keeps_nothing_it_could_not_read(repo, tmp_path, fresh_kept):
    """An unreadable repository is not "no commits" (claunch-j5kp); a
    readable one with none of this session's is, and is kept as such."""
    assert kept(None, [tmp_path / "gone"]) == (None, False)
    assert kept(None, [None]) == (None, False)
    commit(repo, "feat: theirs", session="s191")
    record, changed = kept(None, [tmp_path / "gone", repo])
    assert changed and record["rows"] == [] and kept_rows(record) is None
    assert kept(record, [repo]) == (record, False)


def test_kept_rail_reads_nothing_when_not_allowed(repo, fresh_kept, monkeypatch):
    commit(repo, "feat: a", session="s1")
    calls = count_git(monkeypatch)
    assert kept(None, [repo], read=False) == (None, False)
    assert calls == []


def test_kept_rail_shows_the_newest_and_counts_the_rest(repo, fresh_kept, monkeypatch):
    monkeypatch.setattr(session_commits, "RAIL_LIMIT", 2)
    for n in range(4):
        commit(repo, f"feat: {n}", session="s1")
    record, _ = kept(None, [repo])
    view = session_commits.kept_view(record)
    assert [c["subject"] for c in view["commits"]] == ["feat: 3", "feat: 2"]
    assert view["more"] == 2 and len(record["rows"]) == 4


def test_the_session_list_carries_rail_commits_for_the_card():
    """The rail view filters fields by name; a field left out of that list
    never reaches the card (the same trap the comment there records)."""
    src = (Path(__file__).resolve().parent.parent / "src/claude_launcher/daemon/api.py").read_text(encoding="utf-8")
    assert '"kind", "progress", "prs", "rail_commits",' in src
    assert 'info["rail_commits"] = chips' in src


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
