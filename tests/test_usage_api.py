"""Dashboard usage endpoint normalization and error boundary."""

import asyncio
import json
from types import SimpleNamespace

from claude_launcher.daemon import api
from claude_launcher import usage


def test_usage_endpoint_normalizes_report(monkeypatch):
    selected = SimpleNamespace(selector="work:codex")
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
    monkeypatch.setattr(api.profile_mod, "require_selector", lambda _: selected)
    monkeypatch.setattr(usage, "resolve_target", lambda value: value)
    monkeypatch.setattr(usage, "fetch", lambda value: report)
    request = SimpleNamespace(query={"profile": "work:codex"})

    response = asyncio.run(api.h_usage(request))

    assert response.status == 200
    body = json.loads(response.text)
    assert body == {
        "profile": "work:codex",
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
