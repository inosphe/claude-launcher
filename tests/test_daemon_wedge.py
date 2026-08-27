"""Telling a wedged daemon apart from an absent one, and getting past it.

A daemon whose event loop stops turning keeps everything that makes it look
alive -- its pid, its port, its ``daemon.json`` -- and, decisively, its
singleton lock. The client used to report that as "not running", which sends
the operator to start a replacement that then stands down against a lock the
"absent" daemon is still holding; the way out was for a human to find the pid
themselves. These tests pin the diagnosis that ends that, and the one
recovery it enables.

The diagnosis is budget-based: WEDGED means zero answered probes across a
budget no shorter than the one claunch's own start path waits out
(VERDICT_BUDGET = START_TIMEOUT), from a process that still lives. Every
duration below derives from the probe timeout through ``_shrink_budgets`` --
no test pins a wall-clock number the implementation does not.
"""

from __future__ import annotations

import contextlib
import http.server
import os
import subprocess
import sys
import threading
import time

import pytest

from claude_launcher import cli, daemon_client
from claude_launcher.daemon import runtime_state


@pytest.fixture(autouse=True)
def immediate_path(monkeypatch):
    """This module drives ``daemon restart``'s immediate path, so say so.

    ``cli_sessions._cmd_daemon`` branches on ``CLAUNCH_SESSION``: a restart
    asked for inside a managed session goes to ``_gated_restart`` and waits on
    the web UI's approval instead of stopping the daemon on the spot, and
    ``_force_replace`` hands over to the same function when force stands down
    against a daemon that answers. Both are deliberate (see
    ``daemon/restart_gate.py``) and ``tests/test_restart_gate.py`` is where
    they are pinned.

    Nothing here declared which of the two paths it meant. The variable is set
    in every claunch-managed session and unset on a developer's machine, so
    the same tree answered differently depending on who ran it -- 22 passed
    outside a session, 3 failed inside one, from ``_gated_restart``'s own
    ``diagnose()`` call exhausting a two-answer iterator the test had canned
    (``claunch-uf7m``, first red ``fe0eb1a``). A suite that cannot be read the
    same way twice is worse than one that is red, because the disagreement
    looks like somebody's mistake rather than a missing declaration.

    Autouse rather than three ``delenv`` lines: the trap is the module's, not
    those three tests', and the next test written here would walk into it.
    """
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)


# --------------------------------------------------------------------------- #
# liveness, without the probe that kills what it asks about
# --------------------------------------------------------------------------- #
def test_this_process_is_alive():
    assert daemon_client.process_alive(os.getpid()) is True


def test_a_finished_process_reads_as_gone():
    assert daemon_client.process_alive(_dead_pid()) is False


def test_a_liveness_probe_does_not_kill_its_subject():
    """On Windows ``os.kill(pid, 0)`` is TerminateProcess -- the POSIX habit
    would end the daemon it was merely asking about."""
    child = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()"],
        stdin=subprocess.PIPE,
    )
    try:
        for _ in range(3):
            assert daemon_client.process_alive(child.pid) is True
        assert child.poll() is None  # still there after being asked about
    finally:
        child.kill()
        child.wait(timeout=10)


def test_a_nonsense_pid_is_unknown_rather_than_alive():
    assert daemon_client.process_alive(0) is None
    assert daemon_client.process_alive(-1) is None


# --------------------------------------------------------------------------- #
# diagnosis
# --------------------------------------------------------------------------- #
def test_nothing_announced_is_not_running(home):
    report = daemon_client.diagnose()
    assert report["state"] == daemon_client.NOT_RUNNING
    assert report["pid"] is None


def test_an_announcement_whose_process_is_gone_is_a_stale_record(home, monkeypatch):
    gone = _dead_pid()
    _announce(monkeypatch, pid=gone)
    report = daemon_client.diagnose()
    assert report["state"] == daemon_client.STALE_RECORD
    assert str(gone) in report["why"]
    # A dead pid is not out-waited: the budget is patience for a live
    # process, and this one is gone after the first unanswered probe.
    assert report["probes"] == 1


