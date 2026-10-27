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

import functools
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

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


def test_a_model_change_survives_a_long_run_before_it(tmp_path, ledger):
    # What is kept per provider is where the answer *changed*, not one entry
    # per call: a hundred calls on m1 followed by a hundred on m2 is one
    # mark, and stays one however long those runs grow (a call-count tail
    # would have forgotten the change once it fell off the end).
    directory = tmp_path / "metering"
    write_ledger(directory, [
        record("s1", hour=9, minute=i % 60, upstream=FIREWORKS, model="m1")
        for i in range(100)
    ] + [
        record("s1", hour=10, minute=i % 60, upstream=FIREWORKS, model="m2")
        for i in range(100)
    ])
    ledger.refresh(NOW)
    row = ledger.window("2026-10-21",
                        statsboard.window_start_epoch("2026-10-21", KST))["s1"]
    assert [(s["kind"], s["from_model"], s["to_model"]) for s in row["switches"]] == [
        ("model", "m1", "m2"),
    ]
    assert row["model"] == "m2"


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


def test_a_reborrow_becomes_a_profile_switch_on_the_row(tmp_path, ledger):
    """The ledger cannot see a reborrow, so its event history answers.

    A session moved from its own credentials onto another profile's reaches a
    different upstream only sometimes; when both profiles answer through the
    same one, the ledger's provider/model sequence never changes and the switch
    list used to say nothing happened. The daemon's own event history records
    the change either way.
    """
    write_ledger(tmp_path / "metering", [record("moved", hour=9)])
    ledger.refresh(NOW)
    at = "2026-10-21T10:30:00+09:00"
    item = meta("moved")
    item["profile_switches"] = statsboard.profile_switches([
        {"kind": "borrow", "at": at,
         "details": {"previous": None, "current": "kimi",
                     "previous_null": False, "null_token": False}},
    ])
    payload = statsboard.overview(window="today", now=NOW, zone=KST,
                                  sessions=[item], ledger=ledger, labels={})
    row = payload["sessions"][0]
    assert [s["kind"] for s in row["switches"]] == ["profile"]
    switch = row["switches"][0]
    assert (switch["from"], switch["to"]) == ("own", "kimi")
    assert switch["at"] == at
    # A display-only axis: the figures are the ledger's, unchanged.
    assert row["total"] == 115 and row["source"] == "ledger"


def test_a_borrow_event_that_changes_nothing_is_not_a_switch():
    """Re-recording the same credentials is not a change."""
    rows = [
        {"kind": "borrow", "at": "2026-10-21T10:30:00+09:00",
         "details": {"previous": "kimi", "current": "kimi",
                     "previous_null": False, "null_token": False}},
        {"kind": "kill", "at": "2026-10-21T10:31:00+09:00", "details": {}},
        # Clearing a borrow is a change: the session is back on its own token.
        {"kind": "borrow", "at": "2026-10-21T11:00:00+09:00",
         "details": {"previous": "kimi", "current": None,
                     "previous_null": False, "null_token": False}},
        {"kind": "borrow", "at": "2026-10-21T12:00:00+09:00",
         "details": {"previous": None, "current": None,
                     "previous_null": False, "null_token": True}},
    ]
    switches = statsboard.profile_switches(rows)
    assert [(s["from"], s["to"]) for s in switches] == [
        ("kimi", "own"),        # cleared the borrow
        ("own", "null"),        # moved onto no token at all
    ]
    assert all(s["kind"] == "profile" for s in switches)


def test_a_profile_switch_outside_the_window_is_left_out(tmp_path, ledger):
    write_ledger(tmp_path / "metering", [record("moved", hour=9)])
    ledger.refresh(NOW)
    item = meta("moved")
    item["profile_switches"] = statsboard.profile_switches([
        # yesterday, KST -- outside `today`
        {"kind": "borrow", "at": "2026-10-20T10:30:00+09:00",
         "details": {"previous": None, "current": "kimi",
                     "previous_null": False, "null_token": False}},
    ])
    payload = statsboard.overview(window="today", now=NOW, zone=KST,
                                  sessions=[item], ledger=ledger, labels={})
    assert payload["sessions"][0]["switches"] == []


