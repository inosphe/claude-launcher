"""Command Code usage reader (claunch-j43qd).

The replies below have the shape the ``command-code`` CLI (1.88.3) reads for
its own ``/usage`` view; the Provider-plan reply is the one a real Provider
account returned (identifiers dropped), the windowed one is built from the
field names the CLI renders (``windowLimits.fiveHour`` / ``weekly``).
"""

from __future__ import annotations

import io
import json
import urllib.parse

import pytest

from claude_launcher import cli, credentials, profile, profile_keys, profile_usage, providers, store, usage


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def _commandcode_profile(name="cmdc", endpoints=None, token="user_provider_key"):
    p = profile.create(name)
    entry = {
        "endpoints": endpoints or {"openai": "https://api.commandcode.ai/provider/v1"},
        "models": {"default": "deepseek/deepseek-v4.1-flash"},
    }
    store.update(lambda doc: doc.update({"providers": {"commandcode": entry}}))
    providers.set_profile_selection(p, "commandcode")
    if token:
        credentials.save_token(p, token)
    return p


_PERIOD_END = "2026-12-05T09:10:23.000Z"

PROVIDER_ACCOUNT = {
    "/alpha/whoami": {"success": True, "user": {"id": "u"}, "org": None},
    "/alpha/billing/credits": {
        "credits": {
            "belowThreshold": False, "creditThreshold": 0,
            "monthlyCredits": 12.0, "purchasedCredits": 0, "freeCredits": 0,
        },
        "windowLimits": {"limited": False, "exceeded": None, "fiveHour": None, "weekly": None},
        "sandboxAccess": False, "sandboxMinutes": None,
    },
    "/alpha/billing/subscriptions": {"success": True, "data": {
        "status": "active", "planId": "individual-provider",
        "currentPeriodStart": "2026-11-05T09:10:23.000Z", "currentPeriodEnd": _PERIOD_END,
    }},
}

#: 2026-11-05T15:00:00Z and 2026-11-10T00:00:00Z in epoch milliseconds.
_FIVE_HOUR_RESET_MS = 1793890800000
_WEEKLY_RESET_MS = 1794268800000

WINDOWED_ACCOUNT = {
    "/alpha/whoami": {"success": True, "user": {"id": "u"}, "org": {"id": "org-1"}},
    "/alpha/billing/credits": {
        "credits": {"monthlyCredits": 20.0, "purchasedCredits": 5.0, "freeCredits": 0},
        "windowLimits": {
            "limited": True, "exceeded": None,
            "fiveHour": {"used": 3.0, "cap": 4.0, "resetAt": _FIVE_HOUR_RESET_MS},
            "weekly": {"used": 6.0, "cap": 30.0, "resetAt": _WEEKLY_RESET_MS},
        },
    },
    "/alpha/billing/subscriptions": {"success": True, "data": {
        "status": "active", "planId": "individual-pro-v1",
        "currentPeriodStart": "2026-11-01T00:00:00.000Z", "currentPeriodEnd": _PERIOD_END,
    }},
}


def _serve(monkeypatch, accounts, seen=None):
    """Answer each request from the account its bearer token names."""

    def fake_urlopen(req, timeout):
        parts = urllib.parse.urlsplit(req.full_url)
        assert parts.scheme == "https" and parts.netloc == "api.commandcode.ai"
        token = req.get_header("Authorization").removeprefix("Bearer ")
        if seen is not None:
            seen.append((token, parts.path, dict(urllib.parse.parse_qsl(parts.query))))
        account = accounts[token]
        if account is None:
            raise usage.urllib.error.HTTPError(req.full_url, 401, "denied", {}, io.BytesIO(b"{}"))
        return _FakeResponse(json.dumps(account[parts.path]).encode())

    monkeypatch.setattr(usage.urllib.request, "urlopen", fake_urlopen)


def test_an_openai_endpoint_on_commandcode_selects_the_reader(home):
    p = _commandcode_profile()
    assert usage.reader(usage.resolve_target(p)) == "commandcode"


def test_an_anthropic_endpoint_on_commandcode_selects_the_reader(home):
    p = _commandcode_profile(endpoints={"anthropic": "https://api.commandcode.ai/provider"})
    assert usage.reader(usage.resolve_target(p)) == "commandcode"


