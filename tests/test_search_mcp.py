from __future__ import annotations

import json
import urllib.parse

import pytest

from claude_launcher import daemon_client, search_mcp


class _Client:
    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def get(self, path, **kw):
        self.calls.append(path)
        for prefix, answer in self.answers.items():
            if path.startswith(prefix):
                if isinstance(answer, Exception):
                    raise answer
                return json.loads(json.dumps(answer))
        raise AssertionError(f"unexpected GET {path}")


def _use(monkeypatch, answers):
    client = _Client(answers)
    monkeypatch.setattr(search_mcp, "_client", lambda: client)
    return client


def _params(path):
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(path).query))


UNIFIED = {
    "kind": "all",
    "root": None,
    "reranked": True,
    "warnings": [],
    "index": {"indexed": 90, "total": 100, "syncing": True},
    "results": [
        {
            "id": "comment:b:claunch-x1:7:0", "score": 0.6, "rerank_score": 0.98765,
            "lexical": False, "kind": "comment", "issue": "claunch-x1",
            "title": "claunch-x1 · grid view · comment",
            "excerpt": "claunch-x1 · grid view · comment\nEVIDENCE " + "x" * 2000,
            "root": "F:\\repo", "href": "#/beads/claunch-x1", "at": "2026-09-23T03:56:40Z",
            "sessions": [{"name": "s720", "via": ["assignee"], "status": "busy",
                          "paused": False, "archived": False}],
            "source_url": "api/beads/claunch-x1?cwd=F%3A%5Crepo",
        },
        {
            "id": "session:s9:0", "score": 0.5, "lexical": True, "kind": "session",
            "name": "s9", "title": "s9", "excerpt": "s9\nnote text",
            "sessions": [{"name": "s9", "status": "exited", "paused": False, "archived": True}],
            "href": "#/s/s9",
        },
    ],
}


def test_search_defaults_to_the_unified_corpus_and_compacts_rows(monkeypatch):
    client = _use(monkeypatch, {"/api/search?": UNIFIED})
    answer = search_mcp.call_tool("search", {"query": "grid view"})

    params = _params(client.calls[0])
    assert params == {"q": "grid view", "kind": "all", "limit": "10", "rerank": "1", "wait": "2"}
    assert answer["coverage"] == "90/100 indexed, sync in progress"
    assert answer["reranked"] is True
    comment, session = answer["results"]
    assert comment["kind"] == "comment" and comment["issue"] == "claunch-x1"
    assert comment["sessions"] == ["s720 (busy) via assignee"]
    assert comment["score"] == 0.988
    # The title line the daemon repeats is dropped and the passage is cut.
    assert comment["excerpt"].startswith("EVIDENCE ")
    assert len(comment["excerpt"]) == search_mcp.EXCERPT_CHARS
    assert comment["read"] == "api/beads/claunch-x1?cwd=F%3A%5Crepo"
    assert "href" not in comment and "root" not in comment
    assert session["read"] == "session:s9"
    assert session["exact_match"] is True
    assert session["sessions"] == ["s9 (exited, archived)"]


def test_search_board_corpus_rows_get_an_issue_and_a_read_handle(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    client = _use(monkeypatch, {"/api/search?": {
        "kind": "beads", "root": "F:\\repo", "index": {"indexed": 1, "total": 1},
        "results": [{"id": "claunch-y2", "score": 0.4, "title": "t", "status": "open",
                     "priority": 1, "assignee": "", "updated_at": "2026-09-01T00:00:00Z",
                     "excerpt": "## 목표 ..."}],
    }})
    answer = search_mcp.call_tool("search", {"query": "t", "kind": "beads", "limit": 99,
                                             "rerank": False})
    params = _params(client.calls[0])
    assert params["cwd"] == str(tmp_path)
    assert params["limit"] == "50" and params["rerank"] == "0"
    row = answer["results"][0]
    assert row["kind"] == "beads" and row["issue"] == "claunch-y2"
    assert row["at"] == "2026-09-01T00:00:00Z"
    assert "assignee" not in row
    assert row["read"] == "api/beads/claunch-y2?cwd=F%3A%5Crepo"


def test_search_sessions_corpus_rows_read_as_sessions(monkeypatch):
    _use(monkeypatch, {"/api/search?": {
        "kind": "sessions", "index": {},
        "results": [{"id": "s355", "name": "s355", "status": "exited", "one_line": "did x"}],
    }})
    row = search_mcp.call_tool("search", {"query": "x", "kind": "sessions"})["results"][0]
    assert row == {"kind": "session", "name": "s355", "status": "exited",
                   "excerpt": "did x", "read": "session:s355"}


def test_search_rejects_bad_input(monkeypatch):
    _use(monkeypatch, {})
    with pytest.raises(search_mcp.SearchMcpError):
        search_mcp.call_tool("search", {"query": "  "})
    with pytest.raises(search_mcp.SearchMcpError):
        search_mcp.call_tool("search", {"query": "a", "kind": "files"})


def test_search_unconfigured_names_the_fallback(monkeypatch):
    _use(monkeypatch, {"/api/search?": daemon_client.DaemonClientError(
        "GET /api/search: rag: block not configured (base_url, api_key, embedding_model)")})
    with pytest.raises(search_mcp.SearchMcpError, match="claunch beads search"):
        search_mcp.call_tool("search", {"query": "a"})


def test_read_opens_records_issues_and_sessions(monkeypatch):
    long_comments = [{"id": i, "text": "c" * 1000} for i in range(80)]
    client = _use(monkeypatch, {
        "/api/search/records/s1/briefing-1": {"id": "briefing-1", "text": "brief"},
        "/api/beads/claunch-x1": {"issue": {"id": "claunch-x1", "comments": long_comments}},
        "/api/sessions/s9": {"name": "s9", "status": "exited", "task": "do it",
                             "env": {"SECRET": "x"}, "cols": 80, "note": ""},
    })
    assert search_mcp.call_tool("search_read", {"read": "api/search/records/s1/briefing-1"}) == {
        "id": "briefing-1", "text": "brief"}
    issue = search_mcp.call_tool("search_read", {"read": "api/beads/claunch-x1?cwd=F%3A"})["issue"]
    assert len(json.dumps(issue, ensure_ascii=False)) <= search_mcp.READ_CHARS + 200  # + the note
    assert issue["comments"][-1]["id"] == 79  # the newest survive
    assert "claunch beads comments claunch-x1" in issue["comments_omitted"]
    assert search_mcp.call_tool("search_read", {"read": "session:s9"}) == {
        "name": "s9", "status": "exited", "task": "do it"}
    assert client.calls == [
        "/api/search/records/s1/briefing-1",
        "/api/beads/claunch-x1?cwd=F%3A",
        "/api/sessions/s9",
    ]


@pytest.mark.parametrize("handle", [
    "api/daemon/shutdown", "api/beads/../daemon/restart", "/api/sessions/s1", "session:", "",
])
def test_read_accepts_only_search_handles(monkeypatch, handle):
    client = _use(monkeypatch, {})
    response = search_mcp.SERVER.handle({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "search_read", "arguments": {"read": handle}},
    })
    assert response["result"]["isError"] is True
    assert client.calls == []
