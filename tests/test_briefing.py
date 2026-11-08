"""The session briefing backend: config, transcript tail, LLM call, endpoint.

``GET /api/sessions/{name}/briefing`` turns a session's hybrid evidence
(record + cflow position + transcript tail) into a fixed JSON shape via an
OpenAI-compatible endpoint. These tests pin the four seams: the ``llm:``
config block's parsing, the jsonl tail extractor's caps, the chat/completions
request contract (against a local fake), and the endpoint's frozen response
contract (200/400/404, cache and refresh semantics).
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

import pytest

from claude_launcher import harnesses, profile, profile_keys, store, transcripts
from claude_launcher.daemon import briefing, paths, prompt_presets
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.mesh import MeshManager

CHILD = "import time\nprint('READY')\ntime.sleep(60)\n"
BEARER = {"Authorization": "Bearer sekrit"}


@pytest.fixture(autouse=True)
def _fresh_cache():
    briefing._cache.clear()
    briefing._failures.clear()
    briefing._forgotten.clear()
    yield
    briefing._cache.clear()
    briefing._failures.clear()
    briefing._forgotten.clear()


# --------------------------------------------------------------------------- #
# llm config parsing
# --------------------------------------------------------------------------- #
def _profile_backend_config(
    name: str = "brief",
    *,
    provider: str = "ds",
    api_key: str = "provider-secret",
    models=None,
    endpoints=None,
):
    """A profile whose provider describes an OpenAI-compatible backend.

    The default ``models.default`` carries Claude's ``[1m]`` tag on purpose:
    it is what a real profile aimed at a 1M-context backend records, and an
    OpenAI endpoint asked for the tagged id answers 404.
    """
    prof = profile.create(name)

    def mutate(doc):
        doc.setdefault("providers", {})[provider] = {
            "api_key": api_key,
            "endpoints": {"openai": "https://example.test/v1"}
            if endpoints is None
            else endpoints,
            "models": {"default": "deepseek-flash[1m]", "small": "glm-small"}
            if models is None
            else models,
        }
        doc.setdefault("profiles", {}).setdefault(name, {})["provider"] = provider

    store.update(mutate)
    return prof


def test_llm_config_absent_block_is_disabled_defaults(home):
    cfg = briefing.llm_config({})
    assert cfg == {
        "preset": "",
        "profile": "",
        "provider": "",
        "endpoint": "",
        "model": "",
        "api_key": "",
        "key_id": "",
        "max_tokens": briefing.DEFAULT_MAX_TOKENS,
        "params": {},
    }
    assert not briefing.llm_configured(cfg)
    # malformed block: same disabled shape, no crash
    assert not briefing.llm_configured(briefing.llm_config({"llm": "oops"}))


def test_llm_config_empty_api_key_disables(home):
    doc = {"llm": {"endpoint": "https://x/v1/chat/completions", "model": "m"}}
    assert not briefing.llm_configured(briefing.llm_config(doc))
    doc["llm"]["api_key"] = "   "
    assert not briefing.llm_configured(briefing.llm_config(doc))
    doc["llm"]["api_key"] = "sk-abc"
    assert briefing.llm_configured(briefing.llm_config(doc))


def test_llm_config_reads_the_store_and_keeps_params(config_file):
    store.save(
        {
            "llm": {
                "endpoint": "https://api.example/v1/chat/completions",
                "model": "some/model",
                "api_key": "sk-live",
                "max_tokens": 256,
                "params": {"top_k": 40, "temperature": 0.2},
            }
        }
    )
    cfg = briefing.llm_config()
    assert cfg["endpoint"] == "https://api.example/v1/chat/completions"
    assert cfg["max_tokens"] == 256
    assert cfg["params"] == {"top_k": 40, "temperature": 0.2}
    # a non-numeric max_tokens falls back rather than erroring the endpoint
    cfg = briefing.llm_config({"llm": {"max_tokens": "lots"}})
    assert cfg["max_tokens"] == briefing.DEFAULT_MAX_TOKENS


def test_profile_backend_carries_the_provider_name(config_file):
    """The provider is what decides the request headers the call goes out with.

    :func:`briefing.call_llm` applies the rules that provider declares
    (``session_header``, ``anthropic_auth_header``) so a daemon-side call is
    accepted where a harness session on the same provider is — the shim is not
    in this path.
    """
    _profile_backend_config()
    assert briefing.profile_backend("brief").provider == "ds"


def test_llm_config_provider_is_the_profile_provider_or_blank(config_file):
    _profile_backend_config()
    store.update(lambda doc: doc.update({"llm": {"profile": "brief"}}))
    assert briefing.llm_config()["provider"] == "ds"
    # the typed form belongs to no provider entry, so no rule applies to it
    store.update(lambda doc: doc.update(
        {"llm": {"endpoint": "https://typed.example/v1/chat/completions",
                 "api_key": "sk-typed", "model": "m"}}))
    assert briefing.llm_config()["provider"] == ""


# --------------------------------------------------------------------------- #
# the request headers a briefing call goes out with
# --------------------------------------------------------------------------- #
def _ocgo_profile(name="brief-ocgo", **provider_extra):
    """A profile on a provider that refuses a request without its own header.

    opencode.ai/zen/go answers 400 ``MissingSessionID`` to a call that does
    not carry ``x-opencode-session`` — measured 2026-10-25 on this feature's
    own ``chat/completions`` call, which is why it exists here.
    """
    prof = profile.create(name)

    def mutate(doc):
        doc.setdefault("providers", {})[name] = {
            "api_key": "provider-secret",
            "endpoints": {"openai": "https://opencode.example/v1"},
            "models": {"default": "deepseek-flash"},
            "session_header": "x-opencode-session",
            **provider_extra,
        }
        doc.setdefault("profiles", {}).setdefault(name, {})["provider"] = name

    store.update(mutate)
    return prof


def test_briefing_headers_send_the_marked_id_under_the_required_header(home):
    """The id is this feature's own mark, not the calling session's name."""
    _ocgo_profile()
    store.update(lambda doc: doc.update({"llm": {"profile": "brief-ocgo"}}))
    cfg = briefing.llm_config()
    headers = briefing.briefing_request_headers(cfg)
    assert headers["x-opencode-session"] == briefing.BRIEFING_SESSION_ID
    assert headers["x-opencode-session"] == "claunch-briefing"
    assert headers["Authorization"] == "Bearer provider-secret"
    assert headers["Accept"] == "application/json"


def test_briefing_headers_are_plain_without_a_provider(home):
    cfg = {"endpoint": "https://typed.example/v1/chat/completions",
           "model": "m", "api_key": "sk-typed", "provider": ""}
    assert briefing.briefing_request_headers(cfg) == {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": "Bearer sk-typed",
    }


def test_briefing_headers_stay_a_bearer_on_the_chat_completions_path(home):
    """The Messages-only auth move must not fire on the protocol this speaks."""
    _ocgo_profile(anthropic_auth_header="x-api-key")
    store.update(lambda doc: doc.update({"llm": {"profile": "brief-ocgo"}}))
    headers = briefing.briefing_request_headers(briefing.llm_config())
    assert headers["Authorization"] == "Bearer provider-secret"
    assert "x-api-key" not in headers


def test_briefing_headers_follow_a_live_config_edit(home):
    """Read per call, like the shim's: an edit reaches the next briefing."""
    _ocgo_profile()
    store.update(lambda doc: doc.update({"llm": {"profile": "brief-ocgo"}}))
    briefing.briefing_request_headers(briefing.llm_config())
    store.update(lambda doc: doc["providers"]["brief-ocgo"].update(
        {"session_header": "x-other-session"}))
    headers = briefing.briefing_request_headers(briefing.llm_config())
    assert headers["x-other-session"] == briefing.BRIEFING_SESSION_ID
    assert "x-opencode-session" not in headers


def test_a_malformed_header_rule_fails_the_call_with_the_config_key(home):
    """A typo is named, not silently skipped: the upstream refuses anyway."""
    _ocgo_profile(session_header="x opencode")
    store.update(lambda doc: doc.update({"llm": {"profile": "brief-ocgo"}}))
    with pytest.raises(briefing.BriefingError, match="session_header"):
        briefing.briefing_request_headers(briefing.llm_config())


def test_profile_backend_reads_endpoint_key_and_models(config_file):
    """A profile is resolved the way the Observer resolves its own."""
    _profile_backend_config()
    backend = briefing.profile_backend("brief")
    assert backend.endpoint == "https://example.test/v1/chat/completions"
    assert backend.api_key == "provider-secret"
    # the tag is stripped, and 'default' leads the suggestion list
    assert backend.model == "deepseek-flash"
    assert backend.models == ("deepseek-flash", "glm-small")


def test_profile_backend_refuses_a_profile_with_no_openai_endpoint(config_file):
    """Anthropic-only is a real provider shape, and not one this call speaks."""
    _profile_backend_config(endpoints={"anthropic": "https://example.test/anthropic"})
    with pytest.raises(briefing.BriefingError, match="OpenAI-compatible endpoint"):
        briefing.profile_backend("brief")
    with pytest.raises(briefing.BriefingError, match="does not exist"):
        briefing.profile_backend("no-such-profile")


def test_llm_config_takes_the_backend_from_the_named_profile(config_file):
    _profile_backend_config()
    store.update(lambda doc: doc.update({"llm": {"profile": "brief"}}))
    cfg = briefing.llm_config()
    assert cfg["profile"] == "brief"
    assert cfg["endpoint"] == "https://example.test/v1/chat/completions"
    assert cfg["api_key"] == "provider-secret"
    assert cfg["model"] == "deepseek-flash"
    assert briefing.llm_configured(cfg)


def test_llm_config_profile_owns_the_endpoint_key_pair_and_not_the_model(config_file):
    """The block's endpoint/key are dropped, its model is kept.

    The endpoint and the key identify one backend together, so a key typed
    for the direct form must not be sent to the profile's endpoint. The model
    is this feature's own choice about that backend, so the field the
    Settings card writes wins over the profile's default.
    """
    _profile_backend_config()
    store.update(
        lambda doc: doc.update(
            {
                "llm": {
                    "profile": "brief",
                    "model": "glm-small",
                    "endpoint": "https://typed.example/v1/chat/completions",
                    "api_key": "sk-typed",
                }
            }
        )
    )
    cfg = briefing.llm_config()
    assert cfg["endpoint"] == "https://example.test/v1/chat/completions"
    assert cfg["api_key"] == "provider-secret"
    assert cfg["model"] == "glm-small"


def test_llm_config_unresolvable_profile_leaves_the_feature_off(config_file):
    """A hand-edited name that cannot serve turns it off rather than raising.

    The direct fields are not used as a fallback either: the profile is the
    operator's statement of which backend to call, and quietly calling the
    previous one instead would be worse than composing nothing.
    """
    store.update(
        lambda doc: doc.update(
            {
                "llm": {
                    "profile": "gone",
                    "model": "m",
                    "endpoint": "https://typed.example/v1/chat/completions",
                    "api_key": "sk-typed",
                }
            }
        )
    )
    cfg = briefing.llm_config()
    assert (cfg["endpoint"], cfg["api_key"]) == ("", "")
    assert not briefing.llm_configured(cfg)


def test_llm_config_direct_endpoint_still_works(config_file):
    """The pre-profile form of the block is unchanged."""
    store.update(
        lambda doc: doc.update(
            {
                "llm": {
                    "endpoint": "https://typed.example/v1/chat/completions",
                    "model": "m",
                    "api_key": "sk-typed",
                }
            }
        )
    )
    cfg = briefing.llm_config()
    assert cfg["profile"] == ""
    assert cfg["endpoint"] == "https://typed.example/v1/chat/completions"
    assert cfg["api_key"] == "sk-typed"
    assert briefing.llm_configured(cfg)


# --------------------------------------------------------------------------- #
# key_id: pinning one key of a profile's pool
# --------------------------------------------------------------------------- #
def _pooled_profile(name: str = "brief", *, provider: str = "ds"):
    """The backend profile holding a two-key pool.

    ``profile_keys.add`` makes each added key the effective token, so the
    token file ends holding the SECOND one — the state a pool is in between
    launches, and what an unpinned briefing call reads.
    """
    prof = _profile_backend_config(name, provider=provider)
    first = profile_keys.add(prof, "sk-pool-first", "first")
    second = profile_keys.add(prof, "sk-pool-second", "second")
    return prof, first, second


def test_profile_backend_uses_the_pinned_pool_key(config_file):
    _prof, first, _second = _pooled_profile()
    backend = briefing.profile_backend("brief", key_id=first.id)
    assert backend.api_key == "sk-pool-first"
    assert backend.key_id == first.id


def test_profile_backend_unpinned_reads_the_token_file(config_file):
    """No pin: the token file as before — and the pool is not moved by a read."""
    prof, _first, second = _pooled_profile()
    backend = briefing.profile_backend("brief")
    assert backend.api_key == "sk-pool-second"
    # the display half names which pool key that token is
    assert backend.key_id == second.id
    pool = profile_keys.load(prof)
    assert pool.cursor == second.id
    assert all(k.use_count == 0 for k in pool.keys)


def test_profile_backend_refuses_a_pin_the_pool_does_not_hold(config_file):
    _pooled_profile()
    with pytest.raises(briefing.BriefingError, match="holds no key"):
        briefing.profile_backend("brief", key_id="k-nope")


def test_profile_backend_refuses_a_pin_without_a_pool(config_file):
    _profile_backend_config()
    with pytest.raises(briefing.BriefingError, match="no key pool"):
        briefing.profile_backend("brief", key_id="k-nope")


def test_llm_config_carries_the_pin_to_the_call(config_file):
    _prof, first, _second = _pooled_profile()
    store.update(
        lambda doc: doc.update(
            {"llm": {"profile": "brief", "key_id": first.id}}
        )
    )
    cfg = briefing.llm_config()
    assert cfg["api_key"] == "sk-pool-first"
    assert cfg["key_id"] == first.id


def test_llm_config_drops_the_pin_with_the_profile(config_file):
    """A pin names a key of a profile's pool; with no profile it is inert."""
    store.update(
        lambda doc: doc.update(
            {
                "llm": {
                    "endpoint": "https://typed.example/v1/chat/completions",
                    "model": "m",
                    "api_key": "sk-typed",
                    "key_id": "k-orphan",
                }
            }
        )
    )
    cfg = briefing.llm_config()
    assert cfg["key_id"] == ""
    assert cfg["api_key"] == "sk-typed"


