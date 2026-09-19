"""A finished run's journal must not ride the detail panel's poll whole.

``GET /api/sessions/{name}/meta`` carries the session's cflow slot, and a run
that has finished carries its journal with it (``engine._done_payload``) --
every completed step with the summary and the details filed for it. The panel
draws the newest forty lines, out of the step name and the event, and a count.
It draws neither the summary nor the details: the prose it shows reaches it as
``reports``.

So the size arrives with the last step. A 63-step run made the journal 135KB
of a 166KB answer, polled every five seconds, parsed in the browser on the
thread that also handles keystrokes (s586, 2026-09-20). The agent-facing
payload still carries the run's record in full; this is the web path alone.
"""

from __future__ import annotations

import asyncio
import sys
import time

from aiohttp.test_utils import TestClient, TestServer

from claude_launcher import store
from claude_launcher.daemon import api as api_mod
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager

BEARER = {"Authorization": "Bearer sekrit"}

CHILD = (
    "import sys\n"
    "print('READY')\n"
    "for line in sys.stdin:\n"
    "    print('echo:' + line.strip())\n"
)


def _entry(n: int) -> dict:
    """A run slot shaped as a finished run's is, with ``n`` journal rows."""
    return {
        "cwd": "/somewhere",
        "scope": "s1",
        "status": "done",
        "journal": [
            {
                "step": f"step{i}",
                "summary": f"summary {i} " + "x" * 400,
                "details": f"details {i} " + "y" * 4000,
            }
            for i in range(n)
        ],
    }


def test_the_journal_is_cut_to_what_the_panel_draws():
    clipped = api_mod._clip_journal(_entry(200))
    assert len(clipped["journal"]) == api_mod._META_JOURNAL_TAIL
    # The newest end, which is the end the panel reverses into its list.
    assert clipped["journal"][-1]["step"] == "step199"
    # Said whole, so "newest 40 of 200" is still a true sentence.
    assert clipped["journal_total"] == 200
    for row in clipped["journal"]:
        assert "summary" not in row
        assert "details" not in row
        assert row["step"].startswith("step")


def test_a_short_journal_is_left_alone_but_still_counted():
    clipped = api_mod._clip_journal(_entry(3))
    assert len(clipped["journal"]) == 3
    assert clipped["journal_total"] == 3


def test_a_run_with_no_journal_is_untouched():
    """A run still going has no journal at all, and must not grow one."""
    entry = {"cwd": "/somewhere", "scope": "s1", "status": "running"}
    assert api_mod._clip_journal(entry) == entry
    assert "journal_total" not in api_mod._clip_journal(entry)


def test_the_cut_is_what_reaches_the_detail_panel(home, tmp_path, monkeypatch):
    """Over real HTTP, through the handler that assembles the answer."""
    store.update(
        lambda doc: doc.update(
            {"harnesses": {"py": {"command": [sys.executable, "-u", "-c", CHILD]}}}
        )
    )
    monkeypatch.setattr(
        api_mod, "_cflow_entry", lambda *a, **k: _entry(120),
    )

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        app = build_app(mgr, "sekrit", started_at=time.monotonic())
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            cwd = tmp_path / "work"
            cwd.mkdir()
            mgr.create(SessionDef(name="s1", harness="py", cwd=str(cwd)))
            resp = await client.get("/api/sessions/s1/meta", headers=BEARER)
            assert resp.status == 200
            cflow = (await resp.json())["cflow"]
            assert len(cflow["journal"]) == api_mod._META_JOURNAL_TAIL
            assert cflow["journal_total"] == 120
            assert "details" not in cflow["journal"][0]
        finally:
            await client.close()
            await mgr.shutdown_all()

    asyncio.run(run())
