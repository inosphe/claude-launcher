"""Installing a provider from Settings: presets, validation, the write, the probe.

The load-bearing claims are three, and each is a test below:

* a refusal writes nothing — the config file's bytes are compared across a 400
  (:func:`test_a_refused_entry_leaves_the_file_byte_identical`);
* a preset file that does not parse marks only itself
  (:func:`test_one_broken_preset_does_not_hide_the_others`);
* the form can express every provider shape this machine actually runs —
  endpoints anthropic-only / openai-only / both, authentication as a file key,
  a profile token or none, the model roles, the context fields, the header
  facts and the policy fields (:func:`test_every_installed_shape_round_trips`).
"""

from __future__ import annotations

import asyncio
import hashlib
import json

import yaml

from claude_launcher import provider_install, provider_presets, store
from claude_launcher.daemon import api
from claude_launcher.profile import create as create_profile

# --------------------------------------------------------------------------- #
# presets
# --------------------------------------------------------------------------- #
GOOD_PRESET = """\
label: Example gateway
note: serves both protocols
provider:
  endpoints:
    anthropic: https://gateway.example.com
    openai: https://gateway.example.com/v1
  models: {default: some/model, small: some/model}
  context_window: 200000
"""


def _write_preset(base, name, text):
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"{name}.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_presets_load_from_their_own_directory(home):
    base = provider_presets.directory()
    assert base == home / provider_presets.DIRNAME
    _write_preset(base, "gateway", GOOD_PRESET)
    rows = provider_presets.load_all()
    assert [row.name for row in rows] == ["gateway"]
    assert rows[0].error == ""
    assert rows[0].label == "Example gateway"
    assert rows[0].entry["endpoints"]["openai"] == "https://gateway.example.com/v1"
    assert "label" not in rows[0].entry and "note" not in rows[0].entry


def test_a_preset_may_be_a_bare_entry(home):
    """A block copied out of ~/.claunch.yaml is the common case, not the broken one."""
    _write_preset(provider_presets.directory(), "copied", """\
endpoints: {openai: https://api.example.com/v1}
models: {default: m}
""")
    rows = provider_presets.load_all()
    assert rows[0].error == ""
    assert rows[0].entry["models"]["default"] == "m"


def test_one_broken_preset_does_not_hide_the_others(home):
    base = provider_presets.directory()
    _write_preset(base, "good", GOOD_PRESET)
    _write_preset(base, "broken", "provider: [unclosed\n")
    _write_preset(base, "typo", "endpionts: {openai: https://x.example.com}\n")
    by_name = {row.name: row for row in provider_presets.load_all()}
    assert by_name["good"].error == ""
    assert by_name["broken"].error, "a YAML parse failure must be reported"
    assert "endpionts" in by_name["typo"].error


def test_preset_names_cannot_escape_the_directory(home):
    import pytest

    with pytest.raises(provider_presets.PresetError):
        provider_presets.path_for("../outside")
    with pytest.raises(provider_presets.PresetError):
        provider_presets.path_for("")


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #
def _valid(**over):
    entry = {
        "endpoints": {"openai": "https://api.example.com/v1"},
        "models": {"default": "some/model"},
    }
    entry.update(over)
    return entry


def test_a_valid_entry_has_no_problems(home):
    assert provider_install.validate("demo", _valid()) == []


def test_a_field_name_typo_is_refused(home):
    problems = provider_install.validate("demo", {"endpionts": {"openai": "https://x.example.com"}})
    assert problems and "endpionts" in problems[0]


def test_an_entry_without_endpoints_is_refused(home):
    problems = provider_install.validate("demo", {"models": {"default": "m"}})
    assert any("endpoint" in p for p in problems)


def test_auto_compact_above_context_is_refused(home):
    problems = provider_install.validate(
        "demo", _valid(context_window=1000, auto_compact_at=2000)
    )
    assert any("auto_compact_at" in p for p in problems)


