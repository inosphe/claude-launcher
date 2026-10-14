"""Session QA store (qa/store.py, claunch-l8vyi): who may write what, the
revision compare-and-swap between the session and the user, and the
schema-level guards that hold even for a write that bypasses the store."""

import sqlite3
import threading

import pytest

from claude_launcher.qa import store as Q


@pytest.fixture
def qa(tmp_path):
    return Q.QaStore(tmp_path / "qa.sqlite3")


def test_ask_answer_and_rewrite_keep_the_previous_answer(qa):
    q = qa.ask("s1", title="Which base?", body="ctx", options=["master", {"label": "stack", "description": "d"}])
    assert (q["status"], q["revision"], q["answer"]) == ("open", 1, None)
    assert q["options"] == [{"label": "master"}, {"label": "stack", "description": "d"}]

    first = qa.answer(Q.USER, q["id"], 1, choices=["master"])
    assert first["previous"] is None
    a = first["question"]
    assert (a["status"], a["revision"], a["answer"]["choices"], a["answer"]["saves"]) == ("answered", 2, ["master"], 1)

    second = qa.answer(Q.USER, q["id"], 2, choices=["stack"], text="after all")
    assert second["previous"]["choices"] == ["master"]
    assert second["question"]["answer"]["text"] == "after all"
    assert second["question"]["answer"]["saves"] == 2


def test_user_save_against_a_moved_question_is_refused_with_the_current_one(qa):
    q = qa.ask("s1", title="Q")
    qa.update("s1", q["id"], 1, title="Q, reworded")
    with pytest.raises(Q.Conflict) as err:
        qa.answer(Q.USER, q["id"], 1, text="answer to the old wording")
    assert err.value.current["title"] == "Q, reworded"
    assert err.value.current["revision"] == 2
    assert qa.get(q["id"])["answer"] is None   # nothing filed


def test_agent_edit_against_an_answer_it_did_not_read_is_refused(qa):
    q = qa.ask("s1", title="Q")
    qa.answer(Q.USER, q["id"], 1, text="yes")
    with pytest.raises(Q.Conflict) as err:
        qa.update("s1", q["id"], 1, title="Q2")
    assert "answered" in str(err.value)
    assert err.value.current["answer"]["text"] == "yes"


def test_editing_an_answered_question_marks_it_stale_and_unresolved(qa):
    q = qa.ask("s1", title="Q")
    qa.answer(Q.USER, q["id"], 1, text="yes")
    s = qa.update("s1", q["id"], 2, body="more context")
    assert (s["status"], s["revision"], s["answer"]["text"], s["answer"]["revision"]) == ("stale", 3, "yes", 2)
    assert qa.list("s1")["unresolved"] == 1
    again = qa.answer(Q.USER, q["id"], 3, text="yes")["question"]
    assert again["status"] == "answered"
    assert qa.list("s1")["unresolved"] == 0


def test_unchanged_update_is_a_no_op(qa):
    q = qa.ask("s1", title="Q")
    assert qa.update("s1", q["id"], 1, title="Q")["revision"] == 1


def test_only_the_owner_asks_edits_closes_deletes_and_only_the_user_answers(qa):
    q = qa.ask("s1", title="Q")
    for fn in (lambda: qa.update("s2", q["id"], 1, title="x"),
               lambda: qa.close("s2", q["id"], 1),
               lambda: qa.delete("s2", q["id"], 1),
               lambda: qa.update(Q.USER, q["id"], 1, title="x"),
               lambda: qa.ask(Q.USER, title="x"),
               lambda: qa.answer("s1", q["id"], 1, text="self-answer")):
        with pytest.raises(Q.Forbidden):
            fn()


def test_close_and_delete(qa):
    q = qa.ask("s1", title="Q")
    c = qa.close("s1", q["id"], 1, note="moot")
    assert c["status"] == "closed"
    assert qa.list("s1", closed=False)["questions"] == []
    reopened = qa.update("s1", q["id"], 2, title="Q again")
    assert reopened["status"] == "open"
    assert qa.delete("s1", q["id"], 3) == {"deleted": q["id"], "session": "s1"}
    with pytest.raises(Q.NotFound):
        qa.answer(Q.USER, q["id"], 3, text="x")