def test_another_host_is_not_commandcode(home):
    p = _commandcode_profile(endpoints={"openai": "https://commandcode.example.com/v1"})
    with pytest.raises(usage.UsageError, match="Anthropic service provider"):
        usage.reader(usage.resolve_target(p))


def test_provider_plan_reads_only_the_monthly_credit_pool(home, monkeypatch):
    p = _commandcode_profile()
    seen = []
    _serve(monkeypatch, {"user_provider_key": PROVIDER_ACCOUNT}, seen)

    report = usage.fetch(usage.resolve_target(p))

    assert report.source == "commandcode"
    assert [w.name for w in report.windows] == ["monthly"]
    month = report.windows[0]
    # Pool = the Provider plan's $15 grant; $12 left, so $3 (20%) spent.
    assert month.limit_dollars == 15.0
    assert month.remaining_dollars == 12.0
    assert month.used_dollars == 3.0
    assert month.utilization == pytest.approx(20.0)
    assert month.resets_at == _PERIOD_END
    assert report.raw["mode"] == "provider"
    assert report.raw["plan_name"] == "Provider"
    # A personal account has no org: the billing calls carry no orgId, and an
    # active plan needs no usage summary.
    assert seen == [
        ("user_provider_key", "/alpha/whoami", {"limits": "1"}),
        ("user_provider_key", "/alpha/billing/credits", {}),
        ("user_provider_key", "/alpha/billing/subscriptions", {}),
    ]


def test_windowed_plan_reads_five_hour_weekly_and_monthly(home, monkeypatch):
    p = _commandcode_profile(token="user_pro_key")
    seen = []
    _serve(monkeypatch, {"user_pro_key": WINDOWED_ACCOUNT}, seen)

    report = usage.fetch(usage.resolve_target(p))

    assert [(w.name, round(w.utilization, 1), w.resets_at) for w in report.windows] == [
        ("five_hour", 75.0, "2026-11-05T15:00:00Z"),
        ("weekly", 20.0, "2026-11-10T00:00:00Z"),
        ("monthly", round((85.0 - 25.0) / 85.0 * 100, 1), _PERIOD_END),
    ]
    month = report.windows[-1]
    # Pro v1 grants $80; purchased credits widen the pool.
    assert month.limit_dollars == 85.0
    assert report.raw["mode"] == "windowed"
    assert {query.get("orgId") for _t, path, query in seen if path != "/alpha/whoami"} == {"org-1"}


def test_exceeded_window_is_marked_and_takes_the_key_out(home, monkeypatch):
    account = json.loads(json.dumps(WINDOWED_ACCOUNT))
    limits = account["/alpha/billing/credits"]["windowLimits"]
    limits["exceeded"] = "fiveHour"
    limits["fiveHour"]["used"] = 4.0
    p = _commandcode_profile(token="user_pro_key")
    _serve(monkeypatch, {"user_pro_key": account})

    report = usage.fetch(usage.resolve_target(p))

    assert report.windows[0].status == "exceeded"
    assert usage.health_for_report(report).blocked == "five_hour"


def test_without_an_active_plan_the_pool_is_balance_plus_period_spend(home, monkeypatch):
    account = json.loads(json.dumps(PROVIDER_ACCOUNT))
    account["/alpha/billing/subscriptions"] = {"success": True, "data": None}
    account["/alpha/usage/summary"] = {"totalCost": 4.0}
    p = _commandcode_profile()
    _serve(monkeypatch, {"user_provider_key": account})

    month = usage.fetch(usage.resolve_target(p)).windows[0]

    assert (month.limit_dollars, month.remaining_dollars, month.used_dollars) == (16.0, 12.0, 4.0)
    assert month.resets_at is None


def test_without_a_key_names_set_token(home, monkeypatch):
    p = _commandcode_profile(token=None)
    monkeypatch.setattr(usage.urllib.request, "urlopen", lambda req, timeout: pytest.fail("no request"))
    with pytest.raises(credentials.CredentialsError, match="run 'claunch set-token cmdc' first"):
        usage.fetch(usage.resolve_target(p))