def test_the_event_store_hands_a_reborrow_to_the_meta(tmp_path):
    """``session_meta`` reads the store it is given, by name.

    The three tests above hand ``profile_switches``' output straight to the
    meta, so they would stay green if the store and the meta were never
    connected. This one writes through the real ``Events`` store a reborrow
    would leave behind (``manager.reborrow``) and asks ``session_meta`` for
    the row: drop the ``events`` argument at the call site and the switch
    disappears here.
    """
    from claude_launcher.daemon.session_events import Events

    session = SimpleNamespace(
        sdef=SimpleNamespace(name="reb", harness="claude", profile="ds4"),
        created_at="2026-10-21T00:00:00+00:00",
        exited=True,
        status=lambda: "exited",
        last_activity_at=lambda: "2026-10-21T02:00:00+00:00",
    )
    events = Events(tmp_path)
    events.record(session, "borrow", "세션 인증 프로파일 변경",
                  previous=None, current="kimi",
                  previous_null=False, null_token=False)

    wired = statsboard.session_meta(session, events)
    assert [(s["from"], s["to"], s["kind"]) for s in wired["profile_switches"]] == [
        ("own", "kimi", "profile"),
    ]
    # Without the store the meta has nothing to report -- which is what makes
    # the call site's argument load-bearing rather than decorative.
    assert statsboard.session_meta(session)["profile_switches"] == []


def test_the_two_ended_boxes_are_independent(tmp_path, ledger):
    """``include_killed`` and ``include_archived`` each admit their own
    category, and the counts say how many of each the table holds out."""
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

    def names(**flags):
        payload = statsboard.overview(window="today", now=NOW, zone=KST,
                                      sessions=sessions, ledger=ledger,
                                      labels={}, **flags)
        return (sorted(r["session"] for r in payload["sessions"]), payload)

    rows, default = names()
    assert rows == ["live"]
    assert default["counts"]["killed_excluded"] == 1
    assert default["counts"]["archived_excluded"] == 1
    assert default["include_killed"] is False and default["include_archived"] is False

    rows, killed_only = names(include_killed=True)
    assert rows == ["killed", "live"]
    assert killed_only["counts"]["killed_excluded"] == 0
    assert killed_only["counts"]["archived_excluded"] == 1
    assert killed_only["include_killed"] is True
    assert killed_only["include_archived"] is False

    rows, archived_only = names(include_archived=True)
    assert rows == ["archived", "live"]
    assert archived_only["counts"]["killed_excluded"] == 1
    assert archived_only["counts"]["archived_excluded"] == 0

    rows, both = names(include_killed=True, include_archived=True)
    assert rows == ["archived", "killed", "live"]
    assert both["counts"]["ended_excluded"] == 0
    # ``include_ended`` stays the "both" spelling the older page reads.
    assert both["include_ended"] is True


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


# --------------------------------------------------------------------------- #
# the sessions the ledger cannot speak for
# --------------------------------------------------------------------------- #
def reading(*buckets, harness="claude", partial=False):
    """A transcript reading shaped like ``sessionstats.summarize``'s."""
    rows = []
    for start, fresh, cache_read, output, requests in buckets:
        rows.append({
            "start": start, "input": fresh, "cache_read": cache_read,
            "cache_write": 0, "output": output,
            "total": fresh + cache_read + output, "requests": requests,
            "inputs": {},
        })
    return {"session": "oauth", "harness": harness, "available": True,
            "totals": {}, "buckets": rows, "partial": partial}


def transcripts(**by_name):
    """A Transcripts whose reader answers from a dict of name -> reading."""
    return statsboard.Transcripts(zone=KST,
                                  reader=lambda sdef: by_name.get(sdef.name))


def test_a_session_the_ledger_cannot_speak_for_is_read_from_its_transcript(tmp_path, ledger):
    # An OAuth route writes no metering line. The row used to say "none";
    # now its window figure comes from the session's own transcript, marked
    # with the harness whose reader produced it.
    ledger.refresh(NOW)
    reader = transcripts(oauth=reading(
        ("2026-10-21T09:00:00+09:00", 100, 900, 50, 4)))
    sdef = SimpleNamespace(name="oauth", harness="claude")
    payload = statsboard.overview(
        window="today", now=NOW, zone=KST,
        sessions=[{**meta("oauth", harness="claude"), "sdef": sdef}],
        ledger=ledger, labels={}, transcripts=reader)
    row = payload["sessions"][0]
    assert row["source"] == "transcript"
    assert row["harness"] == "claude"
    assert row["total"] == 1050
    assert (row["input"], row["cache_read"], row["output"]) == (100, 900, 50)
    assert row["requests"] == 4
    assert row["days"] == {"2026-10-21": 1050}
    assert payload["counts"]["transcript"] == 1
    assert payload["counts"]["unmetered"] == 0
    # A transcript reading is bucketed by day, so a bucket's `start` is the
    # day boundary -- midnight for a day bucket. The row leaves the activity
    # columns empty rather than reporting that boundary as a time something
    # happened; the ledger rows are the ones that carry real stamps.
    assert row["first_at"] is None and row["last_at"] is None


