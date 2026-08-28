"""The gate guard: installs deny the AGENT the cflow human commands.

The cflow split is channels — MCP for the agent (no approve on purpose), CLI
for the human — but an agent with a shell tool holds both. The harness
permission layer is the only place that can tell the model's Bash call from
the user's typed ``!`` command, so every install scope plants deny rules for
``claunch cflow approve|select|goto|abort`` there. The CLI itself stays
unguarded: the ``! claunch cflow approve`` flow the gate messages recommend
keeps working exactly as it always has.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from claude_launcher import install as install_mod, settings


@pytest.fixture
def project(tmp_path, monkeypatch):
    d = tmp_path / "proj"
    d.mkdir()
    monkeypatch.chdir(d)
    return d


def _deny(path: Path) -> list:
    return json.loads(path.read_text(encoding="utf-8"))["permissions"]["deny"]


def test_the_rules_cover_every_gate_command_and_both_shells():
    for cmd in ("approve", "select", "goto", "abort"):
        for tool in ("Bash", "PowerShell"):
            assert f"{tool}(claunch cflow {cmd})" in install_mod.GATE_DENY_RULES
            assert f"{tool}(claunch cflow {cmd}:*)" in install_mod.GATE_DENY_RULES
    # ...and nothing beyond them: the read-side commands stay usable
    assert not any("status" in rule for rule in install_mod.GATE_DENY_RULES)
    assert not any("journal" in rule for rule in install_mod.GATE_DENY_RULES)


def test_the_goto_rules_cover_answering_a_request_too():
    """`goto` grew a second half — `--approve`/`--deny`, which answers the
    agent's own `request_goto`. An agent that could type that would be
    granting its own request, so the guard has to reach the flags and not
    just the bare verb. It does, via the `:*` prefix form; asserted here
    because the reachability is the whole point of the request path."""
    for tool in ("Bash", "PowerShell"):
        prefix = f"{tool}(claunch cflow goto:*)"
        assert prefix in install_mod.GATE_DENY_RULES


def test_a_project_install_plants_the_guard_once(project, home):
    lines = install_mod.install_into_project(project)
    path = project / ".claude" / "settings.json"
    assert any(str(path) in line and "gate guard" in line for line in lines)
    deny = _deny(path)
    assert "Bash(claunch cflow approve:*)" in deny

    lines = install_mod.install_into_project(project)  # reinstall: no dupes
    assert any("already present" in line for line in lines)
    assert _deny(path).count("Bash(claunch cflow approve:*)") == 1


def test_a_user_install_plants_the_guard_in_the_config_dir(project, home):
    install_mod.install_into_user()
    path = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "settings.json"
    assert "Bash(claunch cflow select:*)" in _deny(path)


def test_the_merge_keeps_the_users_own_permissions(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "permissions": {
                    "deny": ["Bash(rm -rf:*)"],
                    "allow": ["Bash(git status)"],
                },
                "theme": "dark",
            }
        ),
        encoding="utf-8",
    )
    assert settings.merge_permission_deny(path, install_mod.GATE_DENY_RULES)
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert "Bash(rm -rf:*)" in doc["permissions"]["deny"]
    assert doc["permissions"]["allow"] == ["Bash(git status)"]
    assert doc["theme"] == "dark"


def test_the_merge_leaves_an_exotic_shape_alone(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"permissions": "managed elsewhere"}), "utf-8")
    assert settings.merge_permission_deny(path, install_mod.GATE_DENY_RULES) is False
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "permissions": "managed elsewhere"
    }