def test_a_live_pid_that_answers_nothing_all_budget_long_is_wedged(home, monkeypatch):
    """The whole point: alive + announced + a full verdict budget of silence
    is its own state, and the report carries its evidence -- budget, probe
    count, zero answers -- not just the label."""
    _shrink_budgets(monkeypatch)
    _announce(monkeypatch, pid=os.getpid())
    report = daemon_client.diagnose(gap=daemon_client.HEALTH_TIMEOUT / 10)
    assert report["state"] == daemon_client.WEDGED
    assert report["pid"] == os.getpid()
    assert report["successes"] == 0 and report["probes"] >= 2
    assert "0 answers" in report["why"]


def test_a_daemon_that_answers_is_serving(home, monkeypatch):
    _announce(monkeypatch, pid=os.getpid())
    monkeypatch.setattr(daemon_client, "_health_ok", lambda url: True)
    assert daemon_client.diagnose()["state"] == daemon_client.SERVING


def test_one_answer_anywhere_inside_the_budget_means_serving(home, monkeypatch):
    """A missed check on a loaded machine means busy, not wedged -- and the
    difference decides whether something gets killed. The old rule (a count
    of consecutive misses, ~4.5s worth) judged faster than ensure_running
    waits for a *starting* daemon; the budget rule cannot."""
    _shrink_budgets(monkeypatch)
    _announce(monkeypatch, pid=os.getpid())
    answers = iter([False, False, True])
    monkeypatch.setattr(daemon_client, "_health_ok", lambda url: next(answers))
    report = daemon_client.diagnose(gap=0.0)
    assert report["state"] == daemon_client.SERVING
    assert report["probes"] == 3 and report["successes"] == 1


def test_a_short_budget_reports_an_observation_never_a_verdict(home, monkeypatch):
    """Callers that cannot afford the verdict budget get the same facts under
    a name that judges nothing -- no short look can tell busy from stuck."""
    _shrink_budgets(monkeypatch)
    _announce(monkeypatch, pid=os.getpid())
    report = daemon_client.diagnose(
        budget=daemon_client.OBSERVATION_BUDGET,
        gap=daemon_client.HEALTH_TIMEOUT / 10,
    )
    assert report["state"] == daemon_client.UNRESPONSIVE
    assert report["successes"] == 0
    assert "wedged" not in report["why"].lower()


# --------------------------------------------------------------------------- #
# the CLI: an honest status, and a force that is not automatic
# --------------------------------------------------------------------------- #
def test_status_reports_its_observation_and_never_says_wedged(home, monkeypatch, capsys):
    """status takes a quick look, and a quick look has not earned a verdict:
    it reports what it saw, points at where the full-budget diagnosis lives,
    and recommends killing nothing."""
    _shrink_budgets(monkeypatch)
    _announce(monkeypatch, pid=os.getpid())
    assert cli.main(["daemon", "status"]) == 1
    err = capsys.readouterr().err
    assert "did not answer" in err
    assert "restart" in err          # where the verdict-grade look lives
    assert "WEDGED" not in err       # a short look must not judge
    assert "--force" not in err      # and must not hint at killing
    assert "not running" not in err  # the lie this replaces


def test_restart_refuses_to_flail_against_a_wedged_daemon(home, monkeypatch, capsys):
    """Without this the operator gets 'did not come up within 15s', which
    reads as a broken install rather than a daemon to replace."""
    _shrink_budgets(monkeypatch)
    _announce(monkeypatch, pid=os.getpid())
    monkeypatch.setattr(
        daemon_client, "spawn_daemon",
        lambda *a, **k: pytest.fail("a replacement cannot start against the lock"),
    )
    assert cli.main(["daemon", "restart"]) == 1
    err = capsys.readouterr().err
    assert "WEDGED" in err and "--force" in err
    assert "confirmed twice" in err  # the verdict was earned, not guessed