def test_llm_config_stale_pin_leaves_the_feature_off(config_file):
    """A removed key is an error at compose time, not a silent other key."""
    _pooled_profile()
    store.update(
        lambda doc: doc.update({"llm": {"profile": "brief", "key_id": "k-gone"}})
    )
    cfg = briefing.llm_config()
    assert (cfg["endpoint"], cfg["api_key"], cfg["key_id"]) == ("", "", "")
    assert not briefing.llm_configured(cfg)


def test_preset_pins_a_pool_key(config_file):
    _prof, first, _second = _pooled_profile()
    store.update(
        lambda doc: doc.update(
            {
                "llm": {
                    "presets": [
                        {"id": "pooled", "profile": "brief", "key_id": first.id}
                    ],
                    "preset": "pooled",
                }
            }
        )
    )
    cfg = briefing.llm_config()
    assert cfg["api_key"] == "sk-pool-first"
    assert cfg["key_id"] == first.id
    rows = {r["id"]: r for r in briefing.presets_for_display()}
    assert rows["pooled"]["key_id"] == first.id
    assert rows["pooled"]["resolved"]["key_id"] == first.id
    # the payload crosses to a browser: ids and masks, never a token
    assert "sk-pool" not in json.dumps(rows)


# --------------------------------------------------------------------------- #
# presets — several saved backends, one of them switched on
# --------------------------------------------------------------------------- #
_DIRECT_CHEAP = {
    "id": "cheap",
    "endpoint": "https://cheap.example/v1/chat/completions",
    "model": "glm-small",
    "api_key": "sk-cheap",
}
_DIRECT_OTHER = {
    "id": "other",
    "endpoint": "https://other.example/v1/chat/completions",
    "model": "other-model",
    "api_key": "sk-other",
}


def test_presets_resolve_to_the_active_one(config_file):
    """Pointing at a preset is the whole switch — no other field changes."""
    _profile_backend_config()
    store.update(
        lambda doc: doc.update(
            {
                "llm": {
                    "presets": [
                        {"id": "big", "profile": "brief"},
                        dict(_DIRECT_CHEAP),
                    ],
                    "preset": "cheap",
                    # the block's own fields describe a THIRD backend; the
                    # active preset must win over them entirely
                    "endpoint": "https://typed.example/v1/chat/completions",
                    "model": "typed-model",
                    "api_key": "sk-typed",
                }
            }
        )
    )
    cfg = briefing.llm_config()
    assert cfg["preset"] == "cheap"
    assert cfg["endpoint"] == "https://cheap.example/v1/chat/completions"
    assert cfg["model"] == "glm-small"
    assert cfg["api_key"] == "sk-cheap"
    assert briefing.llm_configured(cfg)

    # switching is a pointer move, nothing else
    store.update(lambda doc: doc["llm"].update({"preset": "big"}))
    cfg = briefing.llm_config()
    assert cfg["profile"] == "brief"
    assert cfg["endpoint"] == "https://example.test/v1/chat/completions"
    assert cfg["api_key"] == "provider-secret"
    # an empty preset model follows the profile's default
    assert cfg["model"] == "deepseek-flash"


def test_preset_without_a_pointer_leaves_the_block_in_charge(config_file):
    """Saved presets that nobody pointed at must not change today's call."""
    store.update(
        lambda doc: doc.update(
            {
                "llm": {
                    "presets": [dict(_DIRECT_CHEAP)],
                    "endpoint": "https://typed.example/v1/chat/completions",
                    "model": "typed-model",
                    "api_key": "sk-typed",
                }
            }
        )
    )
    cfg = briefing.llm_config()
    assert cfg["preset"] == ""
    assert cfg["endpoint"] == "https://typed.example/v1/chat/completions"
    assert cfg["model"] == "typed-model"
    assert cfg["api_key"] == "sk-typed"


def test_preset_does_not_own_the_budget_or_params(config_file):
    """Switching backends must not change what the call is allowed to spend."""
    store.update(
        lambda doc: doc.update(
            {
                "llm": {
                    "presets": [dict(_DIRECT_CHEAP, max_tokens=1)],
                    "preset": "cheap",
                    "max_tokens": 777,
                    "params": {"temperature": 0.1},
                }
            }
        )
    )
    cfg = briefing.llm_config()
    assert cfg["max_tokens"] == 777
    assert cfg["params"] == {"temperature": 0.1}


def test_preset_pointing_at_a_deleted_name_reads_as_none(config_file):
    """A stale pointer falls back to the block rather than disabling the call.

    Hand-edited files drift; the card must open showing what the daemon
    would actually use, and the block's own fields are that.
    """
    store.update(
        lambda doc: doc.update(
            {
                "llm": {
                    "presets": [dict(_DIRECT_CHEAP)],
                    "preset": "gone",
                    "endpoint": "https://typed.example/v1/chat/completions",
                    "model": "typed-model",
                    "api_key": "sk-typed",
                }
            }
        )
    )
    cfg = briefing.llm_config()
    assert cfg["preset"] == ""
    assert cfg["endpoint"] == "https://typed.example/v1/chat/completions"
    assert briefing.llm_configured(cfg)


def test_presets_are_cleaned_and_duplicates_dropped(config_file):
    """A hand-edited preset list costs only its own bad rows."""
    store.update(
        lambda doc: doc.update(
            {
                "llm": {
                    "presets": [
                        dict(_DIRECT_CHEAP),
                        {"id": "BAD NAME", "endpoint": "https://x/v1/chat/completions"},
                        "not an object",
                        {"endpoint": "https://no-id.example/v1/chat/completions"},
                        dict(_DIRECT_CHEAP, endpoint="https://second.example/v1"),
                        dict(_DIRECT_OTHER),
                    ]
                }
            }
        )
    )
    assert [p["id"] for p in briefing.presets()] == ["cheap", "other"]
    # the first row survived, not the later duplicate
    assert briefing.presets()[0]["endpoint"] == (
        "https://cheap.example/v1/chat/completions"
    )


def test_presets_for_display_never_carry_a_key(config_file):
    """This payload reaches a browser; the secrets stay in the daemon."""
    _profile_backend_config()
    store.update(
        lambda doc: doc.update(
            {
                "llm": {
                    "presets": [
                        dict(_DIRECT_CHEAP),
                        {"id": "big", "profile": "brief"},
                        {"id": "broken", "profile": "no-such-profile"},
                    ]
                }
            }
        )
    )
    rows = {r["id"]: r for r in briefing.presets_for_display()}
    assert "sk-cheap" not in json.dumps(rows)
    assert rows["cheap"]["api_key_set"] is True
    assert rows["cheap"]["resolved"] == {
        "endpoint": "https://cheap.example/v1/chat/completions",
        "model": "glm-small",
        "has_key": True,
        "provider": "",  # typed in directly: it belongs to no provider entry
        "key_id": "",  # no profile, so no pool to pin in
    }
    assert rows["big"]["resolved"]["has_key"] is True
    assert rows["big"]["resolved"]["model"] == "deepseek-flash"
    assert "does not exist" in rows["broken"]["error"]


def test_check_preset_list_refuses_what_cannot_be_stored():
    """A submitted list is refused, not pruned: the form sent what it shows."""
    with pytest.raises(briefing.PresetError, match="must be lowercase"):
        briefing.check_preset_list([{"id": "Not Valid", "endpoint": "https://x/v1"}])
    with pytest.raises(briefing.PresetError, match="duplicate"):
        briefing.check_preset_list([
            {"id": "a", "endpoint": "https://x/v1"},
            {"id": "a", "endpoint": "https://y/v1"},
        ])
    with pytest.raises(briefing.PresetError, match="needs a profile or an endpoint"):
        briefing.check_preset_list([{"id": "a", "model": "m"}])
    with pytest.raises(briefing.PresetError, match="at most"):
        briefing.check_preset_list([
            {"id": f"p{i}", "endpoint": "https://x/v1"}
            for i in range(briefing.MAX_PRESETS + 1)
        ])
    assert briefing.check_preset_list(None) == []
    assert briefing.check_preset_list([
        {"id": "a", "profile": "brief", "model": " ", "api_key": ""},
    ]) == [{"id": "a", "profile": "brief"}]


# --------------------------------------------------------------------------- #
# transcript tail extraction
# --------------------------------------------------------------------------- #
def _jl(**kw) -> str:
    return json.dumps(kw, ensure_ascii=False)


def test_tail_events_extracts_text_and_tool_names(tmp_path):
    p = tmp_path / "conv.jsonl"
    lines = [
        _jl(type="user", message={"role": "user", "content": "fix the bug"}),
        "not json at all",
        _jl(
            type="assistant",
            message={
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "looking\nat it"},
                    {"type": "tool_use", "name": "Read", "input": {}},
                    {"type": "tool_use", "name": "Bash", "input": {}},
                ],
            },
        ),
        # tool results ride user entries as blocks; they must not leak in
        _jl(
            type="user",
            message={
                "role": "user",
                "content": [{"type": "tool_result", "content": "big dump"}],
            },
        ),
        _jl(type="summary", summary="ignored"),
    ]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    events = briefing.tail_events(p)
    assert events == [
        "user: fix the bug",
        "assistant: looking at it",
        "assistant tool_use: Read, Bash",
    ]


def test_tail_events_caps_events_text_and_total(tmp_path):
    p = tmp_path / "conv.jsonl"
    rows = [
        _jl(type="user", message={"role": "user", "content": f"msg {i} " + "x" * 900})
        for i in range(120)
    ]
    p.write_text("\n".join(rows), encoding="utf-8")
    events = briefing.tail_events(p)
    # newest EVENT_TAIL survive, oldest first, each clipped to TEXT_LIMIT
    assert len(events) <= briefing.EVENT_TAIL
    assert events[-1].startswith("user: msg 119 ")
    assert all(len(e) <= briefing.TEXT_LIMIT + len("user: ") + 2 for e in events)
    # a tiny total budget keeps only the newest events
    few = briefing.tail_events(p, total_limit=2000)
    assert few and few[-1].startswith("user: msg 119")
    assert sum(len(e) + 1 for e in few) <= 2000


def test_tail_events_missing_file_is_empty(tmp_path):
    assert briefing.tail_events(tmp_path / "absent.jsonl") == []


def test_codex_rollout_is_located_and_its_messages_are_extracted(tmp_path):
    prof = profile.create("codex-brief")
    codex = harnesses.get("codex")
    assert codex is not None
    rollout = (
        codex.profile_home(prof.config_dir)
        / "sessions"
        / "2026"
        / "08"
        / "29"
        / "rollout.jsonl"
    )
    rollout.parent.mkdir(parents=True)
    rows = [
        _jl(type="session_meta", payload={"id": "codex-id", "cwd": str(tmp_path)}),
        _jl(
            type="response_item",
            payload={
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "fix the Codex briefing"}],
            },
        ),
        _jl(
            type="response_item",
            payload={
                "type": "custom_tool_call",
                "name": "exec",
                "call_id": "c1",
            },
        ),
        _jl(
            type="response_item",
            payload={
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "reading the rollout"}],
            },
        ),
    ]
    rollout.write_text("\n".join(rows) + "\n", encoding="utf-8")
    sdef = SessionDef(
        name="codex-session",
        harness="codex",
        profile=prof.name,
        cwd=str(tmp_path),
        conversation_id="codex-id",
    )

    assert briefing.locate_transcript(sdef) == rollout
    assert briefing.tail_events(rollout) == [
        "user: fix the Codex briefing",
        "assistant tool_use: exec",
        "assistant: reading the rollout",
    ]


def test_an_empty_transcript_does_not_shadow_the_written_one(tmp_path):
    """briefing reads the same predicate a restore reads.

    It used to spell its own: ``is_file()`` on the strict address. A
    zero-byte jsonl there answered yes and the id search never ran, so a
    conversation written under a slug this module spells differently read as
    an empty transcript instead of being found. The canonical predicate had
    already stopped counting an empty file as a conversation, and the two
    answered opposite things about the same session
    (claunch-fork-family-recheck-amnqg.1).
    """
    prof = profile.create("brief-empty")
    cid = "d0d0d0d0-0000-0000-0000-000000000001"
    cwd = tmp_path / "here"
    cwd.mkdir()

    strict = transcripts.project_dir(prof.config_dir, str(cwd))
    strict.mkdir(parents=True)
    (strict / f"{cid}.jsonl").write_bytes(b"")

    elsewhere = prof.config_dir / "projects" / "spelled--differently"
    elsewhere.mkdir(parents=True)
    written = elsewhere / f"{cid}.jsonl"
    written.write_text(
        _jl(type="user", message={"role": "user", "content": "it is here"})
        + "\n",
        encoding="utf-8",
    )

    sdef = SessionDef(
        name="brief-empty-session",
        profile=prof.name,
        cwd=str(cwd),
        conversation_id=cid,
    )

    assert briefing.locate_transcript(sdef) == written
    assert briefing.tail_events(written) == ["user: it is here"]
    # the same session, asked the restore's question: one answer, not two
    assert transcripts.exists(prof.config_dir, cid, str(cwd)) is True
    # the shallow caller cannot afford the id scan, and reads the zero-byte
    # file as absent rather than as a conversation with nothing in it
    assert briefing.locate_transcript(sdef, deep=False) is None


# --------------------------------------------------------------------------- #
# prompt gathering
# --------------------------------------------------------------------------- #
def test_build_prompt_carries_all_signals():
    sdef = SessionDef(name="s9", cwd="F:/repo", task="ship the widget")
    cflow_info = {
        "workflow": "improv-worker",
        "status": "step",
        "step": "work",
        "title": "작업 실행",
        "last_report": "intake done",
    }
    prompt = briefing.build_prompt(
        sdef,
        cflow_info,
        ["user: hello", "assistant: hi"],
        {
            "status": "busy",
            "running": True,
            "last_input_at": "2026-08-30T12:00:00+00:00",
            "last_output_at": "2026-08-30T12:00:01+00:00",
        },
        faq=[{"question": "어떤 브랜치인가?"}],
    )
    for needle in (
        '"state": "working|blocked|waiting|idle|done|unknown"',
        "이름: s9",
        "ship the widget",
        "improv-worker",
        "작업 실행",
        "intake done",
        "user: hello",
        "[데몬 실시간 상태]",
        "상태: busy",
        "opening task 안에 포함된 과거 요약 문구",
        "모든 필드 값은 자료의 언어와",
        "자연스럽고 정확한 한국어",
        "변경하면 안 되는 기술적 값은 원문을 유지한다",
        '"faq": [{"question": "사용자 FAQ 질문"',
        "어떤 브랜치인가?",
        "각 질문에 대해 현재 세션 자료에 근거한 답변",
    ):
        assert needle in prompt


