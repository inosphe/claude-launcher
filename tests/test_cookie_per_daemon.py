"""Two daemons on one host keep their logins apart (claunch-id6sp).

Browsers scope cookies by host, not by port. Under one fixed cookie name a
login to ``host:8379`` replaced the cookie ``host:8378`` had set, so the
first daemon's next poll got a 401 and the page showed the token prompt --
every minute, while both dashboards were open. The cookie's name now carries
a digest of the daemon's token, so each daemon reads only its own.
"""
from __future__ import annotations

import asyncio
import time

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher.daemon.api import COOKIE_PREFIX, build_app, cookie_name
from claude_launcher.daemon.manager import SessionManager


def _app(token: str) -> web.Application:
    mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
    return build_app(mgr, token, started_at=time.monotonic())


def test_cookie_name_differs_per_token_and_survives_a_restart():
    assert cookie_name("token-a") != cookie_name("token-b")
    assert cookie_name("token-a") == cookie_name("token-a")
    assert cookie_name("token-a").startswith(COOKIE_PREFIX + "_")
    # The name never carries the token itself.
    assert "token-a" not in cookie_name("token-a")


def test_one_browser_logged_into_two_daemons_stays_logged_into_both(home):
    async def run():
        a = TestClient(TestServer(_app("token-a")))
        b = TestClient(TestServer(_app("token-b")))
        await a.start_server()
        await b.start_server()
        try:
            resp_a = await a.post("/api/auth/session", json={"token": "token-a"})
            resp_b = await b.post("/api/auth/session", json={"token": "token-b"})
            assert resp_a.status == 200 and resp_b.status == 200
            jar_a = {m.key: m.value for m in resp_a.cookies.values()}
            jar_b = {m.key: m.value for m in resp_b.cookies.values()}
            assert set(jar_a) == {cookie_name("token-a")}
            assert set(jar_b) == {cookie_name("token-b")}

            # What a browser sends to either port of the shared host: both.
            shared = {**jar_a, **jar_b}
            assert len(shared) == 2
            a.session.cookie_jar.clear()
            b.session.cookie_jar.clear()
            assert (await a.get("/api/sessions", cookies=shared)).status == 200
            assert (await b.get("/api/sessions", cookies=shared)).status == 200

            # Neither daemon accepts the other's cookie as its own.
            swapped = {cookie_name("token-a"): jar_b[cookie_name("token-b")]}
            assert (await a.get("/api/sessions", cookies=swapped)).status == 401
        finally:
            await a.close()
            await b.close()

    asyncio.run(run())
