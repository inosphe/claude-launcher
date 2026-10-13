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
    with pytest.raises(Q.Conflict):
        qa.answer(Q.USER, q["id"], 2, text="late")
    assert qa.list("s1", closed=False)["questions"] == []
    reopened = qa.update("s1", q["id"], 2, title="Q again")
    assert reopened["status"] == "open"
    assert qa.delete("s1", q["id"], 3) == {"deleted": q["id"], "session": "s1"}
    with pytest.raises(Q.NotFound):
        qa.answer(Q.USER, q["id"], 3, text="x")


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
