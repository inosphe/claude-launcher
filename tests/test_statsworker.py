"""The Stats page's reading in a child process, and the compare mode.

``daemon/statsworker.py`` moves transcript parsing off the daemon's GIL into
one long-lived child (claunch-3r94d); ``daemon/statscompare.py`` turns
several sessions' readings into per-figure spread (mean, median, standard
deviation, z-score, rank) and one shared timeline. The tests cover: the
child answers what an in-process read answers; a dead or stuck child is
replaced and the call reports itself unavailable (so the API reads in a
thread); a reading that raises is an error, not a fallback; the statistics
by hand; and both endpoints end to end with a real child.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

import pytest

from claude_launcher.daemon import ctxsize, sessionstats, statscompare, statsworker, tokenusage
from claude_launcher.daemon.harness import SessionDef

from test_sessionstats import (CID, KST, OPENING, REMINDER, at, mesh_block, reply,
                               source, user, write)


@pytest.fixture(autouse=True)
def _fresh():
    ctxsize.forget()
    tokenusage.forget()
    yield
    tokenusage.forget()
    ctxsize.forget()


OFFSET = 9 * 3600


def _transcript(tmp_path, name="a.jsonl", *, requests=1, read=100):
    lines = [user(OPENING, at(0)), reply("o", at(0, 1), read=read, out=10)]
    for i in range(1, requests):
        lines += [user(f"do step {i}", at(i)), reply(f"r{i}", at(i, 1), read=read, out=10)]
    lines += [user(REMINDER, at(23)), user(mesh_block("s9"), at(23, 5))]
    return write(tmp_path / name, *lines)


def _head(name):
    return {"session": name, "harness": "claude"}


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# the child process
# --------------------------------------------------------------------------- #

def test_the_child_answers_what_an_in_process_read_answers(tmp_path):
    path = _transcript(tmp_path, requests=3)
    request = {"op": "read", "head": _head("s1"), "path": str(path),
               "unit": "hour", "offset": OFFSET}
    inline = json.loads(json.dumps(statsworker.handle(request)))

    async def run():
        worker = statsworker.StatsWorker()
        try:
            got = await worker.call(request)
            again = await worker.call(request)
            return got, again, worker.starts, worker.pid
        finally:
            await worker.close()

    got, again, starts, pid = _run(run())
    assert got == inline == again
    assert got["available"] and got["totals"]["requests"] == 3
    assert starts == 1 and pid is not None    # one child, kept between calls


def test_a_dead_child_is_replaced_on_the_next_call(tmp_path):
    path = _transcript(tmp_path)
    request = {"op": "read", "head": _head("s1"), "path": str(path), "unit": "day"}

    async def run():
        worker = statsworker.StatsWorker()
        try:
            await worker.call(request)
            first = worker.pid
            worker._proc.kill()
            await worker._proc.wait()
            got = await worker.call(request)    # a known exit: started anew
            return first, worker.pid, worker.starts, worker.fallbacks, got
        finally:
            await worker.close()

    first, second, starts, fallbacks, got = _run(run())
    assert got["available"]
    assert first != second and starts == 2 and fallbacks == 0


def test_a_child_that_exits_mid_request_makes_the_call_unavailable():
    # Reads the request, then exits without answering.
    quitter = [sys.executable, "-c", "import sys; sys.stdin.readline()"]

    async def run():
        worker = statsworker.StatsWorker(argv=quitter)
        with pytest.raises(statsworker.WorkerUnavailable, match="exited"):
            await worker.call({"op": "ping"})
        return worker.pid, worker.fallbacks

    assert _run(run()) == (None, 1)


def test_a_child_that_gives_no_answer_is_killed_after_the_timeout():
    silent = [sys.executable, "-c", "import time; time.sleep(60)"]

    async def run():
        worker = statsworker.StatsWorker(argv=silent, timeout=0.5)
        started = time.monotonic()
        with pytest.raises(statsworker.WorkerUnavailable, match="no answer"):
            await worker.call({"op": "ping"})
        took = time.monotonic() - started
        return took, worker.pid

    took, pid = _run(run())
    assert took < 10 and pid is None


def test_a_child_that_cannot_start_is_not_retried_at_once():
    async def run():
        worker = statsworker.StatsWorker(argv=["no-such-program-3r94d"], retry_after=60)
        for _ in range(2):
            with pytest.raises(statsworker.WorkerUnavailable):
                await worker.call({"op": "ping"})
        return worker.starts, worker.fallbacks

    assert _run(run()) == (0, 2)


def test_a_reading_that_raises_is_an_error_and_the_child_lives_on(tmp_path):
    async def run():
        worker = statsworker.StatsWorker()
        try:
            with pytest.raises(statsworker.WorkerError, match="unit"):
                await worker.call({"op": "read", "head": _head("s1"),
                                   "path": str(tmp_path / "x.jsonl"), "unit": "month"})
            pong = await worker.call({"op": "ping"})
            return pong, worker.starts
        finally:
            await worker.close()

    assert _run(run()) == ("pong", 1)


def test_the_child_runs_this_package():
    env = statsworker.child_env()
    root = env["PYTHONPATH"].split(";" if sys.platform == "win32" else ":")[0]
    import claude_launcher
    assert claude_launcher.__file__.startswith(root)


# --------------------------------------------------------------------------- #
# the statistics
# --------------------------------------------------------------------------- #

def test_describe_by_hand():
    got = statscompare.describe([1.0, 2.0, 3.0, 4.0, None])
    assert got["n"] == 4 and got["mean"] == 2.5 and got["median"] == 2.5
    assert got["min"] == 1.0 and got["max"] == 4.0
    sd = (sum((v - 2.5) ** 2 for v in (1, 2, 3, 4)) / 3) ** 0.5    # sample sd
    assert got["stdev"] == pytest.approx(sd)
    assert got["z"][0] == pytest.approx(-1.5 / sd) and got["z"][4] is None
    assert got["rank"] == [4, 3, 2, 1, None]


def test_describe_ties_and_small_samples():
    tie = statscompare.describe([5.0, 5.0, 1.0])
    assert tie["rank"] == [1, 1, 3] and tie["median"] == 5.0
    one = statscompare.describe([7.0])
    assert one["stdev"] is None and one["z"] == [None] and one["rank"] == [1]
    flat = statscompare.describe([2.0, 2.0])
    assert flat["stdev"] == 0 and flat["z"] == [None, None]
    empty = statscompare.describe([None])
    assert empty["n"] == 0 and empty["mean"] is None


def _reading(tmp_path, name, **kw):
    path = _transcript(tmp_path, f"{name}.jsonl", **kw)
    return sessionstats.read_located(_head(name), path, "day", KST)


def test_compare_ranks_sessions_on_each_figure(tmp_path):
    small = _reading(tmp_path, "small", requests=1)
    large = _reading(tmp_path, "large", requests=4, read=1000)
    missing = {"session": "gone", "harness": "claude", "available": False,
               "reason": "no transcript found"}
    got = statscompare.compare([small, missing, large], "day")
    assert got["compared"] == ["small", "large"]
    assert [s["available"] for s in got["sessions"]] == [True, False, True]
    assert got["sessions"][1]["reason"] == "no transcript found"
    metric = {m["key"]: m for m in got["metrics"]}
    req = metric["requests"]
    assert req["values"] == [1, 4] and req["mean"] == 2.5 and req["rank"] == [2, 1]
    assert req["z"][1] > 0 > req["z"][0]
    tok = metric["tokens"]
    assert tok["values"] == [small["totals"]["total"], large["totals"]["total"]]
    # Shares come from each session's own sources.
    share = metric["daemon_messages_share"]["values"][0]
    assert share == source(small, "daemon")["share"]["messages"]
    machine = metric["machine_messages_share"]["values"][0]
    assert machine == pytest.approx(share + source(small, "mesh")["share"]["messages"])
    assert {s["session"] for s in got["shares"]} == {"small", "large"}


def test_the_timeline_shares_one_axis_and_marks_what_a_reading_cut_off():
    def reading(name, since, starts, totals):
        return {"session": name, "available": True, "since": since,
                "buckets": [{"start": s, "total": t, "requests": 1 if t else 0}
                            for s, t in zip(starts, totals)]}

    d = ["2026-09-2{}T00:00:00+09:00".format(i) for i in range(4)]
    a = reading("a", "2026-09-19T20:00:00+00:00", d[0:2], [10, 0])   # began in d[0]
    b = reading("b", "2026-09-10T00:00:00+00:00", d[2:4], [5, 7])    # capped before d[2]
    got = statscompare.timeline([a, b], "day")
    assert got["starts"] == d
    assert got["series"][0]["tokens"] == [10, 0, 0, 0]
    assert got["series"][1]["tokens"] == [None, None, 5, 7]


# --------------------------------------------------------------------------- #
# the endpoints, with a real child
# --------------------------------------------------------------------------- #

def _session(tmp_path, name, *lines):
    from claude_launcher import transcripts

    cwd = tmp_path / name
    cwd.mkdir(exist_ok=True)
    cid = CID[:-2] + f"{len(name):02d}"
    pdir = transcripts.project_dir(tmp_path / ".claude-config", str(cwd))
    write(pdir / f"{cid}.jsonl", *lines)
    return SessionDef(name=name, harness="claude", cwd=str(cwd), conversation_id=cid)


def test_the_endpoints_read_in_the_worker_and_compare(home, tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from claude_launcher.daemon.api import build_app
    from claude_launcher.daemon.manager import SessionManager
    from claude_launcher.daemon.mesh import MeshManager
    from claude_launcher.daemon.session import DeadSession

    one = _session(tmp_path, "s1", user(OPENING, at(0)), reply("a", at(0, 1), read=50, out=5))
    two = _session(tmp_path, "s22", user(OPENING, at(0)), reply("b", at(0, 1), read=500, out=5),
                   user("more", at(1)), reply("c", at(1, 1), read=500, out=5))
    bearer = {"Authorization": "Bearer sekrit"}

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mgr._sessions["s1"] = DeadSession(one, exit_code=0)
        mgr._sessions["s22"] = DeadSession(two, exit_code=0)
        app = build_app(mgr, "sekrit", started_at=time.monotonic(), mesh=MeshManager(mgr))
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.get("/api/sessions/s1/stats?unit=hour", headers=bearer)
            body = await resp.json()
            assert resp.status == 200 and body["reader"] == "worker"
            assert body["totals"]["total"] == 55
            pid = app["stats_worker"].pid
            assert pid is not None

            resp = await client.get("/api/stats/compare?sessions=s1,s22,s1&unit=day",
                                    headers=bearer)
            body = await resp.json()
            assert resp.status == 200 and body["reader"] == "worker"
            assert body["compared"] == ["s1", "s22"]           # duplicates dropped
            req = next(m for m in body["metrics"] if m["key"] == "requests")
            assert req["values"] == [1, 2] and req["rank"] == [2, 1]
            assert app["stats_worker"].pid == pid              # the same child

            for query, status in (("sessions=&unit=day", 400),
                                  ("sessions=s1&unit=month", 400),
                                  ("sessions=" + ",".join(f"x{i}" for i in range(13)), 400),
                                  ("sessions=s1,nobody", 400)):
                resp = await client.get("/api/stats/compare?" + query, headers=bearer)
                assert resp.status == status, query

            # No child: the same answer, read in the daemon.
            await app["stats_worker"].close()
            app["stats_worker"] = statsworker.StatsWorker(argv=["no-such-program-3r94d"])
            resp = await client.get("/api/sessions/s1/stats?unit=hour", headers=bearer)
            body = await resp.json()
            assert resp.status == 200 and body["reader"] == "thread"
            assert body["totals"]["total"] == 55
        finally:
            await client.close()
            await mgr.shutdown_all()

    _run(run())
