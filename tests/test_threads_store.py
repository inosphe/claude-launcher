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
T8 history: every session's threads, newest activity first, paged, filtered
   by session and status, each with its last messages (claunch-8k02k)
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
    assert rail["s1"]["open"] == 1 and rail["s1"]["agent_seq"] == reply["seq"]   # a stays closed
    assert rail["s2"] == {"open": 1, "agent_seq": 0, "replies": []}
    assert st.list("s1")["agent_seq"] == reply["seq"]


def test_t5b_rail_replies(st):
    """The rail carries each session's last agent replies, oldest first, so
    the dashboard can announce them as notices (claunch-gfv7l)."""
    a = st.open(S.USER, "s1", "deploy plan", "how?")["thread"]["id"]
    b = st.open(S.USER, "s1", "flaky test")["thread"]["id"]
    st.open(S.USER, "s2", "other")
    st.post(S.USER, a, "user words are not replies")
    first = st.post("s1", a, "\n  first line of the answer  \nsecond line")["message"]
    second = st.post("s1", b, "x" * 400)["message"]
    replies = st.rail()["s1"]["replies"]
    assert [r["seq"] for r in replies] == [first["seq"], second["seq"]]
    assert replies[0] == {"seq": first["seq"], "thread": a, "title": "deploy plan",
                          "excerpt": "first line of the answer", "at": first["at"]}
    assert replies[1]["thread"] == b and replies[1]["title"] == "flaky test"
    assert len(replies[1]["excerpt"]) == S.RAIL_EXCERPT and replies[1]["excerpt"].endswith("\u2026")
    assert st.rail()["s2"]["replies"] == []
    # Only the newest RAIL_REPLIES per session are kept, and a rename shows.
    seqs = [st.post("s1", a, f"r{i}")["message"]["seq"] for i in range(S.RAIL_REPLIES + 2)]
    st.rename(S.USER, a, "deploy plan v2")
    replies = st.rail()["s1"]["replies"]
    assert [r["seq"] for r in replies] == seqs[-S.RAIL_REPLIES:]
    assert {r["title"] for r in replies} == {"deploy plan v2"}
    # An agent opening a thread with a first message is a reply too.
    opened = st.open("s2", "s2", "agent topic", "I opened this")["message"]
    assert [r["seq"] for r in st.rail()["s2"]["replies"]] == [opened["seq"]]


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


def test_t8_history_across_sessions(st, monkeypatch):
    clock = iter(f"2026-10-14T10:{m:02d}:00+00:00" for m in range(60))
    monkeypatch.setattr(S, "_now", lambda: next(clock))
    a = st.open(S.USER, "s1", "alpha", "a1")["thread"]["id"]
    b = st.open("s2", "s2", "beta", "b1")["thread"]["id"]
    c = st.open(S.USER, "s3", "gamma")["thread"]["id"]
    st.post("s1", a, "a2")                      # alpha is now the most recent
    st.set_status(S.USER, b, "closed")          # ...until beta is closed

    h = st.history()
    assert [t["id"] for t in h["threads"]] == [b, a, c]
    assert h["total"] == 3 and h["page"] == 1 and h["pages"] == 1
    alpha = h["threads"][1]
    assert [(m["author"], m["body"]) for m in alpha["messages"]] == [("user", "a1"), ("agent", "a2")]
    assert alpha["omitted"] == 0 and h["threads"][2]["messages"] == []
    assert [s["session"] for s in h["sessions"]] == ["s2", "s1", "s3"]
    assert all(s["threads"] == 1 for s in h["sessions"])

    p1, p2 = st.history(per_page=2), st.history(page=2, per_page=2)
    assert [t["id"] for t in p1["threads"]] == [b, a] and p1["pages"] == 2
    assert [t["id"] for t in p2["threads"]] == [c]
    assert st.history(page=9, per_page=2)["threads"] == []

    assert [t["id"] for t in st.history(session="s1")["threads"]] == [a]
    assert [t["id"] for t in st.history(status="closed")["threads"]] == [b]
    only = st.history(session="s1", status="closed")
    assert only["threads"] == [] and only["total"] == 0 and only["pages"] == 1
    assert len(only["sessions"]) == 3           # the filter's choices do not shrink with it

    with pytest.raises(S.ThreadError):
        st.history(page="x")
    with pytest.raises(S.ThreadError):
        st.history(status="gone")


def test_t8_history_keeps_the_last_messages(st, monkeypatch):
    monkeypatch.setattr(S, "HISTORY_MESSAGES", 3)
    tid = st.open(S.USER, "s1", "long")["thread"]["id"]
    for i in range(5):
        st.post(S.USER, tid, f"m{i}")
    t = st.history()["threads"][0]
    assert [m["body"] for m in t["messages"]] == ["m2", "m3", "m4"] and t["omitted"] == 2
    assert st.history(per_page=10_000)["per_page"] == S.HISTORY_MAX_PAGE
