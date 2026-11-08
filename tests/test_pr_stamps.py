"""The PR stamp cache: the date a journal recovered, the title ``gh`` filled.

Two user-facing promises are pinned here (claunch-ijkge.4.1.3):

- a row that has no date or title of its own takes them from the cache, and a
  row that has them keeps its own;
- a title is looked up with ``gh`` only on a cache miss, and never on the
  request path — the list handler reads the cache and answers.

The one-off backfill (``tools/pr_stamp_backfill.py``) is pinned too: it reads
journals, and writes the daemon's sidecar cache and nothing else.
"""
from __future__ import annotations

import importlib.util
import json
import threading
import time
from pathlib import Path

import pytest

from claude_launcher.cflow2 import frontend
from claude_launcher.cflow2.store import RunStore
from claude_launcher.daemon import api, pr_actions, pr_stamps, session_prs

URL = "https://ghe.example.com/o/r/pull/7"
TOOL = Path(__file__).resolve().parents[1] / "tools" / "pr_stamp_backfill.py"


def _clear():
    pr_stamps._memo.update({"path": None, "mtime": None, "entries": {}})


@pytest.fixture(autouse=True)
def _isolated(home):
    """Every test starts with an empty cache and the ``gh`` half switched off."""
    _clear()
    pr_stamps.disable()
    session_prs._cache.clear()
    yield
    pr_stamps.disable()
    _clear()
    session_prs._cache.clear()


def _tool():
    spec = importlib.util.spec_from_file_location("pr_stamp_backfill", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(cwd, name, items=None, landing=None):
    st = RunStore(str(cwd), name)
    st.write_chart(frontend.compile_text("""cflow: 2
name: test-prs
nodes:
  wait: {prompt: wait}
body: wait
"""))
    st.save_state({"data": {"landing": landing or {}},
                   "collections": {"prs": {"items": items or []}}})
    return st


# --------------------------------------------------------------------------- #
# the key, and what the cache answers
# --------------------------------------------------------------------------- #
def test_key_names_a_pr_by_host_repository_and_number():
    assert pr_stamps.key(URL) == "ghe.example.com/o/r#7"
    assert pr_stamps.key(URL + "/") == "ghe.example.com/o/r#7"
    assert pr_stamps.key("https://ghe.example.com/o/r/issues/7") is None
    assert pr_stamps.key("") is None
    assert pr_stamps.key(None) is None


def test_stamp_answers_only_the_values_the_cache_holds(home):
    pr_stamps.record(URL, at="2026-11-04T09:18:04+00:00")
    assert pr_stamps.stamp(URL) == {"at": "2026-11-04T09:18:04+00:00"}
    pr_stamps.record(URL, title="Add the PR tab")
    assert pr_stamps.stamp(URL) == {"at": "2026-11-04T09:18:04+00:00",
                                    "title": "Add the PR tab"}
    assert pr_stamps.stamp("https://ghe.example.com/o/r/pull/9") == {}


def test_the_row_takes_the_cached_date_and_title(home, tmp_path):
    _run(tmp_path, "s1", landing={"pr_url": URL, "pr_number": 7, "base": "master"})
    pr_stamps.record(URL, at="2026-11-04T09:18:04+00:00", title="Add the PR tab")
    [row] = session_prs.for_session(str(tmp_path), "s1")
    assert row["at"] == "2026-11-04T09:18:04+00:00"
    assert row["title"] == "Add the PR tab"


def test_a_value_the_row_carries_wins_over_the_cache(home, tmp_path):
    _run(tmp_path, "s1", items=[{"url": URL, "number": 7,
                                 "at": "2026-11-05T00:00:00+00:00",
                                 "title": "from the record"}])
    pr_stamps.record(URL, at="2026-11-04T09:18:04+00:00", title="cached")
    [row] = session_prs.for_session(str(tmp_path), "s1")
    assert row["at"] == "2026-11-05T00:00:00+00:00"
    assert row["title"] == "from the record"


def test_a_filled_title_reaches_the_next_poll(home, tmp_path):
    """The poll is cached by the files it read; the cache file is one of them,
    so a background fill shows up on the next read rather than 30 s later."""
    _run(tmp_path, "s1", items=[{"url": URL, "number": 7,
                                 "at": "2026-11-05T00:00:00+00:00"}])
    assert pr_stamps.path() in session_prs.sources(str(tmp_path), "s1")
    assert "title" not in session_prs.for_session(str(tmp_path), "s1")[0]
    pr_stamps.record(URL, title="filled later")
    assert session_prs.for_session(str(tmp_path), "s1")[0]["title"] == "filled later"


# --------------------------------------------------------------------------- #
# the gh half: on a miss only, and off the request path
# --------------------------------------------------------------------------- #
def test_fill_asks_the_forge_only_for_a_title_it_lacks(home, monkeypatch):
    asked = []

    def detail(host, owner, name, number, run=None):
        asked.append((host, owner, name, number))
        return {"ok": True, "pr": {"title": f"PR {number}"}}

    monkeypatch.setattr(pr_actions, "detail", detail)
    pr_stamps.record(URL, title="already known")
    other = "https://ghe.example.com/o/r/pull/8"
    result = pr_stamps.fill([URL, other])
    assert asked == [("ghe.example.com", "o", "r", 8)]
    assert result["filled"] == 1 and result["asked"] == 1
    assert pr_stamps.stamp(other)["title"] == "PR 8"
    # a cache hit now: the second pass asks nothing
    assert pr_stamps.fill([URL, other]) == {"filled": 0, "failed": 0, "asked": 0}


def test_a_failed_lookup_is_cached_so_it_is_not_repeated_at_once(home, monkeypatch):
    calls = []

    def detail(host, owner, name, number, run=None):
        calls.append(number)
        return {"ok": False, "error": {"kind": "auth", "message": "not logged in"}}

    monkeypatch.setattr(pr_actions, "detail", detail)
    assert pr_stamps.fill([URL])["failed"] == 1
    assert calls == [7]
    assert pr_stamps.fill([URL])["asked"] == 0
    entry = pr_stamps.entries()["ghe.example.com/o/r#7"]
    assert entry["error"] == "not logged in"
    assert "title" not in entry


def test_an_expired_failure_is_asked_again(home, monkeypatch):
    monkeypatch.setattr(pr_actions, "detail",
                        lambda *a, **k: {"ok": True, "pr": {"title": "late"}})
    pr_stamps.write({"ghe.example.com/o/r#7": {"tried_at": time.time()
                                               - pr_stamps.FAILURE_TTL - 1}})
    assert pr_stamps.fill([URL])["filled"] == 1
    assert pr_stamps.stamp(URL)["title"] == "late"


def test_kick_does_nothing_until_the_daemon_enables_it(home, monkeypatch):
    monkeypatch.setattr(pr_stamps, "fill",
                        lambda *a, **k: pytest.fail("gh must not run here"))
    assert pr_stamps.kick([URL]) is False


def test_kick_runs_one_pass_off_the_request_path(home, monkeypatch):
    started, release = threading.Event(), threading.Event()
    passes = []

    def fill(urls, run=None):
        passes.append(list(urls))
        started.set()
        release.wait(5)
        return {"filled": 0, "failed": 0, "asked": len(urls)}

    monkeypatch.setattr(pr_stamps, "fill", fill)
    pr_stamps.enable()
    assert pr_stamps.kick([URL]) is True
    assert started.wait(5), "the pass did not start"
    # the caller returned before the pass finished, and a second poll in that
    # window does not start another one
    assert pr_stamps.kick([URL]) is True
    release.set()
    for _ in range(200):
        if not pr_stamps._filler.locked():
            break
        time.sleep(0.01)
    assert passes == [[URL]]


# --------------------------------------------------------------------------- #
# the backfill: journals in, sidecar out, run state untouched
# --------------------------------------------------------------------------- #
JOURNAL_EVENT = {
    "ts": "2026-11-04T09:18:04+00:00",
    "event": "collection_pushed",
    "collection": "prs",
    "principal": "driver",
    "by": "s1",
    "seq": 1,
    "key": 7,
    "item": {"number": 7, "url": URL, "branch": "b", "base": "master",
             "seq": 1, "at": 1793783884.7479, "by": "s1", "status": "open"},
}


def _journal(tmp_path, *events):
    path = tmp_path / "journal.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n",
                    encoding="utf-8")
    return path


