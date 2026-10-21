"""The Stats page's overview: several sessions' usage over one window.

``daemon/statsboard.py`` answers "which sessions spent what over today, the
last seven days or the last thirty" from the metering ledger instead of every
transcript, groups the figures by provider, and leaves killed and archived
sessions out unless they are asked for. These tests cover the window bounds,
the ledger's incremental reading and per-day / per-provider sums, the provider
resolution behind "group by provider", the borrow/provider switch marks, and
the payload's filtering.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from claude_launcher.daemon import statsboard

KST = timezone(timedelta(hours=9))

#: A local noon to measure windows against; every fixture record is placed
#: relative to it so the tests do not read the machine's clock.
NOW = datetime(2026, 10, 21, 12, 0, tzinfo=KST).timestamp()

DOC = {
    "providers": {
        "fireworks": {"api_key": "k", "endpoints": {
            "anthropic": "https://api.fireworks.ai/inference"}},
        "kimi": {"api_key": "k", "endpoints": {
            "anthropic": "https://api.kimi.com/coding/"}},
        "kimi-small": {"api_key": "k", "endpoints": {
            "anthropic": "https://api.kimi.com/coding/"}},
        "claude": {},
    },
}

FIREWORKS = "https://api.fireworks.ai/inference"
KIMI = "https://api.kimi.com/coding/"


def stamp(day: int, hour: int, minute: int = 0, month: int = 10) -> str:
    """A ledger ``ts``: local time with its own offset, as metering writes it."""
    return datetime(2026, month, day, hour, minute, tzinfo=KST).strftime(
        "%Y-%m-%dT%H:%M:%S%z")


def record(session="s1", *, day=21, hour=10, minute=0, upstream=FIREWORKS,
           model="m1", fresh=10, cached=100, written=0, output=5,
           path="/v1/messages", counted=True, month=10) -> dict:
    return {
        "session": session,
        "ts": stamp(day, hour, minute, month=month),
        "upstream": upstream,
        "model": model,
        "input_tokens": fresh,
        "cache_read": cached,
        "cache_write": written,
        "output_tokens": output,
        "path": path,
        "counted": counted,
        "status": 200,
        "fingerprint": "fp",
    }


def write_ledger(directory: Path, records, name="fp.jsonl") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("".join(json.dumps(r) + "\n" for r in records),
                    encoding="utf-8")
    return path


def meta(name, *, category="running", status="busy", profile="ds4:claude",
         borrow="", created="2026-10-20T00:00:00+00:00",
         activity="2026-10-21T02:00:00+00:00", issue="", harness="claude"):
    return {
        "name": name, "harness": harness, "profile": profile, "borrow": borrow,
        "issue": issue, "project": "claunch", "status": status,
        "category": category, "created_at": created,
        "last_activity_at": activity, "exited_at": None, "paused_at": None,
        "held_at": None, "delayed_at": None, "failed_at": None,
        "archived_at": None,
    }


@pytest.fixture
def ledger(tmp_path):
    return statsboard.Ledger(directory=tmp_path / "metering",
                             labels=statsboard.provider_labels(DOC), zone=KST)


# --------------------------------------------------------------------------- #
# windows
# --------------------------------------------------------------------------- #
def test_today_is_the_local_day_it_is_now():
    start, end = statsboard.window_bounds("today", NOW, KST)
    assert datetime.fromtimestamp(start, KST) == datetime(2026, 10, 21, tzinfo=KST)
    assert end == NOW


def test_week_and_month_count_whole_local_days_including_today():
    week = statsboard.window_days("week", NOW, KST)
    month = statsboard.window_days("month", NOW, KST)
    assert week[0] == "2026-10-15" and week[-1] == "2026-10-21" and len(week) == 7
    assert month[0] == "2026-09-22" and len(month) == 30


def test_an_unknown_window_is_refused():
    with pytest.raises(ValueError):
        statsboard.window_bounds("year", NOW, KST)


# --------------------------------------------------------------------------- #
# one record's components
# --------------------------------------------------------------------------- #
def test_anthropic_components_stay_apart():
    parts = statsboard.record_components(record(fresh=10, cached=100, written=7, output=5))
    assert parts == (10, 100, 7, 5)


def test_openai_prompt_already_contains_the_cached_part():
    # The OpenAI wire format counts the prompt whole; summing cache_read again
    # would double the biggest component (metering.prompt_tokens's own rule).
    parts = statsboard.record_components(
        record(fresh=110, cached=100, output=5, path="/v1/chat/completions"))
    assert parts == (10, 100, 0, 5)


def test_an_error_answer_is_not_a_zero_token_request():
    assert statsboard.record_components(record(counted=False)) is None
    no_usage = record()
    no_usage["input_tokens"] = None
    assert statsboard.record_components(no_usage) is None


# --------------------------------------------------------------------------- #
# providers
# --------------------------------------------------------------------------- #
def test_providers_that_share_an_upstream_group_under_one_name():
    labels = statsboard.provider_labels(DOC)
    assert labels[statsboard._upstream_key(FIREWORKS)] == "fireworks"
    assert labels[statsboard._upstream_key(KIMI)] == "kimi"


def test_an_oauth_provider_has_no_upstream_label():
    labels = statsboard.provider_labels(DOC)
    assert "claude" not in labels.values()


def test_the_same_backend_under_two_spellings_is_one_provider():
    labels = statsboard.provider_labels(DOC)
    for url in (FIREWORKS, FIREWORKS + "/", FIREWORKS + "/v1", FIREWORKS + "/v1/"):
        assert statsboard.provider_of({"upstream": url}, labels) == "fireworks"


def test_an_unknown_upstream_is_named_by_its_host():
    assert statsboard.provider_of({"upstream": "http://127.0.0.1:45117/"}, {}) == "127.0.0.1"
    assert statsboard.provider_of({}, {}) == "unknown"


# --------------------------------------------------------------------------- #
# the ledger
# --------------------------------------------------------------------------- #
def test_a_first_read_sums_per_session_day_and_provider(tmp_path, ledger):
    write_ledger(tmp_path / "metering", [
        record("s1", hour=9, fresh=10, cached=100, output=5),
        record("s1", hour=10, fresh=20, cached=200, output=7),
        record("s2", hour=11, upstream=KIMI, fresh=1, cached=2, output=3),
    ])
    ledger.refresh(NOW)
    assert set(ledger.sessions) == {"s1", "s2"}
    sums = ledger.window("2026-10-21", statsboard.window_start_epoch("2026-10-21", KST))
    assert sums["s1"]["total"] == 10 + 100 + 5 + 20 + 200 + 7
    assert sums["s1"]["requests"] == 2
    assert [p["provider"] for p in sums["s1"]["providers"]] == ["fireworks"]
    assert sums["s2"]["providers"][0]["provider"] == "kimi"
    assert sums["s1"]["days"] == {"2026-10-21": 342}


def test_a_second_read_parses_only_what_was_appended(tmp_path, ledger):
    path = write_ledger(tmp_path / "metering", [record("s1", hour=9)])
    ledger.refresh(NOW)
    assert ledger.window("2026-10-21", 0)["s1"]["requests"] == 1
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record("s1", hour=10)) + "\n")
    ledger.refresh(NOW)
    assert ledger.window("2026-10-21", 0)["s1"]["requests"] == 2
    assert ledger.rebuilds == 0


def test_a_half_written_line_waits_for_its_rest(tmp_path, ledger):
    directory = tmp_path / "metering"
    path = write_ledger(directory, [record("s1", hour=9)])
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"session": "s1", "ts": "2026-10-21T10:0')
    ledger.refresh(NOW)
    assert ledger.window("2026-10-21", 0)["s1"]["requests"] == 1
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('0:00+0900", "input_tokens": 1, "counted": true, '
                 '"upstream": "%s"}\n' % FIREWORKS)
    ledger.refresh(NOW)
    assert ledger.window("2026-10-21", 0)["s1"]["requests"] == 2


def test_a_truncated_file_rebuilds_rather_than_keeping_dead_sums(tmp_path, ledger):
    directory = tmp_path / "metering"
    # Two files, so the rebuild is asked a second question: the file the scan
    # had already passed must not drop out of the same answer. Rebuilding
    # only from the truncated file onward returned {s3} here, not {s1, s3}.
    write_ledger(directory, [record("s1", hour=9)], name="a.jsonl")
    path = write_ledger(directory, [record("s2", hour=10), record("s2", hour=11)],
                        name="b.jsonl")
    ledger.refresh(NOW)
    assert set(ledger.window("2026-10-21", 0)) == {"s1", "s2"}
    path.write_text(json.dumps(record("s3", hour=11)) + "\n", encoding="utf-8")
    ledger.refresh(NOW)
    assert set(ledger.window("2026-10-21", 0)) == {"s1", "s3"}
    assert ledger.rebuilds == 1


def test_a_deleted_record_file_takes_its_sums_with_it(tmp_path, ledger):
    directory = tmp_path / "metering"
    kept = write_ledger(directory, [record("s1", hour=9)], name="a.jsonl")
    dropped = write_ledger(directory, [record("s2", hour=10)], name="b.jsonl")
    ledger.refresh(NOW)
    assert set(ledger.window("2026-10-21", 0)) == {"s1", "s2"}
    dropped.unlink()
    ledger.refresh(NOW)
    assert set(ledger.window("2026-10-21", 0)) == {"s1"}
    assert ledger.rebuilds == 1
    assert kept.exists()


def test_a_provider_change_inside_the_window_is_a_switch(tmp_path, ledger):
    write_ledger(tmp_path / "metering", [
        record("s1", hour=9, upstream=FIREWORKS, model="m1"),
        record("s1", hour=10, upstream=KIMI, model="k3"),
        record("s1", hour=11, upstream=KIMI, model="k3"),
    ])
    ledger.refresh(NOW)
    sums = ledger.window("2026-10-21", statsboard.window_start_epoch("2026-10-21", KST))
    switches = sums["s1"]["switches"]
    assert [s["kind"] for s in switches] == ["provider"]
    assert (switches[0]["from"], switches[0]["to"]) == ("fireworks", "kimi")
    assert switches[0]["to_model"] == "k3"


def test_a_model_change_inside_one_provider_is_a_model_switch(tmp_path, ledger):
    write_ledger(tmp_path / "metering", [
        record("s1", hour=9, model="a"),
        record("s1", hour=10, model="b"),
    ])
    ledger.refresh(NOW)
    sums = ledger.window("2026-10-21", statsboard.window_start_epoch("2026-10-21", KST))
    assert [(s["kind"], s["from_model"], s["to_model"])
            for s in sums["s1"]["switches"]] == [("model", "a", "b")]


def test_one_session_split_across_two_files_switches_by_time(tmp_path, ledger):
    # The ledger's files are per fingerprint, and a fingerprint is the
    # upstream: a provider change splits one session's records across files.
    # Reading the files in name order is not reading them in time order, so
    # the switch has to be derived from each record's ts.
    directory = tmp_path / "metering"
    write_ledger(directory, [
        record("s1", hour=9, upstream=FIREWORKS, model="m1"),
        record("s1", hour=11, upstream=FIREWORKS, model="m1"),
    ], name="1111.jsonl")
    write_ledger(directory, [
        record("s1", hour=10, upstream=KIMI, model="k3"),
    ], name="2222.jsonl")
    ledger.refresh(NOW)
    row = ledger.window("2026-10-21",
                        statsboard.window_start_epoch("2026-10-21", KST))["s1"]
    assert [(s["kind"], s["from"], s["to"], s["at"][11:16])
            for s in row["switches"]] == [
        ("provider", "fireworks", "kimi", "10:00"),
        ("provider", "kimi", "fireworks", "11:00"),
    ]
    # The model column is the last call by time, not the last file read.
    assert row["model"] == "m1"


def test_file_name_order_does_not_decide_the_switch_order(tmp_path, ledger):
    # Same records, the kimi file sorts first: the answer must not change.
    directory = tmp_path / "metering"
    write_ledger(directory, [
        record("s1", hour=10, upstream=KIMI, model="k3"),
    ], name="0001.jsonl")
    write_ledger(directory, [
        record("s1", hour=9, upstream=FIREWORKS, model="m1"),
        record("s1", hour=11, upstream=FIREWORKS, model="m1"),
    ], name="9999.jsonl")
    ledger.refresh(NOW)
    row = ledger.window("2026-10-21",
                        statsboard.window_start_epoch("2026-10-21", KST))["s1"]
    assert [(s["from"], s["to"], s["at"][11:16]) for s in row["switches"]] == [
        ("fireworks", "kimi", "10:00"),
        ("kimi", "fireworks", "11:00"),
    ]
    assert row["model"] == "m1"


def test_a_switch_before_the_window_does_not_mark_the_row(tmp_path, ledger):
    write_ledger(tmp_path / "metering", [
        record("s1", day=19, hour=9, upstream=FIREWORKS),
        record("s1", day=19, hour=10, upstream=KIMI),
        record("s1", day=21, hour=9, upstream=KIMI),
    ])
    ledger.refresh(NOW)
    start, _ = statsboard.window_bounds("today", NOW, KST)
    sums = ledger.window("2026-10-21", start)
    assert sums["s1"]["switches"] == []
    assert sums["s1"]["total"] == 115          # today's record alone


def test_days_older_than_the_retention_are_dropped(tmp_path, ledger):
    write_ledger(tmp_path / "metering", [
        record("s1", month=8, day=1, hour=9),
        record("s1", day=21, hour=9),
    ])
    ledger.refresh(NOW)
    assert ledger.earliest_day() == "2026-10-21"
    sums = ledger.window("2026-08-01", 0)
    assert sums["s1"]["requests"] == 1


# --------------------------------------------------------------------------- #
# the overview payload
# --------------------------------------------------------------------------- #
def test_killed_and_archived_sessions_are_left_out_unless_asked_for(tmp_path, ledger):
    write_ledger(tmp_path / "metering", [
        record("live", hour=9),
        record("killed", hour=10),
        record("archived", hour=11),
    ])
    ledger.refresh(NOW)
    sessions = [
        meta("live"),
        meta("killed", category="killed", status="exited",
             activity="2026-10-21T01:00:00+00:00"),
        meta("archived", category="archived", status="exited",
             activity="2026-10-21T01:00:00+00:00"),
    ]
    default = statsboard.overview(window="today", now=NOW, zone=KST,
                                  sessions=sessions, ledger=ledger, labels={})
    assert [r["session"] for r in default["sessions"]] == ["live"]
    assert default["counts"]["ended_excluded"] == 2

    everything = statsboard.overview(window="today", include_ended=True, now=NOW,
                                     zone=KST, sessions=sessions, ledger=ledger,
                                     labels={})
    assert sorted(r["session"] for r in everything["sessions"]) == [
        "archived", "killed", "live"]
    assert everything["counts"]["ended_excluded"] == 0


def test_a_session_the_ledger_cannot_speak_for_is_listed_with_no_figure(tmp_path, ledger):
    write_ledger(tmp_path / "metering", [record("metered", hour=9)])
    ledger.refresh(NOW)
    sessions = [
        meta("metered"),
        meta("oauth", profile="nc:claude", borrow=""),
    ]
    payload = statsboard.overview(window="today", now=NOW, zone=KST,
                                  sessions=sessions, ledger=ledger, labels={})
    rows = {r["session"]: r for r in payload["sessions"]}
    assert rows["oauth"]["source"] == "none"
    assert rows["oauth"]["total"] == 0 and rows["oauth"]["providers"] == []
    assert payload["counts"]["unmetered"] == 1
    # Sorting puts a row with no figure after every row that has one.
    assert payload["sessions"][0]["session"] == "metered"


def test_an_unmetered_session_idle_before_the_window_is_not_a_row(tmp_path, ledger):
    ledger.refresh(NOW)
    old = meta("old", category="hold", status="exited",
               created="2026-10-01T00:00:00+00:00",
               activity="2026-10-04T00:00:00+00:00")
    old["exited_at"] = "2026-10-04T00:00:00+00:00"
    payload = statsboard.overview(window="today", now=NOW, zone=KST,
                                  sessions=[old], ledger=ledger, labels={})
    assert payload["sessions"] == []
    assert payload["counts"]["unmetered"] == 0


def test_provider_totals_sum_every_row_that_used_them(tmp_path, ledger):
    write_ledger(tmp_path / "metering", [
        record("a", hour=9, upstream=FIREWORKS),
        record("b", hour=10, upstream=KIMI),
        record("b", hour=11, upstream=FIREWORKS),
    ])
    ledger.refresh(NOW)
    payload = statsboard.overview(window="today", now=NOW, zone=KST,
                                  sessions=[meta("a"), meta("b")],
                                  ledger=ledger, labels={})
    providers = {p["provider"]: p for p in payload["providers"]}
    assert providers["fireworks"]["sessions"] == 2
    assert providers["fireworks"]["total"] == 115 * 2
    assert providers["kimi"]["sessions"] == 1
    row = next(r for r in payload["sessions"] if r["session"] == "b")
    assert [p["provider"] for p in row["providers"]] == ["fireworks", "kimi"]
    assert row["provider"] == "fireworks"    # the one it spent most on


def test_the_payload_names_when_the_ledger_starts(tmp_path, ledger):
    write_ledger(tmp_path / "metering", [record("a", day=19, hour=9)])
    ledger.refresh(NOW)
    payload = statsboard.overview(window="month", now=NOW, zone=KST,
                                  sessions=[meta("a")], ledger=ledger, labels={})
    assert payload["ledger_since"] == "2026-10-19"
    assert payload["days"][0] == "2026-09-22" and payload["days"][-1] == "2026-10-21"


def test_the_cache_share_names_the_cache_quantity(tmp_path, ledger):
    write_ledger(tmp_path / "metering", [
        record("a", hour=9, fresh=0, cached=900, output=100)])
    ledger.refresh(NOW)
    payload = statsboard.overview(window="today", now=NOW, zone=KST,
                                  sessions=[meta("a")], ledger=ledger, labels={})
    assert payload["totals"]["total"] == 1000
    assert payload["cache_share"] == 0.9


def test_the_window_is_a_closure_of_local_days_not_a_rolling_span(tmp_path, ledger):
    # 00:30 today is inside "today"; 23:30 yesterday is not -- and a session
    # whose only work was last night is not a row at all, metered or not.
    write_ledger(tmp_path / "metering", [
        record("a", day=20, hour=23, minute=30),
        record("b", day=21, hour=0, minute=30),
    ])
    ledger.refresh(NOW)
    idle = meta("a", activity="2026-10-20T10:00:00+00:00")
    payload = statsboard.overview(window="today", now=NOW, zone=KST,
                                  sessions=[idle, meta("b")],
                                  ledger=ledger, labels={})
    assert [r["session"] for r in payload["sessions"]] == ["b"]


# --------------------------------------------------------------------------- #
# the endpoint
# --------------------------------------------------------------------------- #
def test_the_endpoint_answers_the_overview_and_refuses_an_unknown_window(home, tmp_path):
    import asyncio
    import time

    from aiohttp.test_utils import TestClient, TestServer

    from claude_launcher import config
    from claude_launcher.daemon.api import build_app
    from claude_launcher.daemon.harness import SessionDef
    from claude_launcher.daemon.manager import SessionManager
    from claude_launcher.daemon.mesh import MeshManager
    from claude_launcher.daemon.session import DeadSession

    # The ledger the daemon will read: the isolated home's own metering
    # directory (conftest's `home` fixture moved it here).
    records = config.launcher_home() / "metering"
    write_ledger(records, [
        record("live", hour=9, upstream=FIREWORKS),
        record("gone", hour=10, upstream=KIMI),
    ])
    live = SessionDef(name="live", harness="claude", cwd=str(tmp_path))
    gone = SessionDef(name="gone", harness="claude", cwd=str(tmp_path))

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mgr._sessions["live"] = DeadSession(live, exit_code=None)
        mgr._sessions["gone"] = DeadSession(gone, exit_code=143)
        app = build_app(mgr, "sekrit", started_at=time.monotonic(),
                        mesh=MeshManager(mgr))
        client = TestClient(TestServer(app))
        await client.start_server()
        bearer = {"Authorization": "Bearer sekrit"}
        try:
            resp = await client.get("/api/stats/overview?window=today", headers=bearer)
            body = await resp.json()
            assert resp.status == 200
            assert body["window"] == "today"
            # Both records are DeadSessions, so both sit in the killed
            # partition: held out of the default table, and counted so the
            # page can say how many it hid.
            assert body["sessions"] == []
            assert body["counts"]["ended_excluded"] == 2

            resp = await client.get("/api/stats/overview?window=today&ended=1",
                                    headers=bearer)
            body = await resp.json()
            assert sorted(r["session"] for r in body["sessions"]) == ["gone", "live"]
            live_row = next(r for r in body["sessions"] if r["session"] == "live")
            assert live_row["source"] == "ledger"
            # The isolated home declares no providers, so the upstream is
            # named by its host rather than guessed into a provider name.
            assert live_row["provider"] == "api.fireworks.ai"

            for window in ("year", "", "WEEK"):
                resp = await client.get(f"/api/stats/overview?window={window}",
                                        headers=bearer)
                assert resp.status == 400, window
        finally:
            await client.close()
            await mgr.shutdown_all()

    asyncio.run(run())