def test_the_wedged_brief_carries_what_the_decision_needs(home, monkeypatch, capsys):
    """--force is a decision put to a person, and this brief is all they get
    to decide on: the evidence (two full budgets, zero answers), the log
    signal, the confession that busy and stalled look alike from outside,
    what forcing costs, and the option to wait. The tool judges; the person
    decides."""
    _shrink_budgets(monkeypatch)
    _announce(monkeypatch, pid=os.getpid())
    assert cli.main(["daemon", "restart"]) == 1
    err = capsys.readouterr().err
    assert "confirmed twice" in err and "0 answered" in err
    assert "daemon.log" in err                     # progress: reported fact
    assert "cannot tell" in err                    # the tool's honest limit
    assert "recover on their own" in err           # waiting has won before
    assert "--force" in err and "session" in err   # the act and its cost
    assert "or wait" in err                        # the decision stays human


def test_restart_does_not_recommend_force_off_a_single_round(home, monkeypatch, capsys):
    """One full-budget round of silence buys a second look, not a verdict: a
    daemon that answers on the second round was busy, and busy restarts the
    ordinary way."""
    looks = iter([
        _canned(daemon_client.WEDGED),
        _canned(daemon_client.SERVING, successes=1),
    ])
    monkeypatch.setattr(daemon_client, "diagnose", lambda **kw: next(looks))
    monkeypatch.setattr(daemon_client, "stop", lambda **kw: True)
    monkeypatch.setattr(
        daemon_client, "ensure_running",
        lambda **kw: daemon_client.DaemonClient("http://x", "t"),
    )
    assert cli.main(["daemon", "restart"]) == 0
    captured = capsys.readouterr()
    assert "restarted" in captured.out
    assert "--force" not in captured.err


def test_force_stands_down_when_the_second_look_answers(home, monkeypatch, capsys):
    """Force re-earns its own verdict: the first silent budget is confirmed
    by a second before anything is ended, and a daemon that answers between
    the looks is restarted politely instead."""
    looks = iter([
        _canned(daemon_client.WEDGED),
        _canned(daemon_client.SERVING, successes=1),
    ])
    monkeypatch.setattr(daemon_client, "diagnose", lambda **kw: next(looks))
    monkeypatch.setattr(
        daemon_client, "terminate_process",
        lambda pid, **kw: pytest.fail("one round of silence must not kill"),
    )
    monkeypatch.setattr(daemon_client, "stop", lambda **kw: True)
    monkeypatch.setattr(
        daemon_client, "ensure_running",
        lambda **kw: daemon_client.DaemonClient("http://x", "t"),
    )
    assert cli.main(["daemon", "restart", "--force"]) == 0
    assert "no force needed" in capsys.readouterr().err


def test_force_ends_the_wedged_process_then_starts_a_successor(
    home, monkeypatch, capsys
):
    ended, started = [], []
    _shrink_budgets(monkeypatch)
    _announce(monkeypatch, pid=4242)
    monkeypatch.setattr(daemon_client, "process_alive", lambda pid: True)
    monkeypatch.setattr(
        daemon_client, "terminate_process",
        lambda pid, **kw: ended.append(pid) or True,
    )
    monkeypatch.setattr(
        daemon_client, "ensure_running",
        lambda **kw: started.append(True) or daemon_client.DaemonClient("http://x", "t"),
    )
    assert cli.main(["daemon", "restart", "--force"]) == 0
    assert ended == [4242] and started == [True]
    out = capsys.readouterr().out
    assert "wedged" in out and "sessions went with it" in out


def test_force_on_a_healthy_daemon_restarts_it_gracefully_instead(
    home, monkeypatch, capsys
):
    """--force names a situation, not a bigger hammer: a daemon that answers
    is stopped the polite way even when force was asked for."""
    _announce(monkeypatch, pid=os.getpid())
    monkeypatch.setattr(daemon_client, "_health_ok", lambda url: True)
    monkeypatch.setattr(
        daemon_client, "terminate_process",
        lambda pid, **kw: pytest.fail("a serving daemon must not be killed"),
    )
    monkeypatch.setattr(daemon_client, "stop", lambda **kw: True)
    monkeypatch.setattr(
        daemon_client, "ensure_running",
        lambda **kw: daemon_client.DaemonClient("http://x", "t"),
    )
    assert cli.main(["daemon", "restart", "--force"]) == 0
    assert "no force needed" in capsys.readouterr().err


