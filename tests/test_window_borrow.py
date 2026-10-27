"""Borrowing a waiting test run from another machine's window (claunch-ohsyx).

The lender's half is ``WindowManager``'s borrow state and the
``/peer/window/*`` routes; the borrower's half is
``daemon/window_borrow.BorrowRunner``. The relay between the two daemons is
stood in for by a transport that posts to the lender's aiohttp app; nothing
here reaches a real relay or a second machine.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from claude_launcher import test_window
from claude_launcher.daemon import window as window_mod
from claude_launcher.daemon import window_borrow
from claude_launcher.daemon.window import WindowManager

INFO = {"v": 1, "toplevel": "/nowhere", "rel_cwd": "", "head": "a" * 40,
        "tree": "b" * 40, "dirty": False, "pytest_args": ["tests"]}


def _make(tmp_path, name="window.json", **kwargs) -> WindowManager:
    shape = dict(window_mod.DEFAULT_LIMITS)
    return WindowManager(
        None,
        caps=lambda: (1, 3),
        limits=lambda: shape,
        state_path=tmp_path / name,
        cores=8,
        **kwargs,
    )


async def _queued(w: WindowManager, cls="targeted", info=INFO, wait=30.0, **kwargs):
    """A waiter behind a held sweep; returns (task, grant_id)."""
    task = asyncio.create_task(w.acquire(
        cls, session=None, pid=os.getpid(), label="pytest tests", wait=wait,
        borrow=info, **kwargs))
    for _ in range(100):
        await asyncio.sleep(0.01)
        queue = w.status()["queue"]
        if queue:
            return task, queue[-1]["grant_id"]
    raise AssertionError("the request never queued")


# ---- the lender's state ------------------------------------------------------ #

def test_borrowed_request_keeps_its_place_and_blocks_nothing(tmp_path):
    async def run():
        w = _make(tmp_path)
        held = await w.acquire("sweep", session=None, pid=os.getpid())
        waiter, gid = await _queued(w, cls="sweep")
        lent = w.borrow(gid, "machine-b")
        assert lent["ok"] and lent["token"]
        # The acquire long-poll answers at once; the client awaits from here.
        answer = await asyncio.wait_for(waiter, 5)
        assert answer["borrowed"]["machine"] == "machine-b"
        assert "token" not in answer["borrowed"]
        # Status never shows the token (the relay does not say who asks).
        assert "token" not in w.status()["queue"][0]["borrow"]
        # A borrowed sweep is not "a sweep queued ahead": targeted runs behind
        # it are granted once the window frees.
        w.release(held["grant_id"])
        got = await w.acquire("targeted", session=None, pid=os.getpid(), wait=1)
        assert got["granted"], got
        assert [q["grant_id"] for q in w.status()["queue"]] == [gid]

    asyncio.run(run())


def test_borrow_refusals_leave_the_queue_as_it_was(tmp_path):
    async def run():
        w = _make(tmp_path)
        await w.acquire("sweep", session=None, pid=os.getpid())
        old_client, old_gid = await _queued(w, info=None)
        assert "cannot take a remote result" in w.borrow(old_gid, "b")["error"]
        new_client, gid = await _queued(w)
        assert w.borrow(gid, "b")["ok"]
        # D9: a second borrow of the same request is refused, by name.
        again = w.borrow(gid, "c")
        assert again == {"ok": False, "error": "already borrowed by b"}
        assert w.borrow("nope", "b")["ok"] is False
        old_client.cancel()
        new_client.cancel()

    asyncio.run(run())


def test_beat_result_and_await_hand_the_remote_result_over(tmp_path):
    async def run():
        w = _make(tmp_path)
        await w.acquire("sweep", session=None, pid=os.getpid())
        waiter, gid = await _queued(w)
        token = w.borrow(gid, "b")["token"]
        await waiter
        assert w.borrow_beat(gid, "wrong", "testing") == "revoked"
        assert w.borrow_beat(gid, token, "testing") == "ok"
        pending = await w.await_entry(gid, wait=0.05)
        assert pending["pending"] and pending["borrowed"]["phase"] == "testing"
        remote = {"exit_code": 1, "output": "1 failed, 2 passed in 0.1s\n",
                  "machine": "b", "window_result": {
                      "outcome": "failed", "exit_code": 1, "passed": 2, "failed": 1}}
        assert w.borrow_result(gid, token, remote) == "ok"
        assert w.borrow_result(gid, token, remote) == "ok"  # a retried post
        answer = await w.await_entry(gid, wait=1)
        assert answer["remote"]["exit_code"] == 1
        assert w.status()["queue"] == []
        line = w.history()["entries"][0]
        assert line["end"] == "borrowed"
        assert line["borrowed_by"] == "b"
        assert line["result"]["failed"] == 1
        # The result file is gone with the handover.
        assert not any((tmp_path / "window-borrow-results").iterdir())
        # A beat for an answered request: the borrower stops.
        assert w.borrow_beat(gid, token) == "unknown"

    asyncio.run(run())


def test_lost_heartbeat_revokes_and_the_request_waits_here_again(tmp_path):
    async def run():
        # Above the acquire long-poll's 1s poll, which must see the borrow.
        w = _make(tmp_path, borrow_timeout=1.5)
        held = await w.acquire("sweep", session=None, pid=os.getpid())
        waiter, gid = await _queued(w)
        token = w.borrow(gid, "b")["token"]
        assert (await waiter)["borrowed"]
        await asyncio.sleep(1.6)
        pending = await w.await_entry(gid, wait=0.05)
        assert pending["pending"] and pending["borrowed"] is None
        entry = w.status()["queue"][0]
        assert entry["borrows"][-1]["end"] == "heartbeat-lost"
        # D7: whatever the old borrower sends now is discarded.
        assert w.borrow_beat(gid, token) == "revoked"
        assert w.borrow_result(gid, token, {"exit_code": 0}) == "revoked"
        # Back in the queue at its place: the freed window grants it here.
        w.release(held["grant_id"])
        answer = await w.await_entry(gid, wait=2)
        assert answer["granted"], answer

    asyncio.run(run())


def test_borrowed_time_does_not_count_against_the_wait(tmp_path):
    async def run():
        w = _make(tmp_path)
        await w.acquire("sweep", session=None, pid=os.getpid())
        waiter, gid = await _queued(w, wait=0.4)
        token = w.borrow(gid, "b")["token"]
        await waiter
        await asyncio.sleep(0.6)  # past the wait it asked for, but borrowed
        assert (await w.await_entry(gid, wait=0.05))["pending"]
        assert w.borrow_release(gid, token, "could not fetch")
        assert w.status()["queue"][0]["borrows"][-1]["end"] == "released: could not fetch"
        # The wait left when it was borrowed (~0.4s) runs again from here.
        assert (await w.await_entry(gid, wait=0.05))["pending"]
        await asyncio.sleep(0.5)
        assert (await w.await_entry(gid, wait=0.05)) == {"granted": False, "timeout": True}

    asyncio.run(run())


def test_operator_cancel_of_a_borrowed_request_stops_the_borrower(tmp_path):
    async def run():
        w = _make(tmp_path)
        await w.acquire("sweep", session=None, pid=os.getpid())
        waiter, gid = await _queued(w)
        token = w.borrow(gid, "b")["token"]
        await waiter
        assert w.cancel(gid)
        assert w.borrow_beat(gid, token) == "unknown"
        assert (await w.await_entry(gid, wait=0.05)) == {"granted": False, "cancelled": True}

    asyncio.run(run())


# ---- the client's arguments ----------------------------------------------------- #

def test_portable_args_drop_machine_paths_and_relativise_repo_paths(tmp_path):
    top = tmp_path / "repo"
    (top / "tests").mkdir(parents=True)
    args = ["-q", f"--basetemp={tmp_path}/bt", "--junit-xml", "x.xml",
            str(top / "tests" / "test_a.py") + "::test_one", "-n", "4"]
    assert test_window.portable_args(args, top, top) == [
        "-q", "tests/test_a.py::test_one", "-n", "4"]
    assert test_window.portable_args([str(tmp_path / "elsewhere.py")], top, top) is None


def test_pytest_args_of_and_requested_width():
    assert test_window.pytest_args_of(
        'uv run --no-sync pytest tests -q -n 8 --basetemp="/x/y w"') == [
        "tests", "-q", "-n", "8", "--basetemp=/x/y w"]
    assert test_window.pytest_args_of("/v/bin/python -m pytest tests -x") == ["tests", "-x"]
    assert test_window.pytest_args_of("make test") is None
    assert test_window.requested_width_of(["tests", "-n", "6"]) == 6
    assert test_window.requested_width_of(["-n4"]) == 4
    assert test_window.requested_width_of(["-n", "auto"]) == 0
    assert test_window.requested_width_of(["tests"]) == 1


# ---- reproducing the tree (real git) ------------------------------------------- #

def _git(cwd, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "init.defaultBranch=master",
         "-C", str(cwd), *args],
        check=True, capture_output=True, text=True).stdout.strip()


def _commit(repo: Path, name: str, text: str) -> str:
    (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", name)
    return _git(repo, "rev-parse", "HEAD")


def _configured_remotes(repo: str) -> dict:
    """Remote name -> URL as configured, before ``insteadOf`` rewrites it
    (``git remote -v`` prints the rewritten one: the bare path here)."""
    out = subprocess.run(["git", "-C", repo, "config", "--get-regexp", r"^remote\..*\.url$"],
                         capture_output=True, text=True).stdout
    found = {}
    for line in out.splitlines():
        key, _, url = line.partition(" ")
        found[key[len("remote."):-len(".url")]] = url
    return found


@pytest.fixture
def lender_clone(tmp_path, monkeypatch):
    """A bare remote on a network-looking URL and the lender's clone of it.

    ``reposync.url_key`` keys remotes by host/owner/repo, so the clone's
    origin is an https URL with ``insteadOf`` pointing it back at the bare
    repository on disk, and remotes are read as configured.
    """
    from claude_launcher import reposync

    monkeypatch.setattr(reposync, "remotes", _configured_remotes)
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "master", str(bare)], check=True)
    clone = tmp_path / "lender"
    subprocess.run(["git", "clone", "-q", str(bare), str(clone)], check=True,
                   capture_output=True)
    url = "https://git.example.invalid/team/proj.git"
    _git(clone, "remote", "set-url", "origin", url)
    _git(clone, "config", f"url.{bare.as_posix()}.insteadOf", url)
    (clone / "tests").mkdir()
    _commit(clone, "tests/test_ok.py", "def test_ok():\n    assert True\n")
    _git(clone, "push", "-q", "origin", "HEAD:master")
    _git(clone, "fetch", "-q", "origin")
    return clone, bare, url


def _info(clone: Path, **over) -> dict:
    info = test_window.borrow_info(["tests", "-q"], cwd=str(clone))
    info.update(over)
    return info


def test_recipe_commit_merge_and_refusals(lender_clone):
    clone, _bare, _url = lender_clone
    window_borrow._recipes.clear()
    made = window_borrow.recipe(_info(clone))
    assert made["ok"] and made["kind"] == "commit"
    assert made["refs"][0]["branch"] == "master"
    assert made["keys"] == ["git.example.invalid/team/proj"]

    # A merge of two pushed parents, itself unpushed: replayable.
    base = _git(clone, "rev-parse", "HEAD")
    _git(clone, "checkout", "-q", "-b", "feature")
    _commit(clone, "feature.txt", "f\n")
    _git(clone, "push", "-q", "origin", "feature")
    _git(clone, "checkout", "-q", "master")
    _commit(clone, "main.txt", "m\n")
    _git(clone, "push", "-q", "origin", "master")
    _git(clone, "fetch", "-q", "origin")
    _git(clone, "merge", "-q", "--no-ff", "--no-edit", "feature")
    made = window_borrow.recipe(_info(clone))
    assert made["ok"] and made["kind"] == "merge", made
    assert [r["branch"] for r in made["refs"]] == ["master", "feature"]

    # An unpushed plain commit cannot be rebuilt elsewhere.
    _commit(clone, "local.txt", "l\n")
    made = window_borrow.recipe(_info(clone))
    assert not made["ok"] and "on no remote" in made["reason"]
    assert window_borrow.recipe(_info(clone, dirty=True))["reason"].startswith(
        "the waiter's tree has uncommitted")
    assert not window_borrow.recipe(_info(clone, pytest_args=None))["ok"]
    assert base



@pytest.mark.parametrize("rel_cwd, args", [
    ("", ["tests", "-q"]),
    ("sub/dir", ["../../tests/test_a.py::test_x", "-k", "a or b"]),
    ("", ["--deselect=tests/test_a.py::test_y", "-x"]),
    ("", ["-q", "--tb", "short"]),
    ("", ["--tb=line", "--maxfail=2", "--lf"]),
    ("", ["--deselect", "tests/test_a.py::t", "-n", "4", "--dist", "load"]),
    ("", ["-xq", "-vv", "-rA", "tests"]),
    ("", ["-k", "not slow", "-n", "4", "-m", "-slow-ish"]),
    ("", ["-n4", "-xk", "a"]),
])
def test_vet_borrow_accepts_selections_inside_the_checkout(rel_cwd, args):
    assert window_borrow.vet_borrow({"rel_cwd": rel_cwd, "pytest_args": args}) is None


@pytest.mark.parametrize("rel_cwd, args, said", [
    ("/etc", ["tests"], "rel_cwd"),
    ("C:/work", ["tests"], "rel_cwd"),
    ("..", ["tests"], "rel_cwd"),
    ("sub/../..", ["tests"], "rel_cwd"),
    ("sub\\..\\..", ["tests"], "rel_cwd"),
    ("", ["/abs/tests/test_a.py"], "outside the checkout"),
    ("", ["../elsewhere/test_a.py::t"], "outside the checkout"),
    ("sub", ["../../x.py"], "outside the checkout"),
    ("", ["--deselect=/abs/t.py::t"], "outside the checkout"),
    ("", ["D:\\x\\test_a.py"], "outside the checkout"),
    ("", ["-p", "evil_plugin"], "configuration or plugins"),
    ("", ["-pno:cacheprovider"], "configuration or plugins"),
    ("", ["-c", "other.ini"], "configuration or plugins"),
    ("", ["--config-file=other.ini"], "configuration or plugins"),
    ("", ["--rootdir", "."], "configuration or plugins"),
    ("", ["--confcutdir=."], "configuration or plugins"),
    ("", ["-xc../out/evil.ini"], "configuration or plugins"),
    ("", ["-xpevil"], "configuration or plugins"),
    ("", ["-vxc", "../x.ini"], "configuration or plugins"),
    ("", ["-o", "pythonpath=.."], "configuration or plugins"),
    ("", ["-opythonpath=.."], "configuration or plugins"),
    ("", ["--override-ini=cache_dir=/abs"], "configuration or plugins"),
    ("", ["--override-ini", "pythonpath=/abs"], "configuration or plugins"),
    ("", ["-Z"], "may carry"),
    ("", ["-xZ"], "may carry"),
    ("", ["-k", "x", "/abs/t.py"], "outside the checkout"),
    ("", ["--tx=popen//python=../x", "--dist=load"], "may carry"),
    ("", ["--tx", "popen//python=/abs/x"], "may carry"),
    ("", ["--px=popen"], "may carry"),
    ("", ["--pdbcls=m:C"], "may carry"),
    ("", ["-q", "--pdb"], "may carry"),
    ("", ["--rsyncdir=.."], "may carry"),
    ("", ["--lf=x"], "may carry"),
    ("", ["-Werror"], "may carry"),
    ("", ["--ignore", "/abs/t.py"], "outside the checkout"),
    ("", ["--deselect", "../t.py::t"], "outside the checkout"),
    ("", ["@/abs/args.txt"], "from a file"),
    ("", ["@../args.txt"], "from a file"),
    ("", ["@args.txt"], "from a file"),
    ("", ["-k", "@x"], "from a file"),
])
def test_vet_borrow_refuses_paths_out_and_scope_options(rel_cwd, args, said):
    reason = window_borrow.vet_borrow({"rel_cwd": rel_cwd, "pytest_args": args})
    assert reason and said in reason, reason


def test_recipe_refuses_what_the_borrower_would_refuse(lender_clone):
    clone, _bare, _url = lender_clone
    window_borrow._recipes.clear()
    made = window_borrow.recipe(_info(clone, rel_cwd="../outside"))
    assert not made["ok"] and "rel_cwd" in made["reason"]
    made = window_borrow.recipe(_info(clone, pytest_args=["-p", "x", "tests"]))
    assert not made["ok"] and "plugins" in made["reason"]

# ---- the whole borrow, lender app + borrower runner ----------------------------- #

class _Runner(window_borrow.BorrowRunner):
    """The borrower with this test's interpreter standing in for uv sync."""

    setup_command = staticmethod(lambda checkout: [sys.executable, "-c", ""])
    python_of = staticmethod(lambda checkout: Path(sys.executable))


