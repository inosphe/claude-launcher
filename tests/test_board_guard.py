"""The board guard: installs deny the agent's file tools ``issues.jsonl``.

The issue board is the SQLite database ``.beads/beads.db``. ``issues.jsonl``
beside it is an export of that database, and ``claunch beads`` is the only
supported way in. Writing a line into the export by hand loses it twice over:
the database never sees it, and the next ``br sync --flush-only`` any session
runs rewrites the file from the database, with no error and no warning. The
reverse direction fails louder and wider — an issue in the export that the
database does not have trips ``br``'s stale-export guard, which stops every
flush in that repository for every session.

Sessions were reaching for that file because no layer they read said not to
(``claunch-beads-guidance-missing-from-profile-g77zs``): no profile-level
CLAUDE.md, no beads skill, no beads tool on the MCP server. The written
instruction now lives in the mesh skill, the improv workflows and this
repository's CLAUDE.md. This rule is the half that holds without the agent
having read anything.

One ``Edit`` rule covers all four writing tools: Claude Code consults file
paths on ``Edit`` and ``Read`` rules only, and accepts-then-ignores a path
rule written for ``Write``, ``NotebookEdit`` or ``MultiEdit``. The ``//**/``
anchor is what makes the rule reach every repository on every drive — an
unanchored path in user settings anchors under ``~/.claude`` instead.
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


def test_the_rule_is_one_edit_rule_anchored_at_the_filesystem_root():
    assert install_mod.BOARD_DENY_RULES == ("Edit(//**/.beads/issues.jsonl)",)


def test_no_rule_names_a_tool_whose_paths_are_never_consulted():
    """``Write``/``NotebookEdit``/``MultiEdit`` path rules are accepted, never
    consulted, and warned about at startup. A guard written that way would
    read as present and guard nothing, which is worse than no guard at all."""
    for rule in install_mod.BOARD_DENY_RULES:
        assert rule.startswith("Edit(")


def test_the_rule_leaves_the_rest_of_the_board_directory_alone():
    """Only the export file. ``config.yaml`` and the rest of ``.beads/`` are
    ordinary files a person may have reason to edit."""
    for rule in install_mod.BOARD_DENY_RULES:
        assert rule.endswith("/.beads/issues.jsonl)")


def test_a_project_install_plants_the_board_guard_once(project, home):
    lines = install_mod.install_into_project(project)
    path = project / ".claude" / "settings.json"
    assert any(str(path) in line and "board guard" in line for line in lines)
    assert "Edit(//**/.beads/issues.jsonl)" in _deny(path)

    install_mod.install_into_project(project)  # reinstall: no dupes
    assert _deny(path).count("Edit(//**/.beads/issues.jsonl)") == 1


def test_a_user_install_plants_it_in_the_config_dir(project, home):
    """The profile layer is the point: a session with no cflow run, no issue
    assigned and no project CLAUDE.md gets the guard from here or nowhere."""
    install_mod.install_into_user()
    path = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "settings.json"
    assert "Edit(//**/.beads/issues.jsonl)" in _deny(path)


def test_the_merge_keeps_the_users_own_deny_rules(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps({"permissions": {"deny": ["Bash(rm -rf:*)"]}}),
        encoding="utf-8",
    )
    assert settings.merge_permission_deny(path, install_mod.BOARD_DENY_RULES)
    deny = _deny(path)
    assert "Bash(rm -rf:*)" in deny
    assert "Edit(//**/.beads/issues.jsonl)" in deny


def test_the_merge_is_idempotent(tmp_path):
    path = tmp_path / "settings.json"
    assert settings.merge_permission_deny(path, install_mod.BOARD_DENY_RULES)
    assert settings.merge_permission_deny(path, install_mod.BOARD_DENY_RULES) is False