def test_a_transcript_row_does_not_report_a_day_boundary_as_activity(tmp_path, ledger):
    """``transcript_window`` returns no activity stamps of its own.

    Its only time is a bucket's ``start``, which for a day bucket is midnight
    -- a boundary, not a moment at which anything happened. Reporting it
    would print a time on the row that nothing measured, so the window
    leaves the columns empty and the ledger rows keep carrying real stamps.
    """
    part = statsboard.transcript_window(
        reading(("2026-10-21T00:00:00+09:00", 10, 0, 0, 1), harness="claude"),
        ["2026-10-21"])
    assert part["total"] == 10
    assert part["first"] is None and part["last"] is None


def test_a_transcript_row_counts_only_the_buckets_inside_the_window(tmp_path, ledger):
    ledger.refresh(NOW)
    reader = transcripts(oauth=reading(
        ("2026-10-20T09:00:00+09:00", 100, 0, 0, 1),     # yesterday
        ("2026-10-21T09:00:00+09:00", 200, 0, 0, 2)))    # today
    sdef = SimpleNamespace(name="oauth", harness="claude")
    today = statsboard.overview(
        window="today", now=NOW, zone=KST,
        sessions=[{**meta("oauth"), "sdef": sdef}],
        ledger=ledger, labels={}, transcripts=reader)
    assert today["sessions"][0]["total"] == 200
    week = statsboard.overview(
        window="week", now=NOW, zone=KST,
        sessions=[{**meta("oauth"), "sdef": sdef}],
        ledger=ledger, labels={}, transcripts=reader)
    assert week["sessions"][0]["total"] == 300


def test_a_transcript_with_nothing_in_the_window_stays_a_dash(tmp_path, ledger):
    ledger.refresh(NOW)
    reader = transcripts(oauth=reading(
        ("2026-10-19T09:00:00+09:00", 100, 0, 0, 1)))
    sdef = SimpleNamespace(name="oauth", harness="claude")
    payload = statsboard.overview(
        window="today", now=NOW, zone=KST,
        sessions=[{**meta("oauth"), "sdef": sdef}],
        ledger=ledger, labels={}, transcripts=reader)
    row = payload["sessions"][0]
    assert row["source"] == "none" and row["total"] == 0
    assert payload["counts"]["unmetered"] == 1


def test_a_metered_session_is_not_read_from_its_transcript(tmp_path, ledger):
    # The ledger is the better fact for the shim's own traffic; a reading the
    # two could disagree about is never taken.
    write_ledger(tmp_path / "metering", [record("a", hour=9)])
    ledger.refresh(NOW)
    calls = []

    def reader(sdef):
        calls.append(sdef.name)
        return reading(("2026-10-21T09:00:00+09:00", 999, 0, 0, 9))

    payload = statsboard.overview(
        window="today", now=NOW, zone=KST,
        sessions=[{**meta("a"), "sdef": SimpleNamespace(name="a", harness="claude")}],
        ledger=ledger, labels={},
        transcripts=statsboard.Transcripts(zone=KST, reader=reader))
    assert payload["sessions"][0]["source"] == "ledger"
    assert payload["sessions"][0]["total"] == 115
    assert calls == []