def test_rejected_key_names_set_token(home, monkeypatch):
    p = _commandcode_profile()
    _serve(monkeypatch, {"user_provider_key": None})
    with pytest.raises(usage.UsageError, match=r"rejected the API key \(401\); run 'claunch set-token cmdc'"):
        usage.fetch(usage.resolve_target(p))


def test_cli_names_the_source_and_the_plan(home, monkeypatch, capsys):
    _commandcode_profile()
    _serve(monkeypatch, {"user_provider_key": PROVIDER_ACCOUNT})
    assert cli.main(["usage", "cmdc"]) == 0
    out = capsys.readouterr().out
    assert "(via Command Code billing API)" in out
    assert "plan: Provider" in out
    assert "$3.00 of $15.00, $12.00 left" in out


# --- a pool whose keys are on different plans -------------------------------


def _mixed_pool(monkeypatch):
    p = _commandcode_profile(token=None)
    profile_keys.add(p, "user_provider_key")
    profile_keys.add(p, "user_pro_key")
    _serve(monkeypatch, {"user_provider_key": PROVIDER_ACCOUNT, "user_pro_key": WINDOWED_ACCOUNT})
    return p


def test_commandcode_is_a_keyed_reader(home):
    assert "commandcode" in usage.KEYED_READERS
    assert usage.unmeasurable_reason("commandcode") is None


def test_each_key_of_a_mixed_pool_reads_its_own_plan(home, monkeypatch):
    p = _mixed_pool(monkeypatch)
    selected = usage.resolve_target(p)

    provider_report = usage.report_for_token(selected, "user_provider_key")
    pro_report = usage.report_for_token(selected, "user_pro_key")

    assert [w.name for w in provider_report.windows] == ["monthly"]
    assert [w.name for w in pro_report.windows] == ["five_hour", "weekly", "monthly"]


def test_key_usage_rows_carry_each_keys_own_windows(home, monkeypatch):
    p = _mixed_pool(monkeypatch)
    answer = profile_usage.key_usage(p.name, manual=True)

    assert answer["unmeasurable"] is None
    names = sorted(
        tuple(w["name"] for w in row["windows"]) for row in answer["keys"].values()
    )
    assert names == [("five_hour", "weekly", "monthly"), ("monthly",)]
    assert all(not row.get("error") for row in answer["keys"].values())


def test_tie_break_compares_the_same_window_across_plans():
    """A key with no 5-hour limit is not ordered behind one that has it.

    The two keys tie on the peak. Lined up by window, the Provider-plan key
    is 0 on the 5-hour and week rungs (it has no such limit) and 40 on the
    month; the windowed key is 40, 10 and 20. Comparing the sorted pairs
    instead put ``(five_hour, 40)`` against ``(monthly, 40)`` and ordered the
    windowed key first.
    """
    provider_key = usage.KeyHealth(peak=40.0, windows=(("monthly", 40.0),))
    windowed_key = usage.KeyHealth(
        peak=40.0, windows=(("five_hour", 40.0), ("weekly", 10.0), ("monthly", 20.0))
    )
    assert usage.tie_break_key(provider_key) < usage.tie_break_key(windowed_key)


def test_usage_min_picks_across_a_mixed_pool(home, monkeypatch):
    # Provider key: 20% of its month. Pro key: 75% of its 5 hours.
    p = _mixed_pool(monkeypatch)
    profile_keys.set_strategy(p, "usage-min")
    assert profile_keys.select_for_launch(p).key.token == "user_provider_key"


def test_usage_min_skips_the_key_whose_window_is_spent(home, monkeypatch):
    spent = json.loads(json.dumps(PROVIDER_ACCOUNT))
    spent["/alpha/billing/credits"]["credits"]["monthlyCredits"] = 0
    p = _commandcode_profile(token=None)
    profile_keys.add(p, "user_provider_key")
    profile_keys.add(p, "user_pro_key")
    _serve(monkeypatch, {"user_provider_key": spent, "user_pro_key": WINDOWED_ACCOUNT})
    profile_keys.set_strategy(p, "usage-min")

    assert profile_keys.select_for_launch(p).key.token == "user_pro_key"
