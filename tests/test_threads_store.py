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
T9 images on a message, from the user (claunch-shl2r) or the agent
   (claunch-euaz6); an images-only reply still has a notice line
T10 guests (claunch-p9wes): the user or the owner invites a session into a
   thread; the guest writes there (author guest) and nothing more; it is
   listed apart in the guest's list; a v5 database is rebuilt to v6
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
        st.open(S.USER, "s1", "x" * (S.MAX_TITLE + 1))
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


def test_t6b_blank_title_takes_the_next_default_name(st):
    """claunch-bvbmi: a thread opened without a title is ``Thread #N``, N
    counting the session's threads (closed ones too) and skipping a title
    already taken; the first message stays optional."""
    first = st.open(S.USER, "s1", "  ")
    assert first["thread"]["title"] == "Thread #1" and first["message"] is None
    assert st.read(first["thread"]["id"])["messages"] == []
    st.set_status(S.USER, first["thread"]["id"], "closed")
    assert st.open(S.USER, "s1", None)["thread"]["title"] == "Thread #2"
    st.open(S.USER, "s1", "Thread #4")
    assert st.open(S.USER, "s1", "")["thread"]["title"] == "Thread #5"   # 4 taken by hand
    assert st.open(S.USER, "s2", "")["thread"]["title"] == "Thread #1"   # per session
    named = st.open("s2", "s2", "", "fyi")
    assert named["thread"]["title"] == "Thread #2" and named["message"]["body"] == "fyi"


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


# -- T9 images on a message (claunch-shl2r) ---------------------------------

IMG = {"name": "0123456789abcdef0123.png", "type": "image/png", "bytes": 12}


def test_t9_a_user_message_carries_images_and_may_have_no_text(st):
    res = st.open(S.USER, "s1", "Bug", images=[IMG])
    m = res["message"]
    assert m["body"] == "" and m["images"] == [IMG]
    tid = res["thread"]["id"]
    res = st.post(S.USER, tid, "and this", images=[IMG])
    assert res["message"]["images"] == [IMG]
    st.post("s1", tid, "seen")
    got = st.read(tid)["messages"]
    assert [m["images"] for m in got] == [[IMG], [IMG], []]
    assert st.has_image(tid, IMG["name"])
    assert not st.has_image(tid, "fedcba98765432100000.png")
    assert not st.has_image(tid, "../threads.sqlite3")


def test_t9_bad_images(st):
    tid = st.open(S.USER, "s1", "Bug")["thread"]["id"]
    with pytest.raises(S.ThreadError, match="text is required"):
        st.post(S.USER, tid, "", images=[])
    with pytest.raises(S.ThreadError, match="not a stored thread image"):
        st.post(S.USER, tid, "x", images=[{"name": "../../etc/passwd"}])
    with pytest.raises(S.ThreadError, match="at most"):
        st.post(S.USER, tid, "x", images=[IMG] * (S.MAX_IMAGES + 1))
    assert st.read(tid)["messages"] == []


def test_t9_the_agent_attaches_images_too(st):
    """The owning session attaches images the way the user does
    (claunch-euaz6): on its own message, alone or with text, when it opens
    a thread or replies in one; another session still may not write there."""
    res = st.open("s1", "s1", "Chart", "here it is", images=[IMG])
    tid = res["thread"]["id"]
    assert res["message"]["author"] == S.AGENT and res["message"]["images"] == [IMG]
    only = st.post("s1", tid, "", images=[IMG])["message"]
    assert only["body"] == "" and only["images"] == [IMG]
    assert st.has_image(tid, IMG["name"])
    with pytest.raises(S.Forbidden):
        st.post("s2", tid, "", images=[IMG])
    assert [m["images"] for m in st.read(tid)["messages"]] == [[IMG], [IMG]]


def test_t9_an_images_only_reply_has_a_notice_line(st):
    """The rail's excerpt counts a reply's pictures, so an images-only reply
    is not a blank notice card (claunch-euaz6)."""
    tid = st.open(S.USER, "s1", "Chart", "show me")["thread"]["id"]
    st.post("s1", tid, "", images=[IMG, IMG])
    st.post("s1", tid, "with words", images=[IMG])
    st.post("s1", tid, "words only")
    assert [r["excerpt"] for r in st.rail()["s1"]["replies"]] == [
        "\U0001f5bc \uc774\ubbf8\uc9c0 2\uc7a5", "with words \U0001f5bc1", "words only"]


