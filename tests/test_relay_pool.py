"""Tests for connecting one daemon to several relays at once.

Three layers are covered: the config file shapes ``store`` reads and writes,
the builder that turns those rows into uplinks, and ``RelayPool``'s routing of
peer traffic across the uplinks it holds.

Async tests follow this repo's convention of an inner ``run()`` driven by
``asyncio.run`` (no pytest-asyncio dependency). The pool talks to its uplinks
only through ``connected``/``peering``/``listing``/``peer_http``/``peer_list``,
so a small stub stands in for a real uplink.
"""

from __future__ import annotations

import asyncio

import pytest

from claude_launcher import store
from claude_launcher.daemon import relay_uplink
from claude_launcher.daemon.relay_uplink import PeerError, RelayPool, RelayUplink


# --------------------------------------------------------------------------- #
# config file shapes
# --------------------------------------------------------------------------- #
def test_single_relay_keeps_the_legacy_block(home):
    """A config that never had a second relay is not rewritten into a list."""
    store.set_relay_field("url", "wss://a/agent")
    store.set_relay_field("token", "t1")

    assert store.load()["daemon"] == {"relay": {"url": "wss://a/agent", "token": "t1"}}
    rows = store.relays_config()
    assert [r["url"] for r in rows] == ["wss://a/agent"]
    # The entry gets a positional handle so the CLI can address it.
    assert rows[0]["id"] == "relay1"
    # relay_config() still answers what it always did.
    assert store.relay_config()["url"] == "wss://a/agent"


def test_naming_a_handle_migrates_to_the_list(home):
    store.set_relay_field("url", "wss://a/agent")
    store.set_relay_field("token", "t1")

    store.set_relay_field("url", "wss://b/agent", relay="home")
    store.set_relay_field("token", "t2", relay="home")

    daemon = store.load()["daemon"]
    assert "relay" not in daemon  # the legacy block moved, it was not duplicated
    assert daemon["relays"] == [
        {"url": "wss://a/agent", "token": "t1"},
        {"id": "home", "url": "wss://b/agent", "token": "t2"},
    ]
    assert [r["id"] for r in store.relays_config()] == ["relay1", "home"]
    # The un-handled call keeps addressing the first entry after the migration.
    store.set_relay_field("name", "first-pc")
    assert store.relays_config()[0]["name"] == "first-pc"


def test_relays_list_wins_over_a_stale_single_block(home):
    """Both shapes present: the list is the answer, the block is ignored."""
    store.update(
        lambda doc: doc.setdefault("daemon", {}).update(
            {
                "relay": {"url": "wss://old/agent", "token": "old"},
                "relays": [{"id": "new", "url": "wss://new/agent", "token": "t"}],
            }
        )
    )
    rows = store.relays_config()
    assert [r["id"] for r in rows] == ["new"]
    assert store.relay_config()["url"] == "wss://new/agent"


def test_add_and_remove_relay(home):
    store.add_relay("work", url="wss://w/agent", token="t1")
    store.add_relay("home", url="wss://h/agent", token="t2")
    assert [r["id"] for r in store.relays_config()] == ["work", "home"]

    with pytest.raises(ValueError):
        store.add_relay("work", url="wss://dup/agent")

    assert store.remove_relay("work") is True
    assert [r["id"] for r in store.relays_config()] == ["home"]
    # Removing the last one drops the block entirely, like clearing used to.
    assert store.remove_relay("home") is True
    assert store.relays_config() == []
    assert "relays" not in store.load().get("daemon", {})


def test_remove_unknown_relay_leaves_the_file_alone(home):
    store.set_relay_field("url", "wss://a/agent")
    before = store.load()["daemon"]

    assert store.remove_relay("nope") is False
    assert store.load()["daemon"] == before  # still the legacy block


def test_clearing_a_handled_relays_last_key_drops_the_entry(home):
    store.add_relay("work", url="wss://w/agent")
    store.add_relay("home", url="wss://h/agent")

    store.set_relay_field("url", None, relay="work")
    assert [r["id"] for r in store.relays_config()] == ["home"]


# --------------------------------------------------------------------------- #
# building uplinks from those rows
# --------------------------------------------------------------------------- #
def test_pool_from_config_builds_one_uplink_per_row(monkeypatch):
    monkeypatch.delenv("CLAUNCH_RELAY_TOKEN", raising=False)
    pool = relay_uplink.pool_from_config(
        [
            {"id": "work", "url": "wss://w/agent", "token": "t1", "name": "pc"},
            {"id": "home", "url": "wss://h/agent", "token": "t2", "name": "pc"},
        ],
        local_host="127.0.0.1",
        local_port=9,
    )
    assert pool is not None
    assert [up.id for up in pool.uplinks] == ["work", "home"]
    assert [up.url for up in pool.uplinks] == ["wss://w/agent", "wss://h/agent"]
    # One machine identity, whichever relay carries it.
    assert pool.name == "pc"