def test_a_bad_endpoint_scheme_is_refused(home):
    problems = provider_install.validate(
        "demo", {"endpoints": {"anthropic": "ftp://api.example.com"}, "models": {"default": "m"}}
    )
    assert any("http" in p for p in problems)


def test_the_launchers_own_providers_cannot_be_shadowed(home):
    problems = provider_install.validate("default", _valid())
    assert any("default" in p for p in problems)


def test_the_facts_other_modules_own_are_checked_by_them(home):
    """session_affinity/session_header/allowed_harnesses are not re-implemented here."""
    problems = provider_install.validate("demo", _valid(session_affinity="yes"))
    assert any("session_affinity" in p for p in problems)
    problems = provider_install.validate("demo", _valid(session_header="not a header!"))
    assert any("session_header" in p for p in problems)
    problems = provider_install.validate("demo", _valid(anthropic_auth_header="x-auth"))
    assert any("anthropic_auth_header" in p for p in problems)
    problems = provider_install.validate("demo", _valid(allowed_harnesses="claude"))
    assert any("allowed_harnesses" in p for p in problems)


# --------------------------------------------------------------------------- #
# the write
# --------------------------------------------------------------------------- #
def _config_bytes():
    return store.path().read_bytes()


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def test_save_writes_the_entry_the_readers_see(home):
    provider_install.save("demo", _valid())
    doc = store.load()
    assert doc["providers"]["demo"]["models"]["default"] == "some/model"
    # The same read `claunch providers` makes.
    assert "demo" in provider_install.listing()["providers"][0]["name"]


def test_a_refused_entry_leaves_the_file_byte_identical(home):
    provider_install.save("demo", _valid())
    before = _sha(_config_bytes())
    try:
        provider_install.save("bad", {"endpionts": {"openai": "https://x.example.com"}})
    except provider_install.InstallError as exc:
        assert "endpionts" in str(exc)
    else:  # pragma: no cover - the refusal is the point
        raise AssertionError("a typo'd entry was accepted")
    assert _sha(_config_bytes()) == before
    assert "bad" not in (store.load().get("providers") or {})


def test_save_can_pin_a_profile_in_the_same_write(home):
    profile = create_profile("work")
    provider_install.save("demo", _valid(), profile=profile.name)
    assert store.profile_entry("work")["provider"] == "demo"
    rows = {row["name"]: row for row in provider_install.listing()["profiles"]}
    assert rows["work"]["provider"] == "demo"


def test_a_pin_to_a_missing_profile_is_refused_and_nothing_is_written(home):
    import pytest

    from claude_launcher.profile import ProfileError

    before = _sha(_config_bytes()) if store.path().is_file() else ""
    with pytest.raises(ProfileError):
        provider_install.save("demo", _valid(), profile="../escape")
    after = _sha(_config_bytes()) if store.path().is_file() else ""
    assert before == after


# --------------------------------------------------------------------------- #
# every shape this machine runs
# --------------------------------------------------------------------------- #
SHAPES = [
    # anthropic-only (a company gateway, a vendor's official endpoint)
    {"endpoints": {"anthropic": "https://api.deepseek.com/anthropic"},
     "models": {"default": "deepseek-flash", "small": "deepseek-flash"}},
    # openai-only
    {"endpoints": {"openai": "https://api.example.com/v1"},
     "models": {"default": "owner/model"}},
    # both protocols, a million-token window and the tag that goes with it
    {"endpoints": {"anthropic": "https://apis.opengateway.ai",
                   "openai": "https://apis.opengateway.ai/v1"},
     "models": {"default": "deepseek/deepseek-v4.1-flash-ultrafast",
                "small": "deepseek/deepseek-v4.1-flash-ultrafast",
                "large": "deepseek/deepseek-v4.1-flash-ultrafast",
                "xlarge": "deepseek/deepseek-v4.1-flash-ultrafast",
                "subagent": "deepseek/deepseek-v4.1-flash-ultrafast"},
     "context_window": 1000000,
     "auto_compact_at": 900000,
     "harness_options": {"claude": {"model_tag": ""}}},
    # a key in the file, the sticky-session header, a reasoning format
    {"api_key": "sk-test",
     "endpoints": {"openai": "https://api.fireworks.ai/inference/v1"},
     "models": {"default": "accounts/fireworks/models/x"},
     "session_affinity": True,
     "openai_reasoning_format": "openai",
     "reasoning_effort": "high"},
    # a session header plus the credential moved to x-api-key
    {"endpoints": {"anthropic": "https://opencode.example.com"},
     "models": {"default": "claude-sonnet-5-5"},
     "session_header": "x-opencode-session",
     "anthropic_auth_header": "x-api-key"},
    # a policy-only provider: no endpoint, an allow-list
    {"allowed_harnesses": ["codex"], "service": "custom"},
]