def test_a_closed_question_takes_a_rewritten_answer_and_reopens(qa):
    # claunch-564kn: closing is the session's "acted on it", not the end of
    # the user's say -- a later answer reopens it for the session to act on.
    q = qa.ask("s1", title="Q")
    qa.answer(Q.USER, q["id"], 1, text="first")
    qa.close("s1", q["id"], 2, note="done")
    res = qa.answer(Q.USER, q["id"], 3, text="second thoughts")
    assert res["reopened"] is True and res["previous"]["text"] == "first"
    after = res["question"]
    assert (after["status"], after["revision"], after["answer"]["text"]) == ("answered", 4, "second thoughts")
    assert qa.answer(Q.USER, q["id"], 4, text="again")["reopened"] is False
    unanswered = qa.ask("s1", title="never answered")
    qa.close("s1", unanswered["id"], 1)
    assert qa.answer(Q.USER, unanswered["id"], 2, text="late")["question"]["status"] == "answered"


def test_recheck_reopens_a_closed_question_and_the_next_close_is_its_reply(qa):
    q = qa.ask("s1", title="Which base?")
    qa.answer(Q.USER, q["id"], 1, text="master")
    qa.close("s1", q["id"], 2)
    seq_before = qa.rail()["s1"]["agent_seq"]
    res = qa.recheck(Q.USER, q["id"], 3, note="you are on the stack branch")
    assert res["reopened"] is True
    assert (res["question"]["status"], res["question"]["revision"]) == ("answered", 4)
    assert res["question"]["answer"]["text"] == "master"
    assert qa.rail()["s1"]["agent_seq"] == seq_before, "the user's check does not pop the pane open"
    check = qa.list("s1")["questions"][0]["check"]
    assert (check["note"], check["settled"], check["reply"]) == ("you are on the stack branch", False, "")

    qa.close("s1", q["id"], 4, note="moved back to master")
    check = qa.list("s1")["questions"][0]["check"]
    assert (check["settled"], check["reply"]) == (True, "moved back to master")

    # On an answered (open) question nothing about the row moves.
    qa.answer(Q.USER, q["id"], 5, text="master, still")
    again = qa.recheck(Q.USER, q["id"], 6)
    assert again["reopened"] is False and again["question"]["revision"] == 6
    check = qa.list("s1")["questions"][0]["check"]
    assert (check["note"], check["settled"]) == ("", False)


def test_recheck_needs_an_answer_for_the_wording_shown(qa):
    q = qa.ask("s1", title="Q")
    with pytest.raises(Q.QaError, match="no answer"):
        qa.recheck(Q.USER, q["id"], 1)
    qa.answer(Q.USER, q["id"], 1, text="a")
    with pytest.raises(Q.Forbidden):
        qa.recheck("s1", q["id"], 2)
    with pytest.raises(Q.Conflict):
        qa.recheck(Q.USER, q["id"], 1)
    qa.update("s1", q["id"], 2, title="Q, reworded")              # stale
    with pytest.raises(Q.QaError, match="changed after its answer"):
        qa.recheck(Q.USER, q["id"], 3)
    qa.close("s1", q["id"], 3)                                    # closed while stale
    with pytest.raises(Q.QaError, match="changed after its answer"):
        qa.recheck(Q.USER, q["id"], 4)
    assert qa.get(q["id"])["status"] == "closed"


def test_answer_validation(qa):
    one = qa.ask("s1", title="one", options=["a", "b"])
    with pytest.raises(Q.QaError, match="one choice"):
        qa.answer(Q.USER, one["id"], 1, choices=["a", "b"])
    with pytest.raises(Q.QaError, match="not an option"):
        qa.answer(Q.USER, one["id"], 1, choices=["z"])
    with pytest.raises(Q.QaError, match="text or a choice"):
        qa.answer(Q.USER, one["id"], 1, text="  ")
    many = qa.ask("s1", title="many", options=["a", "b", "c"], multi=True)
    got = qa.answer(Q.USER, many["id"], 1, choices=["c", "a"])["question"]
    assert got["answer"]["choices"] == ["a", "c"]   # the question's order
    with pytest.raises(Q.QaError, match="revision is required"):
        qa.answer(Q.USER, many["id"], None, text="x")
    with pytest.raises(Q.QaError, match="multi needs options"):
        qa.ask("s1", title="bad", multi=True)
    with pytest.raises(Q.QaError, match="twice"):
        qa.ask("s1", title="dup", options=["a", "a"])


