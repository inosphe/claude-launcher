"""A roster of sessions lists each directory once, not once per session.

``beads.queues_view`` asks ``cflow_clock.run_summary`` for one summary per
lane, and the containment check that opens it -- "is this session's name a
scope with run state here?" -- listed the lane's directory every time. The
answer is a property of the directory, and lanes share directories heavily:
on the live daemon (s586, 2026-09-22) 703 sessions stood in 132 directories,
307 of them in one and 252 in another. ``scopes_in`` is an ``iterdir`` plus
two ``is_file`` per scope, so the listings cost 2618 ms per response asked
per session and 31 ms asked per directory, on the event loop that also pumps
the terminals.

``run_summarizer`` hands out a ``run_summary`` that shares one map for the
length of a response. These pin the cost and the answer: the listing happens
once per directory, the summaries are what the uncached calls would give,
and a caller that passes no map still reads the directory live.
"""

from __future__ import annotations

import pytest

from claude_launcher.cflow import engine as cflow_engine, state as cflow_state
from claude_launcher.daemon import cflow_clock

from test_run_event_clock import proj  # noqa: F401 -- fixture


def _counted(monkeypatch) -> list:
    """Records one entry per directory listing the implementation performs."""
    seen: list = []
    real = cflow_state.scopes_in

    def watched(cwd=None):
        seen.append(cwd)
        return real(cwd)

    monkeypatch.setattr(cflow_clock.cflow_state, "scopes_in", watched)
    return seen


def test_many_sessions_in_one_directory_list_it_once(proj, monkeypatch):  # noqa: F811
    """The case that was costing the loop: a board whose lanes share a cwd."""
    cwd = str(proj)
    cflow_engine.start("linear", cwd=cwd, scope="w1")
    summarize = cflow_clock.run_summarizer()
    seen = _counted(monkeypatch)
    for name in ["w1"] + [f"other{i}" for i in range(40)]:
        summarize(name, cwd)
    assert len(seen) == 1, f"listed the directory {len(seen)} times"


def test_each_directory_is_listed_once(proj, monkeypatch, tmp_path):  # noqa: F811
    """Two directories, many sessions: one listing each, not one per name."""
    a = str(proj)
    b = str(tmp_path / "elsewhere")
    (tmp_path / "elsewhere").mkdir()
    cflow_engine.start("linear", cwd=a, scope="w1")
    summarize = cflow_clock.run_summarizer()
    seen = _counted(monkeypatch)
    for i in range(10):
        summarize(f"w{i}", a)
        summarize(f"w{i}", b)
    assert len(seen) == 2, f"listed {len(seen)} times for two directories"


def test_the_summary_equals_what_the_uncached_call_gives(proj):  # noqa: F811
    """The map changes the cost, not the answer."""
    cwd = str(proj)
    cflow_engine.start("linear", cwd=cwd, scope="w1")
    summarize = cflow_clock.run_summarizer()
    assert summarize("w1", cwd) == cflow_clock.run_summary("w1", cwd)
    assert summarize("w2", cwd) is None
    assert cflow_clock.run_summary("w2", cwd) is None


def test_a_step_taken_is_still_seen_through_the_same_summarizer(proj):  # noqa: F811
    """Only the containment check is shared. The run's position is read live,
    so a lane that advances mid-response is not reported at its old step."""
    cwd = str(proj)
    cflow_engine.start("linear", cwd=cwd, scope="w1")
    summarize = cflow_clock.run_summarizer()
    assert summarize("w1", cwd)["step"] == "one"
    cflow_engine.report("done", cwd=cwd, scope="w1")
    cflow_engine.next_step(cwd=cwd, scope="w1")
    assert summarize("w1", cwd)["step"] != "one"


def test_a_new_summarizer_sees_a_run_started_since(proj):  # noqa: F811
    """The map is per response, so the next response finds the new lane."""
    cwd = str(proj)
    summarize = cflow_clock.run_summarizer()
    assert summarize("w1", cwd) is None
    cflow_engine.start("linear", cwd=cwd, scope="w1")
    assert cflow_clock.run_summarizer()("w1", cwd)["workflow"] == "linear"


def test_run_summary_without_a_map_reads_the_directory_each_time(proj, monkeypatch):  # noqa: F811
    """The ``children`` view calls it bare, and that path is unchanged."""
    cwd = str(proj)
    cflow_engine.start("linear", cwd=cwd, scope="w1")
    seen = _counted(monkeypatch)
    for _ in range(3):
        cflow_clock.run_summary("w1", cwd)
    assert len(seen) == 3


def test_an_empty_name_or_directory_never_lists_anything(proj, monkeypatch):  # noqa: F811
    summarize = cflow_clock.run_summarizer()
    seen = _counted(monkeypatch)
    assert summarize("", str(proj)) is None
    assert summarize("w1", "") is None
    assert seen == []
