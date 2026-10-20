"""Mega thread store (megathread/store.py, claunch-v5nhp).

T1 a cluster is created, read back and listed; status and title change
T2 members are (session, thread) pairs: joining twice is one member, a
   session may join with two of its threads, removing one leaves the other
T3 a session that ends leaves every cluster
T4 the routing log keeps the verdict, every member's score and the tokens;
   the 24-hour usage is the sum over that window
T5 bad input: empty title, unknown cluster, unknown verdict, empty text
"""

import pytest

from claude_launcher.megathread import store as S


@pytest.fixture
def st(tmp_path):
    return S.MegaThreadStore(tmp_path / "megathread.sqlite3")


def test_t1_create_get_list_and_update(st, tmp_path):
    mega = st.create("  Frontend work ")
    assert mega["id"].startswith("mt-") and mega["title"] == "Frontend work"
    assert mega["status"] == "open" and mega["members"] == []
    again = S.MegaThreadStore(tmp_path / "megathread.sqlite3").get(mega["id"])
    assert again["title"] == "Frontend work"
    assert [m["id"] for m in st.list()["megathreads"]] == [mega["id"]]
    assert st.set_status(mega["id"], "closed")["status"] == "closed"
    assert st.list(closed=False)["megathreads"] == []
    assert st.rename(mega["id"], "Renamed")["title"] == "Renamed"


def test_t2_members_are_idempotent_pairs(st):
    mega = st.create("m")
    first = st.add_member(mega["id"], "s1", "th-a")
    assert first["added"] is True and len(first["mega"]["members"]) == 1
    assert st.add_member(mega["id"], "s1", "th-a")["added"] is False
    both = st.add_member(mega["id"], "s1", "th-b")["mega"]
    assert len(both["members"]) == 2 and both["sessions"] == 1
    removed = st.remove_member(mega["id"], "th-a")
    assert removed["removed"] is True
    assert [m["thread"] for m in removed["mega"]["members"]] == ["th-b"]
    assert st.remove_member(mega["id"], "th-a")["removed"] is False


def test_t3_session_end_drops_its_memberships(st):
    one, two = st.create("one"), st.create("two")
    st.add_member(one["id"], "s1", "th-a")
    st.add_member(two["id"], "s1", "th-b")
    st.add_member(two["id"], "s2", "th-c")
    assert st.drop_session("s1") == 2
    assert st.members(one["id"]) == []
    assert [m["session"] for m in st.members(two["id"])] == ["s2"]
    assert [m["id"] for m in st.clusters_of("s1")] == []


def test_t4_route_log_and_usage(st):
    mega = st.create("m")
    row = st.record_route(
        mega["id"], "where does this go?", verdict="choose", threshold=2.197,
        candidates=[{"session": "s1", "thread": "th-a", "p_mine": 0.9, "p_not_mine": 0.1,
                     "logit": 2.197, "choice": "mine", "confidence": 0.9}],
        chosen=[], tokens=4200, model="jev-latest")
    assert row["verdict"] == "choose" and row["tokens"] == 4200
    assert row["candidates"][0]["thread"] == "th-a" and row["delivered"] is False
    st.record_route(mega["id"], "and this?", verdict="manual", threshold=2.197,
                    chosen=["th-a"], tokens=0, delivered=True)
    routes = st.routes(mega["id"])["routes"]
    assert [r["verdict"] for r in routes] == ["manual", "choose"]
    assert st.usage_tokens() == 4200
    assert st.usage_tokens(mega=mega["id"]) == 4200
    assert st.usage_tokens(mega="mt-other") == 0


def test_t5_validation(st):
    with pytest.raises(S.MegaError):
        st.create("   ")
    with pytest.raises(S.NotFound):
        st.get("mt-nope")
    mega = st.create("m")
    with pytest.raises(S.NotFound):
        st.add_member("mt-nope", "s1", "th-a")
    with pytest.raises(S.MegaError):
        st.record_route(mega["id"], "", verdict="auto", threshold=1.0)
    with pytest.raises(S.MegaError):
        st.record_route(mega["id"], "x", verdict="maybe", threshold=1.0)
    with pytest.raises(S.MegaError):
        st.add_member(mega["id"], "", "th-a")
