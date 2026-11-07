"""``/api/config/doc`` and the profile token routes (claunch-gr62q).

A remote settings page reads this machine's config file as JSON with its
hash, sends back path ops against that hash, and carries a profile's
``.launcher-token`` across. The app here is the daemon's own auth middleware
in front of the four handlers, so a request that passes or is refused here is
passed or refused the same way by the running daemon.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher import config_doc, credentials, profile, store
from claude_launcher.daemon import api

TOKEN = "daemon-bearer-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SECRET = "sk-ant-oat01-SECRET-VALUE-0123456789"


def _app(cookies=()):
    app = web.Application(middlewares=[api.build_auth_middleware(TOKEN, set(cookies))])
    app.router.add_get("/api/config/doc", api.h_config_doc_get)
    app.router.add_post("/api/config/doc", api.h_config_doc_post)
    app.router.add_get("/api/config/profiles/{profile}/token", api.h_config_profile_token_get)
    app.router.add_put("/api/config/profiles/{profile}/token", api.h_config_profile_token_put)
    return app


def call(method, url, *, json=None, headers=AUTH, cookies=None, app_cookies=()):
    """``(status, body)`` of one request through the middleware and router."""

    async def run():
        client = TestClient(TestServer(_app(app_cookies)), cookies=cookies)
        await client.start_server()
        try:
            resp = await client.request(method, url, json=json, headers=headers)
            return resp.status, await resp.json()
        finally:
            await client.close()

    return asyncio.run(run())


def write_config(doc):
    store.save(doc)
    return hashlib.sha256(store.path().read_bytes()).hexdigest()


# --------------------------------------------------------------------------- #
# GET /api/config/doc
# --------------------------------------------------------------------------- #
def test_get_returns_the_document_its_file_hash_and_the_machine(home):
    digest = write_config({"version": 2, "profiles": {"work": {"env": {"A": "1"}}}})
    status, body = call("GET", "/api/config/doc")
    assert status == 200
    assert body["hash"] == digest
    assert body["doc"]["profiles"]["work"]["env"] == {"A": "1"}
    assert body["machine"] == config_doc.machine()


def test_get_reports_each_profile_credential_kind_without_the_value(home):
    write_config({
        "version": 2,
        "harnesses": {"plain": {"command": "plain", "auth": "none"}},
        "profiles": {
            "work": {},
            "cx": {"harness": "codex"},
            "nokey": {"harness": "plain"},
        },
    })
    for name in ("work", "cx", "nokey"):
        profile.create(name)
    credentials.save_token(profile.resolve("work"), SECRET)
    status, body = call("GET", "/api/config/doc")
    assert status == 200
    assert body["credentials"]["work"] == {"kind": "token", "has_token": True}
    assert body["credentials"]["cx"] == {"kind": "oauth", "has_token": False}
    assert body["credentials"]["nokey"] == {"kind": "none", "has_token": False}
    assert SECRET not in repr(body)


def test_get_without_a_config_file_hashes_the_empty_file(home):
    status, body = call("GET", "/api/config/doc")
    assert status == 200
    assert body["hash"] == hashlib.sha256(b"").hexdigest()
    assert body["doc"] == {"version": store.VERSION}


def test_get_carries_yaml_dates_as_text(home):
    store.path().parent.mkdir(parents=True, exist_ok=True)
    store.path().write_text("version: 2\nnote: 2026-11-06\n", encoding="utf-8")
    status, body = call("GET", "/api/config/doc")
    assert status == 200
    assert body["doc"]["note"] == "2026-11-06"


# --------------------------------------------------------------------------- #
# POST /api/config/doc
# --------------------------------------------------------------------------- #
def test_post_applies_set_and_delete_and_returns_the_new_hash(home):
    digest = write_config({"version": 2, "harnesses": {"py": {"command": "py"}}})
    ops = [
        {"op": "set", "path": ["profiles", "foo", "env"], "value": {"K": "v"}},
        {"op": "delete", "path": ["harnesses", "py"]},
    ]
    status, body = call("POST", "/api/config/doc", json={"base_hash": digest, "ops": ops})
    assert status == 200
    on_disk = store.path().read_bytes()
    assert body["hash"] == hashlib.sha256(on_disk).hexdigest() != digest
    # set made the missing intermediate mappings.
    assert store.load()["profiles"]["foo"]["env"] == {"K": "v"}
    assert "py" not in store.load()["harnesses"]
    assert body["doc"]["profiles"]["foo"]["env"] == {"K": "v"}


def test_post_addresses_sequence_items_by_index(home):
    digest = write_config({"version": 2, "shared": {"plugins": ["a@m", "b@m"]}})
    ops = [
        {"op": "set", "path": ["shared", "plugins", 1], "value": "c@m"},
        {"op": "set", "path": ["shared", "plugins", 2], "value": "d@m"},
        {"op": "delete", "path": ["shared", "plugins", 0]},
    ]
    status, _ = call("POST", "/api/config/doc", json={"base_hash": digest, "ops": ops})
    assert status == 200
    assert store.load()["shared"]["plugins"] == ["c@m", "d@m"]


def test_post_with_a_stale_hash_is_409_with_the_current_hash(home):
    write_config({"version": 2})
    before = store.path().read_bytes()
    status, body = call("POST", "/api/config/doc", json={
        "base_hash": "0" * 64,
        "ops": [{"op": "set", "path": ["profiles", "x"], "value": {}}],
    })
    assert status == 409
    assert body["hash"] == hashlib.sha256(before).hexdigest()
    assert body["error"]
    assert store.path().read_bytes() == before


def _write_right_after_the_check(monkeypatch, other):
    """Make the first hash read return the file, then write ``other`` over it.

    That is the window the handler has between the hash check and the write:
    another process (the CLI's ``store.save``) replaces the file there.
    """
    real = config_doc.file_bytes
    calls = []

    def file_bytes():
        data = real()
        if not calls:
            store.save(other)
        calls.append(data)
        return data

    monkeypatch.setattr(config_doc, "file_bytes", file_bytes)
    return calls


def test_post_when_the_file_changes_right_after_the_check_is_409(home, monkeypatch):
    digest = write_config({"version": 2, "profiles": {"a": {}}})
    calls = _write_right_after_the_check(
        monkeypatch, {"version": 2, "profiles": {"a": {}, "from_cli": {}}})
    status, body = call("POST", "/api/config/doc", json={
        "base_hash": digest,
        "ops": [{"op": "set", "path": ["profiles", "web"], "value": {}}],
    })
    assert status == 409
    assert len(calls) == 2
    after = store.path().read_bytes()
    assert body["hash"] == hashlib.sha256(after).hexdigest() != digest
    on_disk = store.parse_text(after.decode("utf-8"), store.path())
    # the other writer's change stays and the ops were not applied over it
    assert set(on_disk["profiles"]) == {"a", "from_cli"}


def test_post_does_not_read_the_document_again_before_the_write(home, monkeypatch):
    digest = write_config({"version": 2, "profiles": {"a": {}}})
    events = []
    real_read, real_save = config_doc.read, store.save
    monkeypatch.setattr(config_doc, "read", lambda: events.append("read") or real_read())
    monkeypatch.setattr(store, "save", lambda doc: events.append("save") or real_save(doc))
    status, body = call("POST", "/api/config/doc", json={
        "base_hash": digest,
        "ops": [{"op": "set", "path": ["profiles", "web"], "value": {}}],
    })
    assert status == 200, body
    # the only read is the answer's, after the write
    assert events == ["save", "read"]
    assert set(body["doc"]["profiles"]) == {"a", "web"}


def test_build_app_registers_the_config_doc_and_token_routes(home):
    from claude_launcher.daemon.manager import SessionManager

    async def routes():
        manager = SessionManager(idle_threshold=0.5, scrollback=100, restore_default=False)
        app = api.build_app(manager, TOKEN, started_at=0.0)
        found = {}
        for route in app.router.routes():
            info = route.resource.get_info() if route.resource is not None else {}
            path = info.get("path") or info.get("formatter")
            found.setdefault(path, set()).add(route.method)
        return found

    found = asyncio.run(routes())
    assert {"GET", "POST"} <= found.get("/api/config/doc", set())
    assert {"GET", "PUT"} <= found.get("/api/config/profiles/{profile}/token", set())


@pytest.mark.parametrize("ops", [
    # a harness declaration the strict parser refuses
    [{"op": "set", "path": ["harnesses", "bad"], "value": {"command": "x", "auth": "magic"}}],
    # a schema version this build cannot read
    [{"op": "set", "path": ["version"], "value": store.VERSION + 1}],
    # a profile entry that is not a mapping
    [{"op": "set", "path": ["profiles", "p"], "value": "text"}],
    # a path through a scalar
    [{"op": "set", "path": ["version", "x"], "value": 1}],
    # an index past the end of a sequence
    [{"op": "set", "path": ["shared", "plugins", 5], "value": "z@m"}],
    # an unknown op
    [{"op": "move", "path": ["profiles"]}],
])
def test_post_that_fails_the_checks_is_422_and_leaves_the_file(home, ops):
    digest = write_config({"version": 2, "shared": {"plugins": ["a@m"]}})
    before = store.path().read_bytes()
    status, body = call("POST", "/api/config/doc", json={"base_hash": digest, "ops": ops})
    assert status == 422, body
    assert body["error"]
    assert store.path().read_bytes() == before


def test_post_without_base_hash_is_400(home):
    write_config({"version": 2})
    status, _ = call("POST", "/api/config/doc", json={"ops": []})
    assert status == 400


def test_apply_ops_does_not_touch_its_input():
    doc = {"profiles": {"a": {"env": {"K": "1"}}}}
    out = config_doc.apply_ops(doc, [{"op": "set", "path": ["profiles", "a", "env", "K"], "value": "2"}])
    assert doc["profiles"]["a"]["env"]["K"] == "1"
    assert out["profiles"]["a"]["env"]["K"] == "2"


# --------------------------------------------------------------------------- #
# the profile token
# --------------------------------------------------------------------------- #
def test_token_put_then_get_round_trips_without_echo_or_log(home, caplog):
    caplog.set_level(logging.DEBUG)
    profile.create("work")
    status, body = call("PUT", "/api/config/profiles/work/token", json={"token": SECRET})
    assert status == 200
    assert body == {"profile": "work", "has_token": True}
    assert credentials.stored_token(profile.resolve("work")) == SECRET
    status, body = call("GET", "/api/config/profiles/work/token")
    assert status == 200
    assert body == {"token": SECRET}
    # The GET answer is the one place the value may appear; no log line has it.
    assert SECRET not in caplog.text


def test_token_get_of_a_token_profile_without_a_token_is_404(home):
    profile.create("work")
    status, _ = call("GET", "/api/config/profiles/work/token")
    assert status == 404


def test_token_get_of_an_unknown_profile_is_404(home):
    status, _ = call("GET", "/api/config/profiles/ghost/token")
    assert status == 404


def test_an_oauth_profile_is_400_for_both_get_and_put(home, caplog):
    caplog.set_level(logging.DEBUG)
    write_config({"version": 2, "profiles": {"cx": {"harness": "codex"}}})
    profile.create("cx")
    status, _ = call("GET", "/api/config/profiles/cx/token")
    assert status == 400
    status, body = call("PUT", "/api/config/profiles/cx/token", json={"token": SECRET})
    assert status == 400
    assert SECRET not in repr(body)
    assert credentials.stored_token(profile.resolve("cx")) is None
    assert SECRET not in caplog.text


def test_an_api_key_harness_profile_is_a_token_profile(home):
    write_config({"version": 2, "profiles": {"p": {"harness": "pi"}}})
    profile.create("p")
    status, _ = call("PUT", "/api/config/profiles/p/token", json={"token": SECRET})
    assert status == 200


# --------------------------------------------------------------------------- #
# authentication: the daemon's middleware, unchanged
# --------------------------------------------------------------------------- #
def test_no_credential_is_401(home):
    status, _ = call("GET", "/api/config/doc", headers={})
    assert status == 401


def test_a_same_origin_cookie_request_passes(home):
    cookie = "logged-in-session"
    status, _ = call(
        "GET", "/api/config/doc",
        headers={"Sec-Fetch-Site": "same-origin"},
        cookies={api.cookie_name(TOKEN): cookie},
        app_cookies=[cookie],
    )
    assert status == 200


def test_a_cross_site_cookie_request_is_refused(home):
    cookie = "logged-in-session"
    status, _ = call(
        "POST", "/api/config/doc", json={"base_hash": "x", "ops": []},
        headers={"Sec-Fetch-Site": "cross-site"},
        cookies={api.cookie_name(TOKEN): cookie},
        app_cookies=[cookie],
    )
    assert status == 403