def test_t9_a_v2_database_is_rebuilt_with_its_rows(tmp_path):
    import sqlite3
    path = tmp_path / "threads.sqlite3"
    db = sqlite3.connect(str(path))
    db.executescript("""
    CREATE TABLE threads (id TEXT PRIMARY KEY, session TEXT NOT NULL, title TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open', opened_by TEXT NOT NULL, last_seq INTEGER NOT NULL DEFAULT 0,
        agent_read INTEGER NOT NULL DEFAULT 0, qa TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE messages (seq INTEGER PRIMARY KEY AUTOINCREMENT, thread TEXT NOT NULL REFERENCES threads (id),
        session TEXT NOT NULL, author TEXT NOT NULL CHECK (author IN ('user', 'agent')),
        body TEXT NOT NULL CHECK (length(body) > 0), at TEXT NOT NULL);
    CREATE INDEX messages_thread ON messages (thread, seq);
    INSERT INTO threads VALUES ('th-aaaaaa', 's1', 'Old', 'open', 'user', 7, 0, NULL, 't', 't');
    INSERT INTO messages VALUES (5, 'th-aaaaaa', 's1', 'user', 'hello', 't');
    INSERT INTO messages VALUES (7, 'th-aaaaaa', 's1', 'agent', 'hi', 't');
    PRAGMA user_version = 2;
    """)
    db.close()
    st = S.ThreadStore(path)
    got = st.read("th-aaaaaa")["messages"]
    assert [(m["seq"], m["body"], m["images"]) for m in got] == [(5, "hello", []), (7, "hi", [])]
    m = st.post(S.USER, "th-aaaaaa", "", images=[IMG])["message"]
    assert m["seq"] == 8                                  # the sequence goes on past the old rows
    S.ThreadStore(path)                                   # a second open is a no-op
    assert len(st.read("th-aaaaaa")["messages"]) == 3


def test_t10_the_user_or_the_owner_invites_and_a_guest_only_writes(st):
    tid = st.open(S.USER, "s1", "Review", "look at this")["thread"]["id"]
    res = st.invite(S.USER, tid, "s2")
    assert res["added"] is True and [g["session"] for g in res["thread"]["guests"]] == ["s2"]
    assert res["thread"]["guests"][0]["invited_by"] == "user"
    assert st.invite(S.USER, tid, "s2")["added"] is False          # twice is one guest
    assert st.invite("s1", tid, "s3")["thread"]["guests"][1]["invited_by"] == "s1"
    with pytest.raises(S.Forbidden):
        st.invite("s2", tid, "s4")                                # a guest does not invite
    with pytest.raises(S.Forbidden):
        st.invite("s9", tid, "s4")                                # nor does a stranger
    with pytest.raises(S.ThreadError):
        st.invite(S.USER, tid, "s1")                              # the owner is no guest
    with pytest.raises(S.ThreadError):
        st.invite(S.USER, tid, "")
    m = st.post("s2", tid, "consider X")
    assert m["as"] == S.GUEST
    assert (m["message"]["author"], m["message"]["guest"]) == ("guest", "s2")
    assert m["thread"]["agent_unread"] == 2                       # the user's and the guest's
    with pytest.raises(S.Forbidden):
        st.set_status("s2", tid, "closed")
    with pytest.raises(S.Forbidden):
        st.rename("s2", tid, "mine now")
    with pytest.raises(S.Forbidden):
        st.post("s9", tid, "hi")                                  # not a guest
    assert st.post("s1", tid, "ok")["message"]["author"] == "agent"
    assert "guest" not in st.read(tid)["messages"][0]


def test_t10_guest_threads_are_listed_apart_with_their_unread(st):
    tid = st.open(S.USER, "s1", "Review", "one")["thread"]["id"]
    st.open(S.USER, "s2", "own")
    st.invite("s1", tid, "s2")
    st.post("s1", tid, "two")
    doc = st.list("s2")
    assert [t["title"] for t in doc["threads"]] == ["own"]
    [g] = doc["guest_threads"]
    assert (g["id"], g["session"], g["guest"]["invited_by"], g["guest"]["unread"]) == (tid, "s1", "s1", 2)
    st.read(tid, reader="s2")
    assert st.list("s2")["guest_threads"][0]["guest"]["unread"] == 0
    st.post("s2", tid, "advice")                                  # its own message is not unread
    st.post(S.USER, tid, "thanks")
    assert st.list("s2")["guest_threads"][0]["guest"]["unread"] == 1
    assert st.read(tid, reader="s2")["thread"]["agent_read"] == 2  # still the owner's last reply
    assert st.list("s1")["guest_threads"] == []
    st.set_status(S.USER, tid, "closed")
    assert st.list("s2", closed=False)["guest_threads"] == []


def test_t10_uninvite_and_leave(st):
    tid = st.open(S.USER, "s1", "Review")["thread"]["id"]
    for g in ("s2", "s3", "s4"):
        st.invite(S.USER, tid, g)
    with pytest.raises(S.Forbidden):
        st.uninvite("s3", tid, "s2")                              # one guest does not remove another
    assert st.uninvite("s2", tid, "s2")["removed"] is True        # leaving
    assert st.uninvite("s1", tid, "s3")["thread"]["guests"][0]["session"] == "s4"
    assert st.uninvite(S.USER, tid, "s4")["thread"]["guests"] == []
    with pytest.raises(S.NotFound):
        st.uninvite(S.USER, tid, "s4")
    with pytest.raises(S.Forbidden):
        st.post("s2", tid, "still here?")
    assert st.list("s2")["guest_threads"] == []
    assert st.guests_of(tid) == []


