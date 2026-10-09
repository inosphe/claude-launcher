"""``claunch retro``: the retrospective prompt pack, sent to a running session.

The pack is on-demand (``prompt_packs.ON_DEMAND``): never part of an opening.
Two callers share one route. A person types it bare into a session whose run
did not end normally; improv-worker-v2's last node has the agent run it with
``--print`` and read the text in its own turn (claunch-ygbsd).
"""

from __future__ import annotations

import pytest

from claude_launcher import cli, daemon_client


class _FakeClient:
    def __init__(self, replies):
        self.calls = []
        self._replies = replies

    def get(self, path):
        self.calls.append(("GET", path))
        return self._replies.get(path, {})

    def post(self, path, body=None):
        self.calls.append(("POST", path))
        return self._replies.get(path, {})


@pytest.fixture
def daemon(monkeypatch):
    def install(replies):
        client = _FakeClient(replies)
        monkeypatch.setattr(daemon_client, "ensure_running", lambda: client)
        return client

    return install


ROUTE = "/api/sessions/s1/retrospective"


def test_retro_types_the_pack_into_the_named_session(daemon, capsys):
    client = daemon({ROUTE: {"ok": True, "pack": "retrospective", "delivered": True}})
    assert cli.main(["retro", "s1"]) == 0
    assert client.calls == [("POST", ROUTE)]
    assert "sent the retrospective pack to 's1'" in capsys.readouterr().out


def test_retro_says_so_when_the_session_did_not_take_it(daemon, capsys):
    daemon({ROUTE: {"ok": True, "pack": "retrospective", "delivered": False}})
    assert cli.main(["retro", "s1"]) == 1
    assert "did not take" in capsys.readouterr().err


def test_retro_print_reads_the_text_for_the_calling_session(daemon, capsys, monkeypatch):
    monkeypatch.setenv("CLAUNCH_SESSION", "s1")
    client = daemon({ROUTE: {"block": "# claunch prompt pack: retrospective"}})
    assert cli.main(["retro", "--print"]) == 0
    assert client.calls == [("GET", ROUTE)]
    assert capsys.readouterr().out.startswith("# claunch prompt pack: retrospective")


def test_retro_names_another_on_demand_pack(daemon):
    client = daemon({})
    cli.main(["retro", "s1", "--print", "--pack", "house-retro"])
    assert client.calls == [("GET", ROUTE + "?pack=house-retro")]


def test_retro_needs_a_session(daemon, monkeypatch):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    daemon({})
    assert cli.main(["retro"]) == 2
