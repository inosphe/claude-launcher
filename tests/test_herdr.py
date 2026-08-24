"""The Herdr CLI surface claunch uses: agent reports on attach panes.

A pane running ``claunch attach`` mirrors a session rather than running the
agent, so Herdr's detection never sees claude there — the attach reports it
through Herdr's official external-source hook instead. These tests pin the
CLI arguments claunch composes (and that nothing is sent outside Herdr).
"""

from __future__ import annotations

from claude_launcher import herdr


def test_report_agent_composes_the_cli(monkeypatch):
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HERDR_PANE_ID", "w4:p7")
    calls = []

    def fake_run(args):
        calls.append(args)
        return True

    monkeypatch.setattr(herdr, "_run", fake_run)
    assert herdr.report_agent("claude", state="working", message="s38")
    assert calls == [
        ["pane", "report-agent", "w4:p7", "--source", "claunch",
         "--agent", "claude", "--state", "working", "--message", "s38"]
    ]


def test_report_agent_omits_message_when_empty(monkeypatch):
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HERDR_PANE_ID", "w4:p7")
    calls = []

    def fake_run(args):
        calls.append(args)
        return True

    monkeypatch.setattr(herdr, "_run", fake_run)
    assert herdr.report_agent("claude")
    assert calls == [
        ["pane", "report-agent", "w4:p7", "--source", "claunch",
         "--agent", "claude", "--state", "unknown"]
    ]


def test_release_agent_composes_the_cli(monkeypatch):
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HERDR_PANE_ID", "w4:p7")
    calls = []

    def fake_run(args):
        calls.append(args)
        return True

    monkeypatch.setattr(herdr, "_run", fake_run)
    assert herdr.release_agent("claude")
    assert calls == [
        ["pane", "release-agent", "w4:p7", "--source", "claunch",
         "--agent", "claude"]
    ]


def test_no_herdr_no_report(monkeypatch):
    monkeypatch.delenv("HERDR_ENV", raising=False)
    called = {"n": 0}

    def fake_run(args):
        called["n"] += 1
        return True

    monkeypatch.setattr(herdr, "_run", fake_run)
    assert herdr.report_agent("claude") is False
    assert herdr.release_agent("claude") is False
    assert called["n"] == 0
