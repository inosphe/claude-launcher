"""One slow answer is not an absence: the shared liveness check's honesty.

``daemon_client.connect()`` is the single gate all three claunch surfaces go
through -- ``claunch sessions`` (cli_sessions), ``claunch mesh ls`` (cli_mesh)
and the MCP tools (mesh_mcp). It used to send exactly one health probe with a
one-second timeout and read the miss as "the daemon is not running", so a
daemon that was merely slow that instant was reported as absent. Measured
against the live daemon that way: 6 of 100 connects came back absent while the
other 94 in the same loop proved it was up, and /api/health, whose median is
13ms, was seen taking 2.667s.

The bug was intermittent, which is what made it expensive: the wrong answer's
natural next move is to start a replacement daemon, and nobody could reproduce
it on demand to argue otherwise. These tests remove the dice. ``_delay_server``
decides exactly which probe is slow, so "answers late" and "does not answer at
all" are two different servers rather than two rolls of the same one.

The property under test is not "retry harder". It is that the three answers
stay distinguishable:

    absent      -- nothing announced, or the announcement's pid is gone
    unconfirmed -- announced, alive, and did not answer inside a short budget
    serving     -- answered

A mitigation that made an absent daemon report "unconfirmed" would be a
regression of its own, so every case below has its mirror: the slow-daemon
tests assert claunch does NOT say "not running", and the genuinely-absent
tests assert it still does.
"""

from __future__ import annotations

import os

import pytest

from claude_launcher import cli, daemon_client, mesh_mcp

from test_daemon_wedge import _announce, _dead_pid, _delay_server, _shrink_budgets


# --------------------------------------------------------------------------- #
# the reproduction, made deterministic
# --------------------------------------------------------------------------- #
def test_a_daemon_that_misses_one_probe_is_still_connected(home, monkeypatch):
    """The incident itself, with the dice removed.

    The first probe is slower than the probe timeout and every later one is
    instant -- exactly the live daemon's shape (median 13ms, occasional 2.7s
    stall). A single-probe check calls this absent 100% of the time; the
    measured 6% was only that shape sampled once per call.
    """
    _shrink_budgets(monkeypatch, probe=0.2)
    slow = 1.5 * daemon_client.HEALTH_TIMEOUT
    with _delay_server(lambda i: slow if i == 0 else 0.0, max_stall=0.0) as port:
        _announce(monkeypatch, pid=os.getpid(), port=port)
        client = daemon_client.connect()
    assert client is not None


def test_the_stall_that_was_measured_does_not_read_as_absence(home, monkeypatch):
    """The same property stated in the incident's own numbers: a stall of
    2.667s against a 1.0s probe, scaled down through the one knob the clock
    hangs on. Two probes' patience is what turns that from an absence into a
    connection."""
    _shrink_budgets(monkeypatch, probe=0.2)
    stall = (2.667 / 1.0) * daemon_client.HEALTH_TIMEOUT
    with _delay_server(lambda i: stall if i == 0 else 0.0, max_stall=0.0) as port:
        _announce(monkeypatch, pid=os.getpid(), port=port)
        client, report = daemon_client.connect_with_diagnosis()
    # The report goes in the message because the bare assertion says only
    # "None is not None", and the thing worth knowing when this fails on a
    # loaded machine is which branch produced it: how many probes fitted, and
    # what budget they were measured against.
    assert client is not None, report
    assert report["state"] == daemon_client.SERVING
    assert report["probes"] >= 2  # the first one is the one that was missed


def test_a_healthy_daemon_still_connects_on_the_first_probe(home, monkeypatch):
    """The patience must not cost the common case anything: a daemon that
    answers immediately is answered on probe one, with no gap slept."""
    _shrink_budgets(monkeypatch, probe=0.2)
    with _delay_server(lambda i: 0.0, max_stall=0.0) as port:
        _announce(monkeypatch, pid=os.getpid(), port=port)
        client, report = daemon_client.connect_with_diagnosis()
    assert client is not None
    assert report["probes"] == 1 and report["state"] == daemon_client.SERVING


# --------------------------------------------------------------------------- #
# the three answers stay apart
# --------------------------------------------------------------------------- #
def test_a_live_daemon_that_never_answers_is_unconfirmed_not_absent(home, monkeypatch):
    """Silence from a process that is alive and announced is an observation,
    never the verdict "not running" -- the wrong answer this issue is about
    sends its reader to start a replacement."""
    _shrink_budgets(monkeypatch, probe=0.2)
    budget = daemon_client.OBSERVATION_BUDGET
    with _delay_server(lambda i: float("inf"), max_stall=3 * budget) as port:
        _announce(monkeypatch, pid=os.getpid(), port=port)
        client, report = daemon_client.connect_with_diagnosis()
    assert client is None
    assert report["state"] == daemon_client.UNRESPONSIVE
    assert report["state"] != daemon_client.NOT_RUNNING


def test_nothing_announced_is_absent_and_costs_no_probes(home):
    """The mirror. No daemon.json is not ambiguous and must not be padded
    with patience: it is an absence, decided without probing anything."""
    client, report = daemon_client.connect_with_diagnosis()
    assert client is None
    assert report["state"] == daemon_client.NOT_RUNNING
    assert report["probes"] == 0


