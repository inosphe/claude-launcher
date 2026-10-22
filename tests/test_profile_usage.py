"""Refresh policy for the profile usage dashboard adapter."""

from types import SimpleNamespace

from claude_launcher import credentials, profile, profile_usage, providers, store, usage


def _fake_resolution(monkeypatch):
    """Resolve every selector to itself: one cache key per selector string."""
    monkeypatch.setattr(
        profile_usage,
        "_resolve",
        lambda selector: (SimpleNamespace(selector=selector, name=selector), "p", selector),
    )
    monkeypatch.setattr(profile_usage.lineage, "effective_harness", lambda _p: "h")


def test_auto_refresh_is_cached_per_selector_and_manual_refresh_bypasses_it(
    monkeypatch,
):
    now = [1000.0]
    calls = []
    monkeypatch.setattr(profile_usage.time, "time", lambda: now[0])
    monkeypatch.setattr(profile_usage, "_entries", {})
    monkeypatch.setattr(profile_usage, "_locks", {})
    _fake_resolution(monkeypatch)

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
    assert other["key"] == "other:codex"
    assert other_at == 1060.0
    assert manual["windows"][0]["utilization"] == 3
    assert manual_at == 1060.0


def test_auto_refresh_fetches_again_after_five_minute_cooldown(monkeypatch):
    now = [1000.0]
    calls = []
    monkeypatch.setattr(profile_usage.time, "time", lambda: now[0])
    monkeypatch.setattr(profile_usage, "_entries", {})
    monkeypatch.setattr(profile_usage, "_locks", {})
    _fake_resolution(monkeypatch)
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


def _fresh_cache(monkeypatch):
    monkeypatch.setattr(profile_usage, "_entries", {})
    monkeypatch.setattr(profile_usage, "_locks", {})


def test_selectors_of_one_profile_and_provider_share_one_account_read(
    home, monkeypatch
):
    """``work`` and ``work:claude`` are the same account: one provider call."""
    _fresh_cache(monkeypatch)
    work = profile.create("work")
    providers.set_profile_selection(work, "claude")
    calls = []
    monkeypatch.setattr(
        profile_usage.usage,
        "fetch",
        lambda selected: calls.append(selected.selector)
        or usage.UsageReport(windows=[], raw={}, source="test"),
    )

    first, first_cached, _ = profile_usage.fetch("work")
    second, second_cached, _ = profile_usage.fetch("work:claude")

    assert calls == ["work"]
    assert (first_cached, second_cached) == (False, True)
    assert first["key"] == second["key"] == "work/claude"
    assert (first["profile"], first["provider"], first["harness"]) == (
        "work",
        "claude",
        "claude",
    )


def test_targets_lists_each_profile_once_with_its_provider_and_reader(home):
    profile.create("work")
    codex = profile.create("codex")
    (codex.config_dir / "codex").mkdir()
    kimi = profile.create("kimi")
    other = profile.create("other")
    store.update(
        lambda doc: doc.update(
            {
                "providers": {
                    "kimi": {
                        "env": {"ANTHROPIC_BASE_URL": "https://api.kimi.com/coding/"}
                    },
                    "elsewhere": {
                        "env": {"ANTHROPIC_BASE_URL": "https://example.invalid"}
                    },
                }
            }
        )
    )
    providers.set_profile_selection(kimi, "kimi")
    credentials.save_token(kimi, "kimi-secret")
    providers.set_profile_selection(other, "elsewhere")

    rows = {row["profile"]: row for row in profile_usage.targets()}

    assert sorted(rows) == ["codex", "kimi", "other", "work"]
    assert rows["work"] == {
        "profile": "work",
        "provider": "default",
        "harness": "claude",
        "key": "work/default",
        "reader": "anthropic",
        "supported": True,
    }
    assert (rows["codex"]["harness"], rows["codex"]["reader"]) == (
        "codex",
        "codex-app-server",
    )
    assert (rows["kimi"]["provider"], rows["kimi"]["reader"]) == (
        "kimi",
        "kimi-provider",
    )
    assert rows["other"]["supported"] is False
    assert rows["other"]["provider"] == "elsewhere"
    assert "Anthropic service provider" in rows["other"]["reason"]


def test_reader_names_the_fetch_path_without_reaching_an_account(home):
    work = profile.create("work")

    assert usage.reader(profile.require_selector("work:claude")) == "anthropic"
    assert usage.reader(work) == "anthropic"
    agent = profile.create("agent")
    try:
        usage.reader(profile.require_selector(f"{agent.name}:agent"))
    except usage.UsageError as exc:
        assert "not available" in str(exc)
    else:  # pragma: no cover - the assertion message is the report
        raise AssertionError("agent harness has no usage reader")
