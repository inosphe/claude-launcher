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

# The two screens `awaiting_input` was collected from (2026-10-25): a
# permission-mode prompt and an announcement. Both stop the session on a
# keystroke and end on the same footer.
AWAITING_CONFIRM = "  Enter to confirm · Esc to cancel"
AWAITING_CONTINUE = "  Enter to continue · Esc to cancel"
AWAITING_PROMPT = (
    "  Teach auto mode about your environment?",
    "  ❯ 1. Yes",
    "    2. Not now",
    "    3. Don't show again",
    "",
    AWAITING_CONFIRM,
)
AWAITING_NOTICE = (
    "  We're changing auto mode to no longer charge for classifier requests"
    " in Claude Code.",
    "",
    AWAITING_CONTINUE,
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


@pytest.mark.parametrize("rows", [AWAITING_PROMPT, AWAITING_NOTICE],
                         ids=["confirm", "continue"])
def test_an_observed_prompt_screen_reads_as_awaiting_input(rows):
    """The session cannot move until somebody presses a key on its terminal;
    nothing about the account or the service is wrong (claunch-pczoc.3)."""
    det, now = _detector("claude")
    det.scan(_Screen(*rows), now=now["t"])
    view = det.view(now=now["t"])
    assert view["kind"] == "awaiting_input", view["message"]
    assert view["label"] == "waiting for input"
    assert view["resolved_by"] == "input"


@pytest.mark.parametrize("footer", [
    AWAITING_CONFIRM,
    AWAITING_CONTINUE,
    # The separator between the two halves is drawn by the terminal, so the
    # pattern must not read it: a two-space footer is the same prompt.
    "Enter to confirm  Esc to cancel",
])
def test_the_prompt_footer_does_not_depend_on_how_the_terminal_separates_it(footer):
    found = session_fault.default().match("claude", footer)
    assert found and found[0].name == "awaiting_input"


def test_the_prompt_footer_is_read_only_for_the_harness_it_was_seen_in():
    """Both observations are `claude` screens. A harness that draws the same
    footer has not been seen, and the catalogue keeps readings where they
    were collected (docs/session-faults.md, `awaiting_input`)."""
    catalog = session_fault.default()
    assert catalog.match("claude", AWAITING_CONTINUE) is not None
    assert catalog.match("pi", AWAITING_CONTINUE) is None


def test_a_prompt_footer_is_a_named_cause_so_the_lower_row_decides():
    """The footer names a stop the terminal holds; a limit line names a stop
    the account holds. Both are named, so the row painted last decides --
    the same rule as any other two named causes."""
    for rows, kind in (
        ((WEEKLY, AWAITING_CONFIRM), "awaiting_input"),
        ((AWAITING_CONFIRM, WEEKLY), "usage_limit"),
    ):
        det, now = _detector("claude")
        det.scan(_Screen(*rows), now=now["t"])
        assert det.view()["kind"] == kind, f"{rows} read {det.view()['kind']}"


def test_every_packaged_kind_says_what_clears_it():
    """The table in docs/session-faults.md, held as data: a kind added to the
    YAML without a decision about who or what resolves it fails here, and so
    does a grade changed on one side only."""
    graded = {
        name: kind.resolved_by
        for name, kind in session_fault.default().kinds.items()
    }
    assert graded == {
        "usage_limit": "account",
        "account_suspended": "account",
        "rate_limit": "provider",
        "overloaded": "provider",
        "auth": "account",
        "api_status": "provider",
        "network": "provider",
        "policy_blocked": "policy",
        "awaiting_input": "input",
    }


def test_a_status_code_with_no_pattern_is_api_status_and_keeps_the_code():
    det, now = _detector()
    det.scan(_Screen("API Error: 503 Service Unavailable"), now=now["t"])
    view = det.view()
    assert view["kind"] == "api_status"
    assert view["code"] == "503"


@pytest.mark.parametrize("line, kind", [
    ("API Error: 401", "auth"),
    ("API Error: 412", "account_suspended"),
    ("API Error: 429", "rate_limit"),
    ("API Error: 529", "overloaded"),
])
def test_a_bare_status_code_keeps_its_own_reading_by_list_order(line, kind):
    """Each of these codes starts at the same character as the generic
    `API Error: <code>` fallback, and both are `fallback` entries, so nothing
    but catalogue order separates them: the code's own entry is listed above
    the generic one. Move it below and a bare 529 reads `api_status`."""
    found = session_fault.default().match("codex", line)
    assert found is not None, f"{line} matched nothing"
    assert found[0].name == kind, f"{line} read {found[0].name}"


OVERLOADED = "API Error: 529 Overloaded — retrying in 30s (attempt 3/10)"


@pytest.mark.parametrize("harness", ["claude", "pi", "codex", "kimi", "agent"])
def test_a_529_is_read_in_every_harness(harness):
    """The status fallback lives in `common`, so a harness with no list of
    its own still reads a 529 (it did not before claunch-pczoc.2)."""
    found = session_fault.default().match(harness, OVERLOADED)
    assert found is not None, f"{harness}: the 529 line matched nothing"
    assert found[0].name == "overloaded"


def test_overloaded_expires_sooner_than_api_status():
    """A 529 is usually one retry; api_status's 30 minutes would leave the
    pill up long after the provider took the request again."""
    catalog = session_fault.default()
    assert catalog.kinds["overloaded"].window < catalog.kinds["api_status"].window


def test_the_generic_fallback_does_not_swallow_codes_with_their_own_reading():
    """The fallback sits in `common`, which is matched before a harness's own
    list, so the codes with a specific reading must be stated above it or a
    401 would come out as api_status."""
    catalog = session_fault.default()
    cases = {
        "API Error: 500 Internal Server Error": "api_status",
        "API Error: 503 Service Unavailable": "api_status",
        "API Error: 401 Invalid bearer token": "auth",
        "API Error: 412 Account x is suspended": "account_suspended",
        "API Error: 429 Too Many Requests": "rate_limit",
        # claude's own 429 wording, listed here because it must still read as
        # a rate window now that the code-level entry sits above it in
        # `common` (a pattern lower in the list never gets to speak first).
        "API Error: Request rejected (429)": "rate_limit",
    }
    for harness in ("claude", "codex"):
        for line, kind in cases.items():
            if harness != "claude" and "Request rejected" in line:
                continue  # this harness did not paint that wording
            found = catalog.match(harness, line)
            assert found is not None, f"{harness}: {line!r} matched nothing"
            assert found[0].name == kind, (
                f"{harness}: {line!r} matched {found[0].name}, expected {kind}")


def test_a_quota_sentence_beats_the_code_level_429():
    """Both are in `common` now; the sentence must still win, or an exhausted
    plan would be reported as a 15-minute rate window."""
    found = session_fault.default().match("codex", KIMI_429)
    assert found is not None
    assert found[0].name == "usage_limit"


def test_the_529_fault_is_marked_and_keeps_the_status_code():
    det, now = _detector()
    det.scan(_Screen(OVERLOADED), now=now["t"])
    view = det.view()
    assert view["kind"] == "overloaded"
    assert view["code"] == "529"
    assert view["active"] is True
    # The shorter window is what the rail reads: five minutes later it is
    # expired, where api_status would still be active.
    assert det.view(now=now["t"] + 301)["active"] is False


PI_TIMEOUT = "Error: Request timed out."
PI_RETRIES_EXHAUSTED = "Error: Retry failed after 3 attempts: Request timed out."


@pytest.mark.parametrize("harness", ["claude", "pi", "codex", "kimi", "agent"])
def test_a_timed_out_request_is_read_in_every_harness(harness):
    """pi (s1736) painted this line; until claunch-pczoc.2.1 the pattern sat
    in `harnesses.claude`, so every other harness read nothing."""
    found = session_fault.default().match(harness, PI_TIMEOUT)
    assert found is not None, f"{harness}: the timeout line matched nothing"
    assert found[0].name == "network"


@pytest.mark.parametrize("harness", ["claude", "pi", "codex", "kimi", "agent"])
def test_an_exhausted_retry_is_read_in_every_harness(harness):
    found = session_fault.default().match(harness, PI_RETRIES_EXHAUSTED)
    assert found is not None, f"{harness}: the exhausted-retry line matched nothing"
    assert found[0].name == "network"


def test_the_exhausted_retry_line_is_the_one_recorded():
    """Both lines are `network`, so the kind does not separate them; what
    does is position -- the screen that ends with "Retry failed after N
    attempts" is the one where the client stopped, and the lowest matching
    row is what a person reads in the tooltip."""
    det, now = _detector("pi")
    det.scan(_Screen(PI_TIMEOUT, PI_TIMEOUT, PI_TIMEOUT, PI_RETRIES_EXHAUSTED),
             now=now["t"])
    view = det.view()
    assert view["kind"] == "network"
    assert view["harness"] == "pi"
    assert view["message"] == PI_RETRIES_EXHAUSTED


SAFEGUARDS = (
    "API Error: Opus 5.5's safeguards flagged this message "
    "(https://www.anthropic.com/legal/aup)."
)
PI_LOGIN = "Use /login to log into a provider via OAuth or API key. See: ..."


@pytest.mark.parametrize("harness, rows, kind", [
    ("claude", (WEEKLY, PI_TIMEOUT), "usage_limit"),
    ("claude", (PI_TIMEOUT, WEEKLY), "usage_limit"),
    ("claude", (SAFEGUARDS, PI_TIMEOUT), "policy_blocked"),
    ("claude", (PI_TIMEOUT, SAFEGUARDS), "policy_blocked"),
    ("claude", (LOGIN, PI_TIMEOUT), "auth"),
    ("claude", (PI_TIMEOUT, LOGIN), "auth"),
    ("claude", ("API Error: Request rejected (429)", PI_TIMEOUT), "rate_limit"),
    ("claude", (PI_TIMEOUT, "API Error: Request rejected (429)"), "rate_limit"),
    ("pi", (PI_LOGIN, PI_TIMEOUT), "auth"),
    ("pi", (PI_TIMEOUT, PI_LOGIN), "auth"),
])
def test_a_named_cause_survives_a_timeout_on_either_side(harness, rows, kind):
    """The timeout entries are `fallback`: they say the client gave up, not
    why. A screen that also holds a line naming the cause reads that cause
    whichever of the two sits lower. claunch-pczoc.2.1 held this by moving
    the wording above the timeouts, which only worked in one order; the
    marker makes it independent of where the line is listed or painted."""
    det, now = _detector(harness)
    det.scan(_Screen(*rows), now=now["t"])
    view = det.view()
    assert view["kind"] == kind, f"{harness}: {rows} read {view['kind']}"


def test_between_two_named_causes_the_lower_row_decides():
    """Neither line is a fallback, so position settles it: the row painted
    last is the line the session stopped on."""
    for rows, kind in (
        ((WEEKLY, SAFEGUARDS), "policy_blocked"),
        ((SAFEGUARDS, WEEKLY), "usage_limit"),
    ):
        det, now = _detector("claude")
        det.scan(_Screen(*rows), now=now["t"])
        assert det.view()["kind"] == kind, f"{rows} read {det.view()['kind']}"


def test_entries_starting_at_the_same_character_are_decided_by_catalogue_order():
    """Position cannot separate two readings of the same characters, so the
    list decides, and `common` is read before a harness's own list -- which
    is why a provider's quota sentence still beats a harness's own 429 for
    the same text."""
    catalog = session_fault.load(
        """
        version: 1
        kinds:
          usage_limit: {label: usage limit, action: wait, window: 86400, resolved_by: account}
          rate_limit: {label: rate limit, action: wait, window: 900, resolved_by: provider}
        common:
          - kind: usage_limit
            pattern: 'quota'
        harnesses:
          claude:
            - kind: rate_limit
              pattern: 'quota'
        """
    )
    det, now = _detector("claude", catalog=catalog)
    det.scan(_Screen("quota exceeded"), now=now["t"])
    assert det.view()["kind"] == "usage_limit"


def test_the_timeout_lines_are_in_common_so_every_harness_reads_them():
    """A harness added later gets them without a list of its own -- the bug
    this round fixed was a pattern that only `claude` had."""
    sources = " ".join(pattern.source for pattern in session_fault.default().common)
    assert "Retry failed after" in sources
    assert "Request timed out" in sources


@pytest.mark.parametrize("document", [
    {},
    {"version": 2, "kinds": {}},
    {"version": 1, "kinds": {}},
    {"version": 1, "kinds": {"auth": {"window": 1, "resolved_by": "account"}},
     "common": [{"kind": "nope", "pattern": "x"}]},
    {"version": 1, "kinds": {"auth": {"window": 1, "resolved_by": "account"}},
     "common": [{"kind": "auth", "pattern": "("}]},
    {"version": 1, "kinds": {"auth": {"label": "x", "resolved_by": "account"}}},
    {"version": 1, "kinds": {"auth": {"window": 1, "resolved_by": "account"}},
     "common": [{"kind": "auth", "pattern": "x", "fallback": "yes"}]},
    # A kind with no grade, and one with a grade nobody defined: the badge
    # cannot say what clears the stop, so the catalogue refuses to load.
    {"version": 1, "kinds": {"auth": {"window": 1}}},
    {"version": 1, "kinds": {"auth": {"window": 1, "resolved_by": "nobody"}}},
])
def test_a_broken_catalogue_is_refused(document):
    with pytest.raises(session_fault.CatalogError):
        session_fault.Catalog(document)


def test_the_message_a_missing_grade_fails_with_names_the_field():
    with pytest.raises(session_fault.CatalogError) as caught:
        session_fault.Catalog({"version": 1, "kinds": {"auth": {"window": 1}}})
    assert "resolved_by" in str(caught.value)


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


# The bottom of s1736's pi terminal (captured 2026-10-25): the warning pi
# prints when the configured model id is unknown to the provider, an update
# notice drawn inside rules, and the retry loop that stopped the session.
PI_RULE = "─" * 140
PI_MODEL_WARNING = (
    'Warning: Model "deepseek/deepseek-v4.1-flash-ultrafast" not found for',
    'provider "claunch-profile". Using custom model id.',
)
PI_UPDATE_BOX = (
    PI_RULE,
    " Update Available",
    " New version 1.4.0 is available. Run pi update",
    " Changelog: https://pi.dev/changelog",
    PI_RULE,
)
PI_RETRY_ROW = "Error: Retry failed after 3 attempts: Request timed out."
PI_RETRY_SCREEN = (
    *PI_MODEL_WARNING,
    "",
    *PI_UPDATE_BOX,
    "",
    " continue",
    "",
    " Error: Request timed out.",
    "",
    " Error: Request timed out.",
    "",
    " Error: Request timed out.",
    "",
    " Error: Request timed out.",
    "",
    " Error: Retry failed after 3 attempts: Request timed out.",
    "",
    PI_RULE,
)


def test_the_pi_retry_screen_reads_the_line_that_stopped_it():
    """pi draws neither of the two claude screens; its stop is the exhausted
    retry line, which `common` reads for every harness."""
    det, now = _detector("pi")
    det.scan(_Screen(*PI_RETRY_SCREEN), now=now["t"])
    view = det.view(now=now["t"])
    assert view["kind"] == "network"
    assert view["resolved_by"] == "provider"


@pytest.mark.parametrize("rows", [PI_MODEL_WARNING, PI_UPDATE_BOX],
                         ids=["model-warning", "update-notice"])
def test_pi_notices_that_do_not_stop_the_session_are_not_faults(rows):
    """pi answers the unknown model with "Using custom model id." and carries
    on, and the update notice waits for nobody -- neither is a fault."""
    det, now = _detector("pi")
    det.scan(_Screen(*rows), now=now["t"])
    assert det.view() is None


def test_a_drawn_rule_is_not_part_of_the_recorded_message():
    """The message takes the rows under the matched one until its length
    limit, so the rule pi draws below the retry line used to be recorded as
    part of it (measured on s1736, 2026-10-25: 140 box-drawing characters
    after the sentence)."""
    det, now = _detector("pi")
    det.scan(_Screen(*PI_RETRY_SCREEN), now=now["t"])
    assert det.view(now=now["t"])["message"] == PI_RETRY_ROW


def test_the_fault_stays_within_its_window_and_then_expires():
    catalog = session_fault.load(
        "version: 1\n"
        "kinds:\n"
        "  usage_limit: {label: usage limit, action: wait, window: 60, resolved_by: account}\n"
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


def test_a_re_match_inside_the_window_does_not_push_the_window_out():
    """The window belongs to the run, not to the last scan (claunch-5ldt8)."""
    det, now = _detector()
    det.scan(_Screen(WEEKLY), now=1000.0)
    first = det.view(now=1000.0)
    det.scan(_Screen(WEEKLY), now=1500.0)
    later = det.view(now=1500.0)
    assert later["since"] == first["since"]
    assert later["until"] == first["until"]
    assert later["last_seen"] != first["last_seen"]


def test_a_scrolling_quote_keeps_the_run_it_started():
    """The s1812 case: the excerpt of the rows under the match changes every
    second while the same line scrolls, and a changed excerpt used to start a
    new record with a new window -- so an answered session stayed faulted."""
    det, now = _detector()
    det.scan(_Screen("shim died at 16:19 and Request timed out followed"), now=1000.0)
    first = det.view(now=1000.0)
    det.scan(_Screen("then Request timed out again at 16:41"), now=1010.0)
    later = det.view(now=1010.0)
    assert later["kind"] == "network"
    assert later["message"] != first["message"]
    assert later["since"] == first["since"]
    assert later["until"] == first["until"]
    assert later["strikes"] == 1


def test_a_re_match_inside_the_window_keeps_the_reset_it_was_measured_from():
    """One row's `resets:` and `until:` come from the same sighting.

    A screen that draws both a weekly and a five-hour limit line matches the
    same kind twice in a row, so re-reading the hint on a re-match put one
    observation's reset beside another observation's window (claunch-5ldt8
    review)."""
    det, now = _detector()
    det.scan(_Screen(WEEKLY), now=1000.0)
    first = det.view(now=1000.0)
    det.scan(
        _Screen("You've hit your 5-hour limit · resets Oct 25, 2pm (Asia/Seoul)"),
        now=1060.0,
    )
    later = det.view(now=1060.0)
    assert later["kind"] == "usage_limit"
    assert later["reset"] == first["reset"]
    assert later["until"] == first["until"]
    assert later["since"] == first["since"]
    assert later["strikes"] == 1


def test_a_reset_that_has_already_passed_counts_one_run_not_one_per_scan():
    """A message can name a reset time that is already behind the clock.

    `reset_deadline` reads up to six hours back, so such a record starts with
    `until` behind `since`; `now < until` never held, every scan became a new
    run, and `strikes` counted scans instead of sightings -- one per second
    while the screen kept changing (claunch-5ldt8 review)."""
    start = datetime(2026, 10, 26, 3, tzinfo=ZoneInfo("UTC")).timestamp()
    det, _ = _detector()
    det.scan(_Screen("attempt 0 · " + WEEKLY), now=start)
    first = det.view(now=start)
    for step in range(1, 5):
        det.scan(_Screen("attempt %d · %s" % (step, WEEKLY)), now=start + step)
    view = det.view(now=start + 4)
    assert view["active"] is False
    assert view["strikes"] == 1
    assert view["since"] == first["since"]
    assert view["until"] == first["until"]


def test_a_kind_that_comes_back_after_its_window_is_a_second_sighting():
    catalog = session_fault.load(
        "version: 1\n"
        "kinds:\n"
        "  usage_limit: {label: usage limit, action: wait, window: 60, resolved_by: account}\n"
        "common: []\n"
        "harnesses:\n"
        "  claude:\n"
        "    - kind: usage_limit\n"
        "      pattern: 'weekly limit'\n"
    )
    det, now = _detector(catalog=catalog)
    det.scan(_Screen("You've hit your weekly limit"), now=1000.0)
    first = det.view(now=1000.0)
    assert first["strikes"] == 1
    det.scan(_Screen("You've hit your weekly limit"), now=1070.0)
    later = det.view(now=1070.0)
    assert later["strikes"] == 2
    assert later["since"] != first["since"]
    assert later["active"] is True


def test_a_different_kind_starts_the_count_over():
    det, now = _detector()
    det.scan(_Screen(WEEKLY), now=1000.0)
    det.scan(_Screen(LOGIN), now=1005.0)
    assert det.view(now=1005.0)["strikes"] == 1


def test_a_line_that_keeps_scrolling_holds_the_badge_one_window_at_a_time():
    """Half an hour of sightings, thirty seconds apart, each with a different
    excerpt: two fifteen-minute runs, not one record stretched over the half
    hour."""
    det, now = _detector()
    for step in range(0, 1800, 30):
        det.scan(_Screen("attempt %d: Request timed out" % step), now=1000.0 + step)
    view = det.view(now=1000.0 + 1800)
    assert view["strikes"] == 2
    assert view["active"] is False


def test_a_second_scan_inside_the_interval_is_throttled():
    det, now = _detector()
    assert det.scan(_Screen(WEEKLY), now=1000.0) is True
    assert det.scan(_Screen(WEEKLY), now=1000.5) is False
    assert det.scan(_Screen(WEEKLY), now=1001.1) is True


def test_a_harness_with_no_patterns_never_scans():
    catalog = session_fault.load(
        "version: 1\n"
        "kinds:\n"
        "  auth: {label: login, window: 10, resolved_by: account}\n"
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


def test_the_grade_travels_with_the_fault_to_the_information_row(home, tmp_path):
    async def run():
        s = _session(tmp_path)
        s.screen.feed_render(
            b"\r\n" * (s.screen.rows + 1) + (AWAITING_CONTINUE + "\r\n").encode()
        )
        s._fault._clock = lambda: 2000.0
        s._fault.scan(s.screen, now=2000.0)
        row = s.info()["fault"]
        assert row["kind"] == "awaiting_input"
        assert row["resolved_by"] == "input"
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


def _fault_view(harness="claude", rows=(WEEKLY,)):
    det, _ = _detector(harness)
    det.scan(_Screen(*rows), now=1_000_000.0)
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


def test_the_fleet_route_carries_the_grade(mgr, tmp_path):
    """The rail's tooltip and the MCP tool both read this field off the row;
    a route that dropped it would say what is wrong on every row and never
    who can clear it."""
    mgr._sessions["f2"] = _Faulted(
        "f2", tmp_path, "claude", _fault_view(rows=AWAITING_NOTICE)
    )

    async def scenario(client):
        return await (await client.get(
            "/api/session-faults?session=f2", headers=BEARER)).json()

    body = _client(mgr, scenario)
    assert body["count"] == 1
    assert body["faults"][0]["kind"] == "awaiting_input"
    assert body["faults"][0]["resolved_by"] == "input"


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
