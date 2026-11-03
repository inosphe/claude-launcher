"""One long-lived connection per WAL store (claude_launcher/sqlite_shared.py).

S1 a write commits; an exception inside it rolls back
S2 a write nested in a write on the same thread joins it as a savepoint: the
   inner failure undoes only the inner rows, an outer failure undoes both
S3 a block that leaves a transaction open is rolled back when it ends, so the
   next caller never runs inside it
S4 a closed connection refuses use
S5 the daemon's four WAL stores open their file once, however many threads
   call them -- opening and closing a WAL file on two threads at once is the
   SQLite 3.51.0 lock-order inversion that wedged the daemon (2026-11-04)
S6 many threads writing one store at once lose no row
"""

import sqlite3
import threading

import pytest

from claude_launcher import sqlite_shared
from claude_launcher.daemon import db as session_db
from claude_launcher.megathread import store as mega_store
from claude_launcher.qa import store as qa_store
from claude_launcher.threads import store as thread_store


@pytest.fixture
def shared(tmp_path):
    conn = sqlite_shared.SharedConnection(tmp_path / "t.sqlite3")
    with conn.use() as db:
        db.execute("CREATE TABLE t (v INTEGER)")
    yield conn
    conn.close()


def _values(shared):
    with shared.use() as db:
        return [r[0] for r in db.execute("SELECT v FROM t ORDER BY v")]


def test_s1_write_commits_and_rolls_back(shared):
    with shared.write() as db:
        db.execute("INSERT INTO t VALUES (1)")
    with pytest.raises(RuntimeError):
        with shared.write() as db:
            db.execute("INSERT INTO t VALUES (2)")
            raise RuntimeError("boom")
    assert _values(shared) == [1]


def test_s2_nested_write_is_a_savepoint(shared):
    with shared.write() as db:
        db.execute("INSERT INTO t VALUES (1)")
        with pytest.raises(RuntimeError):
            with shared.write() as inner:
                inner.execute("INSERT INTO t VALUES (2)")
                raise RuntimeError("inner")
        with shared.write() as inner:
            inner.execute("INSERT INTO t VALUES (3)")
    assert _values(shared) == [1, 3]

    with pytest.raises(RuntimeError):
        with shared.write() as db:
            db.execute("INSERT INTO t VALUES (4)")
            with shared.write() as inner:
                inner.execute("INSERT INTO t VALUES (5)")
            raise RuntimeError("outer")
    assert _values(shared) == [1, 3]


def test_s3_open_transaction_is_rolled_back_at_the_end(shared):
    with shared.use() as db:
        db.execute("BEGIN")
        db.execute("INSERT INTO t VALUES (9)")
    with shared.use() as db:
        assert not db.in_transaction
    assert _values(shared) == []


def test_s4_closed_connection_refuses_use(tmp_path):
    conn = sqlite_shared.SharedConnection(tmp_path / "t.sqlite3")
    conn.close()
    with pytest.raises(sqlite3.ProgrammingError):
        with conn.use():
            pass


def test_s5_wal_stores_open_their_file_once(tmp_path, monkeypatch):
    opened = []
    real = sqlite3.connect

    def counting(path, *args, **kwargs):
        opened.append(str(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", counting)
    threads = thread_store.ThreadStore(tmp_path / "threads.sqlite3")
    qa = qa_store.QaStore(tmp_path / "qa.sqlite3")
    megas = mega_store.MegaThreadStore(tmp_path / "megathread.sqlite3")
    sessions = session_db.SessionStore(tmp_path / "sessions.db")

    def work(n):
        for i in range(5):
            tid = threads.open("user", f"s{n}", f"t{i}", "hello")["thread"]["id"]
            threads.read(tid)
            qa.ask(f"s{n}", title=f"q{i}")
            megas.create(f"m{n}-{i}")
            megas.list()
            sessions.save([{"def": {"name": f"s{n}-{i}"}}], prune=False)
            sessions.load_all()

    workers = [threading.Thread(target=work, args=(n,)) for n in range(4)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()

    assert sorted(opened) == sorted(str(tmp_path / name) for name in
                                    ("threads.sqlite3", "qa.sqlite3", "megathread.sqlite3", "sessions.db"))
    assert len(sessions.load_all()) == 20
    for store in (threads, qa, megas, sessions):
        store.close_connection()


def test_s6_concurrent_writes_keep_every_row(shared):
    def work(n):
        for i in range(50):
            with shared.write() as db:
                db.execute("INSERT INTO t VALUES (?)", (n * 100 + i,))

    workers = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    assert len(_values(shared)) == 400