def test_a_reading_is_reused_for_a_short_while(tmp_path, ledger):
    # Reading every OAuth session's transcript on every poll would put an
    # 11 GB read behind a table refresh; the reading is kept, and the refresh
    # after it costs nothing.
    ledger.refresh(NOW)
    seen = []

    def reader(sdef):
        seen.append(sdef.name)
        return reading(("2026-10-21T09:00:00+09:00", 100, 0, 0, 1))

    reader_ = statsboard.Transcripts(zone=KST, reader=reader, ttl=60)
    sessions = [{**meta("oauth"), "sdef": SimpleNamespace(name="oauth", harness="claude")}]
    for moment in (NOW, NOW + 10):
        statsboard.overview(window="today", now=moment, zone=KST,
                            sessions=sessions, ledger=ledger, labels={},
                            transcripts=reader_)
    assert seen == ["oauth"]
    reader_.forget("oauth")
    statsboard.overview(window="today", now=NOW + 20, zone=KST,
                        sessions=sessions, ledger=ledger, labels={},
                        transcripts=reader_)
    assert seen == ["oauth", "oauth"]


def test_a_partial_reading_is_reported_as_partial(tmp_path, ledger):
    ledger.refresh(NOW)
    reader = transcripts(oauth=reading(
        ("2026-10-21T09:00:00+09:00", 100, 0, 0, 1), partial=True))
    payload = statsboard.overview(
        window="today", now=NOW, zone=KST,
        sessions=[{**meta("oauth"), "sdef": SimpleNamespace(name="oauth", harness="claude")}],
        ledger=ledger, labels={}, transcripts=reader)
    assert payload["partial"] is True
    assert payload["sessions"][0]["source"] == "transcript"


def test_the_transcript_reader_asks_sessionstats_for_the_panel_s_own_reading(monkeypatch):
    # The detail panel reads /api/sessions/<name>/stats, which is
    # sessionstats.read_located. The overview goes through the same module
    # with the same unit and zone, so the two cannot define a token apart.
    asked = {}

    def fake(sdef, unit="day", zone=None):
        asked.update({"name": sdef.name, "unit": unit, "zone": zone})
        return {"session": sdef.name, "available": True, "buckets": []}

    from claude_launcher.daemon import sessionstats as real

    monkeypatch.setattr(real, "for_session", fake)
    sdef = SimpleNamespace(name="oauth", harness="claude")
    out = statsboard._read_transcript(sdef, KST)
    assert out["session"] == "oauth"
    assert asked == {"name": "oauth", "unit": statsboard.UNIT, "zone": KST}


def test_the_reading_component_names_are_sessionstats_own():
    # transcript_window sums what sessionstats writes; a renamed component on
    # either side would silently report zero for it.
    from claude_launcher.daemon import sessionstats as mod

    assert set(mod.COMPONENTS) <= set(statsboard.transcript_window(
        reading(("2026-10-21T09:00:00+09:00", 1, 2, 3, 1)), ["2026-10-21"]))


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
def test_the_endpoint_answers_the_overview_and_refuses_an_unknown_window(home, tmp_path,
                                                                  monkeypatch):
    import asyncio
    import functools
    import time

    from aiohttp.test_utils import TestClient, TestServer

    from claude_launcher import config
    from claude_launcher.daemon.api import build_app
    from claude_launcher.daemon.harness import SessionDef
    from claude_launcher.daemon.manager import SessionManager
    from claude_launcher.daemon.mesh import MeshManager
    from claude_launcher.daemon.session import DeadSession

    # The fixture records sit on 2026-10-21 KST; pin the endpoint's clock and
    # zone there so the `today` window does not move with the machine's.
    monkeypatch.setattr(statsboard, "overview",
                        functools.partial(statsboard.overview, now=NOW, zone=KST))

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
        app["stats_ledger"] = statsboard.Ledger(zone=KST)
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
            assert body["counts"]["killed_excluded"] == 2
            assert body["counts"]["archived_excluded"] == 0
            assert body["include_killed"] is False
            assert body["include_archived"] is False

            # The two boxes are separate flags, so asking for archived alone
            # does not open the killed partition (both fixture rows are killed).
            resp = await client.get(
                "/api/stats/overview?window=today&killed=0&archived=1",
                headers=bearer)
            body = await resp.json()
            assert body["sessions"] == []
            assert body["include_archived"] is True

            resp = await client.get(
                "/api/stats/overview?window=today&killed=1&archived=0",
                headers=bearer)
            body = await resp.json()
            assert sorted(r["session"] for r in body["sessions"]) == ["gone", "live"]
            assert body["counts"]["killed_excluded"] == 0

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