async def _lender_app(window):
    from aiohttp.test_utils import TestClient, TestServer

    from claude_launcher.daemon.api import build_app
    from claude_launcher.daemon.manager import SessionManager

    manager = SessionManager(idle_threshold=0.5, scrollback=100, restore_default=False)
    app = build_app(manager, "secret", started_at=time.monotonic(), window=window)
    client = TestClient(TestServer(app))
    await client.start_server()

    async def transport(machine, path, body):
        assert machine == "lender"
        resp = await client.post(path, json=body)
        payload = await resp.json()
        if resp.status >= 400:
            raise RuntimeError(payload.get("error") or f"HTTP {resp.status}")
        return payload

    return client, manager, transport


def _borrower_clone(tmp_path, bare: Path, url: str) -> Path:
    from claude_launcher import workspaces

    clone = tmp_path / "borrower"
    subprocess.run(["git", "clone", "-q", str(bare), str(clone)], check=True,
                   capture_output=True)
    _git(clone, "remote", "set-url", "origin", url)
    _git(clone, "config", f"url.{bare.as_posix()}.insteadOf", url)
    workspaces.add(str(clone), name="proj")
    return clone


async def _until(predicate, seconds=60.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.1)
    raise AssertionError("condition not reached")


def test_borrowed_merge_runs_on_the_borrower_and_the_waiter_gets_its_result(
        lender_clone, tmp_path, monkeypatch):
    clone, bare, url = lender_clone
    # A merge the borrower must replay: tree equality is the check.
    _git(clone, "checkout", "-q", "-b", "feature")
    _commit(clone, "tests/test_feature.py", "def test_f():\n    assert 1\n")
    _git(clone, "push", "-q", "origin", "feature")
    _git(clone, "checkout", "-q", "master")
    _git(clone, "fetch", "-q", "origin")
    _git(clone, "merge", "-q", "--no-ff", "--no-edit", "feature")
    window_borrow._recipes.clear()
    monkeypatch.setattr(window_borrow, "heartbeat_interval", lambda: 0.1)
    borrower = _borrower_clone(tmp_path, bare, url)

    async def run():
        lender = _make(tmp_path, "lender.json")
        own = _make(tmp_path, "borrower.json")
        client, manager, transport = await _lender_app(lender)
        runner = _Runner(own, transport, lambda: "borrower",
                         root=tmp_path / "borrows" / "window-borrow",
                         lister=lambda: _names())
        try:
            await lender.acquire("sweep", session=None, pid=os.getpid())
            waiter, gid = await _queued(lender, info=_info(clone, pytest_args=["tests", "-q"]))

            queues = await runner.remote_queues()
            row = queues[0]["queue"][0]
            assert row["borrowable"] and row["local_workspace"] == "proj", row
            assert row["recipe"]["kind"] == "merge"

            run_doc = await runner.start("lender", gid)
            assert (await waiter)["borrowed"]["machine"] == "borrower"
            answer = None
            for _ in range(60):
                answer = await lender.await_entry(gid, wait=1)
                if not answer.get("pending"):
                    break
            assert answer and answer.get("remote"), answer
            remote = answer["remote"]
            assert remote["exit_code"] == 0
            assert "2 passed" in remote["output"]
            assert remote["recipe"] == "merge"
            assert remote["junit"] and "testsuite" in remote["junit"]
            await _until(lambda: runner.runs()[0]["status"] == "passed")
            # The checkout is gone and the borrower's window is free.
            assert not (tmp_path / "borrows" / "window-borrow" / run_doc["id"]).exists()
            assert own.status()["holders"] == []
            assert "token" not in runner.runs()[0]
            assert borrower.exists()
        finally:
            await runner.close()
            await client.close()
            await manager.shutdown_all()

    async def _names():
        return ["lender", "borrower"]

    asyncio.run(run())


