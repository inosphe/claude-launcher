"""Harness-specific usage reporting and its explicit support boundary."""

from __future__ import annotations

import io
import json

import pytest

from claude_launcher import cli, credentials, profile, providers, store, usage


class _FakeStdin(io.StringIO):
    def close(self):
        # Keep the request inspectable after the client closes its side.
        self.closed_by_client = True


class _FakeProcess:
    def __init__(self, reply):
        self.stdin = _FakeStdin()
        self.stdout = iter(
            [json.dumps({"id": 1, "result": {}}) + "\n", json.dumps(reply) + "\n"]
        )
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


class _FakeWebProcess:
    def __init__(self):
        self.stdout = iter(
            ["Kimi server: http://127.0.0.1:54321/#token=server-secret\n"]
        )
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


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


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


def test_bare_codex_profile_uses_its_initialized_same_name_harness(home):
    p = profile.create("codex")
    (p.config_dir / "codex").mkdir()

    selected = usage.resolve_target(p)

    assert selected.selector == "codex:codex"
    explicit = profile.require_selector("codex:claude")
    assert usage.resolve_target(explicit) == explicit


def test_cli_bare_codex_and_kimi_select_their_available_usage_sources(
    home, monkeypatch, capsys
):
    codex = profile.create("codex")
    (codex.config_dir / "codex").mkdir()
    kimi = profile.create("kimi")
    store.update(
        lambda doc: doc.update(
            {
                "providers": {
                    "kimi": {
                        "env": {"ANTHROPIC_BASE_URL": "https://api.kimi.com/coding/"}
                    }
                }
            }
        )
    )
    providers.set_profile_selection(kimi, "kimi")
    credentials.save_token(kimi, "kimi-secret")
    selected = []

    def fake_fetch(p):
        selected.append(p.selector)
        return usage.UsageReport(windows=[], raw={}, source="test")

    monkeypatch.setattr(usage, "fetch", fake_fetch)

    assert cli.main(["usage", "codex", "--json"]) == 0
    assert cli.main(["usage", "kimi", "--json"]) == 0
    capsys.readouterr()
    assert selected == ["codex:codex", "kimi"]


def test_kimi_provider_uses_the_managed_usage_endpoint(home, monkeypatch):
    p = profile.create("kimi")
    store.update(
        lambda doc: doc.update(
            {
                "providers": {
                    "kimi": {
                        "env": {"ANTHROPIC_BASE_URL": "https://api.kimi.com/coding/"}
                    }
                }
            }
        )
    )
    providers.set_profile_selection(p, "kimi")
    credentials.save_token(p, "kimi-secret")
    # A configured provider remains the bare profile's source even if a
    # same-name harness home has also been initialized.
    (p.config_dir / "kimi").mkdir()
    reached = {}
    payload = {
        "usage": {
            "used": "40",
            "limit": "100",
            "resetTime": "2030-01-01T00:00:00Z",
        },
        "limits": [
            {
                "window": {
                    "duration": 300,
                    "timeUnit": "TIME_UNIT_MINUTE",
                },
                "detail": {"used": "10", "limit": "100"},
            },
            {
                "window": {"duration": 7, "timeUnit": "TIME_UNIT_DAY"},
                "detail": {"used": "20", "limit": "80"},
            },
        ],
    }

    def fake_urlopen(req, timeout, context):
        reached["url"] = req.full_url
        reached["authorization"] = req.get_header("Authorization")
        reached["timeout"] = timeout
        reached["certificate_verification"] = (
            context.check_hostname and context.verify_mode == usage.ssl.CERT_REQUIRED
        )
        reached["x509_strict"] = bool(
            context.verify_flags & usage.ssl.VERIFY_X509_STRICT
        )
        return _FakeResponse(json.dumps(payload).encode())

    monkeypatch.setattr(usage.urllib.request, "urlopen", fake_urlopen)

    selected = usage.resolve_target(p)
    report = usage.fetch(selected)

    assert selected.selector == "kimi"
    assert reached == {
        "url": "https://api.kimi.com/coding/v1/usages",
        "authorization": "Bearer kimi-secret",
        "timeout": 30,
        "certificate_verification": True,
        "x509_strict": False,
    }
    assert report.source == "kimi-managed-usage"
    assert [(w.name, w.utilization) for w in report.windows] == [
        ("weekly", 40.0),
        ("five_hour", 10.0),
        ("seven_day", 25.0),
    ]