def test_the_endpoint_wiring_hands_the_overview_the_transcript_source(tmp_path, ledger,
                                                                      monkeypatch):
    """The route builds the payload through ``_stats_overview``; the
    transcript reader is one per daemon like the ledger, so the OAuth rows
    reuse the reading instead of re-reading on every poll."""
    from claude_launcher.daemon import api

    # _stats_overview does not pin the clock (the daemon wants the real one),
    # so the fixture records' day is pinned here the way the endpoint test does.
    monkeypatch.setattr(statsboard, "overview",
                        functools.partial(statsboard.overview, now=NOW, zone=KST))
    accessed = "2026-10-21T03:00:00+09:00"

    class Double:
        exited = False
        created_at = "2026-10-20T00:00:00+09:00"

        def __init__(self, name):
            self.sdef = SimpleNamespace(name=name, harness="codex")

        def status(self):
            return "busy"

        def last_activity_at(self):
            return accessed

    reader = transcripts(oauth=reading(
        ("2026-10-21T09:00:00+09:00", 400, 600, 20, 3), harness="codex"))
    ledger.refresh(NOW)
    payload = api._stats_overview(
        SimpleNamespace(list=lambda: [Double("oauth")]),
        "today", False, ledger, reader)
    row = payload["sessions"][0]
    assert row["source"] == "transcript" and row["total"] == 1020
    assert row["harness"] == "codex"


def test_the_endpoint_wiring_hands_each_meta_its_event_history(tmp_path, ledger,
                                                               monkeypatch):
    """``_stats_overview`` passes ``manager.events`` into ``session_meta``.

    The unit tests above can hand a meta its switches directly; only this
    call site connects the daemon's event store to the row the page draws.
    A manager with no ``events`` attribute (an older double, or a daemon
    whose store failed to open) must still build a payload -- that fallback
    is the other half of what this pins.
    """
    from claude_launcher.daemon import api
    from claude_launcher.daemon.session_events import Events

    monkeypatch.setattr(statsboard, "overview",
                        functools.partial(statsboard.overview, now=NOW, zone=KST))
    accessed = "2026-10-21T03:00:00+09:00"

    class Double:
        exited = False
        created_at = "2026-10-20T00:00:00+00:00"

        def __init__(self, name):
            self.sdef = SimpleNamespace(name=name, harness="claude",
                                        profile="ds4")

        def status(self):
            return "busy"

        def last_activity_at(self):
            return accessed

    write_ledger(tmp_path / "metering", [record("wired", hour=9)])
    ledger.refresh(NOW)
    session = Double("wired")
    events = Events(tmp_path)
    events.record(session, "borrow", "세션 인증 프로파일 변경",
                  previous=None, current="kimi",
                  previous_null=False, null_token=False)

    payload = api._stats_overview(
        SimpleNamespace(list=lambda: [session], events=events),
        "today", False, ledger)
    row = payload["sessions"][0]
    assert row["session"] == "wired"
    assert [(s["kind"], s["from"], s["to"]) for s in row["switches"]] == [
        ("profile", "own", "kimi"),
    ]

    # A manager double without the store must not fail the route.
    payload = api._stats_overview(
        SimpleNamespace(list=lambda: [Double("wired")]), "today", False, ledger)
    assert payload["sessions"][0]["switches"] == []


# --------------------------------------------------------------------------- #
# the fleet's time series (claunch-x15q5)
# --------------------------------------------------------------------------- #
def test_the_series_keeps_every_component_apart_per_bucket(tmp_path, ledger):
    write_ledger(tmp_path / "metering", [
        record("s1", hour=9, minute=0, fresh=10, cached=100, written=1, output=5),
        record("s1", hour=9, minute=2, fresh=20, cached=200, written=2, output=7),
        record("s1", hour=9, minute=40, fresh=1, cached=2, written=0, output=3),
    ])
    ledger.refresh(NOW)
    out = ledger.series("24h", sessions=[meta("s1")], now=NOW)
    assert out["bucket_s"] == statsboard.SERIES_BIN_S
    # Nine o'clock holds the first two; nine-forty is a bin of its own.
    at9 = [p for p in out["points"] if datetime.fromtimestamp(p["t"], KST).hour == 9]
    assert at9[0]["input"] == 30 and at9[0]["cache_read"] == 300
    assert at9[0]["cache_write"] == 3 and at9[0]["output"] == 12
    assert at9[0]["total"] == 30 + 300 + 3 + 12
    assert at9[0]["requests"] == 2
    assert sum(p["requests"] for p in out["points"]) == 3
    # The totals are the sums of the points, so the area drawn from them is
    # the window's total and not an average of it.
    assert out["totals"]["total"] == 10 + 100 + 1 + 5 + 20 + 200 + 2 + 7 + 1 + 2 + 0 + 3


