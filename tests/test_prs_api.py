"""``GET /api/prs``: the PR page's single read, and ``_recorded_prs`` behind it.

The page (``web/static/pr-page.js``) groups by project and lays the rows on a
date timeline, so the two facts it depends on are pinned here: every row says
which project and session it came from, and the list arrives newest first.
Nothing in this read calls ``gh`` -- it is what the daemon already has, which
is why the page works with ``gh`` missing or logged out (claunch-ijkge).
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from claude_launcher.cflow2 import frontend
from claude_launcher.cflow2.store import RunStore
from claude_launcher.daemon import api, pr_actions


def _session(name, project, cwd):
    return SimpleNamespace(sdef=SimpleNamespace(name=name, project=project, cwd=cwd))


def _run(cwd, name, items):
    st = RunStore(str(cwd), name)
    st.write_chart(frontend.compile_text("""cflow: 2
name: test-prs
nodes:
  wait: {prompt: wait}
body: wait
"""))
    st.save_state({"data": {}, "collections": {"prs": {"items": items}}})


@pytest.fixture
def fresh_cache(monkeypatch):
    """``_recorded_prs`` memoises for 30 s; a test must not read another's rows."""
    monkeypatch.setattr(api, "_prs_cache", {"at": 0.0, "rows": None, "projects": None})


def _manager(sessions):
    return SimpleNamespace(list=lambda: sessions)


def test_recorded_prs_merges_sessions_with_project_and_session(home, tmp_path, fresh_cache):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    _run(a, "s1", [{"url": "https://ghe.example.com/o/r/pull/1", "number": 1,
                    "at": "2026-11-01T10:00:00+00:00"}])
    _run(b, "s2", [{"url": "https://ghe.example.com/o/r/pull/2", "number": 2,
                    "at": "2026-11-02T10:00:00+00:00"}])
    rows, projects = api._recorded_prs(_manager([
        _session("s1", "claunch", str(a)),
        _session("s2", "", str(b)),
    ]))
    assert [r["number"] for r in rows] == [2, 1]
    assert rows[0]["session"] == "s2"
    # a session with no project is visible as "default" rather than dropped
    assert rows[0]["project"] == "default"
    assert rows[1]["project"] == "claunch"
    assert projects == ["claunch", "default"]


def test_recorded_prs_collapses_the_same_url_to_one_row(home, tmp_path, fresh_cache):
    """The same PR can be recorded twice -- the wizard's file and the run's
    collection -- and the page must show it once, attributed to the session
    that recorded it."""
    a = tmp_path / "a"
    a.mkdir()
    _run(a, "s1", [{"url": "https://ghe.example.com/o/r/pull/7", "number": 7,
                    "title": "from the collection"}])
    rows, _ = api._recorded_prs(_manager([_session("s1", "p", str(a))]))
    assert len(rows) == 1
    assert rows[0]["title"] == "from the collection"
    assert rows[0]["session"] == "s1"


def test_recorded_prs_skips_sessions_without_a_directory(home, tmp_path, fresh_cache):
    rows, projects = api._recorded_prs(_manager([_session("s1", "p", "")]))
    assert rows == [] and projects == []


def test_the_cache_answers_the_second_read_without_rescanning(home, tmp_path, fresh_cache,
                                                             monkeypatch):
    a = tmp_path / "a"
    a.mkdir()
    _run(a, "s1", [{"url": "https://ghe.example.com/o/r/pull/1", "number": 1}])
    calls = []
    real = api.session_prs.for_session

    def counted(cwd, name):
        calls.append(name)
        return real(cwd, name)

    monkeypatch.setattr(api.session_prs, "for_session", counted)
    manager = _manager([_session("s1", "p", str(a))])
    api._recorded_prs(manager)
    api._recorded_prs(manager)
    assert calls == ["s1"]


def test_h_prs_filters_by_project_and_reports_the_unfiltered_count(home, tmp_path,
                                                                   fresh_cache):
    a = tmp_path / "a"
    a.mkdir()
    _run(a, "s1", [{"url": "https://ghe.example.com/o/r/pull/1", "number": 1,
                    "at": "2026-11-01T10:00:00+00:00"}])
    _run(a, "s2", [{"url": "https://ghe.example.com/o/r/pull/2", "number": 2,
                    "at": "2026-11-02T10:00:00+00:00"}])
    app = {"manager": _manager([_session("s1", "one", str(a)),
                                _session("s2", "two", str(a))])}

    def call(query):
        request = SimpleNamespace(app=app, query=query)
        return json.loads(asyncio.run(api.h_prs(request)).body.decode())

    data = call({})
    assert data["count"] == 2
    assert data["projects"] == ["one", "two"]
    one = call({"project": "one"})
    assert one["count"] == 1 and one["prs"][0]["number"] == 1
    # the project list stays the full list; only the rows are narrowed
    assert one["projects"] == ["one", "two"]


