"""The pytest11 plugin that takes the window outside claunch's own suite (claunch-ersj2)."""
from __future__ import annotations

import argparse
import tomllib
from pathlib import Path

import pytest

from claude_launcher import pytest_window_plugin as plugin
from claude_launcher import test_window

ROOT = Path(__file__).resolve().parents[1]
_GRANT_VARS = (
    test_window.WINDOW_GRANT_ENV,
    test_window.WINDOW_CLASS_ENV,
    test_window.WINDOW_WORKERS_ENV,
    test_window.WINDOW_REPORT_ENV,
)


class _Config:
    def __init__(self, args):
        self.args = args
        self.invocation_params = argparse.Namespace(args=tuple(args))
        self.option = argparse.Namespace(numprocesses=None, tx=[], dist="no")
        self.pluginmanager = self
        self.stash = pytest.Stash()

    def get_plugin(self, name):
        return argparse.Namespace(stats={"passed": [1, 2]})


class _Session:
    def __init__(self, args):
        self.config = _Config(args)
        self.testscollected = 2


@pytest.fixture
def window(monkeypatch):
    """A fresh plugin state with no grant in the environment and a recording daemon."""
    for name in _GRANT_VARS + (plugin.PLUGIN_ENV, "PYTEST_XDIST_WORKER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CLAUNCH_SESSION", "s1")
    calls = {"acquire": [], "release": [], "report": []}

    def acquire(cls, label=None, workers=None, borrow_args=None):
        calls["acquire"].append((cls, label))
        calls.setdefault("borrow_args", []).append(borrow_args)
        grant = test_window.WindowGrant(
            cls=cls, grant_id="g1", advisory_n=4, source="daemon",
            wait_seconds=0.0, session="s1",
        )
        monkeypatch.setattr(grant, "release", lambda: calls["release"].append("g1"))
        return grant

    monkeypatch.setattr(test_window, "acquire", acquire)
    monkeypatch.setattr(test_window, "report_result",
                        lambda gid, result: calls["report"].append((gid, result)) or True)
    yield calls
    for name in _GRANT_VARS:
        monkeypatch.delenv(name, raising=False)


def test_the_plugin_is_registered_as_a_pytest11_entry_point():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["entry-points"]["pytest11"] == {
        "claunch_window": "claude_launcher.pytest_window_plugin"
    }


@pytest.mark.parametrize("args, cls", [
    ([], test_window.SWEEP),
    (["."], test_window.SWEEP),
    (["./"], test_window.SWEEP),
    (["tests/test_x.py"], test_window.TARGETED),
    ([".", "tests/test_x.py"], test_window.TARGETED),
])
def test_a_run_without_a_path_is_a_sweep(args, cls):
    assert plugin.window_class(args) == cls


def test_a_session_run_holds_the_window_reports_and_releases(window):
    session = _Session(["tests/test_x.py"])
    plugin.pytest_sessionstart(session)

    assert window["acquire"] == [(test_window.TARGETED, "pytest tests/test_x.py")]
    assert test_window.inherited_grant().grant_id == "g1"

    plugin.pytest_sessionfinish(session, 0)
    assert [gid for gid, _ in window["report"]] == ["g1"]
    assert window["report"][0][1]["outcome"] == "passed"
    assert window["release"] == ["g1"]
    assert test_window.inherited_grant() is None

    plugin.pytest_unconfigure(session.config)
    assert window["release"] == ["g1"]


@pytest.mark.parametrize("setup", [
    lambda mp: mp.delenv("CLAUNCH_SESSION"),
    lambda mp: mp.setenv("PYTEST_XDIST_WORKER", "gw0"),
    lambda mp: mp.setenv(plugin.PLUGIN_ENV, "0"),
])
def test_the_plugin_stands_aside(window, monkeypatch, setup):
    setup(monkeypatch)
    session = _Session([])
    plugin.pytest_sessionstart(session)
    plugin.pytest_sessionfinish(session, 0)
    assert window == {"acquire": [], "release": [], "report": []}


def test_an_inherited_grant_is_used_and_reported_only_by_the_direct_child(window, monkeypatch):
    monkeypatch.setenv(test_window.WINDOW_GRANT_ENV, "parent")
    monkeypatch.setenv(test_window.WINDOW_CLASS_ENV, test_window.TARGETED)

    session = _Session(["tests/test_x.py"])
    plugin.pytest_sessionstart(session)
    plugin.pytest_sessionfinish(session, 0)
    assert window["acquire"] == [] and window["release"] == [] and window["report"] == []

    monkeypatch.setenv(test_window.WINDOW_REPORT_ENV, "1")
    plugin.pytest_sessionstart(session)
    plugin.pytest_sessionfinish(session, 0)
    assert [gid for gid, _ in window["report"]] == ["parent"]
    assert window["acquire"] == [] and window["release"] == []
    assert test_window.inherited_grant().grant_id == "parent"


def test_a_refused_window_stops_the_run(window, monkeypatch):
    def refuse(*args, **kwargs):
        raise test_window.WindowUnavailable("test window targeted not granted after 1s")

    monkeypatch.setattr(test_window, "acquire", refuse)
    with pytest.raises(pytest.UsageError, match="not granted"):
        plugin.pytest_sessionstart(_Session(["tests/test_x.py"]))


def test_an_in_process_nested_run_leaves_the_outer_grant_alone(window):
    # s737-rev on claunch-ersj2: pytester.runpytest() in a repository where
    # this plugin holds the window loads the same module for a second config.
    outer = _Session(["tests/test_x.py"])
    plugin.pytest_sessionstart(outer)

    inner = _Session(["test_inner.py"])
    plugin.pytest_sessionstart(inner)
    plugin.pytest_sessionfinish(inner, 0)
    plugin.pytest_unconfigure(inner.config)
    assert window["release"] == [] and window["report"] == []
    assert test_window.inherited_grant().grant_id == "g1"

    plugin.pytest_sessionfinish(outer, 0)
    assert window["release"] == ["g1"]
    assert [gid for gid, _ in window["report"]] == ["g1"]


def test_a_session_run_offers_its_arguments_for_borrowing(window):
    plugin.pytest_sessionstart(_Session(["tests/test_x.py", "-q"]))
    assert window["borrow_args"] == [["tests/test_x.py", "-q"]]


def test_a_borrowed_run_ends_as_the_remote_run_ended(window, monkeypatch):
    remote = {"exit_code": 1, "output": "1 failed in 0.1s\n", "machine": "b"}

    def borrowed(*args, **kwargs):
        raise test_window.RemoteResult(remote)

    replayed = []
    monkeypatch.setattr(test_window, "acquire", borrowed)
    monkeypatch.setattr(test_window, "replay_remote",
                        lambda config, got, **kw: replayed.append(got))
    plugin.pytest_sessionstart(_Session(["tests/test_x.py"]))
    assert replayed == [remote]
