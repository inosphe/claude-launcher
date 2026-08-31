from __future__ import annotations

import argparse

from claude_launcher import cli_window


class _Client:
    def __init__(self, response):
        self.response = response
        self.posts = []

    def post(self, path, body):
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


def test_window_cancel_reports_an_empty_match(monkeypatch, capsys):
    client = _Client({"cancelled": 0})
    monkeypatch.setenv("CLAUNCH_SESSION", "s1")
    monkeypatch.setattr(cli_window, "_client", lambda: client)

    assert cli_window._cmd_cancel(argparse.Namespace(grant_id=None, session=None)) == 1
    assert "nothing to cancel" in capsys.readouterr().err