# --------------------------------------------------------------------------- #
# one PR: the daemon's read, the verdict, and the merge (claunch-ijkge.3)
# --------------------------------------------------------------------------- #
REPO = ("github.ncsoft.com", "RnIAgentDev", "claunch")


class _Req(dict):
    """A request double: ``.get`` carries the auth kind, plus match_info/app."""

    def __init__(self, body=None, *, auth="cookie", match=None, manager=None,
                 remote="127.0.0.1"):
        super().__init__()
        if auth is not None:
            self[api.actors.AUTH_VIA_KEY] = auth
        self.match_info = match if match is not None else {
            "host": REPO[0], "owner": REPO[1], "name": REPO[2], "number": "805"}
        self.app = {"manager": manager or _manager([])}
        self.remote = remote
        self.headers = {}
        self._body = body

    async def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


def _runner(*answers):
    """A ``run`` stub handing back ``(code, out, err)`` answers in order."""
    queue = list(answers)
    calls = []

    def run(argv, cwd=None, timeout=None):
        calls.append({"argv": list(argv), "cwd": cwd})
        return queue.pop(0) if queue else (0, "", "")

    run.calls = calls
    return run


def _view(**over):
    row = {
        "number": 805, "title": "t", "state": "OPEN", "draft": False,
        "url": "https://h/o/r/pull/805", "base": "master", "head": "b",
        "mergeable": "MERGEABLE", "merge_state": "CLEAN", "review": "",
        "checks": {"total": 0, "passing": 0, "failing": 0, "pending": 0,
                   "state": "none"},
        "author": "u", "updated_at": "", "merge_commit": "",
    }
    row.update(over)
    return row


def _body_of(response):
    return json.loads(response.body.decode())


# -- the verdict: the one input the merge button is drawn from -------------
def test_a_clean_pr_is_mergeable():
    assert pr_actions.verdict(_view())["can_merge"] is True
    assert pr_actions.verdict(_view())["kind"] == pr_actions.CLEAN


def test_a_conflicting_pr_is_refused_by_both_signals():
    by_mergeable = pr_actions.verdict(_view(mergeable="CONFLICTING"))
    by_state = pr_actions.verdict(_view(merge_state="DIRTY"))
    assert by_mergeable["kind"] == "conflict" and by_mergeable["can_merge"] is False
    assert by_state["kind"] == "conflict" and by_state["can_merge"] is False


def test_a_behind_pr_asks_for_a_rebase_rather_than_merging():
    verdict = pr_actions.verdict(_view(merge_state="BEHIND"))
    assert verdict["can_merge"] is False and verdict["kind"] == "behind"
    assert "master" in verdict["reason"] or "기준선" in verdict["reason"]


def test_running_and_failed_checks_block_the_merge():
    pending = _view(checks={"total": 2, "passing": 1, "failing": 0, "pending": 1,
                            "state": "pending"})
    failing = _view(checks={"total": 2, "passing": 1, "failing": 1, "pending": 0,
                            "state": "failing"})
    assert pr_actions.verdict(pending)["kind"] == "checks-pending"
    assert pr_actions.verdict(failing)["kind"] == "checks-failing"


def test_a_blocked_pr_says_which_kind_of_block_it_is():
    required = pr_actions.verdict(_view(merge_state="BLOCKED", review="REVIEW_REQUIRED"))
    changes = pr_actions.verdict(_view(merge_state="BLOCKED", review="CHANGES_REQUESTED"))
    other = pr_actions.verdict(_view(merge_state="BLOCKED"))
    assert required["kind"] == "review-required"
    assert changes["kind"] == "changes-requested"
    assert other["kind"] == "blocked"
    assert all(v["can_merge"] is False for v in (required, changes, other))


def test_a_pr_that_is_already_over_or_a_draft_is_not_mergeable():
    assert pr_actions.verdict(_view(state="MERGED"))["kind"] == "merged"
    assert pr_actions.verdict(_view(state="CLOSED"))["kind"] == "closed"
    assert pr_actions.verdict(_view(draft=True))["kind"] == "draft"
    assert pr_actions.verdict(None)["can_merge"] is False


def test_an_uncomputed_merge_state_is_not_read_as_clean():
    verdict = pr_actions.verdict(_view(mergeable="UNKNOWN", merge_state="UNKNOWN"))
    assert verdict["can_merge"] is False and verdict["kind"] == "unknown"


# -- the check tally --------------------------------------------------------
def test_checks_are_counted_from_both_rollup_shapes():
    tally = pr_actions.summarize_checks([
        {"status": "COMPLETED", "conclusion": "SUCCESS"},
        {"status": "IN_PROGRESS", "conclusion": ""},
        {"state": "FAILURE"},
        {"state": "SUCCESS"},
        "not-a-dict",
    ])
    assert tally == {"total": 4, "passing": 2, "failing": 1, "pending": 1,
                     "state": "failing"}
    assert pr_actions.summarize_checks(None)["state"] == "none"
    assert pr_actions.summarize_checks([])["total"] == 0