def test_the_series_window_leaves_older_records_out(tmp_path, ledger):
    write_ledger(tmp_path / "metering", [
        record("s1", day=20, hour=9, fresh=5),
        record("s1", day=21, hour=9, fresh=7),
    ])
    ledger.refresh(NOW)
    day = ledger.series("24h", sessions=[meta("s1")], now=NOW)
    assert day["totals"]["input"] == 7
    assert day["start"] <= day["points"][0]["t"] <= NOW
    week = ledger.series("7d", sessions=[meta("s1")], now=NOW)
    assert week["totals"]["input"] == 12


def test_the_series_bucket_widens_so_one_answer_is_one_screen(tmp_path, ledger):
    write_ledger(tmp_path / "metering", [record("s1", hour=9)])
    ledger.refresh(NOW)
    widths = []
    for window in ("24h", "7d", "30d"):
        out = ledger.series(window, sessions=[meta("s1")], now=NOW)
        assert len(out["points"]) <= statsboard.SERIES_MAX_POINTS, window
        assert out["bucket_s"] >= statsboard.SERIES_BIN_S
        assert out["bucket_s"] % statsboard.SERIES_BIN_S == 0
        assert out["start"] % out["bucket_s"] == 0, "buckets stay on one grid"
        widths.append(out["bucket_s"])
    # A wider window needs a wider bucket to keep to one screen.
    assert widths == sorted(widths) and widths[0] < widths[-1], widths
    assert ledger.series("24h", sessions=[meta("s1")], now=NOW)["bucket_s"] \
        == statsboard.SERIES_BIN_S


def test_the_series_leaves_out_what_the_rows_leave_out(tmp_path, ledger):
    write_ledger(tmp_path / "metering", [
        record("live", hour=9, fresh=1),
        record("dead", hour=9, fresh=100),
        record("put", hour=9, fresh=1000),
    ])
    ledger.refresh(NOW)
    metas = [meta("live"), meta("dead", category="killed"),
             meta("put", category="archived")]
    out = ledger.series("24h", sessions=metas, now=NOW)
    assert out["totals"]["input"] == 1
    killed = ledger.series("24h", sessions=metas, now=NOW, include_killed=True)
    assert killed["totals"]["input"] == 101
    both = ledger.series("24h", sessions=metas, now=NOW,
                         include_killed=True, include_archived=True)
    assert both["totals"]["input"] == 1101
    assert both["sessions"] == 3


def test_the_series_narrows_to_one_project(tmp_path, ledger):
    write_ledger(tmp_path / "metering", [
        record("a", hour=9, fresh=3), record("b", hour=9, fresh=5)])
    ledger.refresh(NOW)
    other = dict(meta("b"), project="elsewhere")
    out = ledger.series("24h", sessions=[meta("a"), other], now=NOW,
                        project="claunch")
    assert out["totals"]["input"] == 3
    assert out["sessions"] == 1


def test_the_series_says_unmetered_rather_than_drawing_zero(tmp_path, ledger):
    write_ledger(tmp_path / "metering", [record("a", hour=9, fresh=3)])
    ledger.refresh(NOW)
    out = ledger.series("24h", sessions=[meta("a"), meta("oauth")], now=NOW)
    assert out["totals"]["input"] == 3
    assert out["sessions"] == 1
    assert out["unmetered"] == 1


