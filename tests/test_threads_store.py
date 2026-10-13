"""Session threads store (threads/store.py, claunch-doj98).

T1 a thread opened with text holds that message; the thread survives a new
   store on the same file
T2 the user writes in any session's threads, a session only in its own
T3 the agent's read position: user messages past it are unread; reading or
   replying as the owner moves it, reading as anyone else does not
T4 posting to a closed thread reopens it
T5 list orders open before closed and reports open/agent_seq; rail counts
   per session
T6 bad input: empty title/text, unknown thread, too many open threads
T7 read pages with after/limit
"""

import pytest

from claude_launcher.threads import store as S


@pytest.fixture
def st(tmp_path):
    return S.ThreadStore(tmp_path / "threads.sqlite3")


def test_t1_open_with_text_persists(st, tmp_path):
    res = st.open(S.USER, "s1", " Deploy plan ", "first\r\nline")
    t = res["thread"]
    assert t["id"].startswith("th-") and t["title"] == "Deploy plan"
    assert t["session"] == "s1" and t["status"] == "open" and t["opened_by"] == "user"
    assert res["message"]["body"] == "first\nline" and res["message"]["author"] == "user"
    again = S.ThreadStore(tmp_path / "threads.sqlite3").read(t["id"])
    assert [m["body"] for m in again["messages"]] == ["first\nline"]
    assert again["thread"]["last_seq"] == res["message"]["seq"]
    bare = st.open(S.USER, "s1", "no text yet")
    assert bare["message"] is None and bare["thread"]["last"] is None


def test_t2_who_may_write(st):
    tid = st.open("s1", "s1", "mine")["thread"]["id"]
    assert st.open(S.USER, "s2", "for s2")["thread"]["opened_by"] == "user"
    with pytest.raises(S.Forbidden):
        st.open("s1", "s2", "not mine")
    with pytest.raises(S.Forbidden):
        st.post("s2", tid, "hi")
    with pytest.raises(S.Forbidden):
        st.set_status("s2", tid, "closed")
    with pytest.raises(S.Forbidden):
        st.rename("s2", tid, "x")
    assert st.post(S.USER, tid, "user here")["message"]["author"] == "user"
    assert st.post("s1", tid, "agent here")["message"]["author"] == "agent"
    assert st.rename("s1", tid, "renamed")["thread"]["title"] == "renamed"


def test_t3_agent_read_position(st):
    tid = st.open(S.USER, "s1", "q", "one")["thread"]["id"]
    st.post(S.USER, tid, "two")
    assert st.list("s1")["threads"][0]["agent_unread"] == 2
    st.read(tid, reader="s2")            # someone else reading moves nothing
    st.read(tid)                         # nor does the user
    assert st.list("s1")["threads"][0]["agent_unread"] == 2
    out = st.read(tid, reader="s1")
    assert out["thread"]["agent_unread"] == 0
    st.post(S.USER, tid, "three")
    assert st.list("s1")["threads"][0]["agent_unread"] == 1
    st.post("s1", tid, "answer")         # replying is having read
    assert st.list("s1")["threads"][0]["agent_unread"] == 0


def test_t4_post_reopens_a_closed_thread(st):
    tid = st.open("s1", "s1", "topic", "hello")["thread"]["id"]
    assert st.set_status(S.USER, tid, "closed")["thread"]["status"] == "closed"
    res = st.post(S.USER, tid, "one more thing")
    assert res["reopened"] is True and res["thread"]["status"] == "open"
    assert st.post("s1", tid, "ok")["reopened"] is False
    with pytest.raises(S.ThreadError):
        st.set_status("s1", tid, "archived")


def test_t5_list_and_rail(st):
    a = st.open(S.USER, "s1", "a", "x")["thread"]["id"]
    b = st.open(S.USER, "s1", "b")["thread"]["id"]
    st.open(S.USER, "s2", "c")
    st.set_status(S.USER, a, "closed")
    listed = st.list("s1")
    assert [t["id"] for t in listed["threads"]] == [b, a]
    assert listed["open"] == 1 and listed["agent_seq"] == 0
    assert [t["id"] for t in st.list("s1", closed=False)["threads"]] == [b]
    reply = st.post("s1", b, "agent answer")["message"]
    rail = st.rail()
    assert rail["s1"] == {"open": 1, "agent_seq": reply["seq"]}   # a stays closed
    assert rail["s2"] == {"open": 1, "agent_seq": 0}
    assert st.list("s1")["agent_seq"] == reply["seq"]


def test_t6_bad_input(st):
    with pytest.raises(S.ThreadError, match="title"):
        st.open(S.USER, "s1", "  ")
    with pytest.raises(S.ThreadError, match="session"):
        st.open(S.USER, "", "t")
    tid = st.open(S.USER, "s1", "t")["thread"]["id"]
    with pytest.raises(S.ThreadError, match="text"):
        st.post(S.USER, tid, "   ")
    with pytest.raises(S.ThreadError, match="string"):
        st.post(S.USER, tid, 5)
    with pytest.raises(S.NotFound):
        st.post(S.USER, "th-nope", "x")
    with pytest.raises(S.NotFound):
        st.read("th-nope")
    for i in range(S.MAX_OPEN_PER_SESSION - 1):
        st.open(S.USER, "s1", f"t{i}")
    with pytest.raises(S.Conflict):
        st.open(S.USER, "s1", "one too many")


def test_t7_read_pages(st):
    tid = st.open(S.USER, "s1", "long")["thread"]["id"]
    seqs = [st.post(S.USER, tid, f"m{i}")["message"]["seq"] for i in range(5)]
    first = st.read(tid, limit=2)
    assert [m["body"] for m in first["messages"]] == ["m0", "m1"] and first["more"] is True
    rest = st.read(tid, after=first["messages"][-1]["seq"])
    assert [m["body"] for m in rest["messages"]] == ["m2", "m3", "m4"] and rest["more"] is False
    assert st.read(tid, after=seqs[-1])["messages"] == []
    with pytest.raises(S.ThreadError):
        st.read(tid, after="x")