# -- gh's own words, classified --------------------------------------------
@pytest.mark.parametrize("text,kind", [
    ("X Pull request #1 is not mergeable: the merge commit cannot be created.",
     "conflict"),
    ("GraphQL: Protected branch update failed for refs/heads/master", "protected"),
    ("required status check \"ci\" is expected", "protected"),
    ("HTTP 403: Resource not accessible by integration", "permission"),
    ("API rate limit exceeded", "rate-limit"),
    ("dial tcp: lookup github.ncsoft.com: no such host", "network"),
    ("HTTP 404: Not Found", "not-found"),
    ("something else entirely", "other"),
    ("", "other"),
])
def test_a_failure_is_classified_for_the_page(text, kind):
    assert pr_actions.classify(text) == kind


# -- the read ---------------------------------------------------------------
def test_detail_normalizes_the_gh_answer(monkeypatch):
    monkeypatch.setattr(pr_actions, "_gh_path", lambda: "/usr/bin/gh")
    run = _runner((0, json.dumps({
        "number": 805, "title": "a PR", "state": "OPEN", "isDraft": False,
        "url": "https://h/o/r/pull/805", "baseRefName": "master",
        "headRefName": "feature", "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN", "reviewDecision": "",
        "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
        "author": {"login": "someone"}, "updatedAt": "2026-11-03T00:00:00Z",
        "mergeCommit": None,
    }), ""))
    view = pr_actions.detail(*REPO, 805, run=run)
    assert view["ok"] is True
    assert view["pr"]["head"] == "feature" and view["pr"]["base"] == "master"
    assert view["pr"]["author"] == "someone"
    assert view["pr"]["checks"]["passing"] == 1
    assert view["verdict"]["can_merge"] is True
    assert view["error"] is None
    # the repository is passed as gh's -R slug, host included
    assert "github.ncsoft.com/RnIAgentDev/claunch" in run.calls[0]["argv"]


def test_a_failed_read_is_an_answer_with_a_reason_not_a_crash(monkeypatch):
    monkeypatch.setattr(pr_actions, "_gh_path", lambda: "/usr/bin/gh")
    run = _runner((1, "", "dial tcp: lookup github.ncsoft.com: no such host"))
    view = pr_actions.detail(*REPO, 805, run=run)
    assert view["ok"] is False and view["pr"] is None
    assert view["verdict"]["can_merge"] is False
    assert view["error"]["kind"] == "network"
    assert "no such host" in view["error"]["raw"]


def test_no_gh_is_said_rather_than_guessed(monkeypatch):
    monkeypatch.setattr(pr_actions, "_gh_path", lambda: None)
    view = pr_actions.detail(*REPO, 805, run=_runner())
    assert view["ok"] is False and view["error"]["kind"] == "no-gh"


def test_an_unreadable_answer_is_bad_output(monkeypatch):
    monkeypatch.setattr(pr_actions, "_gh_path", lambda: "/usr/bin/gh")
    view = pr_actions.detail(*REPO, 805, run=_runner((0, "not json", "")))
    assert view["ok"] is False and view["error"]["kind"] == "bad-output"


# -- the merge --------------------------------------------------------------
def test_auto_resolves_to_the_method_the_repository_allows(monkeypatch):
    monkeypatch.setattr(pr_actions, "_gh_path", lambda: "/usr/bin/gh")
    run = _runner(
        (0, json.dumps({"mergeCommitAllowed": False, "squashMergeAllowed": True,
                        "rebaseMergeAllowed": True}), ""),
        (0, "Merged pull request #805", ""),
    )
    result = pr_actions.merge(*REPO, 805, "auto", run=run)
    assert result["ok"] is True and result["method"] == "squash"
    assert result["merged"] is True
    assert run.calls[1]["argv"][-1] == "--squash"


def test_auto_with_no_readable_settings_refuses_instead_of_guessing(monkeypatch):
    monkeypatch.setattr(pr_actions, "_gh_path", lambda: "/usr/bin/gh")
    run = _runner((1, "", "boom"))
    result = pr_actions.merge(*REPO, 805, "auto", run=run)
    assert result["ok"] is False and result["merged"] is False
    assert len(run.calls) == 1, "no merge is attempted without a method"


def test_a_refused_merge_is_classified():
    result = pr_actions.merge(*REPO, 805, "merge", run=_runner(
        (1, "", "X Pull request #805 is not mergeable: the merge commit cannot be created.")))
    assert result["ok"] is False and result["merged"] is False
    assert result["error"]["kind"] == "conflict"
    assert "not mergeable" in result["error"]["raw"]


def test_an_unknown_method_is_refused_before_gh_runs():
    run = _runner()
    result = pr_actions.merge(*REPO, 805, "octopus", run=run)
    assert result["ok"] is False
    assert run.calls == []