def test_rail_counts_unresolved_and_moves_agent_seq_only_on_session_writes(qa):
    q = qa.ask("s1", title="Q")
    qa.ask("s2", title="other")
    rail = qa.rail()
    assert rail["s1"]["unresolved"] == 1 and rail["s2"]["unresolved"] == 1
    seq = rail["s1"]["agent_seq"]
    qa.answer(Q.USER, q["id"], 1, text="a")
    rail = qa.rail()
    assert rail["s1"]["unresolved"] == 0
    assert rail["s1"]["agent_seq"] == seq          # the user's write is not the agent's update
    qa.update("s1", q["id"], 2, title="Q2")
    assert qa.rail()["s1"]["agent_seq"] > seq
    assert qa.list("s1")["agent_seq"] == qa.rail()["s1"]["agent_seq"]


def test_rail_lists_the_questions_waiting_on_the_user(qa):
    # claunch-k7c32: the dashboard announces each of these once as a notice.
    q = qa.ask("s1", title="Q")
    qa.ask("s1", title="Q-other")
    waiting = qa.rail()["s1"]["waiting"]
    assert [w["title"] for w in waiting] == ["Q", "Q-other"]
    assert waiting[0] == {"id": q["id"], "title": "Q", "status": "open", "revision": 1,
                          "answer_revision": None, "updated_at": waiting[0]["updated_at"]}
    qa.answer(Q.USER, q["id"], 1, text="a")
    assert [w["title"] for w in qa.rail()["s1"]["waiting"]] == ["Q-other"]
    qa.update("s1", q["id"], 2, title="Q2")          # answered (rev 2), then rewritten: asked again
    again = [w for w in qa.rail()["s1"]["waiting"] if w["id"] == q["id"]]
    assert again and again[0]["status"] == "stale" and again[0]["answer_revision"] == 2
    qa.update("s1", q["id"], 3, title="Q3")          # the same re-ask, edited once more
    again = [w for w in qa.rail()["s1"]["waiting"] if w["id"] == q["id"]]
    assert again[0]["revision"] == 4 and again[0]["answer_revision"] == 2


def test_schema_refuses_inconsistent_rows_written_around_the_store(qa):
    q = qa.ask("s1", title="Q")
    db = sqlite3.connect(str(qa.path))
    try:
        with pytest.raises(sqlite3.IntegrityError):   # answered without an answer
            db.execute("UPDATE questions SET status='answered', revision=revision+1 WHERE id=?", (q["id"],))
        with pytest.raises(sqlite3.IntegrityError):   # revision rolled back
            db.execute("UPDATE questions SET title='x', revision=1 WHERE id=?", (q["id"],))
        with pytest.raises(sqlite3.IntegrityError):   # owner moved
            db.execute("UPDATE questions SET session='s2', revision=revision+1 WHERE id=?", (q["id"],))
        with pytest.raises(sqlite3.IntegrityError):   # unknown status
            db.execute("UPDATE questions SET status='done', revision=revision+1 WHERE id=?", (q["id"],))
    finally:
        db.close()
    assert qa.get(q["id"])["revision"] == 1


def test_concurrent_saves_on_one_revision_let_exactly_one_through(qa):
    q = qa.ask("s1", title="Q")
    results = []
    barrier = threading.Barrier(2)

    def user():
        barrier.wait()
        try:
            qa.answer(Q.USER, q["id"], 1, text="user")
            results.append("user")
        except Q.Conflict:
            results.append("user-conflict")

    def agent():
        barrier.wait()
        try:
            qa.update("s1", q["id"], 1, title="agent")
            results.append("agent")
        except Q.Conflict:
            results.append("agent-conflict")

    threads = [threading.Thread(target=user), threading.Thread(target=agent)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) in (["agent", "user-conflict"], ["agent-conflict", "user"])
    assert qa.get(q["id"])["revision"] == 2


def test_newer_schema_is_refused(tmp_path):
    path = tmp_path / "qa.sqlite3"
    Q.QaStore(path)
    db = sqlite3.connect(str(path))
    db.execute(f"PRAGMA user_version = {Q.SCHEMA_VERSION + 1}")
    db.close()
    with pytest.raises(Q.QaError, match="schema version"):
        Q.QaStore(path)


