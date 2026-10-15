"""Diagnostics for a failed restart request (claunch-9gm02, RCA claunch-ny5i3)."""

from __future__ import annotations

import sys

from claude_launcher import cli_sessions, daemon_client


def test_spawn_record_names_command_parent_and_reason(monkeypatch):
    monkeypatch.setenv("CLAUNCH_SESSION", "s-test")
    monkeypatch.setattr(sys, "argv", ["claunch", "mesh", "send"])
    line = daemon_client.spawn_record("ensure_running: unresponsive -- busy")
    assert "claunch.spawn" in line and "spawn_daemon:" in line
    for field in ("pid=", "ppid=", "session=s-test", "cmd=", "parent=", "reason="):
        assert field in line
    assert "'claunch mesh send'" in line
    assert line.endswith("busy\n")


def test_spawn_daemon_writes_the_record_before_starting(home, monkeypatch):
    started = []

    class _P:
        pass

    real = daemon_client.subprocess.Popen

    def fake_popen(argv, *a, **k):
        if argv[0] == "git":  # daemon_cwd asking where the checkout is
            return real(argv, *a, **k)
        started.append(argv)
        return _P()

    monkeypatch.setattr(daemon_client.subprocess, "Popen", fake_popen)
    daemon_client.spawn_daemon(reason="unit test reason")
    from claude_launcher.daemon import paths

    text = paths.log_file().read_text(encoding="utf-8")
    assert "spawn_daemon:" in text and "reason=unit test reason" in text
    assert started


def test_diagnose_reports_each_probe_with_duration_and_error(home, monkeypatch):
    from claude_launcher.daemon import runtime_state

    monkeypatch.setattr(
        runtime_state, "read_daemon_json", lambda: {"port": 1, "pid": 4242}
    )
    monkeypatch.setattr(daemon_client, "process_alive", lambda pid: True)

    def fail(url):
        daemon_client._last_probe_error = "TimeoutError: timed out"
        return False

    monkeypatch.setattr(daemon_client, "_health_ok", fail)
    report = daemon_client.diagnose(budget=0.05, gap=0.01)
    assert len(report["trace"]) == report["probes"] >= 2
    assert all(t["outcome"] == "TimeoutError: timed out" for t in report["trace"])
    assert all(t["elapsed"] >= 0 for t in report["trace"])
    text = daemon_client.format_probe_trace(report)
    assert "probe 1:" in text and "TimeoutError" in text


def test_gated_restart_failure_output_includes_probe_trace(monkeypatch, capsys):
    serving = {"state": daemon_client.SERVING, "base_url": "http://x"}
    gone = {
        "state": daemon_client.UNRESPONSIVE,
        "why": "nothing answered",
        "trace": [{"elapsed": 1.002, "outcome": "TimeoutError: timed out"}],
    }
    monkeypatch.setattr(daemon_client, "diagnose", lambda **k: serving)
    monkeypatch.setattr(
        daemon_client, "connect_with_diagnosis", lambda **k: (None, gone)
    )
    monkeypatch.setenv("CLAUNCH_SESSION", "s-test")
    assert cli_sessions._gated_restart.__name__
    rc = cli_sessions._gated_restart(reason="x")
    err = capsys.readouterr().err
    assert rc == 1
    assert "daemon went away before the request could be filed" in err
    assert "probe 1: 1.002s TimeoutError: timed out" in err
