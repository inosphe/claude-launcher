"""What a recurring run carries from round to round.

Two facts are pinned here, both now true by design: recur asks for its next
round by the NAME the round ran under (not the file it happened to resolve
to), so a workflow that later appears in a nearer layer reaches the loop on
its very next round; and a start that fulfils that request — whether it
replays the request's own ``workflow`` field or a human types the same name
independently — is recognised as the SAME loop continuing, not a different
workflow superseding it (the round counter carries forward and the journal
reads ``request_fulfilled``, never ``request_superseded``).
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


def test_recur_asks_by_name_so_a_nearer_layer_reaches_it(layers):
    """The staleness this used to require a hand workaround for, now gone.

    A loop started when only the global layer declared the name asks its
    next round for that NAME. A project override added later — the layer
    that shadows global everywhere else in claunch — reaches the loop the
    moment its own request is replayed, with no workaround and no loss of
    continuity.
    """
    proj, _ = layers
    cwd = str(proj)
    cflow_engine.start("loopy", cwd=cwd, scope="w1")
    assert cflow_engine.status(cwd, scope="w1")["origin"] == "global"

    done = _finish_round(cwd)
    request = done["pending_start"]
    # the fix: the name the round ran under, not a path to the file it used
    assert request["workflow"] == "loopy"
    assert request["name"] == "loopy"
    # `resolved` still pins the exact file this round used, for provenance
    assert request["resolved"].endswith("loopy.yaml")
    assert "\\.claunch\\" not in request["resolved"].replace("/", "\\")

    # the project now declares the same name — nearer layer, newer text
    (proj / ".claunch" / "workflows" / "loopy.yaml").write_text(
        ROUND.format(what="the project one"), "utf-8"
    )
    assert cflow_state.locate("loopy", cwd).origin == "project"

    # ...and the loop, fulfilling its own request as written, sees it
    again = cflow_engine.start(request["workflow"], cwd=cwd, scope="w1")
    assert cflow_engine.status(cwd, scope="w1")["origin"] == "project"
    assert "the project one" in again["instructions"]
    assert again["round"] == 2  # continuity is intact

    events = [
        e.get("event")
        for e in cflow_state.read_journal(cwd, "w1", run_id=again["run"])
    ]
    assert "request_fulfilled" in events
    assert "request_superseded" not in events


def test_starting_by_name_fulfils_the_pending_request_without_resetting_the_round(
    layers,
):
    """A human (or the daemon) typing the same name independently is not
    mistaken for a different workflow superseding the loop's own request —
    it IS the loop's own request, because both now spell it as the name."""
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
    assert after["origin"] == "project"
    assert "the project one" in again["instructions"]
    assert again.get("round", 1) == 2  # continuity preserved, not restarted

    events = [
        e.get("event")
        for e in cflow_state.read_journal(cwd, "w1", run_id=again["run"])
    ]
    assert "request_fulfilled" in events
    assert "request_superseded" not in events