def test_the_merge_ledger_keeps_who_merged_what(home):
    pr_actions.record_merge(*REPO, 805, "squash", {"kind": "user", "via": "web"})
    rows = pr_actions.merged_rows()
    assert len(rows) == 1
    assert rows[0]["number"] == 805 and rows[0]["method"] == "squash"
    assert rows[0]["repo"] == "github.ncsoft.com/RnIAgentDev/claunch"
    assert rows[0]["actor"]["via"] == "web"


# -- the merge is pinned to the head the page showed (claunch-ijkge.3.1) -----
def test_the_merge_is_pinned_to_the_head_the_page_showed():
    pin = "a" * 40
    run = _runner((0, json.dumps({"headRefOid": pin}), ""), (0, "Merged", ""))
    result = pr_actions.merge(*REPO, 805, "merge", match_head_commit=pin, run=run)
    assert result["ok"] is True
    assert run.calls[1]["argv"][-2:] == ["--match-head-commit", pin]


def test_a_head_that_moved_after_the_verdict_is_refused_before_gh_merges():
    run = _runner((0, json.dumps({"headRefOid": "b" * 40}), ""))
    result = pr_actions.merge(*REPO, 805, "merge", match_head_commit="a" * 40, run=run)
    assert result["ok"] is False and result["merged"] is False
    assert result["error"]["kind"] == "head-moved"
    assert len(run.calls) == 1, "the merge itself must not run on a moved head"
    assert "a" * 12 in result["error"]["message"] and "b" * 12 in result["error"]["message"]


def test_a_pin_that_cannot_be_verified_refuses_rather_than_merging_unpinned():
    run = _runner((1, "", "boom"))
    result = pr_actions.merge(*REPO, 805, "merge", match_head_commit="a" * 40, run=run)
    assert result["ok"] is False and result["merged"] is False
    assert "고정" in result["error"]["message"]
    assert len(run.calls) == 1


def test_a_malformed_pin_is_refused_before_gh_runs():
    run = _runner()
    result = pr_actions.merge(*REPO, 805, "merge", match_head_commit="abc", run=run)
    assert result["ok"] is False
    assert run.calls == []


def test_the_detail_carries_the_head_sha_the_page_pins_to():
    assert pr_actions._normalize({"headRefOid": "c" * 40,
                                  "number": 1})["head_sha"] == "c" * 40


# -- the post-merge sweep is queued, or says why it was not (claunch-ijkge.3.2)
def _merged_commit(sha):
    return (0, json.dumps({"mergeCommit": {"oid": sha}}), "")


def test_a_merge_with_no_local_checkout_says_why_it_was_not_swept(monkeypatch):
    monkeypatch.setattr(pr_actions, "find_checkout", lambda *a, **k: None)
    run = _runner(_merged_commit("d" * 40))
    answer = pr_actions.sweep_after_merge(*REPO, 805, issues=["claunch-x"], run=run)
    assert answer["state"] == "skipped" and answer["job"] == ""
    assert "로컬 체크아웃" in answer["why"]


def test_the_sweep_is_queued_under_the_recorded_issue(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    (root / "tools").mkdir(parents=True)
    (root / "tools" / "delayed_sweep.py").write_text("")
    monkeypatch.setattr(pr_actions, "find_checkout", lambda *a, **k: str(root))
    calls = []

    class _Queue:
        def enqueue(self, repo, sha, issue, owner, tool):
            calls.append((repo, sha, issue, owner, tool))
            return {"id": "job1", "ref": f"refs/heads/delayed-sweep/{owner}/{sha}"}

    from claude_launcher import delayed_sweep as queue_module
    monkeypatch.setattr(queue_module, "Queue", lambda *a, **k: _Queue())
    answer = pr_actions.sweep_after_merge(
        *REPO, 805, issues=["claunch-x"], owner_label="s1", run=_runner(_merged_commit("d" * 40)))
    assert answer == {"state": "queued", "job": "job1",
                      "ref": f"refs/heads/delayed-sweep/s1/{'d' * 40}",
                      "issue": "claunch-x", "why": ""}
    assert calls == [(str(root), "d" * 40, "claunch-x", "s1",
                      str(root / "tools" / "delayed_sweep.py"))]


def test_a_queued_sweep_leaves_a_real_job_in_the_queue(monkeypatch, home, tmp_path):
    """The end of the chain the other tests stop short of: a real ``Queue``
    and a real merge commit, so "queued" means a row the daemon will run."""
    import subprocess

    from claude_launcher import delayed_sweep as queue_module

    root = tmp_path / "repo"
    root.mkdir()

    def git(*args):
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                              text=True, check=True).stdout.strip()

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (root / "base").write_text("b")
    git("add", ".")
    git("commit", "-qm", "base")
    git("branch", "-M", "master")
    git("switch", "-qc", "side")
    (root / "side").write_text("s")
    git("add", ".")
    git("commit", "-qm", "side")
    git("switch", "-q", "master")
    git("merge", "--no-ff", "-qm", "merge side", "side")
    merge_sha = git("rev-parse", "HEAD")
    (root / "tools").mkdir()
    (root / "tools" / "delayed_sweep.py").write_text("")

    monkeypatch.setattr(pr_actions, "find_checkout", lambda *a, **k: str(root))
    monkeypatch.setattr(queue_module, "default_database", lambda: tmp_path / "jobs.sqlite3")
    answer = pr_actions.sweep_after_merge(
        *REPO, 805, issues=["claunch-x"], owner_label="s1",
        run=_runner(_merged_commit(merge_sha)))
    assert answer["state"] == "queued" and answer["job"]
    jobs = queue_module.Queue(tmp_path / "jobs.sqlite3").status(str(root))["jobs"]
    assert [job["sha"] for job in jobs] == [merge_sha]
    assert jobs[0]["issue"] == "claunch-x" and jobs[0]["owner"] == "s1"