def test_nudge_is_settled_by_a_question_or_by_nothing(qa):
    n = qa.nudge(Q.USER, "s1")
    assert (n["status"], n["settled_at"]) == ("sent", None) and n["id"].startswith("qn-")
    assert qa.list("s1")["nudge"]["id"] == n["id"]
    q = qa.ask("s1", title="Q")
    asked = qa.list("s1")["nudge"]
    assert (asked["status"], asked["questions"]) == ("asked", [q["id"]])
    with pytest.raises(Q.Conflict) as err:           # already settled by the question
        qa.nothing("s1", n["id"], note="late")
    assert err.value.current["status"] == "asked"

    n2 = qa.nudge(Q.USER, "s1")
    done = qa.nothing("s1", None, note="all decided")  # None: the waiting one
    assert (done["id"], done["status"], done["reply"]) == (n2["id"], "nothing", "all decided")
    with pytest.raises(Q.NotFound):
        qa.nothing("s1")
    qa.ask("s1", title="later")                       # no waiting nudge: nothing to settle
    assert qa.list("s1")["nudge"]["status"] == "nothing"


def test_a_rewritten_question_also_answers_the_nudge(qa):
    q = qa.ask("s1", title="Q")
    n = qa.nudge(Q.USER, "s1")
    qa.update("s1", q["id"], 1, title="Q")              # unchanged: a no-op, not a reply
    assert qa.list("s1")["nudge"]["status"] == "sent"
    qa.update("s1", q["id"], 1, title="Q, reworded")
    after = qa.list("s1")["nudge"]
    assert (after["id"], after["status"], after["questions"]) == (n["id"], "asked", [q["id"]])


def test_a_newer_nudge_supersedes_the_waiting_one_and_moves_agent_seq_only_on_reply(qa):
    seq = qa.list("s1")["agent_seq"]
    first = qa.nudge(Q.USER, "s1")
    second = qa.nudge(Q.USER, "s1")
    assert qa.list("s1")["agent_seq"] == seq          # the user's press is not the agent's change
    db = sqlite3.connect(str(qa.path))
    try:
        rows = dict(db.execute("SELECT id, status FROM nudges").fetchall())
    finally:
        db.close()
    assert rows == {first["id"]: "superseded", second["id"]: "sent"}
    qa.nothing("s1", second["id"])
    assert qa.list("s1")["agent_seq"] > seq           # the reply opens the pane like a question would


def test_nudge_identity_rules(qa):
    with pytest.raises(Q.Forbidden):
        qa.nudge("s1", "s2")                          # sessions do not nudge
    n = qa.nudge(Q.USER, "s1")
    with pytest.raises(Q.Forbidden):
        qa.nothing("s2", n["id"])                     # not the session it was sent to
    with pytest.raises(Q.Forbidden):
        qa.nothing(Q.USER, n["id"])


def test_schema_keeps_one_waiting_nudge_and_settled_nudges_fixed(qa):
    n = qa.nudge(Q.USER, "s1")
    db = sqlite3.connect(str(qa.path))
    try:
        with pytest.raises(sqlite3.IntegrityError):   # a second waiting nudge
            db.execute("INSERT INTO nudges (id, session, created_at) VALUES ('qn-x', 's1', 'now')")
        with pytest.raises(sqlite3.IntegrityError):   # settled without a time
            db.execute("UPDATE nudges SET status='nothing' WHERE id=?", (n["id"],))
        db.execute("UPDATE nudges SET status='nothing', settled_at='now' WHERE id=?", (n["id"],))
        db.commit()
        with pytest.raises(sqlite3.IntegrityError):   # settled stays settled
            db.execute("UPDATE nudges SET status='sent', settled_at=NULL WHERE id=?", (n["id"],))
    finally:
        db.close()


def test_a_version_1_file_gains_the_nudge_table(tmp_path):
    path = tmp_path / "qa.sqlite3"
    db = sqlite3.connect(str(path))
    db.executescript(Q._SCHEMA.split("CREATE TABLE IF NOT EXISTS nudges")[0])
    db.execute("PRAGMA user_version = 1")
    db.close()
    qa = Q.QaStore(path)
    assert qa.nudge(Q.USER, "s1")["status"] == "sent"
