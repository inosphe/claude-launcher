from __future__ import annotations

import argparse

from claude_launcher import cli_window


class _Client:
    def __init__(self, response):
        self.response = response
        self.posts = []

    def post(self, path, body, **kwargs):
        self.posts.append((path, body))
        return self.response


def test_window_cancel_uses_the_current_session(monkeypatch, capsys):
    client = _Client({"cancelled": 2})
    monkeypatch.setenv("CLAUNCH_SESSION", "s1")
    monkeypatch.setattr(cli_window, "_client", lambda: client)

    assert cli_window._cmd_cancel(argparse.Namespace(grant_id=None, session=None)) == 0
    assert client.posts == [("/api/window/cancel", {"session": "s1"})]
    assert capsys.readouterr().out.strip() == "cancelled: 2"


def test_window_cancel_can_address_one_waiting_request(monkeypatch, capsys):
    client = _Client({"cancelled": 1})
    monkeypatch.setattr(cli_window, "_client", lambda: client)

    assert cli_window._cmd_cancel(argparse.Namespace(grant_id="q1", session=None)) == 0
    assert client.posts == [("/api/window/cancel", {"grant_id": "q1"})]
    assert capsys.readouterr().out.strip() == "cancelled: 1"


def _no_daemon():
    raise AssertionError("an operator command inside a session reached the daemon")


def test_operator_overrides_are_refused_inside_a_managed_session(monkeypatch, capsys):
    """claunch-8kald: an agent that could reorder or force the queue could
    always put itself first."""
    monkeypatch.setenv("CLAUNCH_SESSION", "agent")
    monkeypatch.setattr(cli_window, "_client", _no_daemon)

    assert cli_window._cmd_prioritize(argparse.Namespace(grant_id="q1", priority=None)) == 2
    assert cli_window._cmd_force(argparse.Namespace(grant_id="q1")) == 2
    acquire = argparse.Namespace(
        cls="sweep", session=None, label="", wait=0.0, workers=None, force=True
    )
    assert cli_window._cmd_acquire(acquire) == 2
    err = capsys.readouterr().err
    assert err.count("is an operator command") == 3
    assert "managed session 'agent'" in err


def test_prioritize_and_force_from_the_operator_shell(monkeypatch, capsys):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    client = _Client({"priority": 3, "granted": False, "position": 1})
    monkeypatch.setattr(cli_window, "_client", lambda: client)
    assert cli_window._cmd_prioritize(argparse.Namespace(grant_id="q1", priority=3)) == 0
    assert client.posts == [("/api/window/prioritize", {"grant_id": "q1", "priority": 3})]
    assert "priority 3: q1 (position 1)" in capsys.readouterr().out

    client = _Client({"forced": True, "holder": {
        "grant_id": "q1", "cls": "sweep", "session": "s2", "workers": 8, "forced": True,
    }})
    monkeypatch.setattr(cli_window, "_client", lambda: client)
    assert cli_window._cmd_force(argparse.Namespace(grant_id="q1")) == 0
    assert client.posts == [("/api/window/force", {"grant_id": "q1"})]
    out = capsys.readouterr().out
    assert "forced: q1 sweep:s2" in out and "-n 8, forced" in out


def test_prioritize_top_sends_no_priority(monkeypatch):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    client = _Client({"priority": 1, "granted": True, "position": None})
    monkeypatch.setattr(cli_window, "_client", lambda: client)
    assert cli_window._cmd_prioritize(argparse.Namespace(grant_id="q1", priority=None)) == 0
    assert client.posts == [("/api/window/prioritize", {"grant_id": "q1"})]


def test_the_operator_parser_knows_the_new_subcommands():
    import argparse as ap

    parser = ap.ArgumentParser()
    cli_window.register(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["window", "prioritize", "q1", "--priority", "-2"])
    assert (args.grant_id, args.priority) == ("q1", -2)
    args = parser.parse_args(["window", "force", "q1"])
    assert args.func is cli_window._cmd_force
    args = parser.parse_args(["window", "acquire", "--class", "targeted", "--workers", "2"])
    assert (args.workers, args.force) == (2, False)


def test_window_history_prints_the_retention_statistics_and_rows(monkeypatch, capsys):
    class _Reader(_Client):
        def get(self, path):
            self.posts.append(("get", path))
            return {
                "retention_days": 7, "days": 7, "total": 1,
                "stats": {"classes": {"targeted": {
                    "runs": 1, "gave_up_waiting": 0, "forced": 0,
                    "outcomes": {"passed": 1},
                    "wait_seconds": {"p50": 5, "p90": 5, "max": 5},
                    "held_seconds": {"p50": 90, "p90": 90, "max": 90},
                }}},
                "entries": [{
                    "ended_at": "2026-09-28T08:00:00+00:00", "cls": "targeted",
                    "session": "s1", "end": "released", "wait_seconds": 5,
                    "held_seconds": 90, "workers": 2, "label": "pytest x",
                    "result": {"outcome": "passed", "passed": 12},
                }],
            }

    client = _Reader(None)
    monkeypatch.setattr(cli_window, "_client", lambda: client)
    args = argparse.Namespace(days=None, cls="targeted", session=None, limit=20, json=False)
    assert cli_window._cmd_history(args) == 0
    assert client.posts == [("get", "/api/window/history?limit=20&class=targeted")]
    out = capsys.readouterr().out
    assert "keeps 7 days, then drops them" in out
    assert "targeted: 1 run(s) [1 passed]; wait p50 5s" in out
    assert "held p50 1.5m" in out
    assert "released wait 5s held 1.5m -n 2 passed (12 passed) -- pytest x" in out


def test_window_cancel_reports_an_empty_match(monkeypatch, capsys):
    client = _Client({"cancelled": 0})
    monkeypatch.setenv("CLAUNCH_SESSION", "s1")
    monkeypatch.setattr(cli_window, "_client", lambda: client)

    assert cli_window._cmd_cancel(argparse.Namespace(grant_id=None, session=None)) == 1
    assert "nothing to cancel" in capsys.readouterr().err
