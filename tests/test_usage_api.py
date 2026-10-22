"""Dashboard usage endpoint normalization and error boundary."""

import asyncio
import json
from types import SimpleNamespace

from claude_launcher.daemon import api
from claude_launcher import profile_usage, usage


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
    assert body.pop("updated_at") == float(response.headers["X-Usage-Updated-At"])
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
    assert json.loads(response.text) == {"targets": rows}