def test_borrower_stops_and_cleans_up_when_the_lender_revokes(
        lender_clone, tmp_path, monkeypatch):
    clone, bare, url = lender_clone
    _commit(clone, "tests/test_slow.py", "import time\n\ndef test_slow():\n    time.sleep(60)\n")
    _git(clone, "push", "-q", "origin", "HEAD:master")
    _git(clone, "fetch", "-q", "origin")
    window_borrow._recipes.clear()
    monkeypatch.setattr(window_borrow, "heartbeat_interval", lambda: 0.1)
    _borrower_clone(tmp_path, bare, url)

    async def run():
        lender = _make(tmp_path, "lender.json")
        own = _make(tmp_path, "borrower.json")
        client, manager, transport = await _lender_app(lender)
        runner = _Runner(own, transport, lambda: "borrower",
                         root=tmp_path / "borrows" / "window-borrow")
        try:
            await lender.acquire("sweep", session=None, pid=os.getpid())
            waiter, gid = await _queued(lender, info=_info(
                clone, pytest_args=["tests/test_slow.py", "-q"]))
            run_doc = await runner.start("lender", gid)
            await waiter
            await _until(lambda: runner.runs()[0]["status"] == "testing")
            # D11: the lender's operator cancels the borrowed request.
            assert lender.cancel(gid)
            await _until(lambda: runner.runs()[0]["status"] == "aborted", 30)
            assert "unknown" in runner.runs()[0]["error"]
            assert not (tmp_path / "borrows" / "window-borrow" / run_doc["id"]).exists()
            assert own.status()["holders"] == []
        finally:
            await runner.close()
            await client.close()
            await manager.shutdown_all()

    asyncio.run(run())


