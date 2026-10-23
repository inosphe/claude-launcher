"""Session faults: harness messages that stop a session for account reasons.

The catalogue is data (``session_faults.yaml``); every ``sample`` in it is
matched here against its declared kind, so a pattern that drifts away from
the message it was collected from fails the suite instead of silently
missing live faults. The detector is read from the bottom of the rendered
screen, skips diff and tool-call rows, and keeps a fault until its window
(or the reset time the message names) runs out.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher.daemon import api, session_fault
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.session import DeadSession, Session

BEARER = {"Authorization": "Bearer sekrit"}

WEEKLY = "You've hit your weekly limit · resets Oct 26, 9am (Asia/Seoul)"
LOGIN = "●Please run /login · API Error: 401 Invalid bearer token"
KIMI_429 = (
    "API Error: Request rejected (429) · You've reached your weekly (7-day) "
    "usage limit. Your quota will reset when the current 7-day window ends."
)
FIREWORKS_412 = (
    "API Error: 412 Account yusanghyun-uuplax3zp is suspended, possibly due "
    "to reaching the monthly spending limit or failure to pay past invoices. "
    "Please go to https://fireworks.ai/account/billing for more information."
)


class _Screen:
    """The slice of ScreenState the detector reads."""

    def __init__(self, *rows: str) -> None:
        self.rows = list(rows)

    def tail_lines(self, count: int):
        return self.rows[-max(1, int(count)):]


def _detector(harness="claude", catalog=None):
    now = {"t": 1_000_000.0}
    det = session_fault.Detector(
        harness, catalog=catalog, clock=lambda: now["t"]
    )
    return det, now


# ------------------------------------------------------------------ catalogue

def test_the_packaged_catalogue_loads_and_its_samples_match():
    catalog = session_fault.default()
    checked = 0
    for harness, patterns in catalog.harnesses.items():
        for pattern in patterns:
            if not pattern.sample:
                continue
            found = catalog.match(harness, pattern.sample)
            assert found is not None, f"{harness}: sample does not match: {pattern.sample}"
            assert found[0].name == pattern.kind, (
                f"{harness}: {pattern.source!r} matched {found[0].name}, "
                f"declared {pattern.kind}"
            )
            checked += 1
    for pattern in catalog.common:
        if not pattern.sample:
            continue
        found = catalog.match("claude", pattern.sample)
        assert found is not None and found[0].name == pattern.kind
        checked += 1
    # The catalogue is the project's collected evidence; an empty one would
    # make every other test here pass for the wrong reason.
    assert checked >= 6


def test_the_weekly_limit_message_is_a_usage_limit():
    catalog = session_fault.default()
    found = catalog.match("claude", WEEKLY)
    assert found and found[0].name == "usage_limit"


def test_a_provider_quota_sentence_wins_over_the_generic_429():
    """The Kimi 429 also contains "Request rejected (429)"; the quota
    sentence is the more specific reading and must be chosen."""
    found = session_fault.default().match("claude", KIMI_429)
    assert found and found[0].name == "usage_limit"


def test_a_bare_429_is_a_rate_limit():
    found = session_fault.default().match("claude", "API Error: Request rejected (429)")
    assert found and found[0].name == "rate_limit"


def test_common_patterns_apply_to_a_harness_with_no_list_of_its_own():
    found = session_fault.default().match("codex", '"code":"UNAUTHORIZED"')
    assert found and found[0].name == "auth"


def test_a_suspended_account_is_its_own_fault():
    found = session_fault.default().match("claude", FIREWORKS_412)
    assert found and found[0].name == "account_suspended"


def test_a_status_code_with_no_pattern_is_api_status_and_keeps_the_code():
    det, now = _detector()
    det.scan(_Screen("API Error: 503 Service Unavailable"), now=now["t"])
    view = det.view()
    assert view["kind"] == "api_status"
    assert view["code"] == "503"


@pytest.mark.parametrize("document", [
    {},
    {"version": 2, "kinds": {}},
    {"version": 1, "kinds": {}},
    {"version": 1, "kinds": {"auth": {"window": 1}}, "common": [
        {"kind": "nope", "pattern": "x"}]},
    {"version": 1, "kinds": {"auth": {"window": 1}}, "common": [
        {"kind": "auth", "pattern": "("}]},
    {"version": 1, "kinds": {"auth": {"label": "x"}}},
])
def test_a_broken_catalogue_is_refused(document):
    with pytest.raises(session_fault.CatalogError):
        session_fault.Catalog(document)


# ----------------------------------------------------------------- reset time

def test_an_absolute_reset_is_read_in_its_own_zone():
    now = datetime(2026, 10, 23, 12, tzinfo=ZoneInfo("UTC")).timestamp()
    deadline = session_fault.reset_deadline(WEEKLY, now=now)
    assert deadline == datetime(
        2026, 10, 26, 9, tzinfo=ZoneInfo("Asia/Seoul")
    ).timestamp()


def test_a_relative_reset_is_read():
    now = datetime(2026, 10, 23, 12, tzinfo=ZoneInfo("UTC")).timestamp()
    assert session_fault.reset_deadline("usage limit; resets in 3h", now=now) == now + 3 * 3600


def test_an_unreadable_reset_falls_back_to_the_window():
    assert session_fault.reset_deadline("resets Oct 26, 9am (Not/AZone)") is None
    assert session_fault.reset_deadline("resets when the window ends") is None


def test_the_hint_is_kept_as_written():
    assert session_fault.reset_hint(WEEKLY) == "Oct 26, 9am (Asia/Seoul)"


# -------------------------------------------------------------------- detector

def test_the_bottom_of_the_screen_is_what_is_read():
    det, now = _detector()
    det.scan(_Screen("older output", WEEKLY), now=now["t"])
    view = det.view()
    assert view["kind"] == "usage_limit"
    assert WEEKLY in view["message"]
    assert view["active"] is True
    assert view["reset"] == "Oct 26, 9am (Asia/Seoul)"


def test_a_message_wrapped_over_two_rows_still_matches():
    det, now = _detector()
    det.scan(_Screen(
        "API Error: Request rejected (429) · You've reached your weekly (7-day)",
        "usage limit. Your quota will reset when the current 7-day window ends.",
    ), now=now["t"])
    assert det.view()["kind"] == "usage_limit"


def test_a_login_failure_is_an_auth_fault():
    det, now = _detector()
    det.scan(_Screen(LOGIN), now=now["t"])
    assert det.view()["kind"] == "auth"


def test_pi_names_its_own_login():
    det, now = _detector("pi")
    det.scan(_Screen(
        "Use /login to log into a provider via OAuth or API key. See: ..."
    ), now=now["t"])
    assert det.view()["kind"] == "auth"
    assert det.view()["harness"] == "pi"


def test_a_diff_row_does_not_fault_the_session_that_wrote_it():
    det, now = _detector()
    det.scan(_Screen(
        "+    - `You've hit your weekly limit · resets Oct 26, 9am (Asia/Seoul)`",
        "  12 + API Error: 401 Invalid bearer token",
    ), now=now["t"])
    assert det.view() is None


def test_a_tool_call_header_that_quotes_a_message_is_not_a_fault():
    det, now = _detector()
    det.scan(_Screen(
        '⏺ Bash(rg -ao "You\'ve hit your weekly limit" ~/.claude-launcher)'
    ), now=now["t"])
    assert det.view() is None


def test_the_fault_stays_within_its_window_and_then_expires():
    catalog = session_fault.load(
        "version: 1\n"
        "kinds:\n"
        "  usage_limit: {label: usage limit, action: wait, window: 60}\n"
        "common: []\n"
        "harnesses:\n"
        "  claude:\n"
        "    - kind: usage_limit\n"
        "      pattern: 'weekly limit'\n"
    )
    det, now = _detector(catalog=catalog)
    det.scan(_Screen("You've hit your weekly limit"), now=1000.0)
    assert det.view(now=1059.0)["active"] is True
    assert det.view(now=1061.0)["active"] is False
    # The expired record is kept: a person asking later still gets the text.
    assert det.view(now=1061.0)["message"].startswith("You've hit")


def test_a_repeated_message_keeps_its_first_sighting():
    det, now = _detector()
    det.scan(_Screen(WEEKLY), now=1000.0)
    first = det.view(now=1000.0)["since"]
    det.scan(_Screen(WEEKLY), now=1005.0)
    assert det.view(now=1005.0)["since"] == first
    assert det.view(now=1005.0)["last_seen"] != first


def test_a_later_different_fault_replaces_the_record():
    det, now = _detector()
    det.scan(_Screen(WEEKLY), now=1000.0)
    det.scan(_Screen(LOGIN), now=1005.0)
    assert det.view(now=1005.0)["kind"] == "auth"


def test_a_second_scan_inside_the_interval_is_throttled():
    det, now = _detector()
    assert det.scan(_Screen(WEEKLY), now=1000.0) is True
    assert det.scan(_Screen(WEEKLY), now=1000.5) is False
    assert det.scan(_Screen(WEEKLY), now=1001.1) is True


def test_a_harness_with_no_patterns_never_scans():
    catalog = session_fault.load(
        "version: 1\n"
        "kinds:\n"
        "  auth: {label: login, window: 10}\n"
        "common: []\n"
        "harnesses: {}\n"
    )
    det, now = _detector(catalog=catalog)
    assert det.scan(_Screen(LOGIN), now=1000.0) is True
    assert det.view() is None


# -------------------------------------------------------------------- session

def _session(tmp_path, harness="claude"):
    return Session(
        SessionDef(name="sf", harness=harness, cwd=str(tmp_path)),
        idle_threshold=0.5,
        scrollback=200,
    )


def test_the_session_carries_the_fault_and_its_info_row(home, tmp_path):
    async def run():
        s = _session(tmp_path)
        # The scanner reads the bottom of the grid; scroll the message there.
        s.screen.feed_render(b"\r\n" * (s.screen.rows + 1) + (WEEKLY + "\r\n").encode())
        # The record's window and the view's "now" must come from the same
        # clock; the real clock would read a 1970 reset as long expired.
        s._fault._clock = lambda: 2000.0
        s._fault.scan(s.screen, now=2000.0)
        fault = s.fault()
        assert fault["kind"] == "usage_limit"
        assert fault["active"] is True
        assert s.info()["fault"]["kind"] == "usage_limit"
        s._log.close()

    asyncio.run(run())


def test_a_session_that_never_faulted_reports_none(home, tmp_path):
    async def run():
        s = _session(tmp_path)
        assert s.fault() is None
        assert s.info()["fault"] is None
        s._log.close()

    asyncio.run(run())


def test_a_dead_session_has_no_fault(home, tmp_path):
    dead = DeadSession(SessionDef(name="d", cwd=str(tmp_path)), exit_code=0)
    assert dead.fault() is None
    assert dead.info()["fault"] is None


# ------------------------------------------------------------------------ API

def test_the_rail_field_list_carries_fault():
    """A field left out of RAIL_FIELDS never reaches the dashboard."""
    assert "fault" in api.RAIL_FIELDS


@pytest.fixture
def mgr(home):
    return SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)


def _client(mgr, scenario):
    async def run():
        app = build_app(mgr, "sekrit", started_at=time.monotonic())
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            return await scenario(client)
        finally:
            await client.close()

    return asyncio.run(run())


class _Faulted(DeadSession):
    """A session record that already holds a fault view.

    A real ``Session`` needs a running event loop and a PTY-less log; the
    route only asks each session for ``fault()``, so the record can answer
    it directly and the test stays about the route.
    """

    def __init__(self, name, cwd, harness="claude", view=None):
        super().__init__(SessionDef(name=name, harness=harness, cwd=str(cwd)), exit_code=0)
        self._view = view

    def fault(self):
        return self._view


def _fault_view(harness="claude"):
    det, _ = _detector(harness)
    det.scan(_Screen(WEEKLY), now=1_000_000.0)
    return det.view(now=1_000_000.0)


def _faulted(mgr, name, tmp_path, harness="claude"):
    s = _Faulted(name, tmp_path, harness, _fault_view(harness))
    mgr._sessions[name] = s
    return s


def test_the_fleet_route_lists_active_faults(mgr, tmp_path):
    s = _faulted(mgr, "f1", tmp_path)
    mgr._sessions["quiet"] = DeadSession(
        SessionDef(name="quiet", cwd=str(tmp_path)), exit_code=0
    )

    async def scenario(client):
        body = await (await client.get("/api/session-faults", headers=BEARER)).json()
        one = await (await client.get(
            "/api/session-faults?session=f1", headers=BEARER)).json()
        none = await (await client.get(
            "/api/session-faults?session=quiet", headers=BEARER)).json()
        return body, one, none

    body, one, none = _client(mgr, scenario)
    assert body["count"] == 1
    assert body["faults"][0]["session"] == "f1"
    assert body["faults"][0]["kind"] == "usage_limit"
    assert one["count"] == 1
    assert none["count"] == 0


def test_the_route_filters_by_kind_and_expired(mgr, tmp_path):
    s = _faulted(mgr, "f1", tmp_path)
    # An expired record stays readable with active=false, not with the
    # default filter.
    s._view["active"] = False

    async def scenario(client):
        active = await (await client.get(
            "/api/session-faults", headers=BEARER)).json()
        all_rows = await (await client.get(
            "/api/session-faults?active=all", headers=BEARER)).json()
        auth = await (await client.get(
            "/api/session-faults?active=all&kind=auth", headers=BEARER)).json()
        return active, all_rows, auth

    active, all_rows, auth = _client(mgr, scenario)
    assert active["count"] == 0
    assert all_rows["count"] == 1 and all_rows["faults"][0]["active"] is False
    assert auth["count"] == 0