def test_kimi_harness_uses_its_profile_home_and_authenticated_server(home, monkeypatch):
    p = profile.create("kimi-work")
    selected = profile.require_selector("kimi-work:kimi")
    reached = {}
    envelope = {
        "code": 0,
        "data": {
            "kind": "ok",
            "summary": {
                "window": {"duration": 1, "unit": "week"},
                "used": 30,
                "limit": 100,
                "reset_at": "2030-01-01T00:00:00Z",
            },
            "limits": [
                {
                    "window": {"duration": 5, "unit": "hour"},
                    "used": 1,
                    "limit": 4,
                }
            ],
            "extra_usage": None,
        },
    }

    def fake_popen(cmd, **kwargs):
        reached["cmd"] = cmd
        reached["env"] = kwargs["env"]
        return _FakeWebProcess()

    def fake_urlopen(req, timeout, context):
        if req.full_url.endswith("/api/v1/shutdown"):
            reached["shutdown"] = {
                "method": req.get_method(),
                "authorization": req.get_header("Authorization"),
            }
            return _FakeResponse(b"{}")
        reached["url"] = req.full_url
        reached["authorization"] = req.get_header("Authorization")
        reached["timeout"] = timeout
        reached["certificate_verification"] = (
            context.check_hostname and context.verify_mode == usage.ssl.CERT_REQUIRED
        )
        return _FakeResponse(json.dumps(envelope).encode())

    monkeypatch.setattr(usage.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(usage.urllib.request, "urlopen", fake_urlopen)

    report = usage.fetch(selected)

    assert reached["cmd"][-6:] == [
        "web",
        "--no-open",
        "--port",
        "0",
        "--log-level",
        "error",
    ]
    assert reached["env"]["KIMI_CODE_HOME"] == str(p.config_dir / "kimi")
    assert reached["url"] == "http://127.0.0.1:54321/api/v1/oauth/usage"
    assert reached["authorization"] == "Bearer server-secret"
    assert reached["certificate_verification"] is True
    assert reached["shutdown"] == {
        "method": "POST",
        "authorization": "Bearer server-secret",
    }
    assert report.source == "kimi-web-server"
    assert [(w.name, w.utilization) for w in report.windows] == [
        ("weekly", 30.0),
        ("five_hour", 25.0),
    ]


def test_kimi_harness_surfaces_the_local_server_usage_error(home, monkeypatch):
    profile.create("kimi-work")
    selected = profile.require_selector("kimi-work:kimi")
    monkeypatch.setattr(
        usage.subprocess, "Popen", lambda _cmd, **_kwargs: _FakeWebProcess()
    )
    monkeypatch.setattr(
        usage,
        "_kimi_request",
        lambda _url, _token: {
            "code": 0,
            "data": {"kind": "error", "status": 401, "message": "login required"},
        },
    )
    monkeypatch.setattr(usage, "_shutdown_kimi_server", lambda _origin, _token: None)

    with pytest.raises(usage.UsageError, match="login required"):
        usage.fetch(selected)


@pytest.mark.parametrize("name", ["pi", "agent"])
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

    with pytest.raises(usage.UsageError, match="Anthropic service provider"):
        usage.fetch(profile.require_selector(f"{p.name}:claude"))


def test_claude_provider_uses_anthropic_usage_api(home, monkeypatch):
    p = profile.create("work")
    providers.set_profile_selection(p, "claude")
    credentials.save_token(p, "oauth-token")
    reached = {}

    def fake_usage(url, token):
        reached.update(url=url, token=token)
        return {"five_hour": {"utilization": 0.25}}

    monkeypatch.setattr(usage, "_oauth_usage", fake_usage)

    report = usage.fetch(profile.require_selector("work:claude"))

    assert reached == {"url": usage.config.usage_url(), "token": "oauth-token"}
    assert report.source == "oauth-usage"
