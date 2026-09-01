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


def test_restart_requires_a_checklist():
    text = FLOW.replace("    checklist:\n      prompt: did it deploy?\n      then: end\n      items:\n        - id: deployed\n          describe: live service has deployed\n          check: 'python -c \"raise SystemExit(1)\"'\n", "")
    with pytest.raises(WorkflowError, match="requires a 'checklist'"):
        model.parse(text)
