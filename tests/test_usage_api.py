"""Dashboard usage endpoint normalization and error boundary."""

import asyncio
import json
from types import SimpleNamespace

from claude_launcher.daemon import api
from claude_launcher import profile_usage, usage, usage_history


def test_usage_endpoint_normalizes_report(monkeypatch):
    selected = SimpleNamespace(selector="work:codex", name="work")
    report = usage.UsageReport(
        windows=[
            usage.UsageWindow(
                name="five_hour",
                utilization=12.5,
                resets_at="2030-01-01T00:00:00+00:00",
                status="ok",
            )
        ],
        raw={"ignored": True},
        source="codex-app-server",
    )
    monkeypatch.setattr(profile_usage, "_entries", {})
    monkeypatch.setattr(profile_usage, "_locks", {})
    monkeypatch.setattr(
        profile_usage, "_resolve", lambda _: (selected, "openai", "work/openai")
    )
    monkeypatch.setattr(profile_usage.lineage, "effective_harness", lambda _p: "codex")
    monkeypatch.setattr(usage, "fetch", lambda value: report)
    request = SimpleNamespace(query={"profile": "work:codex"})

    response = asyncio.run(api.h_usage(request))

    assert response.status == 200
    body = json.loads(response.text)
    # The cache facts are in the body too: a read through /api/batch gets no
    # headers (claunch-f5w4x.1).
    assert body.pop("cached") is False
    updated_at = body.pop("updated_at")
    assert updated_at == float(response.headers["X-Usage-Updated-At"])
    # When the daemon's clock reads the key again (claunch-f5w4x.3).
    assert body.pop("next_at") == updated_at + profile_usage.PERIOD_SECONDS
    assert response.headers["X-Usage-Cached"] == "false"
    assert body == {
        "key": "work/openai",
        "profile": "work",
        "provider": "openai",
        "harness": "codex",
        "source": "codex-app-server",
        "windows": [
            {
                "name": "five_hour",
                "utilization": 12.5,
                "resets_at": "2030-01-01T00:00:00+00:00",
                "used_dollars": None,
                "limit_dollars": None,
                "remaining_dollars": None,
                "status": "ok",
            }
        ],
    }


def test_usage_endpoint_requires_profile():
    response = asyncio.run(api.h_usage(SimpleNamespace(query={})))
    assert response.status == 400
    assert json.loads(response.text)["error"] == "profile is required"


def test_usage_targets_endpoint_lists_profile_provider_rows(monkeypatch):
    rows = [{"profile": "work", "provider": "claude", "supported": True}]
    monkeypatch.setattr(profile_usage, "targets", lambda: rows)

    response = asyncio.run(api.h_usage_targets(SimpleNamespace(query={})))

    assert response.status == 200
    body = json.loads(response.text)
    assert body["targets"] == rows
    # The period the page counts down with, and what the history weighs.
    assert body["period_s"] == profile_usage.PERIOD_SECONDS
    assert body["history"]["bytes"] == 0 and body["history"]["files"] == 0


def test_usage_history_endpoint_answers_samples_and_disk_stats():
    """claunch-f5w4x.3: one key's samples (time and each window's
    utilization and reset) and what its file weighs."""
    key = "work/default/anthropic"
    window = {"name": "five_hour", "utilization": 4.0, "resets_at": "2030-01-01T00:00:00Z",
              "used_dollars": None, "limit_dollars": None, "status": "allowed"}
    for at in (1000.0, 1900.0, 2800.0):
        usage_history.append(key, {"key": key, "source": "oauth-usage", "windows": [window]}, at)

    response = asyncio.run(api.h_usage_history(
        SimpleNamespace(query={"key": key, "since": "1900"})))

    assert response.status == 200
    body = json.loads(response.text)
    assert [s["at"] for s in body["samples"]] == [1900.0, 2800.0]
    assert body["samples"][0]["windows"] == [
        {"name": "five_hour", "utilization": 4.0, "resets_at": "2030-01-01T00:00:00Z"}]
    stats = body["stats"]
    assert stats["samples"] == 3
    assert stats["bytes"] == usage_history.path_for(key).stat().st_size
    assert stats["basis"] == "projected"


def test_usage_history_samples_carry_the_dollar_amounts_a_reading_has():
    """claunch-f5w4x.5: a Fireworks sample is charted in dollars, so the
    endpoint keeps the dollar fields that are set (and only those)."""
    key = "fw/fireworks/fireworks-billing"
    windows = [
        {"name": "monthly_spend", "utilization": 0.0, "resets_at": "2026-11-01T00:00:00Z",
         "used_dollars": 247.84, "limit_dollars": None, "remaining_dollars": None},
        {"name": "credit", "utilization": 49.4, "resets_at": None,
         "used_dollars": 247.0, "limit_dollars": 500.0, "remaining_dollars": 253.0},
    ]
    usage_history.append(key, {"key": key, "source": "fireworks-billing", "windows": windows}, 1000.0)

    body = json.loads(asyncio.run(api.h_usage_history(SimpleNamespace(query={"key": key}))).text)

    assert body["samples"][0]["windows"] == [
        {"name": "monthly_spend", "utilization": 0.0, "resets_at": "2026-11-01T00:00:00Z",
         "used_dollars": 247.84},
        {"name": "credit", "utilization": 49.4, "resets_at": None,
         "used_dollars": 247.0, "limit_dollars": 500.0, "remaining_dollars": 253.0},
    ]