def test_build_prompt_asks_for_the_work_not_names_or_ids():
    """The summary fields describe the work itself (claunch-u1rew, claunch-zyla3).

    The card already shows the session's name and its issue badges beside
    the one-line, so a summary that opens with "s173의 변경을…" or cites
    "claunch-4avda" repeats the row's label instead of saying what the work
    is. The prompt carries the name and the opening task (with its issue
    id), so the rule has to be stated, with the faq answers left out of it.
    A short issue id with its board prefix dropped ("gds6-rbyq") is still an
    id, so the rule names that form too.
    """
    prompt = briefing.build_prompt(
        SessionDef(name="s173", task="issue: claunch-4avda\nrebase onto master"),
        None,
        [],
    )
    for needle in (
        "작업 내용 자체를",
        "다른 항목을 id로 가리키지 않는다",
        "세션 이름(이 세션과 다른 세션",
        "접두사를 뗀 꼴과 하위 이슈 꼴",
        "gds6-rbyq",
        "PR 번호",
        "커밋 해시",
        "one-line-job-description은 특히 id 없이 읽혀야 한다",
        "다른 항목을 가리키는\nid는 아래 규칙에 따라 쓰지 않는다",
        "자기 지칭 없이",
        "그 식별자 대신 그 일의 내용을",
        "나쁜 예:",
        "좋은 예:",
        "faq 답변에는 이 제한을 적용하지 않는다",
        "이름: s173 (자료 식별용 — 요약 문장에 쓰지 않는다)",
    ):
        assert needle in prompt
    # the field template no longer invites "...하는 세션" self-reference
    assert "이 세션이 맡은 일을 한 줄로" not in prompt


def test_build_prompt_names_the_subject_not_a_bare_referent():
    """A one-line that opens with 「변경」 names nothing (claunch-944s0).

    The card shows that line to a leader who did not watch the session
    (s6553's read "변경의 master 착지를 확인하고 착지 후 단계 전환과 마무리
    정리를 수행한다"), and a bare 「변경」 leaves them asking which change.
    A workflow step's name (「단계 전환」, 「마무리 정리」) is not the work
    either: it says where the run stands, not what the session does there.
    Both shapes are stated as rules with an example each, so the retired
    "good example" — which taught this very phrasing — is gone.
    """
    prompt = briefing.build_prompt(
        SessionDef(name="s6553", task="issue: claunch-hbmhk.1\nland the change"),
        None,
        [],
    )
    for needle in (
        "맡은 일의 대상과 동작을 한 줄로",
        "무엇을 가리키는지가 문장 안에",
        "드러나지 않는 명사는 대상이 될 수 없다",
        "모듈·화면·동작의 이름을 주어로 세운다",
        "워크플로 절차의 이름을 작업 내용 대신 쓰지 않는다",
        "「단계 전환」",
        "「마무리 정리」",
        '나쁜 예: "변경의 master 착지를 확인하고 착지 후 단계 전환과 마무리 정리를 수행한다."',
        "세션 카드 한 줄 요약 지침을 고친 커밋을 master에 착지시키고",
    ):
        assert needle in prompt
    # the example that used to stand here taught the phrasing the rule bans
    assert (
        "리뷰를 통과한 변경의 master 착지를 확인하고 회차 마무리 상태를 점검한다."
        not in prompt
    )


def test_gather_live_reads_the_same_state_as_the_session_rail():
    class Session:
        exited = False
        last_input_at = "in"
        last_output_at = "out"

        @staticmethod
        def status():
            return "busy"

    assert briefing.gather_live(Session()) == {
        "status": "busy",
        "running": True,
        "last_input_at": "in",
        "last_output_at": "out",
    }
    # absent signals leave no dangling sections
    bare = briefing.build_prompt(SessionDef(name="s0"), None, [])
    assert "[cflow 런]" not in bare and "[대화 로그 꼬리" not in bare


def test_gather_cflow_maps_status_and_last_report(monkeypatch, tmp_path):
    payload = {
        "run": "run-1",
        "workflow": "improv-worker",
        "status": "step",
        "step_id": "work",
        "title": "작업 실행",
    }
    journal = [
        {"event": "step_report", "summary": "first"},
        {"event": "gate", "summary": "not a report"},
        {"event": "step_report", "summary": "latest"},
    ]
    monkeypatch.setattr(briefing.cflow_engine, "status", lambda cwd, scope=None: payload)
    monkeypatch.setattr(
        briefing.cflow_state, "read_journal", lambda cwd, scope, run_id=None: journal
    )
    info = briefing.gather_cflow(str(tmp_path), "s9")
    assert info == {
        "workflow": "improv-worker",
        "status": "step",
        "step": "work",
        "title": "작업 실행",
        "last_report": "latest",
    }
    # idle slot -> no cflow signal; empty cwd -> never even asks
    monkeypatch.setattr(
        briefing.cflow_engine, "status", lambda cwd, scope=None: {"status": "idle"}
    )
    assert briefing.gather_cflow(str(tmp_path), "s9") is None
    assert briefing.gather_cflow("", "s9") is None


# --------------------------------------------------------------------------- #
# lenient answer parsing
# --------------------------------------------------------------------------- #
def test_parse_briefing_plain_fenced_and_embedded():
    body = {
        "goal": "g", "now": "n", "state": "working", "progress": "p",
        "one-line-job-description": "한 줄 설명",
    }
    expect = dict(body)
    assert briefing.parse_briefing(json.dumps(body)) == expect
    assert briefing.parse_briefing(f"```json\n{json.dumps(body)}\n```") == expect
    assert briefing.parse_briefing(f"Sure! Here it is:\n{json.dumps(body)}\nDone.") == expect


def test_parse_briefing_absent_one_line_is_empty(home):
    # the one-line is optional input — absent, it parses to "" rather than
    # erroring, so an older model's shape still yields a usable briefing
    parsed = briefing.parse_briefing(
        '{"goal": "g", "now": "n", "state": "working", "progress": "p"}'
    )
    assert parsed["one-line-job-description"] == ""
    assert parsed["goal"] == "g"


def test_digest_serves_the_cached_briefing_fields(home):
    """digest() is the list's cheap read: nothing composed -> None, a
    raw/unshaped result -> None, and a parsed briefing -> its short fields.

    The one-line is what the rail row draws and goal/now/progress are what a
    session tab's hover tooltip draws, so the digest carries both and omits
    the fields the summariser left empty. A briefing with no one-line still
    has a tooltip's worth of text, so it yields a digest; one with no text at
    all does not."""
    assert briefing.digest("s1") is None
    briefing._cache["s1"] = (
        ("key",),
        {"briefing": {"goal": "g", "now": "n", "progress": "", "state": "working",
                       "one-line-job-description": "한 줄"}},
    )
    assert briefing.digest("s1") == {
        "one_line": "한 줄", "state": "working", "goal": "g", "now": "n",
    }
    # a raw (unshaped) result: no parsed briefing, hence no digest
    briefing._cache["s2"] = (("key",), {"briefing": None, "raw": "prose"})
    assert briefing.digest("s2") is None
    # no one-line, but a goal the tooltip can show
    briefing._cache["s3"] = (("key",), {"briefing": {"goal": "g", "state": "idle"}})
    assert briefing.digest("s3") == {"one_line": "", "state": "idle", "goal": "g"}
    # every field empty: nothing for either surface to draw
    briefing._cache["s4"] = (("key",), {"briefing": {"goal": "", "state": "idle"}})
    assert briefing.digest("s4") is None


def test_digest_clips_long_fields(home):
    """One digest per session rides every list poll, and both of its readers
    are a glance, so each field is clipped to a bounded length — the card is
    where the summariser's full sentence is read."""
    briefing._cache["s1"] = (
        ("key",),
        {"briefing": {"goal": "가" * 400, "state": "working",
                       "one-line-job-description": "나" * 400}},
    )
    d = briefing.digest("s1")
    limit = briefing.DIGEST_FIELD_CHARS
    assert d["goal"].startswith("가" * limit) and len(d["goal"]) <= limit + 2
    assert d["one_line"].startswith("나" * limit)


def test_briefing_cache_survives_daemon_restart(home):
    key = ("s1", (123, 456), "work", "busy", None, None, (("faq", "Q", "A", True),))
    result = {
        "session": "s1", "generated_at": "2026-01-01T00:00:00+00:00",
        "cached": False, "source": {"jsonl": True, "cflow": False},
        "briefing": {
            "goal": "목표", "now": "진행", "state": "working",
            "progress": "50%", "one-line-job-description": "작업",
        }, "raw": None,
    }
    paths.session_dir("s1").mkdir(parents=True)
    briefing._cache["s1"] = (key, result)
    briefing._persist("s1")

    # A new daemon process starts with an empty in-memory cache.
    briefing._cache.clear()
    briefing._loaded_cache_path = None
    assert briefing.digest("s1") == {
        "one_line": "작업", "state": "working",
        "goal": "목표", "now": "진행", "progress": "50%",
    }


def _brief_row(one, at):
    return {"key": ["k"], "result": {"session": "x", "generated_at": at,
            "briefing": {"state": "idle", "one-line-job-description": one}}}


def test_briefings_are_kept_one_file_per_session(home):
    """Each session's briefing lives in its own directory, so writing one
    rewrites one file (the fleet file was rewritten whole on every
    composition, and one write emptied all 636, claunch-lluxq), and clearing
    a session's directory takes its summary with it."""
    for name in ("s1", "s2"):
        paths.session_dir(name).mkdir(parents=True)
        briefing._cache[name] = (("k",), _brief_row(name, "t")["result"])
        briefing._persist(name)
    assert json.loads((paths.session_dir("s1") / "briefing.json").read_text(
        encoding="utf-8"))["result"]["briefing"]["one-line-job-description"] == "s1"
    assert not paths.briefings_json().exists()

    # the directory goes (Clear exited with logs): so does the summary
    import shutil
    shutil.rmtree(paths.session_dir("s2"))
    briefing._cache.clear()
    briefing._loaded_cache_path = None
    assert briefing.digest("s1")["one_line"] == "s1"
    assert briefing.digest("s2") is None


def test_the_fleet_file_is_split_into_session_files_once(home):
    """An old daemon's ``briefings.json`` is moved into per-session files on
    first read and renamed ``.migrated``. A session with no directory is not
    brought back; one that already has its own file keeps the later of the
    two (a restore written while the old daemon still ran is older than what
    that daemon composed meanwhile)."""
    for name in ("s1", "s2", "s3"):
        paths.session_dir(name).mkdir(parents=True)
    (paths.session_dir("s2") / "briefing.json").write_text(
        json.dumps(_brief_row("restored", "2026-10-27T00:00:00+00:00")), encoding="utf-8")
    (paths.session_dir("s3") / "briefing.json").write_text(
        json.dumps(_brief_row("own newer", "2026-10-29T00:00:00+00:00")), encoding="utf-8")
    legacy = paths.briefings_json()
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(json.dumps({
        "s1": _brief_row("fleet", "2026-10-28T00:00:00+00:00"),
        "s2": _brief_row("fleet newer", "2026-10-28T00:00:00+00:00"),
        "s3": _brief_row("fleet older", "2026-10-28T00:00:00+00:00"),
        "gone": _brief_row("cleared", "2026-10-28T00:00:00+00:00"),
    }), encoding="utf-8")

    briefing._loaded_cache_path = None
    assert [briefing.digest(n)["one_line"] for n in ("s1", "s2", "s3")] == [
        "fleet", "fleet newer", "own newer"]
    assert briefing.digest("gone") is None
    assert not paths.session_dir("gone").exists()
    assert not legacy.exists()
    assert legacy.with_name("briefings.json.migrated").is_file()


def test_migration_compares_generation_times_as_instants(home):
    """Which of two rows is later is a question about moments, not text.

    ``09:30+09:00`` is 00:30 UTC: earlier than the fleet row's 01:00 UTC
    though its digits sort later, and ``01:30+09:00`` (16:30 UTC the day
    before) is earlier still. A file with no time at all loses to any row
    that has one."""
    rows = {
        "s1": "2026-10-28T09:30:00+09:00",
        "s2": "2026-10-28T01:30:00+09:00",
        "s3": "",
    }
    for name, at in rows.items():
        paths.session_dir(name).mkdir(parents=True)
        (paths.session_dir(name) / "briefing.json").write_text(
            json.dumps(_brief_row("own", at)), encoding="utf-8")
    paths.session_dir("s4").mkdir(parents=True)
    (paths.session_dir("s4") / "briefing.json").write_text(
        json.dumps(_brief_row("own later", "2026-10-28T10:30:00+09:00")), encoding="utf-8")
    legacy = paths.briefings_json()
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(json.dumps({
        name: _brief_row("fleet", "2026-10-28T01:00:00+00:00")
        for name in ("s1", "s2", "s3", "s4")
    }), encoding="utf-8")

    briefing._loaded_cache_path = None
    assert [briefing.digest(n)["one_line"] for n in ("s1", "s2", "s3", "s4")] == [
        "fleet", "fleet", "fleet", "own later"]


def test_retire_all_keeps_every_briefing_and_marks_it_stale(home):
    """A model change used to clear the whole cache, and the rail of 636
    sessions lost every one-liner at once — exited sessions included, which
    nothing recomposes (claunch-lluxq). Retiring keeps the text for the rail,
    marks it stale, and makes the old key unmatchable so the next card or ⟳
    composes with the new model. It survives a daemon restart, and a second
    retire does not stack."""
    key = ("s1", 2, (1, 2), None, "idle", None, None, ())
    paths.session_dir("s1").mkdir(parents=True)
    briefing._cache["s1"] = (key, {"session": "s1", "briefing": {
        "goal": "g", "state": "idle", "one-line-job-description": "한 줄"}})
    # The old backend's failure goes with it: the row must not keep saying
    # "failed" about a model that is no longer asked.
    briefing._failures["s1"] = {"at": "t", "error": "HTTP 402"}
    briefing.retire_all()
    assert "s1" not in briefing._failures
    briefing.retire_all()

    briefing._cache.clear()
    briefing._loaded_cache_path = None
    assert briefing.digest("s1") == {
        "one_line": "한 줄", "state": "idle", "goal": "g", "stale": True,
    }
    assert briefing._cache["s1"][0] == (*key, "retired")
    assert briefing._cache["s1"][0] != key
    assert briefing.cached_result("s1") is None


