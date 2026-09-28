"""Refresh policy for the profile usage dashboard adapter."""

from claude_launcher import profile_usage, usage


def test_auto_refresh_is_cached_per_selector_and_manual_refresh_bypasses_it(
    monkeypatch,
):
    now = [1000.0]
    calls = []
    monkeypatch.setattr(profile_usage.time, "time", lambda: now[0])
    monkeypatch.setattr(profile_usage, "_entries", {})
    monkeypatch.setattr(profile_usage, "_locks", {})
    monkeypatch.setattr(
        profile_usage.profile_mod,
        "require_selector",
        lambda selector: type("Selector", (), {"selector": selector})(),
    )
    monkeypatch.setattr(profile_usage.usage, "resolve_target", lambda value: value)

    def fetch(selector):
        calls.append(selector.selector)
        return usage.UsageReport(
            windows=[
                usage.UsageWindow(
                    name="five_hour", utilization=len(calls), resets_at=None
                )
            ],
            raw={},
            source="test",
        )

    monkeypatch.setattr(profile_usage.usage, "fetch", fetch)

    first, cached, first_at = profile_usage.fetch("work:claude")
    now[0] += 60
    second, cached_again, second_at = profile_usage.fetch("work:claude")
    other, other_cached, other_at = profile_usage.fetch("other:codex")
    manual, manual_cached, manual_at = profile_usage.fetch("work:claude", manual=True)

    assert calls == ["work:claude", "other:codex", "work:claude"]
    assert first["windows"][0]["utilization"] == 1
    assert second == first
    assert (cached, cached_again, other_cached, manual_cached) == (
        False,
        True,
        False,
        False,
    )
    assert first_at == second_at == 1000.0
    assert other["profile"] == "other:codex"
    assert other_at == 1060.0
    assert manual["windows"][0]["utilization"] == 3
    assert manual_at == 1060.0


def test_auto_refresh_fetches_again_after_five_minute_cooldown(monkeypatch):
    now = [1000.0]
    calls = []
    monkeypatch.setattr(profile_usage.time, "time", lambda: now[0])
    monkeypatch.setattr(profile_usage, "_entries", {})
    monkeypatch.setattr(profile_usage, "_locks", {})
    monkeypatch.setattr(
        profile_usage.profile_mod,
        "require_selector",
        lambda selector: type("Selector", (), {"selector": selector})(),
    )
    monkeypatch.setattr(profile_usage.usage, "resolve_target", lambda value: value)
    monkeypatch.setattr(
        profile_usage.usage,
        "fetch",
        lambda _selector: calls.append(now[0])
        or usage.UsageReport(windows=[], raw={}, source="test"),
    )

    profile_usage.fetch("work:claude")
    now[0] += profile_usage.AUTO_COOLDOWN_SECONDS
    _payload, cached, updated_at = profile_usage.fetch("work:claude")

    assert calls == [1000.0, 1300.0]
    assert cached is False
    assert updated_at == 1300.0
