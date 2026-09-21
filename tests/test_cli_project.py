"""``claunch project ...`` — the registry commands, and the flags that file a
session under a project on the create paths."""

from __future__ import annotations

from claude_launcher import cli, projects, store, workspaces


def run(*argv):
    return cli.main(list(argv))


def _workspace(tmp_path, name="hq"):
    d = tmp_path / name
    d.mkdir(exist_ok=True)
    return workspaces.add(str(d), name=name)


def test_project_add_ls_set_workspace_rm(home, tmp_path, capsys):
    ws = _workspace(tmp_path)
    assert run("project", "add", "launcher", "--default-workspace", "hq") == 0
    out = capsys.readouterr().out
    assert "added 'launcher' (default workspace: hq)" in out
    assert store.load()["projects"]["launcher"] == {"default_workspace": "hq"}

    assert run("project", "ls") == 0
    out = capsys.readouterr().out
    assert "default" in out and "unfiled records live here" in out
    assert "launcher" in out and ws.path in out

    assert run("project", "set-workspace", "launcher", "") == 0
    assert "default workspace cleared" in capsys.readouterr().out
    assert projects.get("launcher").default_workspace is None

    assert run("project", "rm", "launcher") == 0
    assert "removed project 'launcher'" in capsys.readouterr().out
    assert projects.get("launcher") is None


def test_project_errors_are_one_line_on_stderr(home, capsys):
    assert run("project", "add", "p", "--default-workspace", "nope") == 1
    assert "no workspace named 'nope'" in capsys.readouterr().err
    assert run("project", "rm", "default") == 1
    assert "cannot be removed" in capsys.readouterr().err
    assert run("project", "rm", "ghost") == 1
    assert "no project named 'ghost'" in capsys.readouterr().err


def test_the_create_and_list_commands_take_a_project_flag():
    """The flag exists on every door a person files a session or mesh
    through, and on both listings, so the tier is reachable without the
    web UI."""
    parser = cli.build_parser()
    for argv in (
        ["new-session", "--profile", "x", "--project", "p"],
        ["spawn", "--project", "p", "--task", "t"],
        ["sessions", "--project", "p"],
        ["mesh", "create", "m", "--project", "p"],
        ["mesh", "ls", "--project", "p"],
    ):
        ns = parser.parse_args(argv)
        assert ns.project == "p", argv
