"""Session PR attribution, persistence, and poll cache invalidation."""
import pytest

from claude_launcher.cflow2 import frontend
from claude_launcher.cflow2.store import RunStore
from claude_launcher.daemon import session_prs

URL = "https://ghe.example.com/o/r/pull/7"


def test_wizard_records_survive_cache_reset_and_failed_push(home, tmp_path):
    result = {"ok": True, "pr": {"url": URL, "isDraft": True},
              "branch": "s1-feature", "base": "master"}
    session_prs.record("s1", result, {"title": "Show PRs"})
    session_prs.record("s1", {"ok": False}, {})
    session_prs._cache.clear()
    rows = session_prs.for_session(str(tmp_path), "s1")
    assert len(rows) == 1
    assert rows[0]["title"] == "Show PRs"
    assert rows[0]["draft"] is True
    assert session_prs.for_session(str(tmp_path), "s2") == []


def test_workflow_collection_overrides_landing_and_invalidates_cache(home, tmp_path):
    st = RunStore(str(tmp_path), "s1")
    st.write_chart(frontend.compile_text("""cflow: 2
name: test-prs
nodes:
  wait: {prompt: wait}
body: wait
"""))
    state = {"data": {"landing": {"pr_url": URL, "base": "master"}},
             "collections": {"prs": {"items": [
                 {"url": URL, "number": 7, "branch": "s1-feature", "status": "open"},
                 {"url": "https://other.example.com/o/r/pull/7", "status": "closed"},
             ]}}}
    st.save_state(state)
    rows = session_prs.for_session(str(tmp_path), "s1")
    assert len(rows) == 2  # same number on another host is a different PR
    assert rows[1]["base"] == "master"
    assert rows[1]["status"] == "open"
    rows[1]["status"] = "corrupted by caller"
    assert session_prs.for_session(str(tmp_path), "s1")[1]["status"] == "open"
    state["collections"]["prs"]["items"][0]["status"] = "merged"
    st.save_state(state)
    # The checkpoint must win even if the JSON projection is stale.
    st.state_path.write_text("{}", encoding="utf-8")
    assert session_prs.for_session(str(tmp_path), "s1")[1]["status"] == "merged"
    assert session_prs.for_session(str(tmp_path), "s2") == []


def test_missing_and_corrupt_state_do_not_break_session_poll(home, tmp_path):
    assert session_prs.for_session(str(tmp_path), "s1") == []
    st = RunStore(str(tmp_path), "s1")
    st.dir.mkdir(parents=True)
    st.state_path.write_text("broken", encoding="utf-8")
    assert session_prs.for_session(str(tmp_path), "s1") == []


@pytest.mark.parametrize("url", ["javascript:alert(1)", "file:///o/r/pull/7",
    "https://ghe.example.com/o/r/issues/7", "https://u:p@ghe.example.com/o/r/pull/7"])
def test_only_safe_pr_urls_are_published(url):
    assert session_prs.clean({"url": url}) is None


def test_collection_stamp_is_kept_and_rows_come_newest_first(home, tmp_path):
    """The PR page files rows under the ``at`` the collection stamped
    (claunch-ijkge). Reading the stamp has to survive ``clean`` -- until it
    did, every row reached the page without a date and the timeline could
    only show one undated bucket."""
    st = RunStore(str(tmp_path), "s1")
    st.write_chart(frontend.compile_text("""cflow: 2
name: test-prs
nodes:
  wait: {prompt: wait}
body: wait
"""))
    st.save_state({"data": {}, "collections": {"prs": {"items": [
        {"url": "https://ghe.example.com/o/r/pull/1", "number": 1,
         "at": "2026-11-01T10:00:00+00:00", "seq": 4},
        {"url": "https://ghe.example.com/o/r/pull/2", "number": 2,
         "at": "2026-11-02T10:00:00+00:00", "seq": 5},
        # written before the stamp existed: it keeps the old order, after the
        # dated rows, and the page says the date is unknown rather than
        # inventing one
        {"url": "https://ghe.example.com/o/r/pull/3", "number": 3},
    ]}}})
    rows = session_prs.for_session(str(tmp_path), "s1")
    assert [r["number"] for r in rows] == [2, 1, 3]
    assert rows[0]["at"] == "2026-11-02T10:00:00+00:00"
    assert rows[0]["seq"] == 5
    assert "at" not in rows[2]


def test_collection_epoch_stamp_becomes_an_iso_string(home, tmp_path):
    """A v2 collection stamps ``at`` as epoch seconds, an ``int``/``float``.
    ``clean`` used to keep only strings, so rows written by a run's own
    collection reached the PR page undated and the date timeline collapsed to
    one ``날짜 미상`` bucket (claunch-ijkge.4.1). The number is normalised to the
    same ISO-8601 UTC form ``utcnow()`` writes, so collection rows and wizard
    rows sort and group together."""
    st = RunStore(str(tmp_path), "s1")
    st.write_chart(frontend.compile_text("""cflow: 2
name: test-prs
nodes:
  wait: {prompt: wait}
body: wait
"""))
    st.save_state({"data": {}, "collections": {"prs": {"items": [
        {"url": "https://ghe.example.com/o/r/pull/1", "number": 1,
         "at": 1793624990.9443326, "seq": 4},
        {"url": "https://ghe.example.com/o/r/pull/2", "number": 2,
         "at": 1793847699.7279878, "seq": 5},
        {"url": "https://ghe.example.com/o/r/pull/3", "number": 3,
         "at": "2026-11-03T12:46:40+00:00", "seq": 6},
    ]}}})
    rows = session_prs.for_session(str(tmp_path), "s1")
    assert [r["number"] for r in rows] == [2, 3, 1]
    assert [r["at"] for r in rows] == ["2026-11-05T03:01:39+00:00",
                                       "2026-11-03T12:46:40+00:00",
                                       "2026-11-02T13:09:50+00:00"]


def test_unusable_stamps_are_dropped_instead_of_breaking_the_poll():
    assert "at" not in session_prs.clean({"url": URL, "at": True})
    assert "at" not in session_prs.clean({"url": URL, "at": float("nan")})
    assert "at" not in session_prs.clean({"url": URL, "at": float("inf")})


def test_wizard_records_carry_a_stamp(home, tmp_path):
    session_prs.record("s1", {"ok": True, "pr": {"url": URL}, "branch": "b", "base": "master"},
                       {"title": "t"})
    [row] = session_prs.for_session(str(tmp_path), "s1")
    assert isinstance(row.get("at"), str) and row["at"]