def test_usage_history_endpoint_rejects_a_missing_key_and_a_bad_since():
    for query, error in (({}, "key is required"),
                         ({"key": "k", "since": "soon"}, "since must be epoch seconds")):
        response = asyncio.run(api.h_usage_history(SimpleNamespace(query=query)))
        assert response.status == 400
        assert json.loads(response.text)["error"] == error


# --- the create form's Runs-as readout (claunch-yx2bd) ----------------------
#
# One answer for the form: the profile's own usage, the pool a launch of it
# selects from with each key's own usage, the key the next launch takes, and
# the strategies that can be set. Nothing here may carry a plaintext token.


def _view(**over):
    return {
        "profile": "work",
        "owner": None,
        "pooled": False,
        "keys": [],
        **over,
    }


def _body():
    return {
        "source": "oauth-usage",
        "windows": [
            {
                "name": "five_hour",
                "utilization": 12.5,
                "resets_at": None,
                "used_dollars": None,
                "limit_dollars": None,
                "remaining_dollars": None,
                "status": "ok",
            }
        ],
    }


def test_usage_keys_reads_a_single_token_profile(monkeypatch):
    prof = SimpleNamespace(name="work")
    monkeypatch.setattr(api.profile_keys, "view", lambda _p: _view())
    monkeypatch.setattr(
        api.profile_usage, "fetch", lambda _s, manual=False: (_body(), True, 1000.0)
    )
    asked = []
    monkeypatch.setattr(
        api.profile_usage,
        "key_usage",
        lambda selector, manual=False, skip=(): asked.append(skip)
        or {"owner": None, "keys": {}, "unmeasurable": None},
    )

    doc = api._credential_preview(prof, "work", False)

    assert doc["pooled"] is False
    assert doc["keys"] == []
    assert doc["strategy"] is None
    assert doc["next"] is None
    assert doc["usage"]["windows"][0]["utilization"] == 12.5
    assert doc["usage"]["cached"] is True
    # When the daemon's clock reads the account again.
    assert doc["usage"]["next_at"] == 1000.0 + profile_usage.PERIOD_SECONDS
    # No pool: nothing to skip and nothing measured per key.
    assert asked == [[]]


def test_usage_keys_marks_the_next_key_and_reuses_the_effective_reading(monkeypatch):
    prof = SimpleNamespace(name="work")
    keys = [
        {
            "id": "k-1", "label": "main", "masked": "sk-A…1111",
            "current": True, "use_count": 2,
        },
        {
            "id": "k-2", "label": "spare", "masked": "sk-B…2222",
            "current": False, "use_count": 0,
        },
    ]
    monkeypatch.setattr(
        api.profile_keys,
        "view",
        lambda _p: _view(pooled=True, owner="work", strategy="rotation", keys=keys),
    )
    monkeypatch.setattr(
        api.profile_keys,
        "peek",
        lambda _p: SimpleNamespace(
            key=SimpleNamespace(id="k-2"), strategy="rotation", fallback=None
        ),
    )
    monkeypatch.setattr(
        api.profile_usage, "fetch", lambda _s, manual=False: (_body(), False, 900.0)
    )
    asked = []

    def key_usage(selector, manual=False, skip=()):
        asked.append(list(skip))
        return {
            "owner": "work",
            "keys": {
                "k-2": {
                    "key_id": "k-2",
                    "windows": [{"name": "five_hour", "utilization": 60.0}],
                    "source": "oauth-usage",
                    "updated_at": 950.0,
                    "error": None,
                }
            },
            "unmeasurable": None,
        }

    monkeypatch.setattr(api.profile_usage, "key_usage", key_usage)

    doc = api._credential_preview(prof, "work", False)

    # The key in the token file is the profile's own reading; it is not read
    # twice.
    assert asked == [["k-1"]]
    assert doc["next"] == {"key_id": "k-2", "strategy": "rotation", "fallback": None}
    assert [row["next"] for row in doc["keys"]] == [False, True]
    assert doc["keys"][0]["usage"]["windows"][0]["utilization"] == 12.5
    assert doc["keys"][1]["usage"]["windows"][0]["utilization"] == 60.0
    assert "sk-AAAA" not in json.dumps(doc)
    assert "sk-BBBB" not in json.dumps(doc)