def test_t10_a_guest_post_does_not_reopen_a_closed_thread(st):
    # claunch-mgh8x: a guest may not reopen a thread any more than close it.
    tid = st.open(S.USER, "s1", "Review")["thread"]["id"]
    st.invite(S.USER, tid, "s2")
    st.set_status(S.USER, tid, "closed")
    res = st.post("s2", tid, "one more thought")
    assert res["reopened"] is False and res["as"] == S.GUEST
    assert res["thread"]["status"] == "closed" and res["message"]["guest"] == "s2"
    assert st.get(tid)["status"] == "closed"
    assert st.post(S.USER, tid, "back to it")["reopened"] is True     # the user still reopens
    assert st.get(tid)["status"] == "open"


def test_t10_an_ended_guest_is_dropped_everywhere(st):
    # claunch-tee5g: the exit hook's store call.
    a = st.open(S.USER, "s1", "A")["thread"]["id"]
    b = st.open(S.USER, "s3", "B")["thread"]["id"]
    for tid in (a, b):
        st.invite(S.USER, tid, "s2")
    st.invite(S.USER, a, "s4")
    st.post("s2", a, "advice")
    assert st.drop_guest("s2") == 2
    assert st.guests_of(a) == ["s4"] and st.guests_of(b) == []
    assert st.list("s2")["guest_threads"] == []
    assert [m["guest"] for m in st.read(a)["messages"]] == ["s2"]    # what it wrote stays
    assert st.drop_guest("s2") == 0


def test_t10_guest_limits(st):
    tid = st.open(S.USER, "s1", "Review")["thread"]["id"]
    for i in range(S.MAX_GUESTS):
        st.invite(S.USER, tid, f"g{i}")
    with pytest.raises(S.Conflict):
        st.invite(S.USER, tid, "one-more")
    with pytest.raises(S.ThreadError):
        st.invite(S.USER, tid, S.operator_owner("op1"))
    op = st.import_thread(S.operator_owner("op1"), "conv", [{"author": "user", "body": "hi"}])
    with pytest.raises(S.ThreadError):
        st.invite(S.USER, op, "s2")
    st.delete_owner(S.operator_owner("op1"))


def test_t10_a_v5_database_is_rebuilt_to_v6_with_its_rows(tmp_path):
    import sqlite3
    path = tmp_path / "threads.sqlite3"
    db = sqlite3.connect(str(path))
    db.executescript("""
    CREATE TABLE threads (id TEXT PRIMARY KEY, session TEXT NOT NULL, title TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open', opened_by TEXT NOT NULL, last_seq INTEGER NOT NULL DEFAULT 0,
        agent_read INTEGER NOT NULL DEFAULT 0, qa TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE messages (seq INTEGER PRIMARY KEY AUTOINCREMENT, thread TEXT NOT NULL REFERENCES threads (id),
        session TEXT NOT NULL, author TEXT NOT NULL CHECK (author IN ('user', 'agent', 'system')),
        body TEXT NOT NULL, at TEXT NOT NULL, images TEXT,
        kind TEXT NOT NULL DEFAULT 'message' CHECK (kind IN ('message', 'ask', 'dispatch', 'system')),
        ref TEXT, data TEXT, files TEXT,
        CHECK (length(body) > 0 OR images IS NOT NULL OR files IS NOT NULL));
    INSERT INTO threads VALUES ('th-aaaaaa', 's1', 'Old', 'open', 'user', 7, 0, NULL, 't', 't');
    INSERT INTO messages (seq, thread, session, author, body, at, files)
        VALUES (5, 'th-aaaaaa', 's1', 'user', '', 't', '[{"id": "0123456789abcdef0123", "name": "a.bin", "bytes": 3}]');
    INSERT INTO messages (seq, thread, session, author, body, at) VALUES (7, 'th-aaaaaa', 's1', 'agent', 'hi', 't');
    PRAGMA user_version = 5;
    """)
    db.close()
    st = S.ThreadStore(path)
    got = st.read("th-aaaaaa")["messages"]
    assert [(m["seq"], m["author"], m.get("files")) for m in got] == [
        (5, "user", [{"id": "0123456789abcdef0123", "name": "a.bin", "bytes": 3}]), (7, "agent", None)]
    st.invite(S.USER, "th-aaaaaa", "s2")
    assert st.post("s2", "th-aaaaaa", "advice")["message"]["seq"] == 8
    S.ThreadStore(path)                                           # a second open is a no-op
    assert len(st.read("th-aaaaaa")["messages"]) == 3
    with sqlite3.connect(str(path)) as raw:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == S.SCHEMA_VERSION == 6
