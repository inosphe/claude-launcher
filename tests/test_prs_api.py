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
from claude_launcher.daemon import api


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
