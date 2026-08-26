"""Harness-specific usage reporting and its explicit support boundary."""

from __future__ import annotations

import io
import json

import pytest

from claude_launcher import profile, providers, store, usage


class _FakeStdin(io.StringIO):
    def close(self):
        # Keep the request inspectable after the client closes its side.
        self.closed_by_client = True


class _FakeProcess:
    def __init__(self, reply):
        self.stdin = _FakeStdin()
        self.stdout = iter([json.dumps({"id": 1, "result": {}}) + "\n", json.dumps(reply) + "\n"])
        self.stderr = io.StringIO("")
        self._returncode = None

    def poll(self):
        return self._returncode

    def terminate(self):
        self._returncode = 0

    def wait(self, timeout=None):
        self._returncode = 0
        return 0

    def kill(self):
        self._returncode = -9


def test_codex_usage_uses_the_profile_home_and_app_server(home, monkeypatch):
    p = profile.create("codex-work")
    selected = profile.require_selector("codex-work:codex")
    reply = {
        "id": 2,
        "result": {
            "rateLimits": {
                "primary": {
                    "usedPercent": 12,
                    "windowDurationMins": 300,
                    "resetsAt": 1787644800,
                },
                "secondary": {
                    "usedPercent": 34,
                    "windowDurationMins": 10080,
                    "resetsAt": 1788249600,
                },
                "rateLimitReachedType": None,
            }
        },
    }
    reached = {}

    def fake_popen(cmd, **kwargs):
        reached["cmd"] = cmd
        reached["env"] = kwargs["env"]
        reached["process"] = _FakeProcess(reply)
        return reached["process"]

    monkeypatch.setattr(usage.subprocess, "Popen", fake_popen)

    report = usage.fetch(selected)

    assert reached["cmd"][-1:] == ["app-server"]
    assert reached["env"]["CODEX_HOME"] == str(p.config_dir / "codex")
    sent = reached["process"].stdin.getvalue()
    assert '"account/rateLimits/read"' in sent
    assert report.source == "codex-app-server"
    assert [(w.name, w.utilization) for w in report.windows] == [
        ("five_hour", 12.0),
        ("seven_day", 34.0),
    ]


def test_codex_usage_reports_every_documented_limit_bucket():
    payload = {
        "rateLimitsByLimitId": {
            "codex": {
                "limitId": "codex",
                "primary": {"usedPercent": 25, "windowDurationMins": 15},
            },
            "codex_other": {
                "limitName": "other",
                "primary": {"usedPercent": 42, "windowDurationMins": 60},
            },
        }
    }

    assert [(w.name, w.utilization) for w in usage._codex_windows(payload)] == [
        ("codex.15_minute", 25.0),
        ("other.60_minute", 42.0),
    ]


@pytest.mark.parametrize("name", ["pi", "kimi", "agent"])
def test_usage_refuses_harnesses_without_a_supported_api(home, name):
    p = profile.create(name)
    with pytest.raises(usage.UsageError, match="not available"):
        usage.fetch(profile.require_selector(f"{p.name}:{name}"))


def test_claude_usage_refuses_a_third_party_provider(home):
    p = profile.create("kimi-through-claude")
    store.update(
        lambda doc: doc.update(
            {"providers": {"kimi": {"env": {"ANTHROPIC_BASE_URL": "https://example"}}}}
        )
    )
    providers.set_profile_selection(p, "kimi")

    with pytest.raises(usage.UsageError, match="default Anthropic provider"):
        usage.fetch(profile.require_selector(f"{p.name}:claude"))
