"""OpenGateway usage: the reading this machine itself recorded.

OpenGateway publishes a gateway API and no usage endpoint a data-plane key can
read -- its management surface answers 401 to one, and the data plane has no
usage route (claunch-firtc). So the reader reports what the routing shim
already wrote for every request a keyed provider forwarded (``metering``),
totalled for today and for the month to date.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from claude_launcher import cli, metering, profile, profile_usage, store, usage

GATEWAY = "https://apis.opengateway.ai"


def _gateway_profile(name="og"):
    """A real profile whose provider is OpenGateway."""
    prof = profile.create(name)

    def write(doc):
        doc.setdefault("providers", {})["opengateway"] = {
            "endpoints": {"anthropic": GATEWAY, "openai": GATEWAY + "/v1"},
            "models": {"default": "deepseek/deepseek-v4.1-flash-ultrafast"},
        }
        doc.setdefault("profiles", {}).setdefault(name, {})["provider"] = "opengateway"

    store.update(write)
    return prof


def _stamp(delta=timedelta(0), now=None):
    """A metering stamp: local time with its offset, as the shim writes it."""
    when = (now or datetime.now(timezone.utc)) + delta
    return when.astimezone().isoformat()


def _record(ts, *, operator=True, upstream=GATEWAY, status=200, **tokens):
    record = {"ts": ts, "upstream": upstream, "status": status, "model": "deepseek/x"}
    if not operator:
        record["error"] = "boom"
    record.update(tokens)
    return record


def _read(prof, *records, now=None):
    for record in records:
        metering.append("fp", record)
    return usage._fetch_opengateway(prof, now)


def test_reader_names_the_local_reading_without_asking_an_account():
    prof = _gateway_profile()

    assert usage.reader(prof) == "opengateway-local"
    assert usage._opengateway_context(prof) == "apis.opengateway.ai"


def test_another_backend_gets_no_opengateway_reading():
    prof = profile.create("plain")
    store.update(lambda doc: doc.setdefault("providers", {}).__setitem__(
        "elsewhere", {"endpoints": {"anthropic": "https://api.example.com"}}))
    store.update(lambda doc: doc.setdefault("profiles", {}).setdefault("plain", {}).__setitem__(
        "provider", "elsewhere"))

    assert usage._opengateway_context(prof) is None


def test_reading_totals_today_and_the_month_to_date():
    prof = _gateway_profile()
    now = datetime.now(timezone.utc)
    report = _read(
        prof,
        _record(_stamp(now=now), input_tokens=1000, cache_read=500, output_tokens=10),
        _record(_stamp(now=now), input_tokens=400, output_tokens=5),
        _record(_stamp(timedelta(days=40), now=now), input_tokens=7, output_tokens=7),
        now=now,
    )

    assert report.source == "opengateway-local"
    # The four quantities stay apart: a single "input" figure would be
    # dominated by the cheapest component (claunch-ha2s9).
    today, month = report.windows
    assert (today.name, today.detail) == ("today", "2 req · in 1.4k · cache 500 · out 15")
    assert (month.name, month.detail) == ("month", "2 req · in 1.4k · cache 500 · out 15")
    # No limit and no reset time: there is no percentage to show.
    assert (today.utilization, today.resets_at) == (0.0, None)
    assert report.raw["records"] == 3
    assert report.raw["upstream"] == "apis.opengateway.ai"


def test_records_for_another_upstream_are_not_counted():
    prof = _gateway_profile()
    report = _read(
        prof,
        _record(_stamp(), input_tokens=100),
        _record(_stamp(), upstream="https://opencode.ai/zen/go", input_tokens=900),
        # ``metering.load`` matches the string, so this lookalike comes back
        # with the gateway's own records and has to be dropped by host.
        _record(_stamp(), upstream="https://apis.opengateway.ai.example/v1", input_tokens=700),
    )

    assert report.raw["records"] == 1
    assert report.windows[0].detail.startswith("1 req · in 100")


def test_a_failed_request_is_counted_and_named_in_the_totals():
    prof = _gateway_profile()
    report = _read(prof, _record(_stamp(), input_tokens=10), _record(_stamp(), status=429))

    totals = usage._opengateway_totals(metering.load(upstream="apis.opengateway.ai"))
    assert (totals["requests"], totals["failed"]) == (2, 1)
    # Named on the line when there is one; a clean reading leaves it out (the
    # exact strings above already pin that).
    assert report.windows[0].detail.endswith("· 1 failed")


def test_a_record_with_no_readable_time_lands_in_neither_bucket():
    prof = _gateway_profile()
    report = _read(prof, _record("not-a-time", input_tokens=10))

    assert report.raw["undated"] == 1
    assert [w.detail for w in report.windows] == [
        "0 req · in 0 · cache 0 · out 0",
        "0 req · in 0 · cache 0 · out 0",
    ]


def test_the_reading_says_why_it_cannot_measure_a_key():
    """Records carry no key identity, so a pool cannot compare keys."""
    assert "no key identity" in usage.unmeasurable_reason("opengateway-local")
    prof = _gateway_profile()
    assert usage.utilization_for_token(prof, "some-key") is None


def test_usage_command_prints_the_reader_line_and_not_a_percentage(capsys):
    windows = [
        usage.UsageWindow(
            name="today", utilization=0.0, resets_at=None,
            detail="2 req · in 1.4k · cache 500 · out 15",
        )
    ]

    cli._print_usage("og", usage.UsageReport(windows=windows, raw={}, source="opengateway-local"))

    out = capsys.readouterr().out
    assert "usage for profile 'og'  (via this machine's request records)" in out
    assert "today              2 req · in 1.4k · cache 500 · out 15" in out
    assert "%" not in out


def test_the_usage_page_payload_carries_the_line(monkeypatch):
    """``profile_usage`` builds the window dict the usage page renders, so the
    detail has to survive that translation."""
    prof = _gateway_profile()
    metering.append("fp", _record(_stamp(), input_tokens=2000, output_tokens=7))
    monkeypatch.setattr(profile_usage, "_entries", {})
    monkeypatch.setattr(profile_usage, "_locks", {})

    payload, _cached, _at = profile_usage.fetch(prof.selector)

    assert payload["source"] == "opengateway-local"
    assert [w["detail"] for w in payload["windows"]] == [
        "1 req · in 2.0k · cache 0 · out 7",
        "1 req · in 2.0k · cache 0 · out 7",
    ]