def test_force_with_nothing_running_just_starts_one(home, monkeypatch, capsys):
    monkeypatch.setattr(
        daemon_client, "terminate_process",
        lambda pid, **kw: pytest.fail("there is nothing to end"),
    )
    monkeypatch.setattr(
        daemon_client, "ensure_running",
        lambda **kw: daemon_client.DaemonClient("http://x", "t"),
    )
    assert cli.main(["daemon", "restart", "--force"]) == 0
    assert "daemon started" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# against real sockets: the timing regressions, every duration derived from
# the probe timeout
# --------------------------------------------------------------------------- #
def test_an_instant_server_is_serving_on_the_first_probe(home, monkeypatch):
    _shrink_budgets(monkeypatch)
    with _delay_server(lambda i: 0.0, max_stall=0.0) as port:
        _announce(monkeypatch, pid=os.getpid(), port=port)
        report = daemon_client.diagnose(gap=daemon_client.HEALTH_TIMEOUT / 10)
    assert report["state"] == daemon_client.SERVING
    assert report["probes"] == 1 and report["successes"] == 1


def test_a_busy_daemon_that_answers_late_in_the_budget_is_serving(home, monkeypatch):
    """The property this redesign exists for: a daemon that misses its first
    probes but answers somewhere inside the verdict budget is SERVING.
    claunch's own start path sits out that much silence as normal
    (ensure_running waits START_TIMEOUT), so a diagnosis that judged sooner
    would call daemons wedged that the rest of the code would have
    out-waited -- and its verdict is what sends an operator toward --force."""
    _shrink_budgets(monkeypatch, probe=0.5)
    probe = daemon_client.HEALTH_TIMEOUT
    slow = 1.2 * probe  # early answers land after the probe has given up
    with _delay_server(lambda i: slow if i < 2 else 0.0, max_stall=0.0) as port:
        _announce(monkeypatch, pid=os.getpid(), port=port)
        report = daemon_client.diagnose(gap=probe / 10)
    assert report["state"] == daemon_client.SERVING
    assert report["probes"] >= 3  # earlier probes failed; the budget held out
    assert report["successes"] == 1


def test_a_server_slower_than_one_probe_is_an_observation_on_a_short_look(
    home, monkeypatch
):
    """Delay = 1.2x the probe timeout: every probe comes back empty, yet the
    short-budget look still reports only the observation. The word WEDGED
    belongs to the full budget and to nobody else."""
    _shrink_budgets(monkeypatch, probe=0.5)
    probe = daemon_client.HEALTH_TIMEOUT
    with _delay_server(lambda i: 1.2 * probe, max_stall=0.0) as port:
        _announce(monkeypatch, pid=os.getpid(), port=port)
        report = daemon_client.diagnose(
            budget=daemon_client.OBSERVATION_BUDGET, gap=probe / 10
        )
    assert report["state"] == daemon_client.UNRESPONSIVE
    assert report["successes"] == 0
    assert "wedged" not in report["why"].lower()


def test_zero_answers_across_the_whole_verdict_budget_is_wedged(home, monkeypatch):
    """The boundary definition itself: WEDGED = zero answered probes across a
    budget no shorter than VERDICT_BUDGET, from a process that still lives.
    Stated as a definition rather than an incident re-enactment, because the
    budget marks the exact line under which claunch's own start path would
    still have been waiting."""
    _shrink_budgets(monkeypatch)
    budget = daemon_client.VERDICT_BUDGET
    with _delay_server(lambda i: float("inf"), max_stall=3 * budget) as port:
        _announce(monkeypatch, pid=os.getpid(), port=port)
        report = daemon_client.diagnose(gap=daemon_client.HEALTH_TIMEOUT / 10)
    assert report["state"] == daemon_client.WEDGED
    assert report["successes"] == 0 and report["probes"] >= 2
    assert "0 answers" in report["why"]


