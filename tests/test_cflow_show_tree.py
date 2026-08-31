"""The cflow inspection command keeps branches readable as a tree."""

from types import SimpleNamespace

from claude_launcher import cli_cflow


def _step(step_id, *, next=None, options=None):
    select = None
    if options is not None:
        select = SimpleNamespace(
            chooser="user",
            delegate=None,
            options={
                name: SimpleNamespace(description=name, next=target, interval=None)
                for name, target in options.items()
            },
        )
    return SimpleNamespace(
        id=step_id,
        title=None,
        next=next,
        select=select,
        gate=None,
        ask=None,
        verify=None,
        done_when=None,
        awaits=None,
        timer=None,
    )


def test_show_renders_branches_and_cycle_references(monkeypatch, capsys):
    wf = SimpleNamespace(
        name="tree",
        description="",
        start="start",
        max_visits=3,
        recur=False,
        recur_auto=False,
        filter_roles=None,
        default_role=None,
        priority=None,
        default_child_cflow=None,
        warnings=[],
        deprecations=[],
        advice=[],
        steps={
            "start": _step("start", options={"left": "left", "right": "right"}),
            "left": _step("left", next="join"),
            "right": _step("right", next="join"),
            "join": _step("join", next="start"),
        },
    )
    monkeypatch.setattr(
        cli_cflow.state_mod,
        "load_workflow",
        lambda _: SimpleNamespace(path="tree.yaml", workflow=wf, bases=[]),
    )

    assert cli_cflow._cmd_show(SimpleNamespace(workflow="tree")) == 0
    out = capsys.readouterr().out
    assert "├─ left: left" in out
    assert "└─ right: right" in out
    assert "↪ join (위에서 표시됨)" in out
    assert "↪ start (위에서 표시됨)" in out