def test_an_announcement_whose_pid_is_gone_is_absent(home, monkeypatch):
    """The other mirror. A daemon.json left behind by a process that died is
    an absence too -- the patience is for live processes only."""
    _shrink_budgets(monkeypatch, probe=0.2)
    _announce(monkeypatch, pid=_dead_pid())
    client, report = daemon_client.connect_with_diagnosis()
    assert client is None
    assert report["state"] == daemon_client.STALE_RECORD
    assert report["probes"] == 1  # not out-waited


def test_the_report_carries_what_a_caller_needs_to_act(home, monkeypatch):
    """Unconfirmed is only useful if it comes with the evidence: which pid is
    announced, since when, and how hard we looked."""
    _shrink_budgets(monkeypatch, probe=0.2)
    budget = daemon_client.OBSERVATION_BUDGET
    with _delay_server(lambda i: float("inf"), max_stall=3 * budget) as port:
        _announce(
            monkeypatch,
            pid=os.getpid(),
            port=port,
            started_at="2026-08-26T04:16:07+00:00",
        )
        _, report = daemon_client.connect_with_diagnosis()
    assert report["pid"] == os.getpid()
    assert report["started_at"] == "2026-08-26T04:16:07+00:00"
    assert report["probes"] >= 1 and report["successes"] == 0


# --------------------------------------------------------------------------- #
# how the answer is phrased
# --------------------------------------------------------------------------- #
def test_the_absent_phrasing_is_the_plain_one(home):
    _, report = daemon_client.connect_with_diagnosis()
    assert daemon_client.unreachable_reason(report) == "daemon is not running"


def test_the_unconfirmed_phrasing_never_says_not_running(home, monkeypatch):
    """The phrasing is the mitigation: a reader who sees "not running" starts
    a daemon, and doing that to a healthy one is how this bug does damage."""
    _shrink_budgets(monkeypatch, probe=0.2)
    budget = daemon_client.OBSERVATION_BUDGET
    with _delay_server(lambda i: float("inf"), max_stall=3 * budget) as port:
        _announce(
            monkeypatch,
            pid=os.getpid(),
            port=port,
            started_at="2026-08-26T04:16:07+00:00",
        )
        _, report = daemon_client.connect_with_diagnosis()
    said = daemon_client.unreachable_reason(report)
    assert "not running" not in said
    assert "did not answer" in said and "may be busy" in said
    assert str(os.getpid()) in said and "2026-08-26T04:16:07+00:00" in said


def test_a_stale_record_says_not_running_and_names_the_gone_pid(home, monkeypatch):
    _shrink_budgets(monkeypatch, probe=0.2)
    gone = _dead_pid()
    _announce(monkeypatch, pid=gone)
    _, report = daemon_client.connect_with_diagnosis()
    said = daemon_client.unreachable_reason(report)
    assert "daemon is not running" in said and str(gone) in said


# --------------------------------------------------------------------------- #
# the three surfaces, which is where the wrong answer was actually read
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "argv, absent_line",
    [
        (["sessions"], "daemon is not running; no sessions"),
        (["mesh", "ls"], "daemon is not running; no meshes"),
    ],
)
def test_a_surface_reports_absence_plainly_when_it_is_absent(
    home, capsys, argv, absent_line
):
    """The broken variant, per surface: with nothing announced, the old
    sentence is still the right one and still exit 0."""
    assert cli.main(argv) == 0
    assert absent_line in capsys.readouterr().out


@pytest.mark.parametrize("argv", [["sessions"], ["mesh", "ls"]])
def test_a_surface_does_not_call_a_slow_daemon_absent(home, monkeypatch, capsys, argv):
    """The fix where it is read. A live-but-silent daemon must not produce the
    sentence whose next step is starting a second one, and must not exit 0 --
    a caller that only checks the status code has to be able to tell an
    unconfirmed look from an empty list."""
    _shrink_budgets(monkeypatch, probe=0.2)
    budget = daemon_client.OBSERVATION_BUDGET
    with _delay_server(lambda i: float("inf"), max_stall=6 * budget) as port:
        _announce(monkeypatch, pid=os.getpid(), port=port)
        rc = cli.main(argv)
    captured = capsys.readouterr()
    said = captured.out + captured.err
    assert rc != 0
    assert "not running" not in said
    assert "did not answer" in said and str(os.getpid()) in said


def test_the_mcp_surface_does_not_call_a_slow_daemon_absent(home, monkeypatch):
    """mesh_mcp raises rather than prints, so the distinction has to survive
    into the exception text the calling agent reads -- and that agent's
    documented next move on "not running" is to start a daemon."""
    _shrink_budgets(monkeypatch, probe=0.2)
    budget = daemon_client.OBSERVATION_BUDGET
    with _delay_server(lambda i: float("inf"), max_stall=3 * budget) as port:
        _announce(monkeypatch, pid=os.getpid(), port=port)
        with pytest.raises(mesh_mcp.MeshMcpError) as slow:
            mesh_mcp._client()
    assert "not running" not in str(slow.value)
    assert "did not answer" in str(slow.value)
    assert str(os.getpid()) in str(slow.value)


def test_the_mcp_surface_still_reports_a_real_absence(home):
    """Its mirror: with nothing announced the old sentence survives intact,
    'claunch daemon start' advice included."""
    with pytest.raises(mesh_mcp.MeshMcpError) as gone:
        mesh_mcp._client()
    assert "daemon is not running" in str(gone.value)
    assert "claunch daemon start" in str(gone.value)