def test_retire_all_async_writes_the_files_off_the_event_loop(home, monkeypatch):
    """Saving a new model retires every session's file — 639 took 183ms on
    the live daemon, inside the request. The async form changes the entries
    on the loop (the next poll reads them stale at once) and writes the files
    from a worker thread; a write that lands after the loop replaced an
    entry carries the replacement, so retiring never puts the old one back."""
    import threading

    for name in ("s1", "s2"):
        paths.session_dir(name).mkdir(parents=True)
        briefing._cache[name] = (("k",), _brief_row(name, "t")["result"])
    briefing._loaded_cache_path = paths.sessions_root()
    writers = []
    real = briefing._write_file
    monkeypatch.setattr(briefing, "_write_file", lambda path, row: (
        writers.append(threading.current_thread()), real(path, row))[-1])

    async def run():
        loop_thread = threading.current_thread()
        await briefing.retire_all_async()
        return loop_thread

    loop_thread = asyncio.run(run())
    assert writers and all(t is not loop_thread for t in writers)
    for name in ("s1", "s2"):
        row = json.loads((paths.session_dir(name) / "briefing.json").read_text(encoding="utf-8"))
        assert row["result"]["stale"] is True and row["key"] == ["k", "retired"]

    # the loop composed s1 anew after the entries were retired and before
    # the worker reached its file: the file holds the new answer
    briefing._cache["s1"] = (("k",), _brief_row("s1", "t")["result"])
    names = briefing._retire_entries()
    assert names == ["s1"]
    briefing._cache["s1"] = (("k2",), _brief_row("new", "t")["result"])
    briefing._write_each(names)
    row = json.loads((paths.session_dir("s1") / "briefing.json").read_text(encoding="utf-8"))
    assert row["key"] == ["k2"]
    assert row["result"]["briefing"]["one-line-job-description"] == "new"


def test_retire_all_does_not_recreate_a_cleared_session(home):
    """"Clear exited" removes a session's directory while the daemon still
    holds its briefing in memory. Retiring must not write that briefing back:
    the directory would reappear, and a new session given the same name would
    show the old session's summary."""
    import shutil

    paths.session_dir("gone").mkdir(parents=True)
    briefing._cache["gone"] = (("k",), _brief_row("gone", "t")["result"])
    briefing._persist("gone")
    shutil.rmtree(paths.session_dir("gone"))

    briefing.retire_all()
    assert not paths.session_dir("gone").exists()


class _Ended:
    """An exited record, enough for the manager's ``remove``/``clear``."""

    exited = True
    exit_code = 0
    pid = None
    created_at = 0.0
    last_output_at = 0.0
    last_visited_at = None
    last_input_at = None
    exited_at = None
    archived_at = None

    def __init__(self, name, parent=None):
        self.sdef = SessionDef(name=name, harness="py", parent=parent)

    def status(self, threshold=None):
        return "exited"

    def delivery_held(self):
        return False


@pytest.mark.parametrize("how", ["clear", "clear-logs", "remove", "remove-children", "discard"])
def test_a_removed_record_takes_its_briefing_along(home, monkeypatch, how):
    """The cache is keyed by name and outlives the record. Removing a record
    while the daemon runs left its briefing in memory, so a session created
    again under that name showed the old summary, and the next retire wrote
    it into the new directory (claunch-lluxq). Removing it drops the entry,
    the recorded failure and the file."""
    import shutil

    mgr = SessionManager(idle_threshold=1.0, scrollback=10, restore_default=False)
    for name in ("gone", "kid", "kept"):
        mgr._sessions[name] = _Ended(name, parent="gone" if name == "kid" else None)
        paths.session_dir(name).mkdir(parents=True)
        briefing._cache[name] = (("k",), _brief_row(name, "t")["result"])
        briefing._failures[name] = {"at": "t", "error": "HTTP 402"}
        briefing._persist(name)

    told = []
    monkeypatch.setattr(briefing, "persist_hooks", [lambda: told.append(1)])
    if how == "remove":
        mgr.remove("gone")
    elif how == "remove-children":
        mgr.remove("gone", children="remove")
    elif how == "discard":
        mgr.discard("gone")
    else:
        mgr.clear(logs=how == "clear-logs", keep=["kept", "kid"])
    assert briefing.digest("gone") is None
    assert not (paths.session_dir("gone") / "briefing.json").exists()
    assert told, "the search index is told the summary went away"
    # a removed child goes with its parent; an escalated one keeps its own
    if how == "remove-children":
        assert briefing.digest("kid") is None
        assert not (paths.session_dir("kid") / "briefing.json").exists()
    else:
        assert briefing.digest("kid")["one_line"] == "kid"

    shutil.rmtree(paths.session_dir("gone"), ignore_errors=True)
    paths.session_dir("gone").mkdir(parents=True)
    briefing.retire_all()
    assert briefing.digest("gone") is None
    assert not (paths.session_dir("gone") / "briefing.json").exists()
    briefing._cache.clear()
    briefing._loaded_cache_path = None
    assert briefing.digest("gone") is None
    assert briefing.digest("kept")["one_line"] == "kept"


@pytest.mark.parametrize("outcome", ["answer", "failure", "truncated"])
def test_a_record_removed_during_composition_keeps_nothing(home, tmp_path, monkeypatch, outcome):
    """The LLM call takes up to a minute. A record removed while it is out
    must not get its summary (or a failure) back when the answer lands:
    no cache entry, no file, no failure (claunch-lluxq)."""
    from types import SimpleNamespace

    session = SimpleNamespace(sdef=SessionDef(name="s1", harness="py", cwd=str(tmp_path)))
    paths.session_dir("s1").mkdir(parents=True)

    async def removed_meanwhile(cfg, prompt, **kw):
        briefing.forget(["s1"])
        if outcome == "failure":
            raise briefing.BriefingError("HTTP 402: insufficient credits")
        if outcome == "truncated":
            return briefing.LlmAnswer(text='{"goal": "cut', finish_reason="length",
                                      completion_tokens=100)
        return briefing.LlmAnswer(
            text=json.dumps({"goal": "g", "state": "idle",
                             "one-line-job-description": "old"}),
            finish_reason="stop", completion_tokens=10,
        )

    monkeypatch.setattr(briefing, "call_llm", removed_meanwhile)
    if outcome in ("failure", "truncated"):
        with pytest.raises(briefing.BriefingError):
            asyncio.run(briefing.compose(session, {"max_tokens": 100}, refresh=True))
    else:
        result = asyncio.run(briefing.compose(session, {"max_tokens": 100}, refresh=True))
        assert result["briefing"]["one-line-job-description"] == "old"
    assert briefing.digest("s1") is None
    assert "s1" not in briefing._failures
    assert not (paths.session_dir("s1") / "briefing.json").exists()


def test_compose_records_a_failure_until_the_next_success(home, tmp_path, monkeypatch):
    """A failed composition is kept, per session and across a daemon restart,
    so a row can tell "never asked" (no digest) from "asked and failed"
    (``failed``). It sits beside the briefing it did not replace, and the
    next success clears it."""
    from types import SimpleNamespace

    session = SimpleNamespace(sdef=SessionDef(name="s1", harness="py", cwd=str(tmp_path)))
    cfg = {"max_tokens": 100}
    paths.session_dir("s1").mkdir(parents=True)

    async def refuse(cfg, prompt, **kw):
        raise briefing.BriefingError("HTTP 402: insufficient credits")

    async def answer(cfg, prompt, **kw):
        return briefing.LlmAnswer(
            text=json.dumps({"goal": "g", "state": "idle",
                             "one-line-job-description": "한 줄"}),
            finish_reason="stop", completion_tokens=10,
        )

    assert briefing.digest("s1") is None
    monkeypatch.setattr(briefing, "call_llm", refuse)
    with pytest.raises(briefing.BriefingError):
        asyncio.run(briefing.compose(session, cfg, refresh=True))

    briefing._failures.clear()
    briefing._loaded_cache_path = None
    d = briefing.digest("s1")
    assert d["one_line"] == "" and d["failed"]["error"] == "HTTP 402: insufficient credits"
    assert d["failed"]["at"]

    monkeypatch.setattr(briefing, "call_llm", answer)
    asyncio.run(briefing.compose(session, cfg, refresh=True))
    assert briefing.digest("s1") == {"one_line": "한 줄", "state": "idle", "goal": "g"}

    # a failure after a success keeps the old text and says it failed
    monkeypatch.setattr(briefing, "call_llm", refuse)
    with pytest.raises(briefing.BriefingError):
        asyncio.run(briefing.compose(session, cfg, refresh=True))
    d = briefing.digest("s1")
    assert d["one_line"] == "한 줄" and d["failed"]["error"].startswith("HTTP 402")

    # an answer cut at the token budget is a failure too
    async def truncated(cfg, prompt, **kw):
        return briefing.LlmAnswer(text='{"goal": "g', finish_reason="length",
                                  completion_tokens=100)

    monkeypatch.setattr(briefing, "call_llm", truncated)
    with pytest.raises(briefing.BriefingError):
        asyncio.run(briefing.compose(session, cfg, refresh=True))
    assert "truncated at max_tokens=100" in briefing.digest("s1")["failed"]["error"]


def test_parse_briefing_bad_state_and_garbage():
    parsed = briefing.parse_briefing('{"goal": "g", "state": "SHRUGGING"}')
    assert parsed["state"] == "unknown" and parsed["now"] == ""
    assert briefing.parse_briefing("no json here") is None
    assert briefing.parse_briefing("") is None
    assert briefing.parse_briefing('["a", "list"]') is None


# --------------------------------------------------------------------------- #
# the LLM HTTP contract, against a local fake endpoint
# --------------------------------------------------------------------------- #
async def _start_llm(handler):
    from aiohttp import web as aioweb
    from aiohttp.test_utils import TestServer

    app = aioweb.Application()
    app.router.add_post("/v1/chat/completions", handler)
    server = TestServer(app)
    await server.start_server()
    return server


def _llm_answer(content: str, finish: str = "stop", spent: int = 42) -> dict:
    return {
        "choices": [
            {"message": {"role": "assistant", "content": content}, "finish_reason": finish}
        ],
        "usage": {"completion_tokens": spent},
    }


def test_call_llm_sends_the_openai_shape_and_reads_the_answer(home):
    from aiohttp import web as aioweb

    seen = {}

    async def handler(request):
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = await request.json()
        return aioweb.json_response(_llm_answer("the answer"))

    async def run():
        server = await _start_llm(handler)
        try:
            cfg = {
                "endpoint": str(server.make_url("/v1/chat/completions")),
                "model": "acc/model",
                "api_key": "sk-test",
                "max_tokens": 128,
                "params": {"top_k": 7},
            }
            got = await briefing.call_llm(cfg, "summarize this")
            assert got.text == "the answer"
            assert got.finish_reason == "stop"
            assert got.completion_tokens == 42
        finally:
            await server.close()

    asyncio.run(run())
    assert seen["auth"] == "Bearer sk-test"
    assert seen["body"]["model"] == "acc/model"
    assert seen["body"]["max_tokens"] == 128
    assert seen["body"]["top_k"] == 7  # params merged into the body
    assert seen["body"]["messages"] == [{"role": "user", "content": "summarize this"}]


def test_call_llm_sends_the_providers_required_session_header(home):
    """The end-to-end half of the fix.

    A provider that refuses a request without its own session header
    (OpenCode Go answers ``400 MissingSessionID``) rejected this feature's
    calls, because ``call_llm`` built its headers by hand and never read the
    provider's rule. Reproduced before the fix on 2026-10-25 against
    ``https://opencode.ai/zen/go/v1/chat/completions``; here the same shape
    is asserted against a local endpoint, so the assertion is that the header
    leaves, not that the provider is up.
    """
    from aiohttp import web as aioweb

    seen = {}

    async def handler(request):
        seen["session"] = request.headers.get("x-opencode-session")
        seen["auth"] = request.headers.get("Authorization")
        return aioweb.json_response(_llm_answer("ok"))

    async def run():
        server = await _start_llm(handler)
        try:
            cfg = {
                "endpoint": str(server.make_url("/v1/chat/completions")),
                "model": "deepseek-v4.1-flash",
                "api_key": "sk-test",
                "max_tokens": 32,
                "provider": "opencode",
            }
            store.update(lambda doc: doc.setdefault("providers", {}).update({
                "opencode": {"session_header": "x-opencode-session"}}))
            got = await briefing.call_llm(cfg, "hi")
            assert got.text == "ok"
        finally:
            await server.close()

    asyncio.run(run())
    assert seen["session"] == "claunch-briefing"
    # the OpenAI protocol keeps the bearer: x-api-key is for Messages only
    assert seen["auth"] == "Bearer sk-test"


def _spy_on_the_session(monkeypatch):
    """Record the connector every ``call_llm`` session is built with.

    The session is created inside the call, so the connector is not reachable
    from the outside; the spy delegates to the real class so the request still
    goes out and the answer is still read.
    """
    made = []
    real = briefing.aiohttp.ClientSession

    def spy(*args, **kwargs):
        made.append(kwargs.get("connector"))
        return real(*args, **kwargs)

    monkeypatch.setattr(briefing.aiohttp, "ClientSession", spy)
    return made


def test_call_llm_verifies_through_the_os_trust_store(home, monkeypatch):
    """The call hands its session a connector backed by the OS trust store.

    A TLS-inspection root the OS already trusts can still fail OpenSSL's own
    chain validation, and the whole feature is then dark on that machine with
    only a 502 on the card to show it. Measured against ``api.deepseek.com``
    before this: ``SSLCertVerificationError ... Basic Constraints of CA cert
    not marked critical``; the same request answered 200 through this context
    (claunch-h3ldl). :mod:`.observer` and :mod:`.rag` already call this way.
    """
    from aiohttp import web as aioweb

    made = _spy_on_the_session(monkeypatch)

    async def handler(request):
        return aioweb.json_response(_llm_answer("ok"))

    async def run():
        server = await _start_llm(handler)
        try:
            cfg = {
                "endpoint": str(server.make_url("/v1/chat/completions")),
                "model": "m",
                "api_key": "sk-test",
                "max_tokens": 64,
            }
            assert (await briefing.call_llm(cfg, "hi")).text == "ok"
        finally:
            await server.close()

    asyncio.run(run())
    assert len(made) == 1
    if briefing._OS_TRUST_CONTEXT is not None:
        assert made[0]._ssl is briefing._OS_TRUST_CONTEXT
    else:  # truststore unavailable in this interpreter
        assert made[0] is None