def test_backfill_reads_a_push_and_a_dry_run_writes_nothing(home, tmp_path, capsys):
    tool = _tool()
    journal = _journal(tmp_path, JOURNAL_EVENT,
                       # another collection's push is not a PR
                       {**JOURNAL_EVENT, "collection": "signals"},
                       # and a push with no stamp recovers no date
                       {**JOURNAL_EVENT, "item": {"number": 8,
                                                  "url": "https://ghe.example.com/o/r/pull/8"}})
    before = journal.read_bytes()
    assert tool.main(["--journal", str(journal), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["pr_rows"] == 1
    assert report["writable"] == 1 and report["written"] == 0
    assert pr_stamps.entries() == {}
    assert journal.read_bytes() == before


def test_backfill_apply_writes_the_sidecar_and_leaves_the_journal_alone(home, tmp_path):
    tool = _tool()
    journal = _journal(tmp_path, JOURNAL_EVENT)
    before = journal.read_bytes()
    assert tool.main(["--journal", str(journal), "--apply"]) == 0
    entry = pr_stamps.entries()["ghe.example.com/o/r#7"]
    assert entry["at"] == "2026-11-04T09:18:04+00:00"
    assert entry["at_source"] == "journal"
    assert journal.read_bytes() == before
    # the repaired date is what the page now groups by
    assert pr_stamps.stamp(URL)["at"] == "2026-11-04T09:18:04+00:00"


def test_backfill_keeps_a_date_the_cache_already_holds(home, tmp_path, capsys):
    tool = _tool()
    journal = _journal(tmp_path, JOURNAL_EVENT)
    pr_stamps.record(URL, at="2026-11-01T00:00:00+00:00")
    assert tool.main(["--journal", str(journal), "--apply", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["already"] == 1 and report["writable"] == 0
    assert pr_stamps.stamp(URL)["at"] == "2026-11-01T00:00:00+00:00"


def test_backfill_finds_runs_under_a_working_directory(home, tmp_path):
    tool = _tool()
    run = tmp_path / ".cflow2" / "runs" / "s1@main"
    run.mkdir(parents=True)
    (run / "journal.jsonl").write_text(json.dumps(JOURNAL_EVENT) + "\n",
                                       encoding="utf-8")
    archived = tmp_path / ".cflow2" / "archive" / "s2@main" / "20261104-000000-abcdef"
    archived.mkdir(parents=True)
    (archived / "journal.jsonl").write_text(
        json.dumps({**JOURNAL_EVENT,
                    "item": {**JOURNAL_EVENT["item"], "url":
                             "https://ghe.example.com/o/r/pull/8", "number": 8}}) + "\n",
        encoding="utf-8")
    found = tool.scan(tool._journals(_Args(cwd=[str(tmp_path)])))
    assert sorted(row["at"] for row in found["rows"].values()) == [
        "2026-11-04T09:18:04+00:00", "2026-11-04T09:18:04+00:00"]
    assert found["journals"] == 2


class _Args:
    def __init__(self, *, cwd=(), root=(), journal=()):
        self.cwd, self.root, self.journal = list(cwd), list(root), list(journal)