def test_every_installed_shape_round_trips(home):
    for shape in SHAPES:
        # The last shape has no endpoint on purpose: it is a policy-only entry
        # (``allowed_harnesses``), the shape this machine runs for claude,
        # codex and devin. Every other shape is a backend.
        assert provider_install.validate("shape", shape) == [], shape
    for index, shape in enumerate(SHAPES):
        provider_install.save(f"shape{index}", shape)
    doc = store.load()
    assert doc["providers"]["shape2"]["context_window"] == 1000000
    assert doc["providers"]["shape3"]["session_affinity"] is True
    assert doc["providers"]["shape4"]["session_header"] == "x-opencode-session"
    assert doc["providers"]["shape5"]["allowed_harnesses"] == ["codex"]


# --------------------------------------------------------------------------- #
# connection check
# --------------------------------------------------------------------------- #
def test_openai_providers_are_asked_for_their_model_list(home):
    method, url, headers, body = provider_install.probe_request(
        {"endpoints": {"openai": "https://apis.opengateway.ai/v1"},
         "models": {"default": "m"}},
        "sk-1",
    )
    assert (method, url) == ("GET", "https://apis.opengateway.ai/v1/models")
    assert headers["Authorization"] == "Bearer sk-1"
    assert body is None


def test_anthropic_providers_are_asked_for_one_message(home):
    method, url, headers, body = provider_install.probe_request(
        {"endpoints": {"anthropic": "https://api.deepseek.com/anthropic"},
         "models": {"default": "deepseek-flash"}},
        "sk-2",
    )
    assert (method, url) == ("POST", "https://api.deepseek.com/anthropic/v1/messages")
    assert headers["Authorization"] == "Bearer sk-2"
    assert body["max_tokens"] == 1 and body["model"] == "deepseek-flash"


def test_the_messages_credential_follows_the_entrys_header_choice(home):
    _, _, headers, _ = provider_install.probe_request(
        {"endpoints": {"anthropic": "https://opencode.example.com"},
         "models": {"default": "m"},
         "anthropic_auth_header": "x-api-key"},
        "sk-3",
    )
    assert headers["x-api-key"] == "sk-3"
    assert "Authorization" not in headers


def test_a_base_that_already_ends_in_v1_is_not_doubled(home):
    _, url, _, _ = provider_install.probe_request(
        {"endpoints": {"anthropic": "https://x.example.com/v1"},
         "models": {"default": "m"}},
    )
    assert url == "https://x.example.com/v1/messages"


def test_the_probe_offers_both_protocols_when_the_entry_has_both(home):
    method, url, _, _ = provider_install.probe_request(
        {"endpoints": {"anthropic": "https://x.example.com",
                       "openai": "https://x.example.com/v1"},
         "models": {"default": "m"}},
    )
    assert method == "GET" and url.endswith("/models")


# --------------------------------------------------------------------------- #
# the daemon routes
# --------------------------------------------------------------------------- #
class _Request:
    def __init__(self, payload=None):
        self._payload = payload

    async def json(self):
        if self._payload is None:
            raise ValueError("no body")
        return self._payload