def test_the_series_carries_each_session_s_totals(tmp_path, ledger):
    """The rows on the Chart page print a session's own figure; it comes from
    this same pass, so a row and the chart under it cannot disagree
    (claunch-mlrga). A session the ledger cannot speak for is absent from the
    map, not present with zeros."""
    write_ledger(tmp_path / "metering", [
        record("s1", hour=9, minute=0, fresh=10, cached=100, written=1, output=5),
        record("s1", hour=9, minute=2, fresh=20, cached=200, written=2, output=7),
        record("s2", hour=9, minute=5, fresh=3, cached=30, written=0, output=4),
    ])
    ledger.refresh(NOW)
    out = ledger.series("24h", sessions=[meta("s1"), meta("s2"), meta("oauth")],
                        now=NOW)
    assert set(out["by_session"]) == {"s1", "s2"}, "an unmetered row is absent"
    s1 = out["by_session"]["s1"]
    assert (s1["input"], s1["cache_read"], s1["cache_write"], s1["output"]) == (
        30, 300, 3, 12)
    assert s1["requests"] == 2
    assert s1["total"] == 30 + 300 + 3 + 12
    s2 = out["by_session"]["s2"]
    assert (s2["total"], s2["requests"]) == (3 + 30 + 0 + 4, 1)
    # The rows sum to the fleet total the chart's area integrates to.
    assert sum(v["total"] for v in out["by_session"].values()) \
        == out["totals"]["total"]
    assert sum(v["requests"] for v in out["by_session"].values()) \
        == out["totals"]["requests"]
    # The map follows the window and the page's own filters, as the points do.
    week = ledger.series("7d", sessions=[meta("s1")], now=NOW)
    assert week["by_session"]["s1"]["total"] == s1["total"]
    killed = ledger.series("24h", sessions=[meta("s1", category="killed")],
                           now=NOW)
    assert killed["by_session"] == {} and killed["unmetered"] == 0
    kept = ledger.series("24h", sessions=[meta("s1", category="killed")],
                         now=NOW, include_killed=True)
    assert kept["by_session"]["s1"]["total"] == s1["total"]
    # A session the ledger has, with nothing inside this window, is a zero
    # rather than an absence: it was measured, and it spent nothing here.
    write_ledger(tmp_path / "metering",
                 [record("old", day=20, hour=9, fresh=1)], name="older.jsonl")
    ledger.refresh(NOW)
    day = ledger.series("24h", sessions=[meta("s1"), meta("old"), meta("oauth")],
                        now=NOW)
    assert "old" in day["by_session"], "a measured session is a zero, not a dash"
    assert day["by_session"]["old"]["total"] == 0
    assert "oauth" not in day["by_session"]
    assert day["unmetered"] == 1


def test_the_series_carries_each_session_s_busy_buckets(tmp_path, ledger):
    """A row draws when its session was spending, on the ledger's
    five-minute grid, so a live bar with no bucket under it reads as idle
    (claunch-58pvy). Only buckets with something in them are listed, in time
    order, and they sum to the row's figure."""
    write_ledger(tmp_path / "metering", [
        record("s1", hour=9, minute=0, fresh=10, cached=0, output=5),
        record("s1", hour=9, minute=2, fresh=20, cached=0, output=7),
        record("s1", hour=11, minute=30, fresh=4, cached=0, output=0),
        record("s2", hour=9, minute=5, fresh=3, cached=0, output=0),
    ])
    ledger.refresh(NOW)
    out = ledger.series("24h", sessions=[meta("s1"), meta("s2")], now=NOW)
    width = out["bucket_s"]
    grid = {p["t"] for p in out["points"]}
    s1 = out["by_session"]["s1"]["points"]
    assert [t for t, _, _ in s1] == sorted(t for t, _, _ in s1)
    assert out["points_bucket_s"] == 300
    assert all(t in grid for t, _, _ in s1), "on a 24h window the grids are one"
    assert all(total or requests for _, total, requests in s1)
    # 09:00 and 09:02 share a five-minute bucket; 11:30 is its own, and the
    # two hours between them are absent -- the idle stretch.
    assert width == 300
    assert [(total, requests) for _, total, requests in s1] == [(42, 2), (4, 1)]
    assert s1[1][0] - s1[0][0] == 2 * 3600 + 30 * 60
    assert sum(v[1] for v in s1) == out["by_session"]["s1"]["total"]
    assert sum(v[2] for v in s1) == out["by_session"]["s1"]["requests"]
    assert [(v[1], v[2]) for v in out["by_session"]["s2"]["points"]] == [(3, 1)]
    # A wider window coarsens the fleet's buckets but not the row's: one
    # request no longer paints a whole hour busy (claunch-9n7g1).
    week = ledger.series("7d", sessions=[meta("s1")], now=NOW)
    assert week["bucket_s"] > 300 and week["points_bucket_s"] == 300
    assert week["by_session"]["s1"]["points"] == s1
    # A measured session with nothing in the window lists no bucket.
    write_ledger(tmp_path / "metering",
                 [record("old", day=20, hour=9, fresh=1)], name="older.jsonl")
    ledger.refresh(NOW)
    day = ledger.series("24h", sessions=[meta("old")], now=NOW)
    assert day["by_session"]["old"]["points"] == []


