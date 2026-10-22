"""Dynamic routes: per-request upstream/auth/model resolution at the shim.

A provider with ``dynamic: true`` gets a shim keyed on the route name, not
the upstream URL. The shim re-reads the provider from the live config on
every request, so editing the endpoint, the api key, the model alias table
— or moving a profile onto another provider with ``claunch set-provider`` —
reaches already-running sessions without a relaunch.
"""

from __future__ import annotations

import json
import time

import pytest

from claude_launcher import provider_spec, providers, routing, runner, store
from claude_launcher.profile import create as create_profile

TABLE = {"default": "ds4", "small": "glm-flash", "large": "ds4",
         "sonnet": "ds4", "haiku": "glm-flash", "opus": "ds4"}


def _provider(name: str, entry: dict) -> None:
    store.update(lambda doc: doc.setdefault("providers", {}).update({name: entry}))


# --- resolve_model_alias ------------------------------------------------------


def test_alias_table_hit_replaces_the_model():
    assert routing.resolve_model_alias("opus", TABLE) == "ds4"
    assert routing.resolve_model_alias("small", TABLE) == "glm-flash"


def test_claude_id_normalizes_to_its_alias_word():
    assert routing.resolve_model_alias("claude-opus-5", TABLE) == "ds4"
    assert routing.resolve_model_alias("claude-opus-4-7", TABLE) == "ds4"
    assert routing.resolve_model_alias("claude-haiku-4-5-20251001", TABLE) == "glm-flash"


def test_role_falls_back_through_the_chain():
    table = {"default": "ds4"}
    assert routing.resolve_model_alias("small", table) == "ds4"
    assert routing.resolve_model_alias("xlarge", table) == "ds4"  # xlarge -> large -> default
    assert routing.resolve_model_alias("subagent", table) == "ds4"


def test_values_the_table_cannot_read_pass_through():
    for model in ("deepseek-v4-pro", "gpt-5", "mystery", ""):
        assert routing.resolve_model_alias(model, TABLE) == model


def test_a_full_id_keyed_table_entry_wins_over_normalization():
    table = {"claude-opus-5": "special"}
    assert routing.resolve_model_alias("claude-opus-5", table) == "special"


def test_alias_word_absent_from_the_table_passes_through():
    table = {"default": "ds4"}
    assert routing.resolve_model_alias("opus", table) == "opus"


# --- configuration ------------------------------------------------------------


def test_dynamic_is_off_by_default_and_reads_the_provider_block(home):
    _provider("fw", {"endpoints": {"anthropic": "https://fw.example.com"}})
    assert routing.dynamic("fw") is False
    _provider("fw", {"dynamic": True,
                     "endpoints": {"anthropic": "https://fw.example.com"}})
    assert routing.dynamic("fw") is True


def test_a_malformed_dynamic_flag_is_rejected(home):
    _provider("fw", {"dynamic": "yes"})
    with pytest.raises(routing.RoutingError, match="dynamic"):
        routing.dynamic("fw")


def test_models_accepts_claude_alias_keys(home):
    _provider("fw", {"models": {"opus": "ds4", "haiku": "glm-flash"}})
    spec = providers.spec("fw")
    assert spec.models["opus"] == "ds4"
    assert spec.models["haiku"] == "glm-flash"


def test_models_still_rejects_unknown_keys(home):
    _provider("fw", {"models": {"tiny": "x"}})
    with pytest.raises(providers.ProviderError, match="models.tiny"):
        providers.spec("fw")


# --- route resolution ---------------------------------------------------------


def test_route_upstream_and_models_read_the_live_config(home):
    _provider("fw", {"endpoints": {"anthropic": "https://fw.example.com"},
                     "models": {"opus": "ds4"}})
    assert routing.route_upstream("fw") == "https://fw.example.com"
    assert routing.route_models("fw") == {"opus": "ds4"}
    # The live read: an edit lands without anything restarting.
    _provider("fw", {"endpoints": {"anthropic": "https://ds.example.com"},
                     "models": {"opus": "ds4-official"}})
    assert routing.route_upstream("fw") == "https://ds.example.com"
    assert routing.route_models("fw") == {"opus": "ds4-official"}


def test_route_upstream_without_an_endpoint_is_an_error(home):
    _provider("fw", {"api_key": "k"})
    with pytest.raises(routing.RoutingError, match="endpoints.anthropic"):
        routing.route_upstream("fw")


def test_route_auth_prefers_the_declared_api_key(home):
    _provider("fw", {"api_key": " declared-key "})
    assert routing.route_auth("fw", session=None) == "declared-key"


def test_route_auth_unknown_route_is_an_error(home):
    with pytest.raises(routing.RoutingError, match="unknown provider"):
        routing.route_models("nope")


# --- fingerprint / front ------------------------------------------------------


def test_route_fingerprint_ignores_the_upstream(home):
    _provider("fw", {"dynamic": True})
    a = routing.fingerprint("https://a.example.com", {}, route="fw")
    b = routing.fingerprint("https://b.example.com", {}, route="fw")
    plain = routing.fingerprint("https://a.example.com", {})
    assert a == b
    assert a != plain


