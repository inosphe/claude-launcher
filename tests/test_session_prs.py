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