def test_borrower_restart_gives_its_open_borrows_back(lender_clone, tmp_path):
    clone, _bare, _url = lender_clone
    window_borrow._recipes.clear()

    async def run():
        lender = _make(tmp_path, "lender.json")
        client, manager, transport = await _lender_app(lender)
        root = tmp_path / "borrows" / "window-borrow"
        try:
            await lender.acquire("sweep", session=None, pid=os.getpid())
            waiter, gid = await _queued(lender, info=_info(clone))
            answer = await transport("lender", "/peer/window/borrow",
                                     {"grant_id": gid, "machine": "borrower"})
            assert answer["ok"]
            await waiter
            # What the previous borrower process left on disk.
            runner = _Runner(_make(tmp_path, "b1.json"), transport, lambda: "borrower",
                             root=root)
            runner._runs.append({"id": "dead", "machine": "lender", "grant_id": gid,
                                 "token": answer["token"], "status": "testing"})
            runner._save()
            restarted = _Runner(_make(tmp_path, "b2.json"), transport, lambda: "borrower",
                                root=root)
            await restarted.recover()
            assert restarted.runs()[0]["status"] == "aborted"
            entry = lender.status()["queue"][0]
            assert "borrow" not in entry
            assert entry["borrows"][-1]["end"].startswith("released: the borrower's daemon")
            waiter2 = await lender.await_entry(gid, wait=0.05)
            assert waiter2["pending"] and waiter2["borrowed"] is None
        finally:
            await client.close()
            await manager.shutdown_all()

    asyncio.run(run())