def test_front_sends_a_dynamic_provider_through_a_route_shim(home, monkeypatch):
    seen = {}

    def _fake_ensure(upstream, block, provider=None, route=None):
        seen.update(upstream=upstream, block=block, provider=provider, route=route)
        return "http://127.0.0.1:1/"

    monkeypatch.setattr(routing, "ensure_shim", _fake_ensure)
    _provider("fw", {"dynamic": True,
                     "endpoints": {"anthropic": "https://fw.example.com"}})
    url = routing.front("https://fw.example.com", "fw")
    assert url == "http://127.0.0.1:1/"
    assert seen["route"] == "provider:fw"
    assert seen["provider"] == "fw"
    assert seen["block"] == {}  # no routing pin: metering-only spec


def test_front_keeps_a_routing_pin_on_a_dynamic_route(home, monkeypatch):
    seen = {}

    def _fake_ensure(upstream, block, provider=None, route=None):
        seen.update(upstream=upstream, block=block, route=route)
        return "http://127.0.0.1:1/"

    monkeypatch.setattr(routing, "ensure_shim", _fake_ensure)
    _provider("fw", {"dynamic": True,
                     "routing": {"order": ["coreweave"]},
                     "endpoints": {"anthropic": "https://fw.example.com"}})
    routing.front("https://fw.example.com", "fw")
    assert seen["block"] == {"order": ["coreweave"]}
    assert seen["route"] == "provider:fw"


def test_front_keys_the_route_on_the_profile_when_given(home, monkeypatch):
    seen = {}

    def _fake_ensure(upstream, block, provider=None, route=None):
        seen.update(route=route)
        return "http://127.0.0.1:1/"

    monkeypatch.setattr(routing, "ensure_shim", _fake_ensure)
    _provider("fw", {"dynamic": True,
                     "endpoints": {"anthropic": "https://fw.example.com"}})
    routing.front("https://fw.example.com", "fw", profile="work")
    assert seen["route"] == "profile:work"


# --- merge_body_model ---------------------------------------------------------


def test_merge_body_model_resolves_and_merges_in_one_parse():
    raw = json.dumps({"model": "opus", "max_tokens": 1}).encode()
    out = json.loads(routing.merge_body_model(raw, {"order": ["c"]}, TABLE))
    assert out["model"] == "ds4"
    assert out["provider"] == {"order": ["c"]}


def test_merge_body_model_keeps_an_explicit_routing_field():
    raw = json.dumps({"model": "opus", "provider": {"order": ["other"]}}).encode()
    out = json.loads(routing.merge_body_model(raw, {"order": ["c"]}, TABLE))
    assert out["provider"] == {"order": ["other"]}
    assert out["model"] == "ds4"


def test_merge_body_model_passes_non_json_through(raw=b"not json"):
    assert routing.merge_body_model(raw, {"order": ["c"]}, TABLE) == raw


# --- launch env ---------------------------------------------------------------


def test_child_env_of_a_dynamic_provider_drops_launch_time_model_ids(home):
    _provider("fw", {"dynamic": True,
                     "api_key": "k",
                     "endpoints": {"anthropic": "https://fw.example.com"},
                     "models": {"default": "ds4", "small": "glm-flash"}})
    prof = create_profile("dyn")
    store.set_profile_field(prof.name, "provider", "fw")
    env = runner.child_env(prof, with_token=True,
                           base_env={"PATH": "/usr/bin", "HOME": str(home)})
    for key in provider_spec.CLAUDE_MODEL_KEYS:
        assert key not in env, f"{key} should be resolved at the shim"
    # The shim is where the request goes; credentials still come from the launch.
    assert routing.is_shim_url(env.get("ANTHROPIC_BASE_URL", ""))
    assert env.get("ANTHROPIC_AUTH_TOKEN")


def test_child_env_of_a_static_provider_keeps_launch_time_model_ids(home):
    _provider("fw", {"api_key": "k",
                     "endpoints": {"anthropic": "https://fw.example.com"},
                     "models": {"default": "ds4"}})
    prof = create_profile("static")
    store.set_profile_field(prof.name, "provider", "fw")
    env = runner.child_env(prof, with_token=True,
                           base_env={"PATH": "/usr/bin", "HOME": str(home)})
    assert env.get("ANTHROPIC_MODEL") == "ds4"


# --------------------------------------------------------------------------- #
# a real route shim in front of real (throwaway) upstreams
# --------------------------------------------------------------------------- #

import urllib.error  # noqa: E402

# The helpers live in the sibling routing suite. Import them by file
# location, not as ``tests.test_routing``: the gate runs the ``pytest``
# console script, which does not put the rootdir on sys.path, so the
# package-style import only works under ``python -m pytest``.
import importlib.util  # noqa: E402
from pathlib import Path as _Path  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "_routing_test_helpers", _Path(__file__).with_name("test_routing.py")
)
_helpers = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_helpers)
_Upstream = _helpers._Upstream
_header = _helpers._header
_post = _helpers._post
_post_with_headers = _helpers._post_with_headers