def _stub_connect(monkeypatch, module, factory):
    """Stub the daemon-connection seam in both of its shapes.

    ``connect_with_diagnosis`` is the primitive -- a client plus the evidence
    behind the answer -- and ``connect`` is its bool-shaped view. A stub that
    only replaced one of them would leave the other reaching for a real
    daemon, so tests replace the pair together.
    """
    client = factory()
    state = daemon_client.NOT_RUNNING if client is None else daemon_client.SERVING
    report = {"state": state, "pid": None, "started_at": None, "probes": 0,
              "successes": 0, "budget": 0.0, "base_url": None, "lock_free": True}
    monkeypatch.setattr(module, "connect", lambda: factory())
    monkeypatch.setattr(
        module, "connect_with_diagnosis", lambda **kw: (factory(), dict(report))
    )


def _dead_pid() -> int:
    """A pid that certainly belonged to a process and certainly does not now."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=30)
    return proc.pid


def _announce(
    monkeypatch, *, pid: int, port: int = 59999, started_at: str = ""
) -> None:
    """Write a daemon.json naming ``pid`` at ``port`` (default: one nothing
    listens on).

    ``started_at`` is what the real daemon records and what an unconfirmed
    diagnosis quotes back ("pid P up since T"); it defaults to absent so the
    tests that do not care about the phrasing stay unchanged.
    """
    doc = {"pid": pid, "host": "127.0.0.1", "port": port, "version": "test"}
    if started_at:
        doc["started_at"] = started_at
    monkeypatch.setattr(runtime_state, "read_daemon_json", lambda: dict(doc))
    monkeypatch.setattr(
        daemon_client.runtime_state, "read_daemon_json", lambda: dict(doc)
    )


def _shrink_budgets(monkeypatch, *, probe: float = 0.2) -> None:
    """Scale the whole diagnosis clock down from the one knob it hangs on.

    Production derives its budgets from HEALTH_TIMEOUT; the tests re-derive
    theirs from ``probe`` the same way, so no duration is pinned twice and
    none is pinned in wall-clock terms.
    """
    monkeypatch.setattr(daemon_client, "HEALTH_TIMEOUT", probe)
    monkeypatch.setattr(daemon_client, "VERDICT_BUDGET", 6 * probe)
    monkeypatch.setattr(daemon_client, "OBSERVATION_BUDGET", 2 * probe)


def _canned(state: str, **over) -> dict:
    """A diagnose() report for tests that pin call-site behavior, not probing."""
    report = {
        "state": state,
        "pid": 4242,
        "base_url": "http://127.0.0.1:59999",
        "lock_free": False,
        "budget": daemon_client.VERDICT_BUDGET,
        "probes": 5,
        "successes": 0,
        "why": "canned report",
    }
    report.update(over)
    return report


@contextlib.contextmanager
def _delay_server(delay_for, *, max_stall: float):
    """A real /api/health endpoint whose response timing the test controls.

    ``delay_for(i)`` gives the delay for the i-th request (0-based);
    ``float("inf")`` means never answer -- the handler holds the connection
    for ``max_stall`` seconds and drops it without a response.
    """
    counter = iter(range(10 ** 6))

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (stdlib handler contract)
            delay = delay_for(next(counter))
            try:
                if delay == float("inf"):
                    time.sleep(max_stall)
                    return
                if delay:
                    time.sleep(delay)
                body = b'{"ok": true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except OSError:
                pass  # the probe gave up first; nobody is listening

        def log_message(self, *args):
            pass

    class Server(http.server.ThreadingHTTPServer):
        daemon_threads = True

        def handle_error(self, request, client_address):
            pass  # aborted probes are this suite's normal weather

    server = Server(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