def _tampering(transport, change):
    """A lender whose borrow answer is rewritten by ``change`` -- one that
    does not check what it lends, or a recipe the borrower cannot meet."""
    async def wrapped(machine, path, body):
        answer = await transport(machine, path, body)
        if path == "/peer/window/borrow" and answer.get("ok"):
            change(answer)
        return answer
    return wrapped


def _given_back(lender, gid) -> str:
    entry = lender.status()["queue"][0]
    assert entry["grant_id"] == gid and "borrow" not in entry
    return entry["borrows"][-1]["end"]


def test_borrower_refuses_a_lent_cwd_outside_its_checkout(lender_clone, tmp_path):
    clone, bare, url = lender_clone
    window_borrow._recipes.clear()
    _borrower_clone(tmp_path, bare, url)

    def outside(answer):
        answer["entry"]["borrow_info"]["rel_cwd"] = "../../etc"

    async def run():
        lender = _make(tmp_path, "lender.json")
        client, manager, transport = await _lender_app(lender)
        runner = _Runner(_make(tmp_path, "borrower.json"), _tampering(transport, outside),
                         lambda: "borrower", root=tmp_path / "borrows" / "window-borrow")
        try:
            await lender.acquire("sweep", session=None, pid=os.getpid())
            waiter, gid = await _queued(lender, info=_info(clone))
            # Given back before the waiter's long-poll sees the borrow: the
            # request simply keeps waiting here.
            run_doc = await runner.start("lender", gid)
            assert run_doc["status"] == "failed" and "rel_cwd" in run_doc["error"]
            assert "window_grant" not in runner.runs()[0]  # pytest never started
            assert _given_back(lender, gid).startswith("released: the borrower refused")
            again = await lender.await_entry(gid, wait=0.05)
            assert again["pending"] and again["borrowed"] is None
            waiter.cancel()
        finally:
            await runner.close()
            await client.close()
            await manager.shutdown_all()

    asyncio.run(run())


def test_borrower_refuses_a_lent_cwd_that_a_symlink_leads_outside(
        lender_clone, tmp_path, monkeypatch):
    clone, bare, url = lender_clone
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "test_out.py").write_text("def test_out():\n    assert True\n",
                                         encoding="utf-8")
    try:
        (clone / "tests" / "link_out").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"cannot create a symlink here: {exc}")
    _git(clone, "add", "tests/link_out")
    _git(clone, "commit", "-q", "-m", "link_out")
    _git(clone, "push", "-q", "origin", "HEAD:master")
    _git(clone, "fetch", "-q", "origin")
    window_borrow._recipes.clear()
    monkeypatch.setattr(window_borrow, "heartbeat_interval", lambda: 0.1)
    _borrower_clone(tmp_path, bare, url)

    async def run():
        lender = _make(tmp_path, "lender.json")
        own = _make(tmp_path, "borrower.json")
        client, manager, transport = await _lender_app(lender)
        runner = _Runner(own, transport, lambda: "borrower",
                         root=tmp_path / "borrows" / "window-borrow")
        try:
            await lender.acquire("sweep", session=None, pid=os.getpid())
            # Inside the checkout as text, so vet_borrow lets it through.
            waiter, gid = await _queued(lender, info=_info(
                clone, rel_cwd="tests/link_out", pytest_args=["-q"]))
            run_doc = await runner.start("lender", gid)
            await _until(lambda: runner.runs()[0]["status"] == "failed", 30)
            done = runner.runs()[0]
            assert "'tests/link_out' resolves outside the checkout" in done["error"]
            assert "window_grant" not in done  # refused before setup and the window
            assert "resolves outside the checkout" in _given_back(lender, gid)
            again = await lender.await_entry(gid, wait=0.05)
            assert again["pending"] and not again.get("remote")
            await _until(lambda: not (
                tmp_path / "borrows" / "window-borrow" / run_doc["id"]).exists(), 30)
            assert own.status()["holders"] == []
            waiter.cancel()
        finally:
            await runner.close()
            await client.close()
            await manager.shutdown_all()

    asyncio.run(run())


