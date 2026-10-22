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


def test_usage_history_endpoint_rejects_a_missing_key_and_a_bad_since():
    for query, error in (({}, "key is required"),
                         ({"key": "k", "since": "soon"}, "since must be epoch seconds")):
        response = asyncio.run(api.h_usage_history(SimpleNamespace(query=query)))
        assert response.status == 400
        assert json.loads(response.text)["error"] == error
