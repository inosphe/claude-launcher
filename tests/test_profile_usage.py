"""Refresh policy for the profile usage dashboard adapter."""

from types import SimpleNamespace

from claude_launcher import (
    credentials,
    profile,
    profile_keys,
    profile_usage,
    providers,
    store,
    usage,
)


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


def test_auto_refresh_fetches_again_after_the_period(monkeypatch):
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

    assert profile_usage.AUTO_COOLDOWN_SECONDS == profile_usage.PERIOD_SECONDS == 15 * 60
    assert calls == [1000.0, 1900.0]
    assert cached is False
    assert updated_at == 1900.0


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
    assert first["key"] == second["key"] == "work/claude/anthropic"
    assert (first["profile"], first["provider"], first["harness"]) == (
        "work",
        "claude",
        "claude",
    )


def test_a_harness_with_its_own_login_is_not_served_the_providers_cache(
    home, monkeypatch
):
    """``work:codex`` reads the codex login, ``work:claude`` the provider's:
    same profile and provider name, two accounts, two reads."""
    _fresh_cache(monkeypatch)
    work = profile.create("work")
    (work.config_dir / "codex").mkdir()
    calls = []

    def fetch(selected):
        calls.append(selected.selector)
        return usage.UsageReport(windows=[], raw={}, source=selected.selector)

    monkeypatch.setattr(profile_usage.usage, "fetch", fetch)

    codex, codex_cached, _ = profile_usage.fetch("work:codex")
    claude, claude_cached, _ = profile_usage.fetch("work:claude")

    assert calls == ["work:codex", "work:claude"]
    assert (codex_cached, claude_cached) == (False, False)
    assert (codex["source"], claude["source"]) == ("work:codex", "work:claude")
    assert codex["key"] == "work/default/codex-app-server"
    assert claude["key"] == "work/default/anthropic"


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
        "key": "work/default/anthropic",
        "reader": "anthropic",
        "supported": True,
        # Never read: due now (claunch-f5w4x.3).
        "schedule": {
            "updated_at": None,
            "next_at": None,
            "period_s": profile_usage.PERIOD_SECONDS,
        },
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


# --- the pool's own keys (claunch-yx2bd) ------------------------------------
#
# The create form shows each key's usage beside the key it belongs to, so the
# pool has a per-key read of its own. It is cached the same way the profile's
# reading is, keyed by the key rather than by the account, and one key's
# failure never blanks the others.


def _keyed_pool(home, monkeypatch, *tokens, reader="anthropic"):
    """A real profile with a pool, resolved without reading config."""
    work = profile.create("work")
    keys = [profile_keys.add(work, token) for token in tokens]
    monkeypatch.setattr(profile_usage, "_entries", {})
    monkeypatch.setattr(profile_usage, "_locks", {})
    monkeypatch.setattr(
        profile_usage, "_resolve", lambda selector: (work, "anthropic", "work/anthropic")
    )
    monkeypatch.setattr(profile_usage.usage, "reader", lambda _profile: reader)
    return work, keys


def _report(utilization):
    return usage.UsageReport(
        windows=[
            usage.UsageWindow(
                name="five_hour", utilization=utilization, resets_at=None
            )
        ],
        raw={},
        source="oauth-usage",
    )


def test_key_usage_reads_every_key_once_and_caches_each(home, monkeypatch):
    _work, keys = _keyed_pool(home, monkeypatch, "sk-AAAA1111", "sk-BBBB2222")
    calls = []

    def report(_profile, token):
        calls.append(token)
        return _report(10.0 if "AAAA" in token else 90.0)

    monkeypatch.setattr(profile_usage.usage, "report_for_token", report)

    first = profile_usage.key_usage("work")
    second = profile_usage.key_usage("work")

    assert calls == ["sk-AAAA1111", "sk-BBBB2222"]
    assert first["owner"] == "work"
    assert first["unmeasurable"] is None
    assert set(first["keys"]) == {key.id for key in keys}
    assert first["keys"][keys[0].id]["windows"][0]["utilization"] == 10.0
    assert first["keys"][keys[1].id]["windows"][0]["utilization"] == 90.0
    # The second call is answered from the cache: no second account read.
    assert second["keys"] == first["keys"]


def test_key_usage_skips_the_key_the_caller_already_read(home, monkeypatch):
    _work, keys = _keyed_pool(home, monkeypatch, "sk-AAAA1111", "sk-BBBB2222")
    calls = []
    monkeypatch.setattr(
        profile_usage.usage,
        "report_for_token",
        lambda _profile, token: calls.append(token) or _report(1.0),
    )

    doc = profile_usage.key_usage("work", skip=[keys[0].id])

    assert calls == ["sk-BBBB2222"]
    assert set(doc["keys"]) == {keys[1].id}


def test_key_usage_keeps_a_failed_key_out_of_the_others(home, monkeypatch):
    _work, keys = _keyed_pool(home, monkeypatch, "sk-AAAA1111", "sk-BBBB2222")

    def report(_profile, token):
        if "AAAA" in token:
            raise usage.UsageError("that key was revoked")
        return _report(42.0)

    monkeypatch.setattr(profile_usage.usage, "report_for_token", report)

    doc = profile_usage.key_usage("work")

    assert doc["keys"][keys[0].id]["error"] == "that key was revoked"
    assert doc["keys"][keys[0].id]["windows"] == []
    assert doc["keys"][keys[1].id]["windows"][0]["utilization"] == 42.0


def test_key_usage_names_a_login_scoped_reader_and_measures_nothing(
    home, monkeypatch
):
    _work, _keys = _keyed_pool(home, monkeypatch, "sk-AAAA1111", reader="codex-app-server")
    calls = []
    monkeypatch.setattr(
        profile_usage.usage,
        "report_for_token",
        lambda _profile, token: calls.append(token) or _report(1.0),
    )

    doc = profile_usage.key_usage("work")

    assert doc["keys"] == {}
    assert doc["owner"] == "work"
    assert "reports usage for its login" in doc["unmeasurable"]
    assert calls == []


def test_key_usage_reads_a_key_again_after_its_token_is_replaced(
    home, monkeypatch
):
    work, keys = _keyed_pool(home, monkeypatch, "sk-AAAA1111", "sk-BBBB2222")
    calls = []
    monkeypatch.setattr(
        profile_usage.usage,
        "report_for_token",
        lambda _profile, token: calls.append(token) or _report(1.0),
    )

    profile_usage.key_usage("work")
    # Same id, new token: the cached reading belongs to the old account, so
    # that key is read again — and only that key, since the other one's token
    # did not move.
    profile_keys.replace(work, "sk-CCCC3333", ident=keys[0].id)
    profile_usage.key_usage("work")

    assert calls == ["sk-AAAA1111", "sk-BBBB2222", "sk-CCCC3333"]


def test_key_usage_without_a_pool_reads_nothing(home, monkeypatch):
    work = profile.create("work")
    credentials.save_token(work, "sk-ONLY1111")
    monkeypatch.setattr(profile_usage, "_entries", {})
    monkeypatch.setattr(profile_usage, "_locks", {})
    monkeypatch.setattr(
        profile_usage, "_resolve", lambda selector: (work, "anthropic", "work/anthropic")
    )
    monkeypatch.setattr(profile_usage.usage, "reader", lambda _profile: "anthropic")

    doc = profile_usage.key_usage("work")

    assert doc == {"owner": None, "keys": {}, "unmeasurable": None}