def test_borrower_checks_the_cwd_again_after_setup(lender_clone, tmp_path, monkeypatch):
    clone, bare, url = lender_clone
    outside = tmp_path / "outside"
    outside.mkdir()
    probe = tmp_path / "probe"
    try:
        probe.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"cannot create a symlink here: {exc}")
    (clone / "tests" / "late").mkdir()
    _commit(clone, "tests/late/test_late.py", "def test_late():\n    assert True\n")
    _git(clone, "push", "-q", "origin", "HEAD:master")
    _git(clone, "fetch", "-q", "origin")
    window_borrow._recipes.clear()
    monkeypatch.setattr(window_borrow, "heartbeat_interval", lambda: 0.1)
    _borrower_clone(tmp_path, bare, url)

    class _Relinking(_Runner):
        # A setup that swaps the lent cwd for a link out of the checkout.
        setup_command = staticmethod(lambda checkout: [sys.executable, "-c", (
            "import os, shutil; shutil.rmtree('tests/late'); "
            f"os.symlink({str(outside)!r}, 'tests/late', target_is_directory=True)")])

    async def run():
        lender = _make(tmp_path, "lender.json")
        own = _make(tmp_path, "borrower.json")
        client, manager, transport = await _lender_app(lender)
        runner = _Relinking(own, transport, lambda: "borrower",
                            root=tmp_path / "borrows" / "window-borrow")
        try:
            await lender.acquire("sweep", session=None, pid=os.getpid())
            waiter, gid = await _queued(lender, info=_info(
                clone, rel_cwd="tests/late", pytest_args=["-q"]))
            await runner.start("lender", gid)
            await _until(lambda: runner.runs()[0]["status"] == "failed", 30)
            done = runner.runs()[0]
            assert "'tests/late' resolves outside the checkout" in done["error"]
            assert "window_grant" not in done  # checked again before the window
            assert "resolves outside the checkout" in _given_back(lender, gid)
            again = await lender.await_entry(gid, wait=0.05)
            assert again["pending"] and not again.get("remote")
            await _until(lambda: own.status()["holders"] == [], 30)
            waiter.cancel()
        finally:
            await runner.close()
            await client.close()
            await manager.shutdown_all()

    asyncio.run(run())


def test_arg_paths_reads_positional_ids_and_path_option_values():
    args = ["-q", "-n", "4", "tests/a.py::test_x", "--ignore", "tests/b",
            "--deselect=tests/c.py::test_y", "--tb", "short", "-k", "slow",
            "--ignore-glob=tests/d*", "-x"]
    assert window_borrow._arg_paths(args) == [
        ("tests/a.py::test_x", "tests/a.py"), ("tests/b", "tests/b"),
        ("--deselect=tests/c.py::test_y", "tests/c.py"),
        ("--ignore-glob=tests/d*", "tests/d*")]


def test_link_scan_finds_a_link_out_but_not_one_inside_or_in_a_venv(tmp_path):
    top, outside = tmp_path / "co", tmp_path / "outside"
    (top / "tests" / "real").mkdir(parents=True)
    outside.mkdir()
    venv = top / ".venv" / "bin"
    venv.mkdir(parents=True)
    (top / ".venv" / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    try:
        (venv / "python").symlink_to(outside / "python3")
        (top / "tests" / "link_in").symlink_to(top / "tests" / "real",
                                               target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"cannot create a symlink here: {exc}")
    top = top.resolve()
    assert window_borrow._link_scan(top) == (None, [top / ".venv"])
    (top / "tests" / "real" / "conftest.py").symlink_to(outside / "conftest.py")
    assert window_borrow._link_scan(top)[0] == "tests/real/conftest.py"


@pytest.mark.parametrize("rel_cwd, pytest_args, error", [
    ("", ["-q", "tests/fake"], "pytest argument 'tests/fake' is inside the virtualenv 'tests/fake'"),
    ("", ["-q", "tests/fake/test_x.py::t"],
     "pytest argument 'tests/fake/test_x.py::t' is inside the virtualenv 'tests/fake'"),
    ("tests/fake", ["-q"], "rel_cwd 'tests/fake' is inside the virtualenv 'tests/fake'"),
    ("tests", ["-q", "fake"], "pytest argument 'fake' is inside the virtualenv 'tests/fake'"),
    # pytest skips a virtualenv it walks into, so a parent is fine.
    ("", ["-q", "tests"], None),
    ("tests", ["-q"], None),
])
def test_lent_cwd_refuses_pointing_pytest_into_a_skipped_venv(tmp_path, rel_cwd, pytest_args, error):
    # A committed pyvenv.cfg makes the scan skip the directory, and pytest
    # collects a virtualenv it is pointed at, links and all (claunch-vur61.1).
    top, outside = tmp_path / "co", tmp_path / "outside"
    (top / "tests" / "fake").mkdir(parents=True)
    outside.mkdir()
    (top / "tests" / "fake" / "pyvenv.cfg").write_text("", encoding="utf-8")
    try:
        (top / "tests" / "fake" / "link_out").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"cannot create a symlink here: {exc}")
    info = {"rel_cwd": rel_cwd, "pytest_args": pytest_args}
    if error is None:
        assert window_borrow._lent_cwd(top, info) == top / rel_cwd
    else:
        with pytest.raises(RuntimeError, match=re.escape(error)):
            window_borrow._lent_cwd(top, info)