@pytest.fixture
def upstreams():
    servers = []
    try:
        yield servers
    finally:
        for server in servers:
            server.close()


@pytest.fixture
def shims(home, monkeypatch):
    """Stop whatever a test started — the shim outlives the process that spawns it."""
    # Only the daemon starts shims; this test stands in for it.
    monkeypatch.setattr(routing, "_OWNER", True)
    yield
    routing.stop()


def _add_upstream(servers) -> "_Upstream":
    server = _Upstream()
    servers.append(server)
    return server


def _dynamic_provider(name: str, url: str, key: str = "route-key") -> None:
    _provider(name, {"dynamic": True,
                     "api_key": key,
                     "endpoints": {"anthropic": url},
                     "models": {"opus": "ds4", "small": "glm-flash",
                                "default": "ds4"}})


@pytest.mark.slow_shim
def test_route_shim_resolves_upstream_model_and_auth_live(home, upstreams, shims):
    first = _add_upstream(upstreams)
    _dynamic_provider("rt", first.url)
    base = routing.ensure_shim(first.url, {}, route="provider:rt")
    status, _ = _post_with_headers(base + "v1/messages",
                                   {"model": "opus", "max_tokens": 1},
                                   {"Authorization": "Bearer stale"})
    assert status == 200
    seen = first.requests[-1]
    assert json.loads(seen["body"])["model"] == "ds4"
    assert _header(seen["headers"], "authorization") == "Bearer route-key"

    # The live edit: endpoint, key and table all move; the same shim serves
    # the running session's next request with the new values.
    second = _add_upstream(upstreams)
    _provider("rt", {"dynamic": True,
                     "api_key": "new-key",
                     "endpoints": {"anthropic": second.url},
                     "models": {"opus": "ds4-official", "default": "ds4-official"}})
    # The shim reads the config through a 100ms-TTL cache; give it up.
    time.sleep(0.3)
    status, _ = _post(base + "v1/messages",
                      {"model": "claude-opus-5", "max_tokens": 1})
    assert status == 200
    assert len(first.requests) == 1  # the second request went to the new upstream
    seen = second.requests[-1]
    assert json.loads(seen["body"])["model"] == "ds4-official"
    assert _header(seen["headers"], "authorization") == "Bearer new-key"


@pytest.mark.slow_shim
def test_route_shim_follows_a_provider_swap_for_a_live_profile(home, upstreams, shims):
    from claude_launcher import providers as providers_mod

    first = _add_upstream(upstreams)
    second = _add_upstream(upstreams)
    _provider("fw", {"api_key": "fw-key",
                     "endpoints": {"anthropic": first.url},
                     "models": {"opus": "fw-ds4", "default": "fw-ds4"}})
    _provider("ds", {"api_key": "ds-key",
                     "endpoints": {"anthropic": second.url},
                     "models": {"opus": "official-ds4", "default": "official-ds4"}})
    prof = create_profile("live-swap")
    store.set_profile_field(prof.name, "provider", "fw")
    base = routing.ensure_shim(first.url, {}, route=f"profile:{prof.name}")
    status, _ = _post(base + "v1/messages", {"model": "opus", "max_tokens": 1})
    assert status == 200
    assert json.loads(first.requests[-1]["body"])["model"] == "fw-ds4"
    assert _header(first.requests[-1]["headers"], "authorization") == "Bearer fw-key"

    # Mid-run: the profile moves to the other provider. Same shim, next
    # request follows — endpoint, key and table all come from the new one.
    providers_mod.set_profile_selection(prof, "ds")
    time.sleep(0.3)  # the shim reads the config through a 100ms-TTL cache
    status, _ = _post(base + "v1/messages", {"model": "opus", "max_tokens": 1})
    assert status == 200
    assert len(first.requests) == 1
    seen = second.requests[-1]
    assert json.loads(seen["body"])["model"] == "official-ds4"
    assert _header(seen["headers"], "authorization") == "Bearer ds-key"


@pytest.mark.slow_shim
def test_route_shim_passes_through_what_the_table_cannot_read(home, upstreams, shims):
    first = _add_upstream(upstreams)
    _dynamic_provider("rt2", first.url)
    base = routing.ensure_shim(first.url, {}, route="provider:rt2")
    status, _ = _post(base + "v1/messages",
                      {"model": "deepseek-v4-pro", "max_tokens": 1})
    assert status == 200
    assert json.loads(first.requests[-1]["body"])["model"] == "deepseek-v4-pro"


@pytest.mark.slow_shim
def test_route_shim_reports_a_route_without_an_endpoint(home, upstreams, shims):
    first = _add_upstream(upstreams)
    _dynamic_provider("rt3", first.url)
    base = routing.ensure_shim(first.url, {}, route="provider:rt3")
    _provider("rt3", {"dynamic": True, "api_key": "k"})  # endpoints gone
    try:
        status, body = _post(base + "v1/messages", {"model": "opus", "max_tokens": 1})
    except urllib.error.HTTPError as exc:
        status, body = exc.code, exc.read()
    assert status == 502
    assert "endpoints.anthropic" in body.decode()
