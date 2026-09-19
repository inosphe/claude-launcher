"""Composing every workflow is not what a five-second poll should pay for.

``GET /api/sessions/{name}/meta`` lists what can be started in the session's
cwd, and building that list composed every workflow file found there on every
call: 161ms for the sixteen declared in this repository, against a detail
panel that polls every five seconds (measured 2026-09-20, s586). The cost is
synchronous Python, so it is not confined to its own caller -- it holds the
event loop, and every terminal the daemon is pumping stops for its duration.

What the answer depends on is files. These pin that the list is rebuilt when
one of those files changes and is served from memory when none has: the found
files, the ones they shadow, and the bases an ``extends`` chain pulled in.
"""

from __future__ import annotations

from claude_launcher.cflow import state as cflow_state
from claude_launcher.daemon import api as api_mod

TINY = "name: {name}\ndescription: {desc}\nsteps:\n  only:\n    instructions: do it\n"


def _write(path, name, desc="a workflow"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TINY.format(name=name, desc=desc), encoding="utf-8")


def _project(tmp_path):
    cwd = tmp_path / "work"
    (cwd / ".claunch" / "workflows").mkdir(parents=True)
    return cwd


def _counting(monkeypatch):
    """``compose_located`` with a tally, since that is the expensive half."""
    calls = []
    real = cflow_state.compose_located

    def counted(located, cwd=None):
        calls.append(located.name)
        return real(located, cwd)

    monkeypatch.setattr(api_mod.cflow_state, "compose_located", counted)
    return calls


def _fresh(monkeypatch):
    """Module-level caches are process-wide; no test may inherit another's."""
    monkeypatch.setattr(api_mod, "_WORKFLOW_LISTS", {})
    monkeypatch.setattr(api_mod, "_WORKFLOW_BASES", {})


def test_the_second_call_composes_nothing(home, tmp_path, monkeypatch):
    _fresh(monkeypatch)
    cwd = _project(tmp_path)
    _write(cwd / ".claunch" / "workflows" / "one.yaml", "one")
    calls = _counting(monkeypatch)

    first = api_mod._startable_workflows(str(cwd))
    assert [f["name"] for f in first] == ["one"]
    assert calls == ["one"]

    again = api_mod._startable_workflows(str(cwd))
    assert again == first
    assert calls == ["one"], "the poll recomposed a file nobody had touched"


def test_an_edited_workflow_is_seen(home, tmp_path, monkeypatch):
    _fresh(monkeypatch)
    cwd = _project(tmp_path)
    path = cwd / ".claunch" / "workflows" / "one.yaml"
    _write(path, "one", desc="before")
    calls = _counting(monkeypatch)

    assert api_mod._startable_workflows(str(cwd))[0]["description"] == "before"
    _write(path, "one", desc="after the edit, which is longer")
    after = api_mod._startable_workflows(str(cwd))
    assert after[0]["description"] == "after the edit, which is longer"
    assert len(calls) == 2


def test_a_new_workflow_is_seen(home, tmp_path, monkeypatch):
    """A file appearing changes the answer without changing any file that
    was already being watched."""
    _fresh(monkeypatch)
    cwd = _project(tmp_path)
    _write(cwd / ".claunch" / "workflows" / "one.yaml", "one")
    api_mod._startable_workflows(str(cwd))

    _write(cwd / ".claunch" / "workflows" / "two.yaml", "two")
    names = [f["name"] for f in api_mod._startable_workflows(str(cwd))]
    assert names == ["one", "two"]


def test_an_edited_base_is_seen(home, tmp_path, monkeypatch):
    """The reason the bases are stamped at all: a layer's own file is
    untouched when the file it extends is edited, and the composed answer
    still changes."""
    _fresh(monkeypatch)
    cwd = _project(tmp_path)
    flows = cwd / ".claunch" / "workflows"
    base = flows / "base.yaml"
    _write(base, "base", desc="base before")
    (flows / "layer.yaml").write_text(
        "name: layer\nextends: base\n", encoding="utf-8"
    )

    def described(entries):
        return {f["name"]: f.get("description") for f in entries}

    first = described(api_mod._startable_workflows(str(cwd)))
    assert first["layer"] == "base before"

    _write(base, "base", desc="base after, a longer description")
    second = described(api_mod._startable_workflows(str(cwd)))
    assert second["layer"] == "base after, a longer description", (
        "the layer was served from a cache that watched only its own file"
    )


def test_two_cwds_do_not_answer_for_each_other(home, tmp_path, monkeypatch):
    _fresh(monkeypatch)
    one = tmp_path / "one"
    (one / ".claunch" / "workflows").mkdir(parents=True)
    _write(one / ".claunch" / "workflows" / "alpha.yaml", "alpha")
    two = tmp_path / "two"
    (two / ".claunch" / "workflows").mkdir(parents=True)
    _write(two / ".claunch" / "workflows" / "beta.yaml", "beta")

    assert [f["name"] for f in api_mod._startable_workflows(str(one))] == ["alpha"]
    assert [f["name"] for f in api_mod._startable_workflows(str(two))] == ["beta"]
    assert [f["name"] for f in api_mod._startable_workflows(str(one))] == ["alpha"]
