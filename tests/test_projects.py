"""The project registry: the tier meshes and sessions are filed under."""

from __future__ import annotations

import pytest

from claude_launcher import projects, store, workspaces
from claude_launcher.projects import DEFAULT, ProjectError


def _workspace(tmp_path, name="hq"):
    d = tmp_path / name
    d.mkdir(exist_ok=True)
    return workspaces.add(str(d), name=name)


def test_the_default_project_always_exists_and_comes_first(home):
    """Nothing in the config file, and there is still exactly one project:
    the one every record written before projects existed is filed under."""
    assert [p.name for p in projects.list_all()] == [DEFAULT]
    assert projects.get("") is not None and projects.get("").is_default
    assert projects.get(None).name == DEFAULT
    assert "projects" not in store.load()


def test_add_records_a_project_in_the_config_file(home, tmp_path):
    ws = _workspace(tmp_path)
    p = projects.add("launcher", default_workspace=ws.name)
    assert p.name == "launcher"
    assert p.default_workspace == "hq"
    assert p.default_cwd() == ws.path
    assert store.load()["projects"]["launcher"] == {"default_workspace": "hq"}
    # the default stays first, the rest name-sorted
    projects.add("alpha")
    assert [p.name for p in projects.list_all()] == [DEFAULT, "alpha", "launcher"]


def test_add_is_idempotent_and_updates_the_default(home, tmp_path):
    _workspace(tmp_path, "one")
    _workspace(tmp_path, "two")
    first = projects.add("p", default_workspace="one")
    assert projects.add("p") == first  # no default given: unchanged
    assert projects.add("p", default_workspace="one") == first
    again = projects.add("p", default_workspace="two")
    assert again.default_workspace == "two"
    assert len(projects.list_all()) == 2


def test_the_default_workspace_must_be_registered(home):
    with pytest.raises(ProjectError, match="no workspace named 'nope'"):
        projects.add("p", default_workspace="nope")
    assert projects.get("p") is None


def test_names_share_the_session_alphabet(home):
    with pytest.raises(ProjectError, match="invalid project name"):
        projects.add("has space")
    with pytest.raises(ProjectError, match="invalid project name"):
        projects.add("")


def test_set_and_clear_the_default_workspace(home, tmp_path):
    ws = _workspace(tmp_path)
    projects.add("p")
    assert projects.set_default_workspace("p", ws.name).default_cwd() == ws.path
    cleared = projects.set_default_workspace("p", "")
    assert cleared.default_workspace is None
    assert store.load()["projects"]["p"] == {}
    with pytest.raises(ProjectError, match="no project named 'zzz'"):
        projects.set_default_workspace("zzz", ws.name)


def test_an_unregistered_workspace_leaves_the_setting_but_no_directory(home, tmp_path):
    """The project keeps naming its workspace; the creation paths simply get
    no default until it is registered again."""
    ws = _workspace(tmp_path)
    projects.add("p", default_workspace=ws.name)
    workspaces.remove(ws.name)
    p = projects.get("p")
    assert p.default_workspace == "hq"
    assert p.default_cwd() is None
    assert projects.default_cwd("p") is None


def test_remove_drops_the_entry_and_refuses_the_default(home):
    projects.add("p")
    assert projects.remove("p").name == "p"
    assert projects.get("p") is None
    assert "projects" not in store.load()
    with pytest.raises(ProjectError, match="cannot be removed"):
        projects.remove(DEFAULT)
    with pytest.raises(ProjectError, match="no project named 'p'"):
        projects.remove("p")


def test_require_names_the_known_projects(home):
    projects.add("p")
    with pytest.raises(ProjectError, match=r"known: default, p"):
        projects.require("q")
    assert projects.require("").name == DEFAULT


def test_matches_reads_absent_as_default_and_blank_filter_as_all(home):
    assert projects.matches(None, "")
    assert projects.matches("p", "")
    assert projects.matches(None, DEFAULT)
    assert projects.matches("", "default")
    assert not projects.matches(None, "p")
    assert projects.matches("p", "p")
    assert not projects.matches("p", "q")


def test_a_hand_edited_file_with_a_bare_name_still_lists_it(home):
    store.update(lambda doc: doc.update({"projects": {"bare": None, "typed": {"default_workspace": ""}}}))
    names = [p.name for p in projects.list_all()]
    assert names == [DEFAULT, "bare", "typed"]
    assert projects.get("typed").default_workspace is None


def test_default_cwd_of_an_unknown_project_is_none(home):
    assert projects.default_cwd("nobody") is None
