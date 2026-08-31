"""Characterization: what a recurring run carries from round to round.

Not a wish list — a description of what the code does TODAY, written so a
change to it fails loudly and on purpose. Two facts are pinned here: recur
asks for its next round by absolute path (so a workflow that later appears
in a nearer layer never reaches the loop), and a start that resolves
somewhere other than that path is treated as a different workflow entirely
(the request reads as superseded and the round counter restarts at 1).
"""

from __future__ import annotations

import pytest

from claude_launcher.cflow import engine as cflow_engine, state as cflow_state

ROUND = """
name: loopy
recur: true
steps:
  only:
    instructions: {what}
"""


@pytest.fixture
def layers(home, tmp_path, monkeypatch):
    """A project dir and a global layer, each able to declare 'loopy'."""
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    proj = tmp_path / "proj"
    (proj / ".claunch" / "workflows").mkdir(parents=True)
    glob = cflow_state.global_workflows_dir()
    glob.mkdir(parents=True, exist_ok=True)
    (glob / "loopy.yaml").write_text(ROUND.format(what="the global one"), "utf-8")
    return proj, glob


def _finish_round(cwd: str, scope: str = "w1") -> dict:
    """Drive the single-step round to its end, which is what files the
    next round's request."""
    cflow_engine.report("done", cwd=cwd, scope=scope)
    return cflow_engine.next_step(cwd=cwd, scope=scope)


def test_recur_asks_by_path_so_a_nearer_layer_never_arrives(layers):
    """The staleness every session works around by hand.

    A loop started when only the global layer declared the name keeps asking
    for that exact file. A project override added later — the layer that
    shadows global everywhere else in claunch — never reaches the loop.
    """
    proj, _ = layers
    cwd = str(proj)
    cflow_engine.start("loopy", cwd=cwd, scope="w1")
    assert cflow_engine.status(cwd, scope="w1")["origin"] == "global"

    done = _finish_round(cwd)
    request = done["pending_start"]
    # the pin: an absolute path to the file this round ran, not the name
    assert request["workflow"].endswith("loopy.yaml")
    assert "\\.claunch\\" not in request["workflow"].replace("/", "\\")
    assert request["name"] == "loopy"

    # the project now declares the same name — nearer layer, newer text
    (proj / ".claunch" / "workflows" / "loopy.yaml").write_text(
        ROUND.format(what="the project one"), "utf-8"
    )
    assert cflow_state.locate("loopy", cwd).origin == "project"

    # ...and the loop, fulfilling its own request as written, does not see it
    again = cflow_engine.start(request["workflow"], cwd=cwd, scope="w1")
    assert cflow_engine.status(cwd, scope="w1")["origin"] == "file"
    assert "the global one" in again["instructions"]
    assert again["round"] == 2  # the loop's continuity is intact, at least


def test_resolving_elsewhere_restarts_the_round_count_and_misreads_the_request(
    layers,
):
    """The cost of the workaround, which nobody predicted.

    Starting by NAME is what lets a nearer layer in — and today that also
    makes the run look like a *different* workflow: `fulfilled` compares
    paths, so the loop's own request is journaled as superseded by something
    else, and the round counter drops back to 1. Both are wrong about what
    happened: the same loop kept running, on the file it should have been
    using all along.
    """
    proj, _ = layers
    cwd = str(proj)
    cflow_engine.start("loopy", cwd=cwd, scope="w1")
    request = _finish_round(cwd)["pending_start"]
    assert request["round"] == 2

    (proj / ".claunch" / "workflows" / "loopy.yaml").write_text(
        ROUND.format(what="the project one"), "utf-8"
    )
    again = cflow_engine.start("loopy", cwd=cwd, scope="w1")

    after = cflow_engine.status(cwd, scope="w1")
    assert after["origin"] == "project"          # the staleness is gone...
    assert "the project one" in again["instructions"]
    assert again.get("round", 1) == 1            # ...and the count restarted

    events = [
        e.get("event")
        for e in cflow_state.read_journal(cwd, "w1", run_id=again["run"])
    ]
    assert "request_superseded" in events
    assert "request_fulfilled" not in events