def _call(handler, payload=None):
    resp = asyncio.run(handler(_Request(payload)))
    return resp.status, json.loads(resp.text)


def test_get_lists_providers_presets_and_profiles(home):
    _write_preset(provider_presets.directory(), "gateway", GOOD_PRESET)
    store.update(lambda doc: doc.setdefault("providers", {}).update({"demo": _valid()}))
    status, body = _call(api.h_providers)
    assert status == 200
    assert [row["name"] for row in body["providers"]] == ["demo"]
    assert [row["name"] for row in body["presets"]] == ["gateway"]
    assert "roles" in body["fields"] and "anthropic_auth_header" in body["fields"]["fields"]


def test_post_saves_and_answers_with_the_fresh_listing(home):
    status, body = _call(api.h_providers_save, {"name": "demo", "entry": _valid()})
    assert status == 200
    assert [row["name"] for row in body["providers"]] == ["demo"]
    assert store.load()["providers"]["demo"]["models"]["default"] == "some/model"


def test_post_refuses_a_typo_with_400_and_writes_nothing(home):
    status, body = _call(api.h_providers_save, {"name": "demo", "entry": _valid()})
    assert status == 200
    before = _sha(_config_bytes())
    status, body = _call(
        api.h_providers_save,
        {"name": "bad", "entry": {"endpionts": {"openai": "https://x.example.com"}}},
    )
    assert status == 400
    assert "endpionts" in body["error"]
    assert _sha(_config_bytes()) == before


def test_post_rejects_a_non_object_entry(home):
    status, body = _call(api.h_providers_save, {"name": "demo", "entry": "nope"})
    assert status == 400
    assert "entry" in body["error"]


def test_check_reports_without_saving(home, monkeypatch):
    seen = {}

    async def fake_probe(entry, api_key=None):
        seen["entry"] = entry
        seen["api_key"] = api_key
        return {"ok": False, "status": 401, "url": "https://x.example.com/v1/models",
                "detail": "HTTP 401: bad key", "elapsed_ms": 3}

    monkeypatch.setattr(provider_install, "probe", fake_probe)
    status, body = _call(
        api.h_providers_check,
        {"entry": _valid(api_key="sk-form"), "profile": None},
    )
    assert status == 200
    assert body["ok"] is False and body["status"] == 401
    assert seen["api_key"] == "sk-form"
    assert body["authenticated"] is True
    assert "demo" not in (store.load().get("providers") or {}), "a check must not write"


def test_check_uses_a_profile_token_when_the_entry_has_no_key(home, monkeypatch):
    from claude_launcher import credentials

    profile = create_profile("work")
    credentials.save_token(profile, "sk-from-profile")
    seen = {}

    async def fake_probe(entry, api_key=None):
        seen["api_key"] = api_key
        return {"ok": True, "status": 200, "url": "", "detail": "HTTP 200",
                "elapsed_ms": 1}

    monkeypatch.setattr(provider_install, "probe", fake_probe)
    status, body = _call(api.h_providers_check, {"entry": _valid(), "profile": "work"})
    assert status == 200
    assert seen["api_key"] == "sk-from-profile"
    assert body["token_source"] == "profile work token"


def test_check_can_name_a_stored_provider(home, monkeypatch):
    provider_install.save("demo", _valid())
    seen = {}

    async def fake_probe(entry, api_key=None):
        seen["entry"] = entry
        return {"ok": True, "status": 200, "url": "", "detail": "HTTP 200",
                "elapsed_ms": 1}

    monkeypatch.setattr(provider_install, "probe", fake_probe)
    status, body = _call(api.h_providers_check, {"name": "demo"})
    assert status == 200
    assert seen["entry"]["models"]["default"] == "some/model"
    status, body = _call(api.h_providers_check, {"name": "nope"})
    assert status == 404
