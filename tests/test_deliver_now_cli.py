"""``claunch deliver-now`` / ``claunch delivery-hold``: the terminal's half of
the dashboard's delivery controls.

Both already existed as daemon behaviour and as buttons on the web page, and
``MeshManager.flush_session`` had exactly one caller in the whole tree — the
HTTP route the page posts to. So an operator working from a shell, which is
how most of a fleet is driven, had no way to overrule a hold at all: the
message sat in the backlog and the only cure was to open a browser.

These pin the shell door onto the same endpoints, and pin what it says back —
"I asked" and "it went in" must not read alike, because the caller is usually
a person about to decide whether to go and look.
"""

from __future__ import annotations

import asyncio

import pytest

from claude_launcher import cli, daemon_client
from claude_launcher.daemon.api import build_app


class _FakeClient:
    """Records what the command posted, answers what the daemon would."""

    def __init__(self, replies):
        self.posts = []
        self._replies = replies

    def post(self, path, body=None):
        self.posts.append((path, body))
        return self._replies.get(path, {})


@pytest.fixture
def daemon(monkeypatch):
    def install(replies):
        client = _FakeClient(replies)
        monkeypatch.setattr(daemon_client, "ensure_running", lambda: client)
        return client

    return install


def _flush(session, **payload):
    return {f"/api/sessions/{session}/queued/flush": payload}


def test_deliver_now_posts_to_the_flush_route_and_says_what_went_in(
    daemon, capsys
):
    client = daemon(_flush(
        "s1", flushed=2, handles=["worker@team"],
        queued={"messages": [], "state": "settling"},
    ))

    assert cli.main(["deliver-now", "s1"]) == 0
    assert client.posts == [("/api/sessions/s1/queued/flush", None)]
    out = capsys.readouterr().out
    assert "delivered 2 message(s) into 's1'" in out
    assert "worker@team" in out


def test_deliver_now_reports_a_backlog_that_did_not_move_and_fails(
    daemon, capsys
):
    """``flushed: 0`` with messages still queued is the one outcome a caller
    must not read as success — it is the whole reason they typed the command.
    Named reason, non-zero exit, so a script can branch on it."""
    daemon(_flush(
        "s1", flushed=0, handles=[],
        queued={"messages": [{"id": "m1"}], "state": "exited"},
    ))

    assert cli.main(["deliver-now", "s1"]) == 1
    out = capsys.readouterr().out
    assert "nothing was delivered into 's1'" in out
    assert "1 message(s) still queued" in out
    assert "the session has exited" in out


def test_deliver_now_on_an_empty_backlog_is_not_a_failure(daemon, capsys):
    """Nothing queued is not nothing working. Exiting non-zero here would
    make 'deliver-now' unusable in the loop it belongs in — run it, then
    carry on — by turning "there was no mail" into an error."""
    daemon(_flush("s1", flushed=0, handles=[], queued={"messages": []}))

    assert cli.main(["deliver-now", "s1"]) == 0
    assert "nothing was queued for 's1'" in capsys.readouterr().out


def test_deliver_now_defaults_to_the_session_it_is_running_inside(
    daemon, monkeypatch, capsys
):
    client = daemon(_flush(
        "s7", flushed=1, handles=["w@m"], queued={"messages": []},
    ))
    monkeypatch.setenv("CLAUNCH_SESSION", "s7")

    assert cli.main(["deliver-now"]) == 0
    assert client.posts[0][0] == "/api/sessions/s7/queued/flush"


def test_deliver_now_without_a_session_says_so_instead_of_guessing(
    daemon, monkeypatch, capsys
):
    daemon({})
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)

    assert cli.main(["deliver-now"]) == 2
    assert "no session" in capsys.readouterr().err


def test_delivery_hold_toggles_when_neither_flag_is_given(daemon, capsys):
    """The header chip's click, in a shell: no read-then-write race, the
    daemon flips whatever it currently is. Omitting the field is what asks
    for that, so the body must not carry one."""
    client = daemon({
        "/api/sessions/s1/queued/hold": {
            "hold": True, "queued": {"messages": [{"id": "m1"}]},
        }
    })

    assert cli.main(["delivery-hold", "s1"]) == 0
    assert client.posts == [("/api/sessions/s1/queued/hold", {})]
    out = capsys.readouterr().out
    assert "delivery held" in out
    assert "1 message(s) waiting" in out
    # The way out is printed with the hold: a hold you cannot find the
    # release for is a trap rather than a setting.
    assert "deliver-now s1" in out


@pytest.mark.parametrize("flag, want", [("--on", True), ("--off", False)])
def test_delivery_hold_states_it_outright_when_asked_to(daemon, flag, want):
    client = daemon({
        "/api/sessions/s1/queued/hold": {"hold": want, "queued": {"messages": []}}
    })

    assert cli.main(["delivery-hold", "s1", flag]) == 0
    assert client.posts == [("/api/sessions/s1/queued/hold", {"hold": want})]


def test_every_hold_the_daemon_can_name_has_a_sentence_here():
    """The reason table mirrors a ladder that lives in the daemon, and a hold
    added there without a line here degrades to "still held" — the one thing
    the command exists to avoid saying. Read the ladder out of the source so
    a new rung fails this instead of shipping mute (``paced`` arrived exactly
    that way, with the mesh backpressure work)."""
    import re
    from pathlib import Path

    from claude_launcher import cli_sessions

    api = Path(cli_sessions.__file__).with_name("daemon") / "api.py"
    ladder = set(re.findall(r'state = "(\w+)"', api.read_text(encoding="utf-8")))
    assert ladder, "could not read the state ladder out of daemon/api.py"
    missing = ladder - set(cli_sessions._QUEUE_HOLD_REASON)
    assert not missing, f"no sentence for {sorted(missing)}"


def test_the_routes_the_cli_posts_to_are_the_routes_the_daemon_serves():
    """The two halves are only one control if they meet at the same door.
    A rename on either side that this does not catch turns 'deliver now' in a
    terminal into a 404 nobody notices until they need it."""
    class _Manager:
        """Only what build_app touches while wiring the router."""
        exit_hooks: list = []

    async def routes():
        # build_app constructs the shell pty, which wants a running loop.
        app = build_app(_Manager(), "t", started_at=0.0)
        return {
            r.resource.canonical
            for r in app.router.routes()
            if r.resource is not None
        }

    served = asyncio.run(routes())
    assert "/api/sessions/{name}/queued/flush" in served
    assert "/api/sessions/{name}/queued/hold" in served