def test_call_llm_falls_back_when_truststore_is_unavailable(home, monkeypatch):
    """No OS-backed context (Python < 3.10, or ``truststore`` not installed)
    leaves the connector unset, so the call keeps working on aiohttp's own
    default context instead of failing for want of the better one."""
    from aiohttp import web as aioweb

    monkeypatch.setattr(briefing, "_OS_TRUST_CONTEXT", None)
    made = _spy_on_the_session(monkeypatch)

    async def handler(request):
        return aioweb.json_response(_llm_answer("still answered"))

    async def run():
        server = await _start_llm(handler)
        try:
            cfg = {
                "endpoint": str(server.make_url("/v1/chat/completions")),
                "model": "m",
                "api_key": "sk-test",
                "max_tokens": 64,
            }
            assert (await briefing.call_llm(cfg, "hi")).text == "still answered"
        finally:
            await server.close()

    asyncio.run(run())
    assert made == [None]


def test_call_llm_http_error_and_bad_shape_raise(home):
    from aiohttp import web as aioweb

    async def failing(request):
        return aioweb.json_response({"error": "nope"}, status=500)

    async def shapeless(request):
        return aioweb.json_response({"choices": []})

    async def not_json(request):
        return aioweb.Response(text="<html>proxy login</html>", content_type="text/html")

    async def run():
        for handler, fragment in ((failing, "answered 500"), (shapeless, "choices"),
                                  (not_json, "not JSON")):
            server = await _start_llm(handler)
            try:
                cfg = {
                    "endpoint": str(server.make_url("/v1/chat/completions")),
                    "model": "m",
                    "api_key": "k",
                    "max_tokens": 64,
                    "params": {},
                }
                with pytest.raises(briefing.BriefingError) as err:
                    await briefing.call_llm(cfg, "hi")
                assert fragment in str(err.value)
                # the key must never surface through the error path
                assert "k" == cfg["api_key"] and cfg["api_key"] not in str(err.value)
            finally:
                await server.close()

    asyncio.run(run())


def test_call_llm_empty_content_is_an_error_not_an_answer(home):
    """A reasoning model that spent max_tokens thinking returns 200 + "".

    Measured against the configured endpoint at ``max_tokens=1024``: 8 of 10
    calls came back exactly like this. Passing "" on made the daemon cache a
    blank briefing and answer 200, which is why the feature looked flaky
    rather than broken.
    """
    from aiohttp import web as aioweb

    async def run():
        for content in ("", "   "):
            async def handler(request, _c=content):
                return aioweb.json_response(_llm_answer(_c, finish="length", spent=1024))

            server = await _start_llm(handler)
            try:
                cfg = {
                    "endpoint": str(server.make_url("/v1/chat/completions")),
                    "model": "m",
                    "api_key": "sk-not-in-the-message",
                    "max_tokens": 1024,
                    "params": {},
                }
                with pytest.raises(briefing.BriefingError) as err:
                    await briefing.call_llm(cfg, "hi")
                msg = str(err.value)
                assert "empty content" in msg
                # the message must name the budget, or nobody can act on it
                assert "1024" in msg and "max_tokens" in msg
                assert cfg["api_key"] not in msg
            finally:
                await server.close()

    asyncio.run(run())


def test_call_llm_blames_the_provider_not_the_budget_when_it_just_stops(home):
    """An empty answer that was NOT cut off is a different fault.

    Measured live while re-checking the budget fix: one session's call came
    back ``finish_reason="stop"`` with ``completion_tokens=1`` and no content.
    Raising ``max_tokens`` cannot touch that, so the error must not send the
    reader to that knob — the two empties look identical without this field.
    """
    from aiohttp import web as aioweb

    async def handler(request):
        return aioweb.json_response(_llm_answer("", finish="stop", spent=1))

    async def run():
        server = await _start_llm(handler)
        try:
            cfg = {
                "endpoint": str(server.make_url("/v1/chat/completions")),
                "model": "m",
                "api_key": "sk-quiet",
                "max_tokens": 4096,
                "params": {},
            }
            with pytest.raises(briefing.BriefingError) as err:
                await briefing.call_llm(cfg, "hi")
            msg = str(err.value)
            assert "provider-side empty completion" in msg
            assert "raise llm.max_tokens" not in msg  # the wrong knob
            assert "finish_reason='stop'" in msg and "completion_tokens=1" in msg
        finally:
            await server.close()

    asyncio.run(run())


def test_call_llm_keeps_a_whole_answer_whatever_the_finish_reason(home):
    """``finish_reason`` is read, not obeyed: real text still comes back."""
    from aiohttp import web as aioweb

    async def handler(request):
        return aioweb.json_response(_llm_answer("plenty of words", finish="length"))

    async def run():
        server = await _start_llm(handler)
        try:
            cfg = {
                "endpoint": str(server.make_url("/v1/chat/completions")),
                "model": "m",
                "api_key": "k",
                "max_tokens": 8,
                "params": {},
            }
            got = await briefing.call_llm(cfg, "hi")
            assert got.text == "plenty of words"
            assert got.finish_reason == "length"
        finally:
            await server.close()

    asyncio.run(run())


def test_default_max_tokens_budgets_for_reasoning_not_just_the_answer():
    """The briefing runs a few hundred characters; the budget is not sized
    for the briefing.

    Pinned because the old 1024 was sized for the answer alone and the
    reasoning tokens are billed to the same allowance — the measured failure.
    """
    assert briefing.DEFAULT_MAX_TOKENS >= 4096


# --------------------------------------------------------------------------- #
# the endpoint: frozen response contract, cache, refresh
# --------------------------------------------------------------------------- #
def _register_py_harness() -> None:
    store.update(
        lambda doc: doc.update(
            {"harnesses": {"py": {"command": [sys.executable, "-u", "-c", CHILD]}}}
        )
    )


def _set_llm(endpoint: str) -> None:
    store.update(
        lambda doc: doc.update(
            {"llm": {"endpoint": endpoint, "model": "m", "api_key": "sk", "params": {}}}
        )
    )