@pytest.mark.parametrize("pytest_args, error", [
    (["-q", "tests/link_out/test_out.py"],
     "pytest argument 'tests/link_out/test_out.py' resolves outside the checkout"),
    (["-q", "--ignore", "tests/link_out"],
     "pytest argument 'tests/link_out' resolves outside the checkout"),
    # pytest walks a directory argument, or the cwd, into the link.
    (["-q", "tests"], "symlink 'tests/link_out' in the checkout resolves outside it"),
    (["-q"], "symlink 'tests/link_out' in the checkout resolves outside it"),
])
def test_borrower_refuses_a_path_argument_that_a_symlink_leads_outside(
        lender_clone, tmp_path, monkeypatch, pytest_args, error):
    clone, bare, url = lender_clone
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "test_out.py").write_text("def test_out():\n    assert True\n",
                                         encoding="utf-8")
    try:
        (clone / "tests" / "link_out").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"cannot create a symlink here: {exc}")
    _git(clone, "add", "tests/link_out")
    _git(clone, "commit", "-q", "-m", "link_out")
    _git(clone, "push", "-q", "origin", "HEAD:master")
    _git(clone, "fetch", "-q", "origin")
    window_borrow._recipes.clear()
    monkeypatch.setattr(window_borrow, "heartbeat_interval", lambda: 0.1)
    _borrower_clone(tmp_path, bare, url)
    marker = tmp_path / "setup-ran"

    class _Recording(_Runner):
        setup_command = staticmethod(lambda checkout: [
            sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"])

    async def run():
        lender = _make(tmp_path, "lender.json")
        own = _make(tmp_path, "borrower.json")
        client, manager, transport = await _lender_app(lender)
        runner = _Recording(own, transport, lambda: "borrower",
                            root=tmp_path / "borrows" / "window-borrow")
        try:
            await lender.acquire("sweep", session=None, pid=os.getpid())
            # Inside the checkout as text, so vet_borrow lets it through.
            waiter, gid = await _queued(lender, info=_info(
                clone, pytest_args=pytest_args))
            run_doc = await runner.start("lender", gid)
            await _until(lambda: runner.runs()[0]["status"] == "failed", 30)
            done = runner.runs()[0]
            assert error in done["error"]
            assert "window_grant" not in done
            assert not marker.exists()  # refused right after the checkout
            assert "resolves outside" in _given_back(lender, gid)
            again = await lender.await_entry(gid, wait=0.05)
            assert again["pending"] and not again.get("remote")
            await _until(lambda: not (
                tmp_path / "borrows" / "window-borrow" / run_doc["id"]).exists(), 30)
            assert own.status()["holders"] == []
            waiter.cancel()
        finally:
            await runner.close()
            await client.close()
            await manager.shutdown_all()

    asyncio.run(run())


def test_borrower_refuses_an_argument_into_a_committed_venv(lender_clone, tmp_path, monkeypatch):
    # The reviewer's form: a committed pyvenv.cfg makes the link scan skip
    # tests/fake, and `pytest tests/fake` collects through its link out.
    clone, bare, url = lender_clone
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "test_out.py").write_text("def test_out():\n    assert True\n",
                                         encoding="utf-8")
    (clone / "tests" / "fake").mkdir()
    try:
        (clone / "tests" / "fake" / "link_out").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"cannot create a symlink here: {exc}")
    _commit(clone, "tests/fake/pyvenv.cfg", "")
    _git(clone, "add", "tests/fake/link_out")
    _git(clone, "commit", "-q", "-m", "fake venv")
    _git(clone, "push", "-q", "origin", "HEAD:master")
    _git(clone, "fetch", "-q", "origin")
    window_borrow._recipes.clear()
    monkeypatch.setattr(window_borrow, "heartbeat_interval", lambda: 0.1)
    _borrower_clone(tmp_path, bare, url)

    async def run():
        lender = _make(tmp_path, "lender.json")
        own = _make(tmp_path, "borrower.json")
        client, manager, transport = await _lender_app(lender)
        runner = _Runner(own, transport, lambda: "borrower",
                         root=tmp_path / "borrows" / "window-borrow")
        try:
            await lender.acquire("sweep", session=None, pid=os.getpid())
            waiter, gid = await _queued(lender, info=_info(
                clone, pytest_args=["-q", "tests/fake"]))
            await runner.start("lender", gid)
            await _until(lambda: runner.runs()[0]["status"] == "failed", 30)
            done = runner.runs()[0]
            assert ("pytest argument 'tests/fake' is inside the virtualenv 'tests/fake'"
                    in done["error"])
            assert "window_grant" not in done
            assert "inside the virtualenv" in _given_back(lender, gid)
            again = await lender.await_entry(gid, wait=0.05)
            assert again["pending"] and not again.get("remote")
            await _until(lambda: own.status()["holders"] == [], 30)
            waiter.cancel()
        finally:
            await runner.close()
            await client.close()
            await manager.shutdown_all()

    asyncio.run(run())


