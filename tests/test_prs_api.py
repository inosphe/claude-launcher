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
    resp = asyncio.run(api.h_pr_merge(_Req({"method": "auto", "confirm": True})))
    body = _body_of(resp)
    assert resp.status == 200 and body["merged"] is True
    ledger = pr_actions.merged_rows()
    assert ledger and ledger[-1]["number"] == 805
    assert ledger[-1]["actor"]["kind"] == "user"
    assert ledger[-1]["actor"]["via"] == "web"


@pytest.mark.parametrize("kind,status", [
    ("conflict", 409), ("protected", 409), ("permission", 403),
    ("network", 502), ("not-found", 404),
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