def test_pool_skips_rows_without_a_token(monkeypatch):
    monkeypatch.delenv("CLAUNCH_RELAY_TOKEN", raising=False)
    pool = relay_uplink.pool_from_config(
        [
            {"id": "work", "url": "wss://w/agent", "token": "t1"},
            {"id": "home", "url": "wss://h/agent"},  # no token → disabled
        ],
        local_host="127.0.0.1",
        local_port=9,
    )
    assert [up.id for up in pool.uplinks] == ["work"]


def test_pool_is_none_when_nothing_is_configured(monkeypatch):
    monkeypatch.delenv("CLAUNCH_RELAY_URL", raising=False)
    assert relay_uplink.pool_from_config(
        [], local_host="127.0.0.1", local_port=9
    ) is None


def test_the_environment_alone_configures_a_relay(monkeypatch):
    """No relay in the config file, but the env names one — it must still run.

    This is how a named daemon instance gets its own relay identity, and
    ``tests/test_multi_daemon_mesh.py`` spawns two daemons exactly this way.
    """
    monkeypatch.setenv("CLAUNCH_RELAY_URL", "ws://127.0.0.1:1/")
    monkeypatch.setenv("CLAUNCH_RELAY_TOKEN", "envtok")
    monkeypatch.setenv("CLAUNCH_RELAY_NAME", "pca")

    pool = relay_uplink.pool_from_config(
        [], local_host="127.0.0.1", local_port=9
    )
    assert pool is not None
    assert [(up.url, up.token, up.name) for up in pool.uplinks] == [
        ("ws://127.0.0.1:1/", "envtok", "pca")
    ]


def test_default_name_covers_a_row_that_names_none(monkeypatch):
    monkeypatch.delenv("CLAUNCH_RELAY_NAME", raising=False)
    monkeypatch.delenv("CLAUNCH_RELAY_TOKEN", raising=False)
    pool = relay_uplink.pool_from_config(
        [{"id": "work", "url": "wss://w/agent", "token": "t1"}],
        local_host="127.0.0.1",
        local_port=9,
        default_name="host-b",
    )
    assert pool.name == "host-b"

    # A row with its own name keeps it.
    pool = relay_uplink.pool_from_config(
        [{"id": "work", "url": "wss://w/agent", "token": "t1", "name": "mine"}],
        local_host="127.0.0.1",
        local_port=9,
        default_name="host-b",
    )
    assert pool.name == "mine"


def test_bare_env_token_is_ignored_with_several_relays(monkeypatch):
    """One ``CLAUNCH_RELAY_TOKEN`` cannot mean two different relays."""
    monkeypatch.setenv("CLAUNCH_RELAY_TOKEN", "shared")
    pool = relay_uplink.pool_from_config(
        [
            {"id": "work", "url": "wss://w/agent", "token": "t1"},
            {"id": "home", "url": "wss://h/agent"},
        ],
        local_host="127.0.0.1",
        local_port=9,
    )
    # 'work' keeps its own file token; 'home' is disabled rather than silently
    # registering with the other relay's secret.
    assert [(up.id, up.token) for up in pool.uplinks] == [("work", "t1")]


def test_scoped_env_token_targets_one_relay(monkeypatch):
    monkeypatch.delenv("CLAUNCH_RELAY_TOKEN", raising=False)
    monkeypatch.setenv("CLAUNCH_RELAY_TOKEN_HOME", "from-env")
    pool = relay_uplink.pool_from_config(
        [
            {"id": "work", "url": "wss://w/agent", "token": "t1"},
            {"id": "home", "url": "wss://h/agent"},
        ],
        local_host="127.0.0.1",
        local_port=9,
    )
    assert [(up.id, up.token) for up in pool.uplinks] == [
        ("work", "t1"),
        ("home", "from-env"),
    ]


def test_bare_env_still_applies_to_a_single_relay(monkeypatch):
    monkeypatch.setenv("CLAUNCH_RELAY_TOKEN", "envtok")
    pool = relay_uplink.pool_from_config(
        [{"url": "wss://only/agent"}], local_host="127.0.0.1", local_port=9
    )
    assert [up.token for up in pool.uplinks] == ["envtok"]


