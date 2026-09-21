"""The gate guard: installs deny the AGENT the cflow human commands.

The cflow split is channels — MCP for the agent (no approve on purpose), CLI
for the human — but an agent with a shell tool holds both. The harness
permission layer is the only place that can tell the model's Bash call from
the user's typed ``!`` command, so every install scope plants deny rules for
``claunch cflow approve|select|goto|abort`` there. The CLI itself stays
unguarded: the ``! claunch cflow approve`` flow the gate messages recommend
keeps working exactly as it always has.

The guard has a second half, planted in the same file and asserted here: an
allow rule naming the claunch MCP server. Deny alone is what broke sessions
in Claude Code's ``auto`` mode — the classifier read the deny list, saw the
MCP tool that produces the same effect, and called it tool-switching
circumvention, leaving the run with no way to advance. Both halves are a
union: a rule list the person curated keeps every entry it had.
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


def _allow(path: Path) -> list:
    return json.loads(path.read_text(encoding="utf-8"))["permissions"]["allow"]


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
    assert "mcp__claunch" in _allow(path)


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
    assert settings.merge_permission_allow(path, install_mod.GATE_ALLOW_RULES)
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert "Bash(rm -rf:*)" in doc["permissions"]["deny"]
    # The entry the person had already allowed survives, and ours is appended
    # to it rather than swapped in for it. This is the assertion the allow half
    # exists for: `apply` writes a declared `permissions.allow` through
    # `dotted_set`, which would have left this list as just ["mcp__claunch"].
    assert doc["permissions"]["allow"] == ["Bash(git status)", "mcp__claunch"]
    assert doc["theme"] == "dark"


def test_the_merge_leaves_an_exotic_shape_alone(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"permissions": "managed elsewhere"}), "utf-8")
    assert settings.merge_permission_deny(path, install_mod.GATE_DENY_RULES) is False
    assert settings.merge_permission_allow(path, install_mod.GATE_ALLOW_RULES) is False
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "permissions": "managed elsewhere"
    }


def test_the_allow_rule_names_the_whole_server():
    """Server-scoped, not one rule per tool: the guard splits channels, and a
    tool-by-tool list would go stale the moment the server gains a tool. It
    also must not name a gate command -- the human's door stays the human's."""
    assert install_mod.GATE_ALLOW_RULES == ("mcp__claunch",)
    assert not any("claunch cflow" in rule for rule in install_mod.GATE_ALLOW_RULES)


def test_the_allow_merge_is_a_union(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps({"permissions": {"allow": ["Bash(git status)"]}}), "utf-8"
    )
    assert settings.merge_permission_allow(path, install_mod.GATE_ALLOW_RULES)
    assert _allow(path) == ["Bash(git status)", "mcp__claunch"]


def test_the_allow_merge_is_idempotent(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"permissions": {}}), "utf-8")
    assert settings.merge_permission_allow(path, install_mod.GATE_ALLOW_RULES)
    assert settings.merge_permission_allow(path, install_mod.GATE_ALLOW_RULES) is False
    assert _allow(path) == ["mcp__claunch"]


def test_the_allow_merge_creates_the_file_and_its_parents(tmp_path):
    path = tmp_path / "nested" / "settings.json"
    assert settings.merge_permission_allow(path, install_mod.GATE_ALLOW_RULES)
    assert _allow(path) == ["mcp__claunch"]


def test_the_allow_merge_leaves_a_non_list_allow_alone(tmp_path):
    """A shape claunch does not recognise is left as found, not repaired."""
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"permissions": {"allow": "managed"}}), "utf-8")
    assert settings.merge_permission_allow(path, install_mod.GATE_ALLOW_RULES) is False
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "permissions": {"allow": "managed"}
    }


def test_a_project_install_plants_both_halves_of_the_guard(project, home):
    """Deny without allow is what left auto-mode sessions unable to advance a
    run: the classifier read the denied Bash verb and the equivalent MCP tool
    as one effect, and refused the second as circumvention of the first."""
    install_mod.install_into_project(project)
    path = project / ".claude" / "settings.json"
    assert "Bash(claunch cflow approve:*)" in _deny(path)
    assert "mcp__claunch" in _allow(path)
