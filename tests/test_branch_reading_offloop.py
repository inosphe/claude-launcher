"""A session's git branch is never read on the daemon's event loop.

A daemon was once found frozen: every session unreachable, every socket
unanswered, the shutdown it was sent unread — because the rail's poll had
called ``_branch_of``, which ran ``git rev-parse`` inline, and that git never
came back. One hung read of one checkout stopped the whole daemon.

So two things hold here. The branch reading happens off the loop (the loop
answers from cache and never waits on a subprocess), and the git underneath
it is bounded (a read that outstays its welcome is a failure, not a wait).
"""

from __future__ import annotations

import asyncio
import subprocess
import threading
import time

from claude_launcher import worktree as worktree_mod
from claude_launcher.daemon import api as api_mod


def _clear_cache() -> None:
    api_mod._branch_cache.clear()
    api_mod._branch_reading.clear()


def test_a_hung_git_does_not_hold_the_event_loop(tmp_path, monkeypatch):
    _clear_cache()
    started = threading.Event()
    release = threading.Event()

    def _slow(cwd):
        started.set()
        release.wait(10)
        return "wedged-branch"

    monkeypatch.setattr(worktree_mod, "current_branch", lambda p: _slow(str(p)))
    monkeypatch.setattr(api_mod.worktree_mod, "current_branch", lambda p: _slow(str(p)))

    async def scenario():
        began = time.monotonic()
        # The reading is in flight and has answered nothing yet: the poll gets
        # the empty branch it can render, immediately.
        assert api_mod._branch_of(str(tmp_path)) == ""
        assert time.monotonic() - began < 2.0
        assert started.wait(5), "the reading never started"
        # And the loop is still the loop: it schedules, sleeps and wakes while
        # the git it asked for is still hanging.
        await asyncio.sleep(0)
        assert api_mod._branch_of(str(tmp_path)) == ""
        release.set()
        for _ in range(100):
            await asyncio.sleep(0.05)
            if api_mod._branch_cache.get(str(tmp_path), ("", 0))[0]:
                break
        # Once it lands, the next asker gets it without a git of their own.
        assert api_mod._branch_of(str(tmp_path)) == "wedged-branch"

    asyncio.run(scenario())
    _clear_cache()


def test_one_reading_per_directory_no_matter_how_often_it_is_polled(
    tmp_path, monkeypatch
):
    _clear_cache()
    calls = []
    release = threading.Event()

    def _slow(_path):
        calls.append(1)
        release.wait(10)
        return "b"

    monkeypatch.setattr(api_mod.worktree_mod, "current_branch", _slow)

    async def scenario():
        for _ in range(20):
            assert api_mod._branch_of(str(tmp_path)) == ""
        await asyncio.sleep(0.2)
        release.set()

    asyncio.run(scenario())
    assert len(calls) == 1, f"a git per poll: {len(calls)}"
    _clear_cache()


def test_a_read_only_git_that_outstays_its_timeout_is_a_failure_not_a_wait(
    monkeypatch, tmp_path
):
    def _timeout(*a, **kw):
        assert kw.get("timeout"), "a reading git must carry a timeout"
        raise subprocess.TimeoutExpired(cmd="git", timeout=kw["timeout"])

    monkeypatch.setattr(subprocess, "run", _timeout)
    done = worktree_mod._git(["rev-parse", "HEAD"], cwd=str(tmp_path), timeout=1)
    assert done.returncode != 0
    assert "did not finish" in done.stderr
    # And the caller above it turns that into "no branch", not an exception.
    assert worktree_mod.current_branch(tmp_path) == ""