# --------------------------------------------------------------------------- #
# pool routing
# --------------------------------------------------------------------------- #
class _StubUplink:
    """An uplink that answers from a dict instead of a WebSocket."""

    def __init__(self, ident, peers, *, connected=True, peering=True,
                 listing=True, list_error=None):
        self.id = ident
        self.name = "pc"
        self.url = f"wss://{ident}/agent"
        self.connected = connected
        self.peering = peering
        self.listing = listing
        self.peers = peers
        self.list_error = list_error
        self.calls = []

    async def peer_list(self, *, timeout=10.0):
        if self.list_error:
            raise PeerError(self.list_error)
        return list(self.peers)

    async def peer_http(self, peer, request, *, timeout=30.0):
        self.calls.append(peer)
        if peer not in self.peers:
            raise PeerError("peer backend is not registered with the relay")
        return b"HTTP/1.1 200 OK\r\n\r\n" + self.id.encode()


def test_peer_list_is_the_union_across_relays():
    async def run():
        pool = RelayPool([
            _StubUplink("work", ["alice", "shared"]),
            _StubUplink("home", ["bob", "shared"]),
        ])
        names = await pool.peer_list()
        assert sorted(names) == ["alice", "bob", "shared"]
        # A name on both relays appears once.
        assert names.count("shared") == 1

    asyncio.run(run())


def test_peer_list_survives_one_relay_failing():
    async def run():
        pool = RelayPool([
            _StubUplink("work", ["alice"], list_error="boom"),
            _StubUplink("home", ["bob"]),
        ])
        assert await pool.peer_list() == ["bob"]

    asyncio.run(run())


def test_peer_list_raises_only_when_no_relay_answers():
    async def run():
        pool = RelayPool([
            _StubUplink("work", [], list_error="work is down"),
            _StubUplink("home", [], list_error="home is down"),
        ])
        with pytest.raises(PeerError) as err:
            await pool.peer_list()
        # Both reasons are reported, not just the last one.
        assert "work is down" in str(err.value)
        assert "home is down" in str(err.value)

    asyncio.run(run())


def test_peer_http_reaches_a_backend_on_the_second_relay():
    async def run():
        work = _StubUplink("work", ["alice"])
        home = _StubUplink("home", ["bob"])
        pool = RelayPool([work, home])

        resp = await pool.peer_http("bob", b"GET / HTTP/1.1\r\n\r\n")
        assert resp.endswith(b"home")
        # The listing told the pool where 'bob' lives, so 'work' was never
        # asked for it.
        assert work.calls == []

    asyncio.run(run())


def test_peer_http_caches_the_route():
    async def run():
        work = _StubUplink("work", ["alice"])
        home = _StubUplink("home", ["bob"])
        pool = RelayPool([work, home])

        await pool.peer_http("bob", b"x")
        await pool.peer_http("bob", b"x")
        assert home.calls == ["bob", "bob"]
        assert work.calls == []

    asyncio.run(run())


def test_peer_http_refinds_a_backend_that_moved_relays():
    async def run():
        work = _StubUplink("work", ["bob"])
        home = _StubUplink("home", [])
        pool = RelayPool([work, home])
        await pool.peer_http("bob", b"x")
        assert work.calls == ["bob"]

        # 'bob' re-registers on the other relay.
        work.peers = []
        home.peers = ["bob"]
        resp = await pool.peer_http("bob", b"x")
        assert resp.endswith(b"home")

    asyncio.run(run())


def test_peer_http_falls_back_to_trying_each_relay_without_listing():
    async def run():
        work = _StubUplink("work", ["alice"], listing=False)
        home = _StubUplink("home", ["bob"], listing=False)
        pool = RelayPool([work, home])

        resp = await pool.peer_http("bob", b"x")
        assert resp.endswith(b"home")
        # No relay could list, so 'work' was probed first and refused.
        assert work.calls == ["bob"]

    asyncio.run(run())


def test_peer_http_reports_every_relays_reason():
    async def run():
        pool = RelayPool([
            _StubUplink("work", [], listing=False),
            _StubUplink("home", [], listing=False),
        ])
        with pytest.raises(PeerError) as err:
            await pool.peer_http("ghost", b"x")
        text = str(err.value)
        assert "ghost" in text and "work:" in text and "home:" in text

    asyncio.run(run())


