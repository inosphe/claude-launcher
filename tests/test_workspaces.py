"""The workspace registry: the directories a session may be spawned in."""

from __future__ import annotations

import pytest

from claude_launcher import store, workspaces
from claude_launcher.workspaces import WorkspaceError


def test_add_names_a_workspace_after_its_directory(home, tmp_path):
    d = tmp_path / "my-project"
    d.mkdir()
    ws = workspaces.add(str(d))
    assert ws.name == "my-project"
    assert ws.path == str(d.resolve())
    assert [w.name for w in workspaces.list_all()] == ["my-project"]
    # ...and it lands in the config file, where the daemon reads it live
    assert store.load()["workspaces"]["my-project"] == str(d.resolve())


def test_add_is_idempotent(home, tmp_path):
    """Safe to repeat and safe to script: re-adding is not a second entry."""
    d = tmp_path / "proj"
    d.mkdir()
    first = workspaces.add(str(d))
    again = workspaces.add(str(d))
    assert first == again
    assert len(workspaces.list_all()) == 1


def test_add_refuses_a_directory_that_is_not_there(home, tmp_path):
    """Vouching for a path that does not exist is the exact mistake the
    registry exists to catch — so the picker can never offer one."""
    with pytest.raises(WorkspaceError, match="no such directory"):
        workspaces.add(str(tmp_path / "nope"))
    with pytest.raises(WorkspaceError, match="not a directory"):
        f = tmp_path / "file.txt"
        f.write_text("x", encoding="utf-8")
        workspaces.add(str(f))


def test_name_collision_between_different_directories_is_suffixed(home, tmp_path):
    (tmp_path / "a" / "proj").mkdir(parents=True)
    (tmp_path / "b" / "proj").mkdir(parents=True)
    first = workspaces.add(str(tmp_path / "a" / "proj"))
    second = workspaces.add(str(tmp_path / "b" / "proj"))
    assert first.name == "proj"
    assert second.name == "proj-2"


def test_explicit_name_never_silently_takes_over_another_directory(home, tmp_path):
    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()
    workspaces.add(str(tmp_path / "one"), name="work")
    with pytest.raises(WorkspaceError, match="already points at"):
        workspaces.add(str(tmp_path / "two"), name="work")


def test_adding_a_known_path_under_a_new_name_renames_it(home, tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    workspaces.add(str(d))
    renamed = workspaces.add(str(d), name="mine")
    assert renamed.name == "mine"
    # the directory is listed once, under the new name
    assert [(w.name, w.path) for w in workspaces.list_all()] == [
        ("mine", str(d.resolve()))
    ]


def test_odd_directory_names_are_made_safe(home, tmp_path):
    d = tmp_path / "my project (v2)"
    d.mkdir()
    ws = workspaces.add(str(d))
    assert ws.name == "my-project-v2"


def test_invalid_explicit_name_rejected(home, tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    with pytest.raises(WorkspaceError, match="invalid workspace name"):
        workspaces.add(str(d), name="has spaces")


def test_find_and_remove_accept_a_name_or_a_path(home, tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    workspaces.add(str(d))
    assert workspaces.find("proj").path == str(d.resolve())
    assert workspaces.find(str(d)).name == "proj"
    assert workspaces.find("nothing") is None

    removed = workspaces.remove(str(d))
    assert removed.name == "proj"
    assert workspaces.list_all() == []
    assert d.is_dir()  # the directory itself is untouched
    with pytest.raises(WorkspaceError, match="no workspace named"):
        workspaces.remove("proj")


def test_missing_directory_is_reported_not_hidden(home, tmp_path):
    """A workspace on an unplugged drive should read as 'not here right now',
    not vanish from the list."""
    d = tmp_path / "removable"
    d.mkdir()
    workspaces.add(str(d))
    d.rmdir()
    (only,) = workspaces.list_all()
    assert only.name == "removable"
    assert only.to_dict()["exists"] is False


def test_workspaces_are_machine_local_by_default(home):
    """Absolute paths mean nothing on another machine, so the section must
    not ride the config sync unless a user explicitly asks for it."""
    from claude_launcher import sync

    assert "workspaces" not in sync.DEFAULT_SECTIONS


def test_a_worktree_is_owned_by_the_workspace_it_sits_in(home, tmp_path):
    """`claunch run --worktree` puts a session in <repo>/.claude/worktrees/x.

    That is the repository the user already vouched for with another branch
    checked out, so it must be accounted for as that workspace -- reporting it
    as belonging to nothing is what the registry exists to prevent.
    """
    repo = tmp_path / "proj"
    tree = repo / ".claude" / "worktrees" / "solo"
    tree.mkdir(parents=True)
    workspaces.add(str(repo))

    # `find` still answers the question it always answered: is this one?
    assert workspaces.find(str(tree)) is None
    owner = workspaces.owning(str(tree))
    assert owner is not None and owner.name == "proj"
    assert workspaces.subpath(owner, str(tree)) == ".claude/worktrees/solo"
    # At the root there is no subpath to report.
    assert workspaces.subpath(owner, str(repo)) == ""


def test_owning_prefers_the_nearest_workspace(home, tmp_path):
    """A subproject registered inside a monorepo keeps its own sessions."""
    outer = tmp_path / "mono"
    inner = outer / "packages" / "api"
    inner.mkdir(parents=True)
    workspaces.add(str(outer), name="mono")
    workspaces.add(str(inner), name="api")
    assert workspaces.owning(str(inner / "sub")).name == "api"


def test_owning_finds_nothing_outside_every_workspace(home, tmp_path):
    registered = tmp_path / "proj"
    registered.mkdir()
    workspaces.add(str(registered))
    stray = tmp_path / "elsewhere"
    stray.mkdir()
    assert workspaces.owning(str(stray)) is None
    # A sibling whose name merely starts the same must not match either.
    sibling = tmp_path / "proj-2"
    sibling.mkdir()
    assert workspaces.owning(str(sibling)) is None