def test_usage_keys_does_not_preview_a_usage_strategy(monkeypatch):
    prof = SimpleNamespace(name="work")
    keys = [{"id": "k-1", "masked": "sk-A…1111", "current": False},
            {"id": "k-2", "masked": "sk-B…2222", "current": False}]
    monkeypatch.setattr(
        api.profile_keys,
        "view",
        lambda _p: _view(pooled=True, owner="work", strategy="usage-min", keys=keys),
    )

    def no_peek(_p):
        raise AssertionError("a usage strategy must not be previewed")

    monkeypatch.setattr(api.profile_keys, "peek", no_peek)
    monkeypatch.setattr(
        api.profile_usage, "fetch", lambda _s, manual=False: (_body(), True, 1000.0)
    )
    monkeypatch.setattr(
        api.profile_usage,
        "key_usage",
        lambda selector, manual=False, skip=(): {
            "owner": "work", "keys": {}, "unmeasurable": None
        },
    )

    doc = api._credential_preview(prof, "work", False)

    assert doc["next"] is None
    assert "usage-min" in doc["next_unknown"]
    assert "lowest" in doc["next_unknown"]


def test_usage_keys_does_not_name_a_random_pick(monkeypatch):
    prof = SimpleNamespace(name="work")
    keys = [{"id": "k-1", "current": False}, {"id": "k-2", "current": False}]
    monkeypatch.setattr(
        api.profile_keys,
        "view",
        lambda _p: _view(pooled=True, owner="work", strategy="random", keys=keys),
    )
    monkeypatch.setattr(
        api.profile_keys,
        "peek",
        lambda _p: (_ for _ in ()).throw(AssertionError("random is not a fact")),
    )
    monkeypatch.setattr(
        api.profile_usage, "fetch", lambda _s, manual=False: (_body(), True, 1000.0)
    )
    monkeypatch.setattr(
        api.profile_usage,
        "key_usage",
        lambda selector, manual=False, skip=(): {
            "owner": "work", "keys": {}, "unmeasurable": None
        },
    )

    doc = api._credential_preview(prof, "work", False)

    assert doc["next"] is None
    assert "random" in doc["next_unknown"]


def test_usage_keys_reports_a_failed_read_without_failing_the_answer(monkeypatch):
    prof = SimpleNamespace(name="work")
    monkeypatch.setattr(api.profile_keys, "view", lambda _p: _view())

    def boom(_s, manual=False):
        raise usage.UsageError("no reader applies")

    monkeypatch.setattr(api.profile_usage, "fetch", boom)
    monkeypatch.setattr(
        api.profile_usage,
        "key_usage",
        lambda selector, manual=False, skip=(): {
            "owner": None, "keys": {}, "unmeasurable": None
        },
    )

    doc = api._credential_preview(prof, "work", False)

    assert doc["usage"] is None
    assert doc["usage_error"] == "no reader applies"


def test_usage_keys_endpoint_requires_profile():
    response = asyncio.run(api.h_usage_keys(SimpleNamespace(query={})))
    assert response.status == 400
    assert json.loads(response.text)["error"] == "profile is required"


def test_strategy_endpoint_writes_the_pools_own_profile(monkeypatch):
    prof = SimpleNamespace(name="child")
    source = SimpleNamespace(name="base")
    monkeypatch.setattr(api.profile_mod, "require_selector", lambda _s: prof)
    monkeypatch.setattr(api.profile_keys, "owner", lambda _p: source)
    written = []

    def set_strategy(profile_arg, strategy):
        written.append((profile_arg.name, strategy))
        return SimpleNamespace(strategy=strategy, keys=["a", "b"])

    monkeypatch.setattr(api.profile_keys, "set_strategy", set_strategy)

    async def body(_request):
        return {"strategy": "usage-min"}

    monkeypatch.setattr(api, "_json_body", body)
    request = SimpleNamespace(match_info={"profile": "child"})

    response = asyncio.run(api.h_profile_key_strategy(request))

    assert response.status == 200
    assert json.loads(response.text) == {
        "profile": "child",
        "owner": "base",
        "strategy": "usage-min",
        "keys": 2,
    }
    # The strategy belongs to the pool, which for a child holding no token of
    # its own is its ancestor's (claunch-wllke.5.1).
    assert written == [("base", "usage-min")]


def test_strategy_endpoint_refuses_a_profile_without_a_pool(monkeypatch):
    prof = SimpleNamespace(name="work")
    monkeypatch.setattr(api.profile_mod, "require_selector", lambda _s: prof)
    monkeypatch.setattr(api.profile_keys, "owner", lambda _p: None)

    response = asyncio.run(
        api.h_profile_key_strategy(SimpleNamespace(match_info={"profile": "work"}))
    )

    assert response.status == 409
    assert "no key pool" in json.loads(response.text)["error"]