def test_borrower_checks_the_path_arguments_again_after_setup(
        lender_clone, tmp_path, monkeypatch):
    clone, bare, url = lender_clone
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "test_out.py").write_text("def test_out():\n    assert True\n",
                                         encoding="utf-8")
    probe = tmp_path / "probe"
    try:
        probe.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"cannot create a symlink here: {exc}")
    (clone / "tests" / "late").mkdir()
    _commit(clone, "tests/late/test_out.py", "def test_out():\n    assert True\n")
    _git(clone, "push", "-q", "origin", "HEAD:master")
    _git(clone, "fetch", "-q", "origin")
    window_borrow._recipes.clear()
    monkeypatch.setattr(window_borrow, "heartbeat_interval", lambda: 0.1)
    _borrower_clone(tmp_path, bare, url)

    class _Relinking(_Runner):
        # A setup that swaps the named test directory for a link out of the checkout.
        setup_command = staticmethod(lambda checkout: [sys.executable, "-c", (
            "import os, shutil; shutil.rmtree('tests/late'); "
            f"os.symlink({str(outside)!r}, 'tests/late', target_is_directory=True)")])

    async def run():
        lender = _make(tmp_path, "lender.json")
        own = _make(tmp_path, "borrower.json")
        client, manager, transport = await _lender_app(lender)
        runner = _Relinking(own, transport, lambda: "borrower",
                            root=tmp_path / "borrows" / "window-borrow")
        try:
            await lender.acquire("sweep", session=None, pid=os.getpid())
            waiter, gid = await _queued(lender, info=_info(
                clone, pytest_args=["-q", "tests/late/test_out.py"]))
            await runner.start("lender", gid)
            await _until(lambda: runner.runs()[0]["status"] == "failed", 30)
            done = runner.runs()[0]
            assert ("pytest argument 'tests/late/test_out.py' resolves outside the checkout"
                    in done["error"])
            assert "window_grant" not in done
            assert "resolves outside the checkout" in _given_back(lender, gid)
            again = await lender.await_entry(gid, wait=0.05)
            assert again["pending"] and not again.get("remote")
            await _until(lambda: own.status()["holders"] == [], 30)
            waiter.cancel()
        finally:
            await runner.close()
            await client.close()
            await manager.shutdown_all()

    asyncio.run(run())


def test_borrower_refuses_a_link_out_that_setup_makes(lender_clone, tmp_path, monkeypatch):
    clone, bare, url = lender_clone
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "test_out.py").write_text("def test_out():\n    assert True\n",
                                         encoding="utf-8")
    probe = tmp_path / "probe"
    try:
        probe.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"cannot create a symlink here: {exc}")
    window_borrow._recipes.clear()
    monkeypatch.setattr(window_borrow, "heartbeat_interval", lambda: 0.1)
    _borrower_clone(tmp_path, bare, url)

    class _Linking(_Runner):
        # A setup that adds a link out under the directory pytest will walk.
        setup_command = staticmethod(lambda checkout: [sys.executable, "-c", (
            f"import os; os.symlink({str(outside)!r}, 'tests/made_out', "
            "target_is_directory=True)")])

    async def run():
        lender = _make(tmp_path, "lender.json")
        own = _make(tmp_path, "borrower.json")
        client, manager, transport = await _lender_app(lender)
        runner = _Linking(own, transport, lambda: "borrower",
                          root=tmp_path / "borrows" / "window-borrow")
        try:
            await lender.acquire("sweep", session=None, pid=os.getpid())
            waiter, gid = await _queued(lender, info=_info(clone, pytest_args=["-q", "tests"]))
            await runner.start("lender", gid)
            await _until(lambda: runner.runs()[0]["status"] == "failed", 30)
            done = runner.runs()[0]
            assert "symlink 'tests/made_out' in the checkout resolves outside it" in done["error"]
            assert "window_grant" not in done  # checked again before the window
            assert "resolves outside" in _given_back(lender, gid)
            again = await lender.await_entry(gid, wait=0.05)
            assert again["pending"] and not again.get("remote")
            await _until(lambda: own.status()["holders"] == [], 30)
            waiter.cancel()
        finally:
            await runner.close()
            await client.close()
            await manager.shutdown_all()

    asyncio.run(run())


def test_borrower_fails_when_the_rebuilt_tree_is_not_the_lenders(
        lender_clone, tmp_path, monkeypatch):
    clone, bare, url = lender_clone
    window_borrow._recipes.clear()
    monkeypatch.setattr(window_borrow, "heartbeat_interval", lambda: 0.1)
    _borrower_clone(tmp_path, bare, url)

    def other_tree(answer):
        answer["recipe"] = {**answer["recipe"], "tree": "0" * 40}

    async def run():
        lender = _make(tmp_path, "lender.json")
        own = _make(tmp_path, "borrower.json")
        client, manager, transport = await _lender_app(lender)
        runner = _Runner(own, _tampering(transport, other_tree), lambda: "borrower",
                         root=tmp_path / "borrows" / "window-borrow")
        try:
            await lender.acquire("sweep", session=None, pid=os.getpid())
            waiter, gid = await _queued(lender, info=_info(clone))
            run_doc = await runner.start("lender", gid)
            await _until(lambda: runner.runs()[0]["status"] == "failed", 30)
            done = runner.runs()[0]
            assert "is not the lender's 000000000000" in done["error"]
            assert "window_grant" not in done  # no test ran on the wrong tree
            # No result reached the lender: the request waits there again.
            assert "the rebuilt tree" in _given_back(lender, gid)
            again = await lender.await_entry(gid, wait=0.05)
            assert again["pending"] and not again.get("remote")
            await _until(lambda: not (
                tmp_path / "borrows" / "window-borrow" / run_doc["id"]).exists(), 30)
            assert own.status()["holders"] == []
            waiter.cancel()
        finally:
            await runner.close()
            await client.close()
            await manager.shutdown_all()

    asyncio.run(run())

def test_borrow_route_refuses_a_session(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from claude_launcher.daemon import actors
    from claude_launcher.daemon.api import build_app
    from claude_launcher.daemon.manager import SessionManager

    async def run():
        manager = SessionManager(idle_threshold=0.5, scrollback=100, restore_default=False)
        app = build_app(manager, "secret", started_at=time.monotonic(),
                        window=_make(tmp_path))
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.post(
                "/api/window/borrow", json={"machine": "x", "grant_id": "y"},
                headers={"Authorization": "Bearer secret", actors.HEADER: "agent:s1"})
            assert resp.status == 403
        finally:
            await client.close()
            await manager.shutdown_all()

    asyncio.run(run())