async def _serve(mgr):
    from aiohttp.test_utils import TestClient, TestServer

    app = build_app(
        mgr, "sekrit", started_at=time.monotonic(), mesh=MeshManager(mgr)
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


def test_briefing_endpoint_contract_cache_and_refresh(home, tmp_path, monkeypatch):
    from aiohttp import web as aioweb

    _register_py_harness()
    hits = []
    answer = {
        "goal": "목표", "now": "작업 중", "state": "working", "progress": "70%",
        "one-line-job-description": "브리핑 백엔드",
    }

    async def handler(request):
        hits.append(await request.json())
        return aioweb.json_response(_llm_answer(json.dumps(answer, ensure_ascii=False)))

    async def run():
        llm = await _start_llm(handler)
        _set_llm(str(llm.make_url("/v1/chat/completions")))
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        client = await _serve(mgr)
        try:
            cwd = str(tmp_path / "work")
            (tmp_path / "work").mkdir()
            mgr.create(
                SessionDef(
                    name="s1", harness="py", cwd=cwd,
                    conversation_id="cafe0000-0000-0000-0000-000000000001",
                    task="브리핑 백엔드 구현",
                )
            )
            # the transcript claude would keep for this conversation, under
            # the config dir the conftest points CLAUDE_CONFIG_DIR at
            pdir = transcripts.project_dir(
                tmp_path / ".claude-config", cwd
            )
            pdir.mkdir(parents=True)
            (pdir / "cafe0000-0000-0000-0000-000000000001.jsonl").write_text(
                _jl(type="user", message={"role": "user", "content": "please build it"})
                + "\n",
                encoding="utf-8",
            )

            # The cache key carries this session's live state -- status,
            # last_input_at, last_output_at (bd9f2e02) -- and a session that
            # has only just started moves through that state on its own: the
            # harness child's first output flips status from 'starting' to
            # 'busy'/'idle' and fills last_output_at.  A second request that
            # lands after that arrival then misses a cache the first request
            # correctly filled, and the endpoint is right both times.
            # Measured here: with no delay between the two GETs the key held;
            # with 0.6s or 1.5s injected it differed on exactly those two
            # fields, and the assertion below failed 5/5.  What this test
            # pins is that an UNCHANGED key is served from cache, so the live
            # block is stated as a premise rather than raced against the
            # child's first paint.
            monkeypatch.setattr(
                briefing,
                "gather_live",
                lambda session: {
                    "status": "idle",
                    "running": True,
                    "last_input_at": None,
                    "last_output_at": None,
                },
            )

            # Opening with no saved result still composes the first briefing.
            resp = await client.get("/api/sessions/s1/briefing?cached=1", headers=BEARER)
            assert resp.status == 200
            body = await resp.json()
            assert body["session"] == "s1"
            assert body["cached"] is False
            assert body["generated_at"]
            assert body["source"] == {"jsonl": True, "cflow": False}
            assert body["briefing"] == answer
            assert body["raw"] is None
            # the prompt actually carried the evidence
            sent = hits[0]["messages"][0]["content"]
            assert "please build it" in sent and "브리핑 백엔드 구현" in sent

            # unchanged inputs: served from cache, no second LLM call
            resp = await client.get("/api/sessions/s1/briefing", headers=BEARER)
            body = await resp.json()
            assert body["cached"] is True and len(hits) == 1

            # A browser reload loses its cache, but opening the panel must
            # read the durable result even after the session's evidence moves.
            briefing._cache.clear()
            briefing._loaded_cache_path = None
            monkeypatch.setattr(briefing, "gather_live", lambda session: {"status": "busy"})
            resp = await client.get("/api/sessions/s1/briefing?cached=1", headers=BEARER)
            saved = await resp.json()
            assert resp.status == 200
            assert saved["cached"] is True and len(hits) == 1
            assert saved["generated_at"] == body["generated_at"]
            assert saved["briefing"] == answer

            # refresh=1 bypasses the cache
            resp = await client.get(
                "/api/sessions/s1/briefing?cached=1&refresh=1", headers=BEARER
            )
            body = await resp.json()
            assert body["cached"] is False and len(hits) == 2

            await mgr.shutdown_all()
        finally:
            await client.close()
            await llm.close()

    asyncio.run(run())


def test_briefing_cache_is_invalidated_when_the_live_state_moves(
    home, tmp_path, monkeypatch
):
    """A change in the session's live state forces a fresh briefing.

    This pins the reason ``bd9f2e02`` put ``status``, ``last_input_at`` and
    ``last_output_at`` into the cache key: a briefing describes a session
    that is running right now, so a cached one goes stale the moment that
    state moves. Take any of the three back out of the key and this test
    goes red on that field — which is what stops the next reader from
    dropping them to make a cache hit easier to assert.

    The state is stated (a stubbed :func:`briefing.gather_live`) rather than
    driven through a real harness, because the values are the input under
    test here and a live child moves them on its own schedule.
    """
    from aiohttp import web as aioweb

    _register_py_harness()
    hits = []
    answer = {
        "goal": "목표", "now": "작업 중", "state": "working", "progress": "70%",
        "one-line-job-description": "브리핑 백엔드",
    }

    async def handler(request):
        hits.append(await request.json())
        return aioweb.json_response(_llm_answer(json.dumps(answer, ensure_ascii=False)))

    base = {
        "status": "busy",
        "running": True,
        "last_input_at": "2026-01-01T00:00:00+00:00",
        "last_output_at": "2026-01-01T00:00:01+00:00",
    }

    live = dict(base)

    def pin(**changes):
        # changes ACCUMULATE, so every step below moves exactly one field
        # away from the state the previous request was served on. Restating
        # the whole dict each time would move two at once and let a key that
        # had dropped one of the fields still look correct.
        live.update(changes)
        state = dict(live)
        monkeypatch.setattr(briefing, "gather_live", lambda session: dict(state))

    async def run():
        llm = await _start_llm(handler)
        _set_llm(str(llm.make_url("/v1/chat/completions")))
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        client = await _serve(mgr)

        async def get():
            resp = await client.get("/api/sessions/s1/briefing", headers=BEARER)
            assert resp.status == 200
            return await resp.json()

        try:
            cwd = str(tmp_path / "work")
            (tmp_path / "work").mkdir()
            mgr.create(
                SessionDef(
                    name="s1", harness="py", cwd=cwd,
                    conversation_id="cafe0000-0000-0000-0000-000000000002",
                    task="브리핑 백엔드 구현",
                )
            )

            pin()
            assert (await get())["cached"] is False and len(hits) == 1
            # the same state twice: the key holds, so no second call
            pin()
            assert (await get())["cached"] is True and len(hits) == 1

            # each of the three live fields invalidates on its own
            pin(last_output_at="2026-01-01T00:00:09+00:00")
            assert (await get())["cached"] is False and len(hits) == 2
            pin(status="idle")
            assert (await get())["cached"] is False and len(hits) == 3
            pin(last_input_at="2026-01-01T00:00:08+00:00")
            assert (await get())["cached"] is False and len(hits) == 4

            # back to the original state: the cache holds ONE entry per
            # session (``_cache[name] = (key, result)``), so the entry now
            # carries the last state and the original one is a miss too
            pin(**base)
            assert (await get())["cached"] is False and len(hits) == 5

            # a change to the prompt's instructions invalidates too, so a
            # briefing worded under the old rules is not served on
            monkeypatch.setattr(briefing, "PROMPT_VERSION", briefing.PROMPT_VERSION + 1)
            assert (await get())["cached"] is False and len(hits) == 6
            assert (await get())["cached"] is True and len(hits) == 6

            await mgr.shutdown_all()
        finally:
            await client.close()
            await llm.close()

    asyncio.run(run())



def test_briefing_faq_can_be_managed_and_is_persisted(home, tmp_path):
    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        client = await _serve(mgr)
        try:
            resp = await client.post(
                "/api/briefing/faq", json={"question": "Q", "answer": "A"},
                headers=BEARER,
            )
            assert resp.status == 201
            body = await resp.json()
            row = body["entry"]
            assert row["question"] == "Q" and row["enabled"] is True

            resp = await client.get("/api/briefing/faq", headers=BEARER)
            assert (await resp.json())["faq"] == [row]
            resp = await client.put(
                f"/api/briefing/faq/{row['id']}",
                json={**row, "answer": "A2", "enabled": False}, headers=BEARER,
            )
            assert resp.status == 200
            assert (await resp.json())["entry"]["answer"] == "A2"
            resp = await client.delete(
                f"/api/briefing/faq/{row['id']}", headers=BEARER
            )
            assert resp.status == 200
            assert (await resp.json())["faq"] == []
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_briefing_faq_is_independent_of_the_global_config(home, tmp_path):
    """FAQ writes do not need to replace the user-wide settings file."""

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        client = await _serve(mgr)
        try:
            store.path().write_text("{ this: is: not: valid", encoding="utf-8")
            resp = await client.get("/api/briefing/faq", headers=BEARER)
            assert resp.status == 200 and (await resp.json())["faq"] == []
            resp = await client.post(
                "/api/briefing/faq", json={"question": "Q"}, headers=BEARER,
            )
            assert resp.status == 201
            row = (await resp.json())["entry"]
            resp = await client.put(
                f"/api/briefing/faq/{row['id']}",
                json={**row, "answer": "A"}, headers=BEARER,
            )
            assert resp.status == 200
            resp = await client.delete(
                f"/api/briefing/faq/{row['id']}", headers=BEARER,
            )
            assert resp.status == 200
        finally:
            await client.close()

    asyncio.run(run())


def test_briefing_faq_imports_the_legacy_global_setting(home):
    store.save({"briefing": {"faq": [{"question": "기존 질문"}]}})

    entries = briefing.faq_entries()

    assert entries[0]["question"] == "기존 질문"
    assert json.loads(paths.briefing_faq_json().read_text(encoding="utf-8")) == {
        "faq": entries
    }


def test_prompt_presets_can_be_managed_and_are_daemon_local(home):
    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        client = await _serve(mgr)
        try:
            resp = await client.post(
                "/api/prompt-presets",
                json={"name": "Review", "text": "Please review this change."},
                headers=BEARER,
            )
            assert resp.status == 201
            row = (await resp.json())["preset"]
            assert row["name"] == "Review" and row["enabled"] is True

            resp = await client.get("/api/prompt-presets", headers=BEARER)
            assert (await resp.json())["presets"] == [row]

            resp = await client.put(
                f"/api/prompt-presets/{row['id']}",
                json={**row, "text": "Please review the latest change.", "enabled": False},
                headers=BEARER,
            )
            assert resp.status == 200
            assert (await resp.json())["preset"]["enabled"] is False
            assert prompt_presets.entries()[0]["text"] == "Please review the latest change."

            resp = await client.delete(
                f"/api/prompt-presets/{row['id']}", headers=BEARER,
            )
            assert resp.status == 200
            assert (await resp.json())["presets"] == []
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_briefing_endpoint_unconfigured_unknown_and_raw(home, tmp_path):
    from aiohttp import web as aioweb

    _register_py_harness()

    async def rambling(request):
        return aioweb.json_response(_llm_answer("I cannot answer in JSON, sorry"))

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        client = await _serve(mgr)
        try:
            mgr.create(SessionDef(name="s1", harness="py", cwd=str(tmp_path)))

            # no llm: block at all -> the frozen 400
            resp = await client.get("/api/sessions/s1/briefing", headers=BEARER)
            assert resp.status == 400
            assert (await resp.json()) == {"error": "llm not configured"}

            # unknown session -> 404, and it wins over the config check
            resp = await client.get("/api/sessions/nope/briefing", headers=BEARER)
            assert resp.status == 404

            # a model that ignores the JSON instruction: raw, not an error
            llm = await _start_llm(rambling)
            try:
                _set_llm(str(llm.make_url("/v1/chat/completions")))
                resp = await client.get("/api/sessions/s1/briefing", headers=BEARER)
                assert resp.status == 200
                body = await resp.json()
                assert body["briefing"] is None
                assert body["raw"] == "I cannot answer in JSON, sorry"
                assert body["source"] == {"jsonl": False, "cflow": False}
            finally:
                await llm.close()

            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_briefing_endpoint_502s_on_a_truncated_answer(home, tmp_path):
    """Cut off at the budget is a failure, not a ``raw`` briefing.

    The live symptom this pins: one of 30 sessions came back with 68 bytes of
    a JSON object that stops mid-string, and the endpoint served it 200 with
    ``raw`` set — indistinguishable, to the UI and to the cache, from a model
    that simply answered in prose. ``finish_reason`` tells them apart, and
    so does ``completion_tokens``: over the 48-call budget sweep it equalled
    ``max_tokens`` in 5 of 5 cut-off calls and in 0 of 43 that finished (the
    largest of those spent 1855). This test pins the first because that is
    the field the contract defines for the purpose; the second is a
    corroborator, not a substitute.
    """
    from aiohttp import web as aioweb

    async def cut_off(request):
        return aioweb.json_response(
            _llm_answer('{"goal": "half a sen', finish="length", spent=1024)
        )

    async def empty(request):
        return aioweb.json_response(_llm_answer("", finish="length", spent=1024))

    _register_py_harness()

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        client = await _serve(mgr)
        try:
            mgr.create(SessionDef(name="s1", harness="py", cwd=str(tmp_path)))
            for handler, fragment in ((cut_off, "truncated"), (empty, "empty content")):
                llm = await _start_llm(handler)
                try:
                    _set_llm(str(llm.make_url("/v1/chat/completions")))
                    resp = await client.get(
                        "/api/sessions/s1/briefing?refresh=1", headers=BEARER
                    )
                    assert resp.status == 502
                    assert fragment in (await resp.json())["error"]
                finally:
                    await llm.close()
            # nothing was cached on the way out: the next good answer serves.
            # The failure itself is kept, so the row says "failed" rather
            # than "not yet" (claunch-lluxq).
            d = briefing.digest("s1")
            assert d["one_line"] == "" and "empty content" in d["failed"]["error"]
            assert briefing.cached_result("s1") is None
            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_sessions_list_says_whether_llm_is_configured(home, tmp_path):
    """The session list carries ``llm_configured`` so the web rail can show
    the briefing toggles disabled (with the why) before anyone clicks one."""

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        client = await _serve(mgr)
        try:
            # no llm: block -> the rail is told the feature is off
            resp = await client.get("/api/sessions", headers=BEARER)
            assert resp.status == 200
            assert (await resp.json())["llm_configured"] is False

            # writing the config flips the very next poll, no restart
            _set_llm("http://llm.example/v1/chat/completions")
            resp = await client.get("/api/sessions", headers=BEARER)
            assert (await resp.json())["llm_configured"] is True
        finally:
            await client.close()

    asyncio.run(run())


def test_sessions_list_survives_a_config_it_cannot_read(home, tmp_path):
    """An unreadable config costs the caller one toggle, never the rail.

    ``llm_configured`` is recomputed from disk on every poll (that is what
    makes writing the config flip the very next one, above), so a file that
    cannot be parsed reaches this handler as a ``StoreError`` — which
    ``error_middleware`` does not list, and which would therefore turn the
    one request the web UI cannot do without into a 500. The rail rebuilds
    its entire list off this response, so a poll that fails takes every row
    with it; the toggle it could not answer for is the cheaper thing to
    lose.
    """

    _register_py_harness()

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        client = await _serve(mgr)
        try:
            mgr.create(SessionDef(name="s1", harness="py", cwd=str(tmp_path)))
            # Broken *after* the session exists — the harness registry lives
            # in this same file, and the point is a config that goes bad
            # under a daemon that is already running.
            store.path().write_text("{ this: is: not: valid", encoding="utf-8")

            resp = await client.get("/api/sessions", headers=BEARER)
            assert resp.status == 200
            body = await resp.json()
            assert [s["name"] for s in body["sessions"]] == ["s1"]
            assert body["llm_configured"] is False

            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_sessions_list_survives_a_config_it_cannot_read_outside_the_read_window(
    home, tmp_path, monkeypatch
):
    """claunch-wgdcm: the poll that reaches the unreadable file for real.

    ``store.load`` serves a repeat read inside ``_DISK_TTL`` from memory, so
    the test above only reaches the broken file when the machine is slow
    enough for 100ms to pass between ``mgr.create`` and the poll (a loaded
    sweep). With the window at zero the poll always does.
    """
    monkeypatch.setattr(store, "_DISK_TTL", 0.0)
    _register_py_harness()

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        client = await _serve(mgr)
        try:
            mgr.create(SessionDef(name="s1", harness="py", cwd=str(tmp_path)))
            store.path().write_text("{ this: is: not: valid", encoding="utf-8")

            resp = await client.get("/api/sessions", headers=BEARER)
            assert resp.status == 200
            body = await resp.json()
            assert [s["name"] for s in body["sessions"]] == ["s1"]

            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_sessions_list_attaches_the_cached_briefing_digest(home, tmp_path):
    """The list poll pours each session's cached one-liner: a rail row can
    show it folded or open, and a browser refresh repaints it from the
    daemon's session state instead of regenerating — the list itself never
    calls the LLM, it only reads what compose() already left."""

    _register_py_harness()

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        client = await _serve(mgr)
        try:
            cwd = str(tmp_path / "work")
            (tmp_path / "work").mkdir()
            mgr.create(
                SessionDef(
                    name="s1", harness="py", cwd=cwd,
                    task="브리핑을 한 줄로 담기",
                )
            )
            # nothing composed yet: no digest, but the recorded task is there
            # for the row to fall back on
            resp = await client.get("/api/sessions", headers=BEARER)
            row = (await resp.json())["sessions"][0]
            assert "briefing" not in row
            assert row["task"] == "브리핑을 한 줄로 담기"

            # what one compose() would have left in the cache — the very next
            # poll carries it as the digest, with no LLM call involved
            briefing._cache["s1"] = (
                ("key",),
                {"session": "s1", "briefing": {
                    "goal": "g", "state": "waiting",
                    "one-line-job-description": "한 줄",
                }},
            )
            resp = await client.get("/api/sessions", headers=BEARER)
            row = (await resp.json())["sessions"][0]
            assert row["briefing"] == {
                "one_line": "한 줄", "state": "waiting", "goal": "g",
            }

            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# the settings endpoint the web dashboard's Briefing model card edits
# --------------------------------------------------------------------------- #
def test_briefing_llm_settings_endpoint_reads_and_saves(home):
    """``GET``/``PUT /api/briefing/llm``: the card's two calls.

    What is pinned here is the contract the card draws from — the written
    fields beside the resolved ones, the profile list it offers, and that the
    key is reported as a boolean and never returned.
    """
    _profile_backend_config()

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        client = await _serve(mgr)
        try:
            resp = await client.get("/api/briefing/llm", headers=BEARER)
            assert resp.status == 200
            body = await resp.json()
            assert body["profile"] == "" and body["configured"] is False
            assert body["api_key_set"] is False
            assert [row["name"] for row in body["profiles"]] == ["brief"]
            assert body["profiles"][0]["models"] == ["deepseek-flash", "glm-small"]
            assert body["profiles"][0]["has_key"] is True

            resp = await client.put(
                "/api/briefing/llm", headers=BEARER,
                json={"profile": "brief", "model": "glm-small", "max_tokens": 2048},
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["configured"] is True
            assert body["resolved"] == {
                "endpoint": "https://example.test/v1/chat/completions",
                "model": "glm-small",
                "has_key": True,
                # the profile holds no key pool, so nothing to pin or report
                "key_id": "",
                # the provider decides which request headers go out
                "provider": "ds",
            }
            # no secret crosses the boundary, in either direction of the save
            assert "api_key" not in json.dumps(body) or body["api_key_set"] is False

            # and it is the file that changed, so the next reader agrees
            assert briefing.llm_config()["model"] == "glm-small"
            resp = await client.get("/api/briefing/llm", headers=BEARER)
            assert (await resp.json())["profile"] == "brief"

            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_briefing_llm_save_clears_the_pair_a_profile_takes_over(home):
    """Choosing a profile takes the block's own endpoint and key out of the file.

    Both values are dead once a profile is named — the call reads neither —
    but they stay visible, and the one the card showed as "the endpoint in
    use" was whichever URL had been typed before. The card sends ``""`` for
    the endpoint and ``null`` for the key so the file says what the card
    says, and so a credential for a backend nobody calls does not sit there.
    """
    _profile_backend_config()
    store.update(
        lambda doc: doc.update(
            {
                "llm": {
                    "endpoint": "https://typed.test/v1/chat/completions",
                    "api_key": "typed-secret",
                    "model": "typed-model",
                    "max_tokens": 4096,
                }
            }
        )
    )

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        client = await _serve(mgr)
        try:
            resp = await client.put(
                "/api/briefing/llm",
                headers=BEARER,
                json={
                    "profile": "brief",
                    "model": "deepseek-flash",
                    "max_tokens": 4096,
                    "endpoint": "",
                    "api_key": None,
                },
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["endpoint"] == "" and body["api_key_set"] is False
            assert body["resolved"]["endpoint"] == (
                "https://example.test/v1/chat/completions"
            )

            block = store.load()["llm"]
            assert "endpoint" not in block and "api_key" not in block
            assert block["profile"] == "brief"
            assert block["model"] == "deepseek-flash"

            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_briefing_llm_settings_key_handling_and_refusals(home):
    """The api_key convention, and what a save refuses.

    A blank password field arrives on every reload, so blank keeps the stored
    key and ``null`` is the explicit removal. The refusals are the values a
    form cannot produce but a script can: an unknown field, an unknown
    profile, a URL carrying credentials, a budget that is not a number.
    """
    _profile_backend_config()

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        client = await _serve(mgr)
        try:
            resp = await client.put(
                "/api/briefing/llm", headers=BEARER,
                json={
                    "endpoint": "https://typed.example/v1/chat/completions",
                    "model": "m",
                    "api_key": "sk-typed",
                },
            )
            assert resp.status == 200 and (await resp.json())["api_key_set"] is True

            # blank keeps it
            resp = await client.put(
                "/api/briefing/llm", headers=BEARER, json={"api_key": ""},
            )
            assert (await resp.json())["api_key_set"] is True
            assert briefing.llm_config()["api_key"] == "sk-typed"

            # null removes it
            resp = await client.put(
                "/api/briefing/llm", headers=BEARER, json={"api_key": None},
            )
            body = await resp.json()
            assert body["api_key_set"] is False and body["configured"] is False
            assert briefing.llm_config()["api_key"] == ""

            for payload, fragment in [
                ({"params": {"temperature": 0.2}}, "unsupported"),
                ({"profile": "no-such-profile"}, "does not exist"),
                ({"endpoint": "https://user:pw@host/v1"}, "without credentials"),
                ({"endpoint": "ftp://host/v1"}, "without credentials"),
                ({"max_tokens": "lots"}, "whole number"),
                ({"max_tokens": 0}, "between 1"),
            ]:
                resp = await client.put(
                    "/api/briefing/llm", headers=BEARER, json=payload,
                )
                assert resp.status == 400, payload
                assert fragment in (await resp.json())["error"], payload
            # a refused save changed nothing
            assert briefing.llm_config()["model"] == "m"

            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_briefing_llm_key_pin_through_the_settings_endpoint(home):
    """The card's key pin: offered per profile, saved, resolved — never a token."""
    _prof, first, second = _pooled_profile()

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        client = await _serve(mgr)
        try:
            resp = await client.get("/api/briefing/llm", headers=BEARER)
            assert resp.status == 200
            body = await resp.json()
            row = next(r for r in body["profiles"] if r["name"] == "brief")
            assert [k["id"] for k in row["keys"]] == [first.id, second.id]
            # the token file holds the second key (add() made it effective)
            assert [k["current"] for k in row["keys"]] == [False, True]
            assert "sk-pool" not in json.dumps(body)

            resp = await client.put(
                "/api/briefing/llm", headers=BEARER,
                json={"profile": "brief", "key_id": first.id},
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["key_id"] == first.id
            assert body["resolved"]["key_id"] == first.id
            assert body["configured"] is True
            assert "sk-pool" not in json.dumps(body)
            # and it is the file that changed, so the next briefing agrees
            assert briefing.llm_config()["api_key"] == "sk-pool-first"
            assert store.load()["llm"]["key_id"] == first.id

            # a pin the pool does not hold is refused, and changes nothing
            resp = await client.put(
                "/api/briefing/llm", headers=BEARER,
                json={"profile": "brief", "key_id": "k-nope"},
            )
            assert resp.status == 400
            assert "holds no key" in (await resp.json())["error"]
            assert store.load()["llm"]["key_id"] == first.id

            # unpinning is a blank value
            resp = await client.put(
                "/api/briefing/llm", headers=BEARER,
                json={"profile": "brief", "key_id": ""},
            )
            assert resp.status == 200
            assert (await resp.json())["key_id"] == ""
            assert "key_id" not in store.load()["llm"]

            # a pin without a profile is dropped, not stored
            resp = await client.put(
                "/api/briefing/llm", headers=BEARER,
                json={
                    "profile": "",
                    "key_id": first.id,
                    "endpoint": "https://typed.example/v1/chat/completions",
                    "model": "m",
                    "api_key": "sk-typed",
                },
            )
            assert resp.status == 200
            block = store.load()["llm"]
            assert "key_id" not in block and "profile" not in block
            assert briefing.llm_config()["api_key"] == "sk-typed"

            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_briefing_llm_test_sends_the_pinned_key(home):
    """``POST /api/briefing/llm/test`` with a pin authenticates with that key.

    The probe walks the same resolution as the save, so what is asserted is
    the Authorization header the fake endpoint actually received.
    """
    from aiohttp import web as aioweb

    _prof, first, _second = _pooled_profile()
    seen = {"auth": []}

    async def handler(request):
        seen["auth"].append(request.headers.get("Authorization"))
        return aioweb.json_response(_llm_answer("pong"))

    async def run():
        server = await _start_llm(handler)
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        client = await _serve(mgr)
        try:
            store.update(
                lambda doc: doc["providers"]["ds"].update(
                    {"endpoints": {"openai": str(server.make_url("/v1"))}}
                )
            )
            resp = await client.post(
                "/api/briefing/llm/test", headers=BEARER,
                json={"profile": "brief", "model": "", "key_id": first.id},
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["ok"] is True
            assert body["key_id"] == first.id

            # without the pin the probe takes the token file's key
            resp = await client.post(
                "/api/briefing/llm/test", headers=BEARER,
                json={"profile": "brief", "model": ""},
            )
            assert (await resp.json())["ok"] is True

            await mgr.shutdown_all()
        finally:
            await client.close()
            await server.close()

    asyncio.run(run())
    # the pinned call first, then the token file's key for the unpinned one
    assert seen["auth"] == ["Bearer sk-pool-first", "Bearer sk-pool-second"]


def test_briefing_llm_presets_save_switch_and_keep_their_keys(home):
    """The whole point of presets, through the endpoint the card calls.

    Saving several backends and moving the pointer between them has to be
    enough to change which model answers — and a save that touches one
    preset must not wipe another's key, because every password box arrives
    blank and blank means "keep".
    """
    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        client = await _serve(mgr)
        try:
            presets = [
                {"id": "cheap", "endpoint": "https://cheap.example/v1/chat/completions",
                 "model": "glm-small", "api_key": "sk-cheap"},
                {"id": "big", "endpoint": "https://big.example/v1/chat/completions",
                 "model": "big-model", "api_key": "sk-big"},
            ]
            resp = await client.put(
                "/api/briefing/llm", headers=BEARER,
                json={"presets": presets, "preset": "cheap",
                      "endpoint": "", "model": "", "api_key": None},
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["preset"] == "cheap"
            assert body["configured"] is True
            assert body["resolved"]["model"] == "glm-small"
            assert body["api_key_set"] is False  # the block's own key
            rows = {r["id"]: r for r in body["presets"]}
            assert rows["cheap"]["api_key_set"] is True
            assert rows["big"]["api_key_set"] is True
            assert "sk-cheap" not in json.dumps(body)
            assert briefing.llm_config()["model"] == "glm-small"

            # moving the pointer is the switch — nothing else is sent
            resp = await client.put(
                "/api/briefing/llm", headers=BEARER, json={"preset": "big"},
            )
            body = await resp.json()
            assert body["preset"] == "big"
            assert body["resolved"]["model"] == "big-model"
            assert briefing.llm_config()["api_key"] == "sk-big"

            # a save that sends blank keys for both rows keeps both secrets
            resp = await client.put(
                "/api/briefing/llm", headers=BEARER,
                json={"presets": [
                    {"id": "cheap", "endpoint": "https://cheap.example/v1/chat/completions",
                     "model": "glm-small-2", "api_key": ""},
                    {"id": "big", "endpoint": "https://big.example/v1/chat/completions",
                     "model": "big-model", "api_key": ""},
                ]},
            )
            assert resp.status == 200
            stored = {p["id"]: p for p in briefing.presets()}
            assert stored["cheap"]["api_key"] == "sk-cheap"
            assert stored["big"]["api_key"] == "sk-big"
            assert briefing.llm_config()["model"] == "big-model"

            # null drops one preset's key and leaves the other alone
            resp = await client.put(
                "/api/briefing/llm", headers=BEARER,
                json={"presets": [
                    {"id": "cheap", "endpoint": "https://cheap.example/v1/chat/completions",
                     "model": "glm-small-2"},
                    {"id": "big", "endpoint": "https://big.example/v1/chat/completions",
                     "model": "big-model", "api_key": None},
                ]},
            )
            assert resp.status == 200
            stored = {p["id"]: p for p in briefing.presets()}
            assert stored["cheap"]["api_key"] == "sk-cheap"
            assert "api_key" not in stored["big"]
            # the call is now unconfigured: the active preset lost its key
            assert (await resp.json())["configured"] is False

            for payload, fragment in [
                ({"preset": "nope"}, "not one of the saved presets"),
                ({"presets": [{"id": "BAD", "endpoint": "https://x/v1"}]},
                 "lowercase"),
                ({"presets": [{"id": "a", "model": "m"}]},
                 "needs a profile or an endpoint"),
            ]:
                resp = await client.put(
                    "/api/briefing/llm", headers=BEARER, json=payload,
                )
                assert resp.status == 400, payload
                assert fragment in (await resp.json())["error"], payload
                # a refused save changed nothing
                assert [p["id"] for p in briefing.presets()] == ["cheap", "big"]

            # deleting the active preset leaves the pointer cleared, not stale
            resp = await client.put(
                "/api/briefing/llm", headers=BEARER,
                json={"presets": [{"id": "cheap",
                                   "endpoint": "https://cheap.example/v1/chat/completions",
                                   "model": "glm-small-2"}],
                      "preset": ""},
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["preset"] == ""
            assert [p["id"] for p in body["presets"]] == ["cheap"]

            # renaming a preset keeps its stored key: the card's Name box
            # edits the id in place, and the secret is filed under the name it
            # was saved as, so the row travels with its previous name. Without
            # that, editing a name would silently drop the credential and turn
            # the briefing off (peer-review, claunch-wug8b).
            resp = await client.put(
                "/api/briefing/llm", headers=BEARER,
                json={"presets": [
                    {"id": "cheap-renamed", "previous_id": "cheap",
                     "endpoint": "https://cheap.example/v1/chat/completions",
                     "model": "glm-small-2"},
                ], "preset": "cheap-renamed"},
            )
            assert resp.status == 200
            stored = {p["id"]: p for p in briefing.presets()}
            assert stored["cheap-renamed"]["api_key"] == "sk-cheap"
            body = await resp.json()
            assert body["configured"] is True
            assert body["api_key_set"] is False  # the block's own key
            assert briefing.llm_config()["api_key"] == "sk-cheap"

            # a rename that names no stored row inherits nothing
            resp = await client.put(
                "/api/briefing/llm", headers=BEARER,
                json={"presets": [
                    {"id": "fresh", "previous_id": "never-existed",
                     "endpoint": "https://x/v1/chat/completions", "model": "m"},
                ], "preset": "fresh"},
            )
            assert resp.status == 200
            assert (await resp.json())["configured"] is False

            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_briefing_llm_reports_a_pointer_at_a_preset_that_is_gone(home):
    """A pointer naming no saved preset is reported, not silently dropped.

    ``active_preset()`` filters the pointer, so the payload cannot read the
    filtered value to tell the card what happened. It reads the block's own
    name: the call falls back to the block's fields, and the card says so
    instead of showing an unexplained "none" (peer-review, claunch-wug8b).
    """
    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        client = await _serve(mgr)
        try:
            store.update(lambda doc: doc.update({"llm": {
                "endpoint": "https://block.example/v1/chat/completions",
                "model": "block-model", "api_key": "sk-block",
                "presets": [{"id": "cheap", "endpoint": "https://cheap/v1",
                             "model": "glm"}],
                "preset": "gone",
            }}))

            resp = await client.get("/api/briefing/llm", headers=BEARER)
            body = await resp.json()
            # the call falls back to the block, and the pointer is still named
            assert body["preset"] == ""
            assert body["resolved"]["model"] == "block-model"
            assert body["configured"] is True
            assert "gone" in body["error"]
            assert "not saved" in body["error"]

            # a saved pointer reports no error
            store.update(lambda doc: doc["llm"].update({"preset": "cheap"}))
            body = await (await client.get("/api/briefing/llm", headers=BEARER)).json()
            assert body["preset"] == "cheap"
            assert body["error"] == ""

            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_briefing_llm_save_retires_cached_briefings_when_the_backend_changes(home):
    """A model change takes effect on the next card, not on the next commit.

    The cache key is a session's evidence, so a briefing written by the
    previous model would otherwise stay on the card until that session's
    transcript moved. A save that leaves the resolved backend alone (here,
    the token budget) keeps the cache, because the answers are still that
    model's.
    """
    _profile_backend_config()

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        client = await _serve(mgr)
        try:
            seed = lambda: briefing._cache.__setitem__(
                "s1",
                (("key",), {"session": "s1", "briefing": {
                    "goal": "g", "state": "working",
                    "one-line-job-description": "한 줄",
                }}),
            )
            seed()
            resp = await client.put(
                "/api/briefing/llm", headers=BEARER,
                json={"profile": "brief", "model": "glm-small"},
            )
            assert resp.status == 200
            # retired, not dropped: the rail keeps the text, marked stale,
            # and the card no longer serves it (claunch-lluxq)
            assert briefing.digest("s1") == {
                "one_line": "한 줄", "state": "working", "goal": "g", "stale": True,
            }
            assert briefing.cached_result("s1") is None

            seed()
            resp = await client.put(
                "/api/briefing/llm", headers=BEARER, json={"max_tokens": 8192},
            )
            assert resp.status == 200
            assert briefing.digest("s1") is not None

            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# "Test connection": one minimal call through the briefing's own path
# --------------------------------------------------------------------------- #
def test_probe_llm_reports_a_working_backend(home):
    """A reachable endpoint that answers is reported with its status code.

    The point of the probe is that it walks the same path a briefing does, so
    what is asserted here is the shape that is actually sent: the configured
    model, a budget floored to the probe's own, and the authorization header.
    """
    from aiohttp import web as aioweb

    seen = {}

    async def handler(request):
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = await request.json()
        return aioweb.json_response(_llm_answer("pong"))

    async def run():
        server = await _start_llm(handler)
        try:
            cfg = {
                "endpoint": str(server.make_url("/v1/chat/completions")),
                "model": "acc/model",
                "api_key": "sk-test",
                "provider": "",
                "max_tokens": 4096,
                "params": {},
            }
            got = await briefing.probe_llm(cfg)
            assert got.ok is True
            assert got.status == 200
            assert got.model == "acc/model"
            assert "the backend replied" in got.detail
        finally:
            await server.close()

    asyncio.run(run())
    # same credential as a briefing, and the probe's own small budget
    assert seen["auth"] == "Bearer sk-test"
    assert seen["body"]["model"] == "acc/model"
    assert seen["body"]["max_tokens"] == briefing.CONNECTION_MAX_TOKENS
    assert seen["body"]["messages"] == [
        {"role": "user", "content": briefing.CONNECTION_PROMPT}
    ]


def test_probe_llm_caps_the_budget_after_merging_the_saved_params(home):
    """``params`` cannot lift the probe's budget back to the block's.

    ``params`` is merged into the body, and it can carry ``max_tokens`` of its
    own. Applied before the cap it would win, and the probe would spend the
    saved 4096 to learn what a 16-token call already answers.
    """
    from aiohttp import web as aioweb

    seen = {}

    async def handler(request):
        seen["body"] = await request.json()
        return aioweb.json_response(_llm_answer("pong"))

    async def run():
        server = await _start_llm(handler)
        try:
            cfg = {
                "endpoint": str(server.make_url("/v1/chat/completions")),
                "model": "m",
                "api_key": "k",
                "provider": "",
                "max_tokens": 4096,
                "params": {"max_tokens": 4096, "temperature": 0.2},
            }
            got = await briefing.probe_llm(cfg)
            assert got.ok is True
        finally:
            await server.close()

    asyncio.run(run())
    assert seen["body"]["max_tokens"] == briefing.CONNECTION_MAX_TOKENS
    # the rest of the saved params still travel: only the budget is bounded
    assert seen["body"]["temperature"] == 0.2


def test_probe_llm_applies_the_providers_session_header(home):
    """The half that makes the button worth having.

    A provider that refuses a briefing without its session header
    (OpenCode Go answers ``400 MissingSessionID``) also refuses the probe —
    unless the probe goes through ``briefing_request_headers``, which it does.
    A probe that built its own request would report "connected" for a backend
    every real briefing then fails on.
    """
    from aiohttp import web as aioweb

    _profile_backend_config(provider="ocgo")
    seen = {}

    async def handler(request):
        seen["session"] = request.headers.get("x-ocgo-session")
        return aioweb.json_response(_llm_answer("pong"))

    async def run():
        server = await _start_llm(handler)
        try:
            store.update(
                lambda doc: doc.setdefault("providers", {}).setdefault(
                    "ocgo", {}
                ).update({"session_header": "x-ocgo-session"})
            )
            cfg = {
                "endpoint": str(server.make_url("/v1/chat/completions")),
                "model": "m",
                "api_key": "k",
                "provider": "ocgo",
                "max_tokens": 4096,
                "params": {},
            }
            got = await briefing.probe_llm(cfg)
            assert got.ok is True
        finally:
            await server.close()

    asyncio.run(run())
    assert seen["session"] == briefing.BRIEFING_SESSION_ID


def test_probe_llm_reports_an_upstream_refusal_with_its_status(home):
    """A 400 is a result the card shows, not an exception thrown at it.

    The body snippet is what makes the message actionable — ``400
    MissingSessionID`` names the missing header, where a bare "failed" sends
    whoever reads it to the network.
    """
    from aiohttp import web as aioweb

    async def handler(request):
        return aioweb.json_response({"error": "MissingSessionID"}, status=400)

    async def run():
        server = await _start_llm(handler)
        try:
            cfg = {
                "endpoint": str(server.make_url("/v1/chat/completions")),
                "model": "m",
                "api_key": "k",
                "provider": "",
                "max_tokens": 4096,
                "params": {},
            }
            got = await briefing.probe_llm(cfg)
            assert got.ok is False
            assert got.status == 400
            assert "answered 400" in got.detail
            assert "MissingSessionID" in got.detail
        finally:
            await server.close()

    asyncio.run(run())


def test_probe_llm_says_what_is_missing_instead_of_calling(home):
    """Nothing configured is reported as such, and no request goes out."""
    async def run():
        got = await briefing.probe_llm(
            {"endpoint": "", "model": "", "api_key": "", "provider": ""}
        )
        assert got.ok is False
        assert got.status is None
        assert got.detail.startswith("not sent:")
        # all three are named, so one fix does not reveal the next
        for field in ("endpoint", "model", "api_key"):
            assert field in got.detail

    asyncio.run(run())


def test_probe_llm_reports_a_body_that_is_not_a_chat_completion(home):
    """A 200 whose body has no choices is not a working briefing backend.

    An HTML error page behind a proxy arrives as 200 often enough that
    treating status alone as success would report a backend that cannot
    answer a briefing.
    """
    from aiohttp import web as aioweb

    async def handler(request):
        return aioweb.Response(text="<html>gateway</html>", status=200)

    async def run():
        server = await _start_llm(handler)
        try:
            cfg = {
                "endpoint": str(server.make_url("/v1/chat/completions")),
                "model": "m",
                "api_key": "k",
                "provider": "",
                "max_tokens": 4096,
                "params": {},
            }
            got = await briefing.probe_llm(cfg)
            assert got.ok is False
            assert got.status == 200
            assert "no choices[0]" in got.detail
        finally:
            await server.close()

    asyncio.run(run())


def test_briefing_llm_test_endpoint_tests_the_unsaved_fields(home):
    """``POST /api/briefing/llm/test``: the card's button.

    The fields it is given win over the file, because the backend an operator
    is checking is the one they just typed — a probe that read the saved block
    would confirm the backend they are trying to replace. A blank key means
    the stored one, the same convention the save's password box follows.
    """
    from aiohttp import web as aioweb

    seen = {}

    async def handler(request):
        seen["body"] = await request.json()
        seen["auth"] = request.headers.get("Authorization")
        return aioweb.json_response(_llm_answer("pong"))

    async def run():
        server = await _start_llm(handler)
        try:
            url = str(server.make_url("/v1/chat/completions"))
            # the file holds a different backend than the one being tested
            _set_llm("https://saved.test/v1/chat/completions")
            mgr = SessionManager(
                idle_threshold=0.5, scrollback=200, restore_default=True
            )
            client = await _serve(mgr)
            try:
                resp = await client.post(
                    "/api/briefing/llm/test", headers=BEARER,
                    json={"endpoint": url, "model": "typed-model", "api_key": "typed-key"},
                )
                assert resp.status == 200
                body = await resp.json()
                assert body["ok"] is True
                assert body["status"] == 200
                assert body["endpoint"] == url
                assert body["model"] == "typed-model"
                # nothing is written: a test is not a save
                assert briefing.llm_config()["endpoint"] == (
                    "https://saved.test/v1/chat/completions"
                )
            finally:
                await client.close()
                await mgr.shutdown_all()
        finally:
            await server.close()

    asyncio.run(run())
    assert seen["body"]["model"] == "typed-model"
    # the typed key goes out, not the saved one: the credential on the wire is
    # the one being tested, which is the whole point of the unsaved-fields axis
    assert seen["auth"] == "Bearer typed-key"


def test_briefing_llm_test_endpoint_uses_the_stored_key_when_blank(home):
    """The card cannot send a secret back, so a blank key is "the stored one".

    Without this the button would report "not sent: api_key not set" on every
    backend whose key was already saved — which is all of them, since the
    password box arrives blank on each render.
    """
    from aiohttp import web as aioweb

    seen = {}

    async def handler(request):
        seen["auth"] = request.headers.get("Authorization")
        return aioweb.json_response(_llm_answer("pong"))

    async def run():
        server = await _start_llm(handler)
        try:
            url = str(server.make_url("/v1/chat/completions"))
            _set_llm(url)  # stores api_key "sk"
            mgr = SessionManager(
                idle_threshold=0.5, scrollback=200, restore_default=True
            )
            client = await _serve(mgr)
            try:
                resp = await client.post(
                    "/api/briefing/llm/test", headers=BEARER,
                    json={"endpoint": url, "model": "m", "api_key": ""},
                )
                assert resp.status == 200
                assert (await resp.json())["ok"] is True
            finally:
                await client.close()
                await mgr.shutdown_all()
        finally:
            await server.close()

    asyncio.run(run())
    assert seen["auth"] == "Bearer sk"


def test_briefing_llm_test_endpoint_never_returns_the_key(home):
    """The reported result carries the outcome, never the credential.

    The card renders ``detail`` verbatim and the response is a JSON body on
    the wire, so a key that reached either one would be on screen and in
    whatever recorded the call. Both paths are checked: a connected backend
    and an upstream refusal, whose snippet could otherwise be assembled from
    the request.
    """
    from aiohttp import web as aioweb

    secret = "sk-live-9c1f-never-echo"

    async def ok_handler(request):
        return aioweb.json_response(_llm_answer("pong"))

    async def bad_handler(request):
        return aioweb.json_response(
            {"error": "invalid key", "given": "…"}, status=401
        )

    async def run():
        for handler, expect_ok in ((ok_handler, True), (bad_handler, False)):
            server = await _start_llm(handler)
            try:
                url = str(server.make_url("/v1/chat/completions"))
                mgr = SessionManager(
                    idle_threshold=0.5, scrollback=200, restore_default=True
                )
                client = await _serve(mgr)
                try:
                    resp = await client.post(
                        "/api/briefing/llm/test", headers=BEARER,
                        json={"endpoint": url, "model": "m", "api_key": secret},
                    )
                    assert resp.status == 200
                    text = await resp.text()
                    body = json.loads(text)
                    assert body["ok"] is expect_ok
                    assert secret not in text
                    assert secret not in body["detail"]
                finally:
                    await client.close()
                    await mgr.shutdown_all()
            finally:
                await server.close()

    asyncio.run(run())


def test_probe_llm_does_not_echo_the_key_on_failure(home):
    """The failure sentence is built from the response, not the request.

    ``detail`` goes straight into the card, so an unreachable host's exception
    text and an upstream body are the two places a key could ride out. Neither
    does: the transport error names the endpoint, the refusal names the status.
    """
    from aiohttp import web as aioweb

    secret = "sk-live-7f2a-never-echo"
    seen = {}

    async def handler(request):
        seen["auth"] = request.headers.get("Authorization")
        return aioweb.json_response({"error": "nope"}, status=503)

    async def run():
        server = await _start_llm(handler)
        try:
            cfg = {
                "endpoint": str(server.make_url("/v1/chat/completions")),
                "model": "m",
                "api_key": secret,
                "provider": "",
                "max_tokens": 4096,
                "params": {},
            }
            got = await briefing.probe_llm(cfg)
            assert got.ok is False
            assert got.status == 503
            assert secret not in got.detail
            # the key did travel, in the header only
            assert seen["auth"] == f"Bearer {secret}"
        finally:
            await server.close()

    asyncio.run(run())


def test_briefing_llm_test_endpoint_reports_a_profile_that_cannot_serve(home):
    """A profile the picker cannot use is answered, not raised.

    The card offers hand-edited names too, and "this profile has no OpenAI
    endpoint" is precisely what the operator needs to read there.
    """
    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        client = await _serve(mgr)
        try:
            resp = await client.post(
                "/api/briefing/llm/test", headers=BEARER,
                json={"profile": "gone", "model": "m"},
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["ok"] is False
            assert body["detail"].startswith("not sent:")
            assert "gone" in body["detail"]
        finally:
            await client.close()
            await mgr.shutdown_all()

    asyncio.run(run())


def test_briefing_llm_test_endpoint_refuses_a_body_it_cannot_test(home):
    """Fields the probe has no meaning for are refused, not ignored.

    ``presets``/``max_tokens`` describe the block rather than the backend, and
    a probe that quietly dropped one would report on something other than
    what the caller asked about. ``preset`` is accepted (it names which saved
    row a blank field comes from) but only as text.
    """
    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        client = await _serve(mgr)
        try:
            for bad in ({"max_tokens": 8}, {"presets": []}, {"preset": 7}):
                resp = await client.post(
                    "/api/briefing/llm/test", headers=BEARER, json=bad,
                )
                assert resp.status == 400, bad
        finally:
            await client.close()
            await mgr.shutdown_all()

    asyncio.run(run())


def test_briefing_llm_test_endpoint_tests_the_chosen_presets_stored_key(home):
    """A blank key resolves against the preset being tested, not the active one.

    The card sends ``preset`` for exactly this: the password box never holds a
    secret, so the daemon has to look the key up somewhere, and looking it up
    under whichever preset happens to be *saved* would send one backend's
    credential to another backend's endpoint. Here "cheap" is saved and active,
    "big" is the row being tried out, and the key that must go out is big's.
    """
    from aiohttp import web as aioweb

    seen = {}

    async def handler(request):
        seen["auth"] = request.headers.get("Authorization")
        return aioweb.json_response(_llm_answer("pong"))

    async def run():
        server = await _start_llm(handler)
        try:
            url = str(server.make_url("/v1/chat/completions"))
            store.update(
                lambda doc: doc.update(
                    {
                        "llm": {
                            "preset": "cheap",
                            "max_tokens": 4096,
                            "presets": [
                                {"id": "cheap", "endpoint": "https://cheap.test/v1",
                                 "model": "glm-small", "api_key": "cheap-key"},
                                {"id": "big", "endpoint": url,
                                 "model": "big-model", "api_key": "big-key"},
                            ],
                        }
                    }
                )
            )
            mgr = SessionManager(
                idle_threshold=0.5, scrollback=200, restore_default=True
            )
            client = await _serve(mgr)
            try:
                resp = await client.post(
                    "/api/briefing/llm/test", headers=BEARER,
                    json={"preset": "big", "profile": "", "model": "big-model",
                          "endpoint": url, "api_key": ""},
                )
                assert resp.status == 200
                body = await resp.json()
                assert body["ok"] is True
                assert body["endpoint"] == url
            finally:
                await client.close()
                await mgr.shutdown_all()
        finally:
            await server.close()

    asyncio.run(run())
    assert seen["auth"] == "Bearer big-key"


def test_a_locked_index_does_not_turn_a_composed_briefing_into_a_500(
    home, tmp_path, monkeypatch
):
    """A locked index DB is a derived-store failure, not a compose failure.

    ``compose`` caches the briefing and calls ``_persist`` before it
    mirrors the result into ``search_records``. That mirror opens SQLite, so
    the failure that arrives is ``sqlite3.OperationalError`` -- not an
    ``OSError``, and not a ``BriefingError`` -- and with no guard it left
    ``GET /api/sessions/{name}/briefing`` as an uncaught exception: the route
    catches only ``BriefingError`` (for the 502 a refused model call gets) and
    ``error_middleware`` re-raises what it does not name, so aiohttp answered
    500 with nothing about the index in it. The daemon's
    ``briefing`` trigger did the same thing through
    ``cflow_clock.recompose_briefing`` and reported "could not be composed"
    for a briefing that had in fact been composed and cached.

    The route is what is asserted on, plus the durable file the briefing was
    already on: a 200 with the composed payload, ``cached: False`` because
    this compose really did run, and the briefing present in the cached read
    after the in-memory cache is dropped.

    Mutation: dropping the guard in ``_index_briefing`` makes this red on the
    status code.
    """
    import sqlite3

    from aiohttp import web as aioweb

    from claude_launcher.daemon import search_records

    _register_py_harness()
    answer = {
        "goal": "목표", "now": "작업 중", "state": "working", "progress": "70%",
        "one-line-job-description": "색인 가드",
    }

    async def handler(request):
        return aioweb.json_response(
            _llm_answer(json.dumps(answer, ensure_ascii=False))
        )

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    async def run():
        llm = await _start_llm(handler)
        _set_llm(str(llm.make_url("/v1/chat/completions")))
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        client = await _serve(mgr)
        try:
            cwd = str(tmp_path / "work")
            (tmp_path / "work").mkdir()
            mgr.create(SessionDef(name="s1", harness="py", cwd=cwd))
            # Stated, not raced: the child's first output moves `status`, and
            # this test is about the index guard, not the cache key.
            monkeypatch.setattr(
                briefing, "gather_live",
                lambda session: {"status": "idle", "running": True,
                                 "last_input_at": None, "last_output_at": None},
            )
            monkeypatch.setattr(search_records, "capture", locked)

            composing = await client.get(
                "/api/sessions/s1/briefing?refresh=1", headers=BEARER
            )
            status = composing.status
            body = await composing.json() if status == 200 else {}

            # The briefing is already on disk; a locked index must not take
            # that away from the reads that do not go through the index.
            briefing._cache.clear()
            briefing._loaded_cache_path = None
            reread = await client.get(
                "/api/sessions/s1/briefing?cached=1", headers=BEARER
            )
            saved_status = reread.status
            saved = await reread.json() if saved_status == 200 else {}
        finally:
            await mgr.shutdown_all()
            await client.close()
            await llm.close()
        return status, body, saved_status, saved

    status, body, saved_status, saved = asyncio.run(run())
    assert status == 200
    assert body["cached"] is False
    assert body["briefing"] == answer
    assert saved_status == 200
    assert saved["cached"] is True
    assert saved["briefing"] == answer
