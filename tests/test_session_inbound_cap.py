"""The reader thread must not bury the event loop under a flooding session.

On 2026-09-11 fourteen pi job sessions wrote ~800 KiB/s between them. Every
PTY read became one loop callback that also wrote the transcript to disk;
the loop fell behind, its ready queue reached 17,000 callbacks and 4 GB, and
it never returned to accept(). Two things changed: the transcript is written
on the reader thread, and the reader stops posting once the loop is
``INBOUND_MAX`` bytes behind (logging on, telling the owner once).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from claude_launcher import lineage
from claude_launcher import profile as profile_mod
from claude_launcher.daemon import session as session_mod
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.harness import SessionDef


def _session(mgr: SessionManager, tmp_path):
    if not profile_mod.resolve("p").exists():
        lineage.set_harness(profile_mod.create("p"), "claude")
    return mgr.create(SessionDef(name="flood", harness="claude", profile="p",
                                 cwd=str(tmp_path)))


def test_a_flooding_reader_logs_everything_but_posts_only_inbound_max(
    home, tmp_path, monkeypatch
):
    monkeypatch.setattr(session_mod, "INBOUND_MAX", 4096)
    chunk = b"x" * 1024

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        try:
            s = _session(mgr, tmp_path)
            posted = []
            s._loop = SimpleNamespace(call_soon_threadsafe=lambda fn, *a: posted.append((fn, a)))
            notices = []
            monkeypatch.setattr(s, "notify", lambda text, **kw: notices.append(kw) or 0)
            before = s._log_path.stat().st_size

            for _ in range(10):                      # what _read_pump does per read
                s._append_log(chunk)
                s._post_output(chunk)

            outputs = [a[0] for fn, a in posted if fn == s._on_output]
            overflows = [fn for fn, a in posted if fn == s._on_input_overflow]
            assert sum(map(len, outputs)) == 4096     # the cap, not 10 KiB
            assert len(overflows) == 1                # told once per episode
            assert s.inbound_dropped_bytes == 6 * 1024
            assert s._log_path.stat().st_size - before == 10 * 1024   # disk has it all

            # The loop catches up: consume what was posted (the real child
            # process trickles a few bytes through the same path meanwhile,
            # so drain until quiet rather than count to zero).
            while posted:
                fn, a = posted.pop(0)
                fn(*a)
            assert s._inflight < 4096 // 2
            assert not s._inbound_overflowing
            assert s._log_path.stat().st_size - before == 10 * 1024   # _on_output no longer logs
            assert len(notices) >= 1 and notices[0]["level"] == "warn"

            # ...and the reader posts again.
            del posted[:]
            s._post_output(chunk)
            assert s._on_output in [fn for fn, a in posted]
            await s._feeder.drained()
        finally:
            await mgr.shutdown_all()

    asyncio.run(run())
