"""Project-local restart commands on checklist deployment steps."""

from __future__ import annotations

import sys

import pytest

from claude_launcher.cflow import engine, mcp, model, state as state_mod
from claude_launcher.cflow.model import WorkflowError
from claude_launcher.daemon import cflow_clock


FLOW = """
name: deployer
steps:
  prepare:
    instructions: prepare deployment
    next: deploy
  deploy:
    instructions: wait for deployment
    checklist:
      prompt: did it deploy?
      then: end
      items:
        - id: deployed
          describe: live service has deployed
          check: 'python -c "raise SystemExit(1)"'
    restart:
      windows: 'powershell -File tools/restart_live.ps1'
      linux: 'bash tools/restart_live.sh'
      timeout: 45
"""


@pytest.fixture
def proj(home, tmp_path, monkeypatch):
    project = tmp_path / "project"
    (project / ".claunch" / "workflows").mkdir(parents=True)
    (project / ".claunch" / "workflows" / "deployer.yaml").write_text(FLOW, encoding="utf-8")
    monkeypatch.chdir(project)
    monkeypatch.delenv(state_mod.SESSION_ENV, raising=False)
    mcp._seen_run = None
    return project


def _at_deploy():
    engine.start("deployer")
    engine.report("prepared")
    engine.next_step()


def test_restart_parses_platform_commands_and_is_exposed(proj):
    restart = model.parse(FLOW).steps["deploy"].restart
    assert restart.command_for("Windows") == "powershell -File tools/restart_live.ps1"
    assert restart.command_for("Linux") == "bash tools/restart_live.sh"
    _at_deploy()
    payload = engine.status()
    assert payload["status"] == "waiting_checklist"
    assert payload["restart"]["status"] == "pending"
    assert payload["restart"]["timeout"] == 45


def test_restart_is_claimed_once_and_result_is_durable(proj):
    _at_deploy()
    action = engine.claim_restart(platform="Windows", boot_id="boot-a")
    assert action["kind"] == "run"
    assert action["command"].startswith("powershell")
    assert engine.claim_restart(platform="Windows", boot_id="boot-a") is None
    result = engine.complete_restart(
        step_id="deploy", visit=1, exit_code=0, output="restart complete"
    )
    assert result["exit_code"] == 0
    payload = engine.status()
    assert payload["restart"]["status"] == "succeeded"
    assert payload["restart"]["output"] == "restart complete"
    assert engine.claim_restart(platform="Windows", boot_id="boot-a") is None


def test_restart_running_across_a_new_boot_is_not_repeated(proj):
    _at_deploy()
    engine.claim_restart(platform="Linux", boot_id="old-boot")
    action = engine.claim_restart(platform="Linux", boot_id="new-boot")
    assert action["kind"] == "interrupted"
    assert engine.status()["restart"]["status"] == "interrupted"


def test_restart_clock_executes_in_the_run_cwd(proj):
    action = {
        "command": f'"{sys.executable}" -c "import os; print(os.getcwd())"',
        "timeout": 10,
    }
    result = cflow_clock.RestartClock._execute(str(proj), action)
    assert result["exit_code"] == 0
    assert str(proj).lower() in result["output"].lower()


def _echo_session_command():
    """A command that prints whatever ``CLAUNCH_SESSION`` it was handed."""
    body = "import os; print('SESSION=' + os.environ.get('CLAUNCH_SESSION', 'unset'))"
    return f'"{sys.executable}" -c "{body}"'


def test_restart_command_carries_the_runs_session(proj, monkeypatch):
    """The restart goes out as the run's session, not as the daemon.

    ``claunch daemon restart`` inside the script reads ``CLAUNCH_SESSION`` to
    decide whether it is the operator restarting their own daemon (immediate)
    or an agent asking for one (filed with the web UI's approval gate). The
    clock runs the command as a child of the daemon, whose environment has
    the variable unset -- so without this the gate was never reached and the
    daemon restarted with nobody asked. The operator's own value must not
    leak through either: the run's scope is the answer, whatever the daemon
    was started with.
    """
    monkeypatch.setenv(state_mod.SESSION_ENV, "whoever-started-the-daemon")
    action = {"command": _echo_session_command(), "timeout": 20}
    result = cflow_clock.RestartClock._execute(str(proj), action, "s469")
    assert result["exit_code"] == 0
    assert "SESSION=s469" in result["output"]


@pytest.mark.parametrize("scope", ["", "default"])
def test_restart_command_carries_no_session_for_an_unmanaged_run(proj, monkeypatch, scope):
    """A run outside a managed session answers to no name.

    Passing ``default`` through would file a gate request attributed to a
    session that does not exist; the operator's inherited value would be
    worse still, since the restart is not theirs. Both are removed, which is
    the immediate path -- the same behaviour such a run had before the gate.
    """
    monkeypatch.setenv(state_mod.SESSION_ENV, "whoever-started-the-daemon")
    action = {"command": _echo_session_command(), "timeout": 20}
    result = cflow_clock.RestartClock._execute(str(proj), action, scope)
    assert result["exit_code"] == 0
    assert "SESSION=unset" in result["output"]


def test_restart_requires_a_checklist():
    text = FLOW.replace("    checklist:\n      prompt: did it deploy?\n      then: end\n      items:\n        - id: deployed\n          describe: live service has deployed\n          check: 'python -c \"raise SystemExit(1)\"'\n", "")
    with pytest.raises(WorkflowError, match="requires a 'checklist'"):
        model.parse(text)


def test_restart_clock_returns_when_the_shell_exits_not_its_descendants(proj):
    """A grandchild that inherited stdout must not hold ``_execute`` open.

    ``tools/restart_live.ps1`` runs ``claunch daemon restart``, which
    outlives the shell it was started from. With a stdout *pipe*,
    ``subprocess.run`` waits for every inheritor of the write end — and on
    a timeout kills only the shell, then waits again without one. The
    output is captured through a file instead, so the call returns the
    moment the shell does.
    """
    import os
    import signal
    import time

    spawn = (
        "import subprocess, sys; "
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], "
        "close_fds=False); "
        "print('grandchild', p.pid, flush=True)"
    )
    action = {"command": f'"{sys.executable}" -c "{spawn}"', "timeout": 10}
    started = time.monotonic()
    result = cflow_clock.RestartClock._execute(str(proj), action)
    elapsed = time.monotonic() - started
    grandchild = int(result["output"].split("grandchild", 1)[1].split()[0])
    try:
        assert result["exit_code"] == 0
        assert "timed out" not in result["output"]
        assert elapsed < 5.0, f"_execute waited {elapsed:.1f}s for the grandchild"
    finally:
        try:
            os.kill(grandchild, signal.SIGTERM)
        except OSError:
            pass