def test_the_series_totals_cover_only_their_window(tmp_path, ledger):
    """A record outside the window must not reach the row's figure: the
    column and the chart both speak about the same span."""
    write_ledger(tmp_path / "metering", [
        record("s1", day=20, hour=9, fresh=100),
        record("s1", day=21, hour=9, fresh=7),
    ])
    ledger.refresh(NOW)
    day = ledger.series("24h", sessions=[meta("s1")], now=NOW)
    assert day["by_session"]["s1"]["input"] == 7
    week = ledger.series("7d", sessions=[meta("s1")], now=NOW)
    assert week["by_session"]["s1"]["input"] == 107


def test_the_series_refuses_an_unknown_window(tmp_path, ledger):
    with pytest.raises(ValueError):
        ledger.series("year", sessions=[], now=NOW)


def test_an_empty_series_still_answers_a_window(tmp_path, ledger):
    ledger.refresh(NOW)
    out = ledger.series("7d", sessions=[], now=NOW)
    assert out["totals"]["total"] == 0
    assert out["cache_share"] is None
    assert 1 <= len(out["points"]) <= statsboard.SERIES_MAX_POINTS


def test_the_series_endpoint_answers_and_refuses_an_unknown_window(
        home, tmp_path, monkeypatch):
    import asyncio
    import functools
    import time

    from aiohttp.test_utils import TestClient, TestServer

    from claude_launcher import config
    from claude_launcher.daemon.api import build_app
    from claude_launcher.daemon.harness import SessionDef
    from claude_launcher.daemon.manager import SessionManager
    from claude_launcher.daemon.mesh import MeshManager
    from claude_launcher.daemon.session import DeadSession

    monkeypatch.setattr(statsboard.Ledger, "series",
                        functools.partial(statsboard.Ledger.series, now=NOW))
    records = config.launcher_home() / "metering"
    write_ledger(records, [record("live", hour=9, upstream=FIREWORKS)])
    live = SessionDef(name="live", harness="claude", cwd=str(tmp_path))

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mgr._sessions["live"] = DeadSession(live, exit_code=None)
        app = build_app(mgr, "sekrit", started_at=time.monotonic(),
                        mesh=MeshManager(mgr))
        app["stats_ledger"] = statsboard.Ledger(zone=KST)
        client = TestClient(TestServer(app))
        await client.start_server()
        bearer = {"Authorization": "Bearer sekrit"}
        try:
            resp = await client.get("/api/stats/series?window=24h&killed=1",
                                    headers=bearer)
            body = await resp.json()
            assert resp.status == 200, body
            assert body["window"] == "24h"
            assert body["bucket_s"] == statsboard.SERIES_BIN_S
            # record()'s defaults: 10 fresh + 100 cache read + 5 output.
            assert body["totals"]["total"] == 115
            assert body["totals"]["requests"] == 1
            assert body["sessions"] == 1
            assert body["points"], "a window always has its buckets"

            resp = await client.get("/api/stats/series?window=fortnight",
                                    headers=bearer)
            body = await resp.json()
            assert resp.status == 400
            assert "window must be one of" in body["error"]
        finally:
            await client.close()

    asyncio.run(run())


def test_the_page_and_the_series_agree_on_the_range_keys():
    """The Chart page sends its own range key as `?window`; a key the daemon
    does not know is a 400 the page draws as a failed read. Reading the page's
    own RANGES is what keeps the two lists from drifting (claunch-x15q5)."""
    import re
    from pathlib import Path

    page = (Path(__file__).resolve().parents[1]
            / "src" / "claude_launcher" / "web" / "static" / "chart-page.js")
    text = page.read_text(encoding="utf-8")
    block = text.split("const RANGES = [", 1)[1].split("];", 1)[0]
    keys = re.findall(r'\[\s*"([^"]+)"', block)
    assert keys, "chart-page.js RANGES has no keys"
    assert sorted(keys) == sorted(statsboard.SERIES_WINDOWS), (
        f"the page offers {keys}, the daemon knows "
        f"{sorted(statsboard.SERIES_WINDOWS)}")