def test_peer_calls_skip_disconnected_relays():
    async def run():
        down = _StubUplink("work", ["bob"], connected=False)
        up = _StubUplink("home", ["bob"])
        pool = RelayPool([down, up])

        await pool.peer_http("bob", b"x")
        assert down.calls == []
        assert up.calls == ["bob"]

    asyncio.run(run())


def test_peer_http_refuses_when_nothing_is_connected():
    async def run():
        pool = RelayPool([_StubUplink("work", ["bob"], connected=False)])
        with pytest.raises(PeerError) as err:
            await pool.peer_http("bob", b"x")
        assert str(err.value) == "no relay uplink is connected"

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# status surface
# --------------------------------------------------------------------------- #
def test_pool_connected_means_at_least_one_relay():
    pool = RelayPool([
        _StubUplink("work", [], connected=False),
        _StubUplink("home", [], connected=True),
    ])
    assert pool.connected is True

    pool.uplinks[1].connected = False
    assert pool.connected is False


def test_pool_state_keeps_the_single_uplink_shape():
    pool = RelayPool([
        _StubUplink("work", [], connected=True),
        _StubUplink("home", [], connected=False),
    ])
    state = pool.state()
    # Readers that predate multiple relays read these four and still work.
    assert state["configured"] is True
    assert state["connected"] is True
    assert state["name"] == "pc"
    assert state["url"] == "wss://work/agent"
    # And the per-relay detail is there for readers that want it.
    assert state["count"] == 2 and state["connected_count"] == 1
    assert [r["id"] for r in state["relays"]] == ["work", "home"]
    assert [r["connected"] for r in state["relays"]] == [True, False]


def test_pool_state_rows_carry_each_relays_round_trip():
    """The web badge's daemon<->relay half (claunch-8ufey). Every row has the
    three keys; a disconnected relay, or an uplink that cannot measure,
    reports none rather than a stale number."""
    live = _StubUplink("work", [], connected=True)
    live.latency = lambda: {"rtt_ms": 41.5, "rtt_age": 3.0, "pending_ms": None}
    gone = _StubUplink("home", [], connected=False)
    gone.latency = lambda: {"rtt_ms": 9.0, "rtt_age": 99.0, "pending_ms": None}
    bare = _StubUplink("lab", [], connected=True)
    rows = RelayPool([live, gone, bare]).state()["relays"]
    assert rows[0]["rtt_ms"] == 41.5 and rows[0]["rtt_age"] == 3.0
    for row in rows[1:]:
        assert (row["rtt_ms"], row["rtt_age"], row["pending_ms"]) == (None, None, None)


def test_unconfigured_state_has_the_same_keys_as_a_pools():
    """A reader takes the same keys whether or not a relay is configured.

    ``daemon/api.py`` serves this shape when no uplink is running, so the two
    must not drift — a badge that reads ``relays`` would otherwise crash on a
    daemon with no relay.
    """
    from claude_launcher.daemon.relay_uplink import unconfigured_state

    off = unconfigured_state()
    on = RelayPool([_StubUplink("work", [])]).state()
    assert off.keys() == on.keys()
    assert off["configured"] is False and off["relays"] == []


def test_the_api_serves_that_same_unconfigured_shape():
    from claude_launcher.daemon.api import _relay_unconfigured
    from claude_launcher.daemon.relay_uplink import unconfigured_state

    assert _relay_unconfigured() == unconfigured_state()


def test_real_uplink_satisfies_the_pool_surface():
    """The pool's stub stands in for this; keep the two shapes in step."""
    up = RelayUplink(url="ws://x", token="t", name="pc",
                     local_host="127.0.0.1", local_port=1, id="work")
    for attr in ("id", "name", "url", "connected", "peering", "listing",
                 "peer_http", "peer_list"):
        assert hasattr(up, attr)
    # An uplink with no explicit handle falls back to its backend name.
    assert RelayUplink(url="ws://x", token="t", name="pc",
                       local_host="127.0.0.1", local_port=1).id == "pc"


def test_relay_line_shows_the_per_relay_state():
    from claude_launcher import cli_mesh

    pool = RelayPool([
        _StubUplink("work", [], connected=True),
        _StubUplink("home", [], connected=False),
    ])
    line = cli_mesh.relay_line(pool.state())
    assert "connected as 'pc'" in line
    assert "1/2 relays" in line
    assert "work=up" in line and "home=down" in line

    # A single relay keeps the original wording, with no count.
    one = RelayPool([_StubUplink("work", [], connected=True)])
    assert cli_mesh.relay_line(one.state()) == "relay: connected as 'pc'"
