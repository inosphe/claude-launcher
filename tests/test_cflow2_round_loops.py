"""Which loops the packaged charts call rounds (``loop.rounds``).

``round_now`` shows the round a run is in. For a workflow that recurs that is
the slot's ``round``; for one that runs its rounds as iterations of a loop
inside a single run -- ``improv-worker-v2``, ``perf-monitor-v2`` -- it is that
loop's iteration count. The loop is the one the definition marks ``rounds:
true`` (claunch-oxmso.1). Before the mark the rule was "the outermost live
while/until loop", and ``operator-v2``'s ``service`` loop -- which only polls
``claunch operators watch`` -- made a session's rail count its polling passes
as rounds.

The declarations are the fragile half: the marker lives in the YAML, and
dropping it would not fail any test that only exercises the chart's own
behavior -- it would just take the rail back to ``round 1`` while
``state["round"]`` stayed correct. This module reads the packaged definitions
through the live package layer (``layers.live_package_dir``), not through the
per-test project layer other suites install, and pins both directions: the
round loops carry the mark, the watch loops do not.
"""

from __future__ import annotations

from claude_launcher.cflow2 import layers


def _marked_loops(name: str):
    chart = layers.load_file(layers.live_package_dir() / f"{name}.yaml")
    return sorted(nid for nid, node in chart.nodes.items()
                  if node.get("rounds") is True)


def test_the_round_loops_of_the_package_carry_the_mark():
    assert _marked_loops("improv-worker-v2") == ["rounds"]
    assert _marked_loops("perf-monitor-v2") == ["rounds"]


def test_the_watch_loops_are_not_marked():
    """``service`` polls the operator queue and has no rounds at all.

    Marking it would put a polling count back on the session's rail, which is
    the bug this rule replaced.
    """
    assert _marked_loops("operator-v2") == []
    assert _marked_loops("gds-operator-v2") == []