def test_an_unreadable_merge_commit_is_a_skip_with_its_reason(monkeypatch):
    monkeypatch.setattr(pr_actions, "find_checkout",
                        lambda *a, **k: pytest.fail("no checkout is read without a SHA"))
    answer = pr_actions.sweep_after_merge(*REPO, 805, run=_runner((1, "", "boom")))
    assert answer["state"] == "skipped" and "SHA" in answer["why"]


def test_a_merge_the_queue_refuses_is_a_skip_with_that_reason(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    (root / "tools").mkdir(parents=True)
    (root / "tools" / "delayed_sweep.py").write_text("")
    monkeypatch.setattr(pr_actions, "find_checkout", lambda *a, **k: str(root))

    class _Queue:
        def enqueue(self, *a, **k):
            raise ValueError("verification checkpoint must be a two-parent merge")

    from claude_launcher import delayed_sweep as queue_module
    monkeypatch.setattr(queue_module, "Queue", lambda *a, **k: _Queue())
    answer = pr_actions.sweep_after_merge(*REPO, 805, run=_runner(_merged_commit("d" * 40)))
    assert answer["state"] == "skipped"
    assert "two-parent merge" in answer["why"], "a squash merge cannot be swept, and the ledger says so"


# -- the conflicts ----------------------------------------------------------
def test_conflict_files_without_a_checkout_are_unknown_not_empty(monkeypatch):
    monkeypatch.setattr(pr_actions, "find_checkout", lambda *a, **k: None)
    answer = pr_actions.conflict_files(*REPO, "master", "feature", run=_runner())
    assert answer["computed"] is False
    assert answer["files"] == []
    assert "로컬 체크아웃" in answer["why"], "the reason names what is missing"


def test_merge_tree_paths_stops_before_the_informational_section():
    """The shape git actually prints (measured on git 2.4x): tree object,
    one path per line, a blank line, then the merge's own messages."""
    out = ("0f1e2d\n\nsrc/a.py\nsrc/b.py\n\nAuto-merging src/a.py\n"
           "CONFLICT (content): Merge conflict in src/a.py\n")
    # a real answer puts the paths first, then the blank line and messages
    real = ("0f1e2d\nsrc/a.py\nsrc/b.py\n\nAuto-merging src/a.py\n"
            "CONFLICT (content): Merge conflict in src/a.py\n")
    assert pr_actions._merge_tree_paths(real) == ["src/a.py", "src/b.py"]
    assert pr_actions._merge_tree_paths("0f1e2d\n") == [], "a clean merge lists nothing"
    assert pr_actions._merge_tree_paths("") == []
    # the first line is the tree object even when it looks like the only path
    assert pr_actions._merge_tree_paths(out) == []


def test_conflict_files_lists_what_merge_tree_reports(monkeypatch):
    run = _runner(
        (0, "", ""),                                   # git fetch
        (1, "0f1e2d\nsrc/a.py\nsrc/b.py\n\nAuto-merging src/a.py\n", ""),   # merge-tree
        (0, "", ""),                                   # update-ref -d
    )
    answer = pr_actions.conflict_files(*REPO, "master", "feature", root="/tmp/x", run=run)
    assert answer["computed"] is True
    assert answer["files"] == ["src/a.py", "src/b.py"]
    assert answer["why"] == ""
    assert run.calls[2]["argv"][-2:] == ["-d", run.calls[2]["argv"][-1]]
    assert run.calls[2]["argv"][-1].startswith("refs/claunch/")


def test_a_failed_fetch_is_reported_with_its_reason(monkeypatch):
    answer = pr_actions.conflict_files(
        *REPO, "master", "feature", root="/tmp/x",
        run=_runner((128, "", "could not read from remote repository")))
    assert answer["computed"] is False
    assert "remote repository" in answer["why"]


# -- the routes -------------------------------------------------------------
def test_the_detail_route_rejects_a_malformed_address():
    for match in ({"host": "h", "owner": "o", "name": "n", "number": "x"},
                  {"host": "h", "owner": "o", "name": "n", "number": "0"},
                  {"host": "h", "owner": "bad/name", "name": "n", "number": "5"}):
        resp = asyncio.run(api.h_pr_detail(_Req(match=match)))
        assert resp.status == 400


def test_the_detail_route_carries_the_recorded_row_and_the_repo(monkeypatch,
                                                               home, tmp_path,
                                                               fresh_cache):
    monkeypatch.setattr(pr_actions, "detail", lambda *a, **k: {
        "ok": True, "pr": _view(), "verdict": pr_actions.verdict(_view()),
        "conflicts": None, "error": None})
    a = tmp_path / "a"
    a.mkdir()
    _run(a, "s1", [{"url": "https://github.ncsoft.com/RnIAgentDev/claunch/pull/805",
                    "number": 805, "at": "2026-11-03T00:00:00+00:00"}])
    resp = asyncio.run(api.h_pr_detail(_Req(manager=_manager([
        _session("s1", "claunch", str(a))]))))
    body = _body_of(resp)
    assert resp.status == 200
    assert body["repo"] == "github.ncsoft.com/RnIAgentDev/claunch"
    assert body["number"] == 805
    assert body["recorded"]["session"] == "s1"
    assert body["verdict"]["can_merge"] is True


def test_conflict_paths_are_computed_only_when_the_verdict_says_conflict(monkeypatch):
    calls = []

    def detail(*a, **k):
        return {"ok": True, "pr": _view(mergeable="CONFLICTING"),
                "verdict": {"can_merge": False, "kind": "conflict", "reason": "x"},
                "conflicts": None, "error": None}

    def conflicts(*a, **k):
        calls.append(a)
        return {"computed": True, "files": ["src/a.py"], "why": ""}

    monkeypatch.setattr(pr_actions, "detail", detail)
    monkeypatch.setattr(pr_actions, "conflict_files", conflicts)
    body = _body_of(asyncio.run(api.h_pr_detail(_Req())))
    assert calls and body["conflicts"]["files"] == ["src/a.py"]

    calls.clear()
    monkeypatch.setattr(pr_actions, "detail", lambda *a, **k: {
        "ok": True, "pr": _view(), "verdict": pr_actions.verdict(_view()),
        "conflicts": None, "error": None})
    asyncio.run(api.h_pr_detail(_Req()))
    assert calls == [], "a clean PR costs no fetch"


def test_the_merge_route_refuses_a_token_request(monkeypatch):
    ran = []
    monkeypatch.setattr(pr_actions, "merge", lambda *a, **k: ran.append(a) or {"ok": True})
    resp = asyncio.run(api.h_pr_merge(_Req({"method": "auto", "confirm": True},
                                           auth=api.actors.AUTH_BEARER)))
    assert resp.status == 403
    assert ran == [], "the merge must not run for a bearer token"
    assert "대시보드" in _body_of(resp)["error"]


def test_the_merge_route_needs_the_second_confirmation(monkeypatch):
    ran = []
    monkeypatch.setattr(pr_actions, "merge", lambda *a, **k: ran.append(a) or {"ok": True})
    for body in (None, {"method": "auto"}, {"method": "auto", "confirm": "yes"},
                 {"method": "auto", "confirm": 1}):
        resp = asyncio.run(api.h_pr_merge(_Req(body)))
        assert resp.status == 400
    assert ran == []


def test_the_merge_route_runs_gh_and_writes_the_ledger(monkeypatch, home):
    monkeypatch.setattr(pr_actions, "merge", lambda *a, **k: {
        "ok": True, "merged": True, "method": "squash", "output": "Merged", "error": None})
    monkeypatch.setattr(pr_actions, "sweep_after_merge", lambda *a, **k: {
        "state": "skipped", "job": "", "ref": "", "issue": "", "why": "test"})
    resp = asyncio.run(api.h_pr_merge(_Req({"method": "auto", "confirm": True})))
    body = _body_of(resp)
    assert resp.status == 200 and body["merged"] is True
    ledger = pr_actions.merged_rows()
    assert ledger and ledger[-1]["number"] == 805
    assert ledger[-1]["actor"]["kind"] == "user"
    assert ledger[-1]["actor"]["via"] == "web"
    assert ledger[-1]["sweep"]["why"] == "test", "the ledger keeps why a merge was not swept"


def test_the_merge_route_passes_the_pin_and_records_the_queued_sweep(monkeypatch, home):
    seen = {}

    def merge(*a, **k):
        seen["pin"] = k.get("match_head_commit")
        return {"ok": True, "merged": True, "method": "merge", "output": "Merged", "error": None}

    monkeypatch.setattr(pr_actions, "merge", merge)
    monkeypatch.setattr(pr_actions, "sweep_after_merge", lambda *a, **k: {
        "state": "queued", "job": "job1", "ref": "refs/x", "issue": "claunch-x", "why": ""})
    resp = asyncio.run(api.h_pr_merge(
        _Req({"method": "merge", "confirm": True, "match_head_commit": "A" * 40})))
    body = _body_of(resp)
    assert resp.status == 200
    assert seen["pin"] == "a" * 40, "the pin reaches gh in the lower-case form gh compares"
    assert body["sweep"]["state"] == "queued"
    assert pr_actions.merged_rows()[-1]["sweep"]["job"] == "job1"


def test_the_merge_route_refuses_a_malformed_pin(monkeypatch):
    ran = []
    monkeypatch.setattr(pr_actions, "merge", lambda *a, **k: ran.append(a) or {"ok": True})
    for pin in ("abc", "z" * 40, "a" * 39):
        resp = asyncio.run(api.h_pr_merge(
            _Req({"method": "auto", "confirm": True, "match_head_commit": pin})))
        assert resp.status == 400
    assert ran == [], "a malformed pin is refused before the merge runs"


@pytest.mark.parametrize("kind,status", [
    ("conflict", 409), ("protected", 409), ("permission", 403),
    ("network", 502), ("not-found", 404), ("head-moved", 409),
])
def test_a_refused_merge_maps_its_kind_to_a_status(monkeypatch, kind, status):
    monkeypatch.setattr(pr_actions, "merge", lambda *a, **k: {
        "ok": False, "merged": False, "method": "merge", "output": "",
        "error": {"kind": kind, "message": "nope", "raw": "raw text"}})
    resp = asyncio.run(api.h_pr_merge(_Req({"method": "merge", "confirm": True})))
    assert resp.status == status
    assert _body_of(resp)["error"]["raw"] == "raw text"


def test_the_rebase_request_says_when_it_was_not_delivered(monkeypatch, home, tmp_path,
                                                           fresh_cache):
    a = tmp_path / "a"
    a.mkdir()
    _run(a, "s1", [{"url": "https://github.ncsoft.com/RnIAgentDev/claunch/pull/805",
                    "number": 805}])
    manager = _manager([_session("s1", "claunch", str(a))])
    monkeypatch.setattr(api, "_mesh_mgr", lambda request: SimpleNamespace(
        list=lambda: []))
    resp = asyncio.run(api.h_pr_rebase_request(_Req({}, manager=manager)))
    body = _body_of(resp)
    assert resp.status == 200
    assert body["delivered"] is False and body["session"] == "s1"
    assert body["text"], "the text is returned so the user can still pass it on"
    assert "메시" in body["why"]


def test_the_rebase_request_refuses_a_token_request(home, tmp_path, fresh_cache):
    """``external`` means "the operator typed this" -- so it is cookie-only.

    Without the gate a session holding the daemon token could put words in
    the operator's mouth into any member's terminal (review, s6357-rev2)."""
    a = tmp_path / "a"
    a.mkdir()
    _run(a, "s1", [{"url": "https://github.ncsoft.com/RnIAgentDev/claunch/pull/805",
                    "number": 805}])
    resp = asyncio.run(api.h_pr_rebase_request(_Req(
        {}, auth="bearer", manager=_manager([_session("s1", "claunch", str(a))]))))
    assert resp.status == 403


class _Board:
    """A beads service double: one root, every comment it was asked to write."""

    def __init__(self, root=None, fail=()):
        self.root = root
        self.fail = set(fail)
        self.comments = []

    async def root_for(self, cwd):
        return self.root

    async def br(self, root, args):
        if args[2] in self.fail:
            raise RuntimeError("br said no")
        self.comments.append(list(args))
        return {}


def test_the_rebase_request_leaves_a_line_on_the_issue(monkeypatch, home, tmp_path,
                                                       fresh_cache):
    """A mesh message reaches only a live member; the board is what survives.

    The row names the issues the PR was opened for, so the ask is written
    there too -- and the line says the message did not go through, because a
    request that reached only the board must not read as one that reached
    the session."""
    a = tmp_path / "a"
    a.mkdir()
    _run(a, "s1", [{"url": "https://github.ncsoft.com/RnIAgentDev/claunch/pull/805",
                    "number": 805, "issues": ["claunch-x1"]}])
    board = _Board(root=a)
    monkeypatch.setattr(api, "_mesh_mgr", lambda request: SimpleNamespace(
        list=lambda: []))
    request = _Req({}, manager=_manager([_session("s1", "claunch", str(a))]))
    request.app["beads"] = board
    body = _body_of(asyncio.run(api.h_pr_rebase_request(request)))
    assert body["delivered"] is False
    assert body["noted"] == ["claunch-x1"]
    argv = board.comments[0]
    assert argv[:3] == ["comments", "add", "claunch-x1"]
    assert argv[3].startswith("REBASE REQUEST @ github.ncsoft.com/RnIAgentDev/claunch#805:")
    assert "메시 전달 실패" in argv[3]


def test_the_board_line_says_when_the_message_did_go_through(monkeypatch, home,
                                                            tmp_path, fresh_cache):
    a = tmp_path / "a"
    a.mkdir()
    _run(a, "s1", [{"url": "https://github.ncsoft.com/RnIAgentDev/claunch/pull/805",
                    "number": 805, "issues": ["claunch-x1"]}])
    board = _Board(root=a)

    class _Mesh:
        name = "claunch"
        members = {"s1": SimpleNamespace(session="s1")}

    async def send(*a, **k):
        return {"id": "m1"}

    monkeypatch.setattr(api, "_mesh_mgr", lambda request: SimpleNamespace(
        list=lambda: [_Mesh()], send=send))
    request = _Req({}, manager=_manager([_session("s1", "claunch", str(a))]))
    request.app["beads"] = board
    body = _body_of(asyncio.run(api.h_pr_rebase_request(request)))
    assert body["delivered"] is True and body["noted"] == ["claunch-x1"]
    assert "메시로도 전달됨" in board.comments[0][3]


def test_one_issue_that_refuses_does_not_sink_the_others(monkeypatch, home, tmp_path,
                                                         fresh_cache):
    a = tmp_path / "a"
    a.mkdir()
    _run(a, "s1", [{"url": "https://github.ncsoft.com/RnIAgentDev/claunch/pull/805",
                    "number": 805, "issues": ["claunch-x1", "claunch-x2"]}])
    board = _Board(root=a, fail={"claunch-x1"})
    monkeypatch.setattr(api, "_mesh_mgr", lambda request: SimpleNamespace(
        list=lambda: []))
    request = _Req({}, manager=_manager([_session("s1", "claunch", str(a))]))
    request.app["beads"] = board
    body = _body_of(asyncio.run(api.h_pr_rebase_request(request)))
    assert body["noted"] == ["claunch-x2"]
    assert "claunch-x1" in body["noted_why"]


def test_a_record_with_no_issues_says_why_nothing_was_written(monkeypatch, home,
                                                              tmp_path, fresh_cache):
    a = tmp_path / "a"
    a.mkdir()
    _run(a, "s1", [{"url": "https://github.ncsoft.com/RnIAgentDev/claunch/pull/805",
                    "number": 805}])
    board = _Board(root=a)
    monkeypatch.setattr(api, "_mesh_mgr", lambda request: SimpleNamespace(
        list=lambda: []))
    request = _Req({}, manager=_manager([_session("s1", "claunch", str(a))]))
    request.app["beads"] = board
    body = _body_of(asyncio.run(api.h_pr_rebase_request(request)))
    assert body["noted"] == [] and "이슈" in body["noted_why"]
    assert board.comments == []


def test_the_routes_read_the_record_off_the_event_loop(monkeypatch, home, tmp_path,
                                                       fresh_cache):
    """A cold ``_recorded_prs`` walks every session's run state. On the loop
    that is a stall per open card (review, s6357-rev2)."""
    import threading

    a = tmp_path / "a"
    a.mkdir()
    _run(a, "s1", [{"url": "https://github.ncsoft.com/RnIAgentDev/claunch/pull/805",
                    "number": 805}])
    manager = _manager([_session("s1", "claunch", str(a))])
    seen = []
    real = api._recorded_pr

    def spy(*args):
        seen.append(threading.current_thread())
        return real(*args)

    monkeypatch.setattr(api, "_recorded_pr", spy)
    monkeypatch.setattr(pr_actions, "detail", lambda *a, **k: {
        "ok": False, "pr": {}, "verdict": {}, "conflicts": None,
        "error": {"kind": "other", "message": "x", "raw": ""}})
    monkeypatch.setattr(api, "_mesh_mgr", lambda request: SimpleNamespace(list=lambda: []))
    main = threading.current_thread()
    asyncio.run(api.h_pr_detail(_Req(manager=manager)))
    asyncio.run(api.h_pr_rebase_request(_Req({}, manager=manager)))
    assert len(seen) == 2
    assert all(t is not main for t in seen), "both routes must await to_thread"


def test_the_rebase_request_reaches_the_session_over_its_mesh(monkeypatch, home,
                                                              tmp_path, fresh_cache):
    a = tmp_path / "a"
    a.mkdir()
    _run(a, "s1", [{"url": "https://github.ncsoft.com/RnIAgentDev/claunch/pull/805",
                    "number": 805}])
    sent = []

    class _Mesh:
        name = "claunch"
        members = {"s1": SimpleNamespace(session="s1")}

    async def send(name, sender, to, body, **kwargs):
        sent.append({"name": name, "sender": sender, "to": to, "body": body,
                     **kwargs})
        return {"id": "m1"}

    monkeypatch.setattr(api, "_mesh_mgr", lambda request: SimpleNamespace(
        list=lambda: [_Mesh()], send=send))
    resp = asyncio.run(api.h_pr_rebase_request(_Req(
        {"note": "확인 부탁"}, manager=_manager([_session("s1", "claunch", str(a))]))))
    body = _body_of(resp)
    assert body["delivered"] is True
    assert sent[0]["to"] == "s1" and sent[0]["external"] is True
    assert sent[0]["body"] == "확인 부탁"
