"""``awaits``: a step declares what it waits for, and the daemon signals it.

The reminder is a clock. It repeats a step's instructions for as long as the
position does not move, and it has no opinion about whether what the step is
waiting for has arrived — which is how a run ends up quoting a ``verify`` that
went green ten minutes ago. ``awaits`` gives the same clock the condition, and
with it three behaviours that are the whole contract and are pinned here:

* the exit code MOVES  -> one signal, saying what changed, waking even a
  session that ended its turn to wait for it;
* the exit code does NOT move -> **silence**, the clock reminder included.
  That is the point of the feature and the one thing a test must not let
  regress: an unchanged condition is not news;
* the probe cannot be measured -> the ordinary clock reminder comes straight
  back. A broken probe must never be the reason a stalled run goes quiet.

The parser's ceilings are pinned too, because they are what stops "wait until
the suite is green" from being written as "run the suite every minute".
"""

from __future__ import annotations

import asyncio
import sys
import textwrap
import time

import pytest

from claude_launcher import store
from claude_launcher.cflow import engine as cflow_engine
from claude_launcher.cflow import model
from claude_launcher.cflow.model import WorkflowError
from claude_launcher.daemon import cflow_clock
from claude_launcher.daemon.harness import SessionDef


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
class _FakeSession:
    def __init__(self, name: str, cwd: str, status: str = "busy") -> None:
        self.exited = False
        self.sdef = SessionDef(name=name, cwd=cwd)
        self.delivered: list = []
        self.status_value = status

    def status(self, threshold=None):
        return self.status_value

    async def deliver(self, text: str) -> bool:
        self.delivered.append(text)
        return True


class _FakeManager:
    def __init__(self, sessions: dict) -> None:
        self._sessions = sessions

    def get(self, name: str):
        if name not in self._sessions:
            raise KeyError(name)
        return self._sessions[name]


def _probe_script(tmp_path, body: str) -> str:
    """A probe as a real subprocess — the thing the clock actually runs.

    Written as a script rather than a shell one-liner so the same test text
    runs under cmd and sh: quoting, not logic, is what would differ.
    """
    path = tmp_path / "probe.py"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return f'"{sys.executable}" "{path}"'


@pytest.fixture
def proj(home, tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    d = tmp_path / "proj"
    (d / ".claunch" / "workflows").mkdir(parents=True)
    return d


def _declare(proj, name: str, body: str) -> None:
    (proj / ".claunch" / "workflows" / f"{name}.yaml").write_text(
        textwrap.dedent(body), encoding="utf-8"
    )


# --------------------------------------------------------------------------- #
# the syntax
# --------------------------------------------------------------------------- #
def test_the_reserved_word_reuses_the_steps_own_verify():
    wf = model.parse(
        "name: t\n"
        "steps:\n"
        "  one:\n"
        "    instructions: wait\n"
        "    verify: 'check the thing'\n"
        "    awaits: verify\n"
    )
    step = wf.steps["one"]
    assert step.awaits.probe is None            # the reserved spelling
    assert step.awaits.command(step) == "check the thing"
    assert step.awaits.poll == model.DEFAULT_AWAITS_POLL
    assert step.awaits.timeout == model.DEFAULT_AWAITS_TIMEOUT


def test_a_command_is_written_under_probe_and_never_as_a_bare_string():
    """No shorthand, on purpose: one scalar cannot be a keyword sometimes and
    a shell command the rest of the time."""
    wf = model.parse(
        "name: t\n"
        "steps:\n"
        "  one:\n"
        "    instructions: wait\n"
        "    awaits: {probe: 'ask git', poll: 30, describe: the tip moved}\n"
    )
    step = wf.steps["one"]
    assert step.awaits.command(step) == "ask git"
    assert step.awaits.poll == 30
    assert step.awaits.describe == "the tip moved"
    # a bare command string is refused, and the error says how to write it
    with pytest.raises(WorkflowError, match="probe"):
        model.parse(
            "name: t\nsteps:\n  one:\n    instructions: w\n    awaits: 'ask git'\n"
        )


def test_awaits_verify_on_a_step_with_no_verify_is_a_parse_error():
    with pytest.raises(WorkflowError, match="has none"):
        model.parse(
            "name: t\nsteps:\n  one:\n    instructions: w\n    awaits: verify\n"
        )


def test_awaits_verify_on_a_select_step_is_a_parse_error():
    """A select step takes no verify, so there is nothing to re-measure —
    but naming a probe on one is fine: a branch may well wait for something."""
    body = (
        "name: t\n"
        "steps:\n"
        "  pick:\n"
        "    awaits: {}\n"
        "    select:\n"
        "      prompt: which\n"
        "      chooser: agent\n"
        "      options:\n"
        "        a: {description: a}\n"
    )
    with pytest.raises(WorkflowError, match="select step"):
        model.parse(body.replace("awaits: {}", "awaits: verify"))
    wf = model.parse(body.replace("awaits: {}", "awaits: {probe: 'ask git'}"))
    assert wf.steps["pick"].awaits.command(wf.steps["pick"]) == "ask git"


def test_the_ceilings_are_refusals_not_advice():
    """The shape this field invites is "wait until the suite is green", and
    that is the one thing it must not become: a probe is re-run for as long as
    the step sits there. So the limits are parse errors."""
    def parse(awaits: str):
        return model.parse(
            "name: t\nsteps:\n  one:\n    instructions: w\n"
            f"    verify: 'x'\n    awaits: {awaits}\n"
        )

    with pytest.raises(WorkflowError, match="may not exceed"):
        parse("{probe: verify, poll: 600, timeout: 300}")
    with pytest.raises(WorkflowError, match="at least"):
        parse("{probe: verify, poll: 5}")
    with pytest.raises(WorkflowError, match="longer than"):
        parse("{probe: verify, poll: 15, timeout: 20}")
    with pytest.raises(WorkflowError, match="number of seconds"):
        parse("{probe: verify, poll: soon}")
    # and the default timeout never exceeds the poll it sits inside
    assert parse("{probe: verify, poll: 15}").steps["one"].awaits.timeout == 10


def test_a_workflow_without_awaits_is_untouched():
    """The field is absent from every run already in flight — a run reads the
    snapshot it started on — and an absent ``awaits`` must be today's clock."""
    wf = model.parse("name: t\nsteps:\n  one:\n    instructions: w\n")
    assert wf.steps["one"].awaits is None


# --------------------------------------------------------------------------- #
# the payload
# --------------------------------------------------------------------------- #
def test_the_payload_carries_the_resolved_command_and_tells_the_agent_to_stop(proj):
    """The clock reads runs through ``status`` and nothing else, so this dict
    is the entire interface between the workflow file and the thing that acts
    on it — and it must hand over the RESOLVED command, not the spelling."""
    _declare(
        proj,
        "waiting",
        """
        name: waiting
        steps:
          one:
            instructions: wait for it
            verify: 'check the thing'
            awaits: {probe: verify, poll: 30, describe: the daemon restarted}
        """,
    )
    cwd = str(proj)
    payload = cflow_engine.start("waiting", cwd=cwd, scope="w1")
    assert payload["awaits"]["probe"] == "check the thing"
    assert payload["awaits"]["poll"] == 30
    assert payload["awaits"]["describe"] == "the daemon restarted"
    # the instruction that makes the feature pay: end the turn, do not poll
    assert "do not poll it yourself" in payload["awaits"]["note"]
    assert "do not read silence" in payload["awaits"]["note"]


# --------------------------------------------------------------------------- #
# the clock
# --------------------------------------------------------------------------- #
def _marker_flow(proj, tmp_path, poll=30, timeout=5):
    """A run whose step waits for a file to appear."""
    marker = tmp_path / "arrived"
    probe = _probe_script(
        tmp_path,
        f"""
        import os, sys
        here = os.path.exists(r"{marker}")
        print("arrived" if here else "not yet")
        sys.exit(0 if here else 1)
        """,
    )
    _declare(
        proj,
        "waiting",
        f"""
        name: waiting
        steps:
          one:
            instructions: wait for the marker
            awaits:
              probe: '{probe}'
              poll: {poll}
              timeout: {timeout}
              describe: the marker file appeared
        """,
    )
    cflow_engine.start("waiting", cwd=str(proj), scope="w1")
    return marker


def test_it_signals_once_on_the_change_and_says_nothing_the_rest_of_the_time(
    proj, tmp_path
):
    """The contract in one test: baseline silent, unchanged silent (the clock
    reminder included), the change spoken once, then silent again."""
    cwd = str(proj)
    marker = _marker_flow(proj, tmp_path)
    clock = cflow_clock.ReminderClock(_FakeManager({}))
    t = 1000.0

    assert clock.scan(t) == []                      # baseline: never news
    assert clock.scan(t + 100) == []                # re-measured, unchanged
    # ...and well past the 600s clock reminder, still nothing. This assertion
    # IS the feature: today, without awaits, this is where the step would be
    # repeated verbatim for the third time.
    assert clock.scan(t + 700) == []

    marker.write_text("here", encoding="utf-8")
    due = clock.scan(t + 800)
    assert len(due) == 1
    cwd_out, scope, block, kind = due[0]
    assert (cwd_out, scope, kind) == (cwd, "w1", "signal")
    assert "changed: exit 1 -> exit 0" in block
    assert "awaiting: the marker file appeared" in block
    assert "probe said:" in block and "arrived" in block
    # it is NOT the step restated -- that is the reminder's job, not this one
    assert "wait for the marker" not in block

    # and it fires once: the new code is the baseline from here on
    assert clock.scan(t + 900) == []
    assert clock.scan(t + 2000) == []


def test_output_is_evidence_and_is_not_what_counts_as_a_change(proj, tmp_path):
    """A probe free to print a timestamp would otherwise "change" on every
    sample. The exit code is the fact; the output only rides along."""
    counter = tmp_path / "n"
    probe = _probe_script(
        tmp_path,
        f"""
        import sys
        from pathlib import Path
        p = Path(r"{counter}")
        n = int(p.read_text()) + 1 if p.exists() else 1
        p.write_text(str(n))
        print("sample", n)
        sys.exit(3)
        """,
    )
    _declare(
        proj,
        "waiting",
        f"""
        name: waiting
        steps:
          one:
            instructions: wait
            awaits: {{probe: '{probe}', poll: 30, timeout: 5}}
        """,
    )
    cflow_engine.start("waiting", cwd=str(proj), scope="w1")
    clock = cflow_clock.ReminderClock(_FakeManager({}))
    assert clock.scan(1000.0) == []
    assert clock.scan(1100.0) == []
    assert clock.scan(1200.0) == []
    assert int(counter.read_text()) == 3            # it really did re-measure


def test_a_probe_is_not_re_run_inside_its_own_poll_interval(proj, tmp_path):
    """Sampling, not hammering: the clock ticks every 15s and a probe with a
    30s poll must skip the ticks in between — and stay quiet while it does."""
    counter = tmp_path / "n"
    probe = _probe_script(
        tmp_path,
        f"""
        import sys
        from pathlib import Path
        p = Path(r"{counter}")
        p.write_text(str(int(p.read_text()) + 1 if p.exists() else 1))
        sys.exit(1)
        """,
    )
    _declare(
        proj,
        "waiting",
        f"""
        name: waiting
        steps:
          one:
            instructions: wait
            awaits: {{probe: '{probe}', poll: 30, timeout: 5}}
        """,
    )
    cflow_engine.start("waiting", cwd=str(proj), scope="w1")
    clock = cflow_clock.ReminderClock(_FakeManager({}))
    for tick in (1000.0, 1015.0, 1029.0):
        assert clock.scan(tick) == []
    assert int(counter.read_text()) == 1            # one sample, three ticks
    assert clock.scan(1031.0) == []
    assert int(counter.read_text()) == 2


def test_an_unmeasurable_probe_falls_back_to_the_clock_reminder(proj, tmp_path):
    """The negative control. A probe that cannot answer is NOT the answer "not
    yet" — so the ordinary reminder resumes. Silence is only ever granted while
    something is actually watching."""
    probe = _probe_script(
        tmp_path,
        """
        import time
        time.sleep(30)
        """,
    )
    _declare(
        proj,
        "waiting",
        f"""
        name: waiting
        steps:
          one:
            instructions: wait for the slow thing
            awaits: {{probe: '{probe}', poll: 15, timeout: 1}}
        """,
    )
    cwd = str(proj)
    cflow_engine.start("waiting", cwd=cwd, scope="w1")
    clock = cflow_clock.ReminderClock(_FakeManager({}))
    t = 1000.0
    assert clock.scan(t) == []                      # arrival still arms only
    due = clock.scan(t + 601)                       # the default clock interval
    assert len(due) == 1
    _, _, block, kind = due[0]
    assert kind == "reminder"
    assert "wait for the slow thing" in block       # today's behaviour, intact


def test_a_probe_that_breaks_after_measuring_still_reports_the_move_it_missed(
    proj, tmp_path
):
    """A gap in the watching is not a reason to swallow a change: the next
    answer is compared against the last one, not against nothing."""
    marker = tmp_path / "arrived"
    broken = tmp_path / "broken"
    probe = _probe_script(
        tmp_path,
        f"""
        import os, sys, time
        if os.path.exists(r"{broken}"):
            time.sleep(30)
        sys.exit(0 if os.path.exists(r"{marker}") else 1)
        """,
    )
    _declare(
        proj,
        "waiting",
        f"""
        name: waiting
        steps:
          one:
            instructions: wait
            awaits: {{probe: '{probe}', poll: 30, timeout: 1}}
        """,
    )
    cflow_engine.start("waiting", cwd=str(proj), scope="w1")
    clock = cflow_clock.ReminderClock(_FakeManager({}))
    assert clock.scan(1000.0) == []                 # baseline: exit 1
    broken.write_text("x", encoding="utf-8")        # the probe stops answering
    assert clock.scan(1100.0) == []                 # unmeasurable, clock not due
    marker.write_text("x", encoding="utf-8")        # the thing arrives, unseen
    broken.unlink()                                 # the probe recovers
    due = clock.scan(1200.0)
    assert len(due) == 1 and due[0][3] == "signal"
    assert "changed: exit 1 -> exit 0" in due[0][2]


def test_a_signal_wakes_an_idle_session_where_a_reminder_would_wait(proj, tmp_path):
    """The delivery rules are not the same for the two, and the difference is
    not a preference: a reminder restates what the agent has, a signal carries
    what it does not — about the very thing it ended its turn to wait for."""
    cwd = str(proj)
    marker = _marker_flow(proj, tmp_path)
    sess = _FakeSession("w1", cwd, status="idle")   # the turn is over
    clock = cflow_clock.ReminderClock(_FakeManager({"w1": sess}))
    clock.scan(1000.0)
    marker.write_text("here", encoding="utf-8")
    due = clock.scan(1100.0)
    assert len(due) == 1
    asyncio.run(clock._deliver(*due[0]))
    assert len(sess.delivered) == 1                 # woken, not held
    assert "# claunch cflow: signal" in sess.delivered[0]

    # the same session, the same idleness, a reminder: held instead
    asyncio.run(clock._deliver(cwd, "w1", "a reminder block", "reminder"))
    assert len(sess.delivered) == 1


def test_moving_the_run_re_baselines_instead_of_signalling(proj, tmp_path):
    """A new position starts a new question. Carrying the previous step's
    measurement across would fire a signal about something the agent left."""
    cwd = str(proj)
    marker = tmp_path / "arrived"
    probe = _probe_script(
        tmp_path,
        f"""
        import os, sys
        sys.exit(0 if os.path.exists(r"{marker}") else 1)
        """,
    )
    _declare(
        proj,
        "waiting",
        f"""
        name: waiting
        steps:
          one:
            instructions: first
            awaits: {{probe: '{probe}', poll: 30, timeout: 5}}
            next: two
          two:
            instructions: second
            awaits: {{probe: '{probe}', poll: 30, timeout: 5}}
        """,
    )
    cflow_engine.start("waiting", cwd=cwd, scope="w1")
    clock = cflow_clock.ReminderClock(_FakeManager({}))
    assert clock.scan(1000.0) == []                 # baseline at step one: exit 1
    marker.write_text("here", encoding="utf-8")
    cflow_engine.report("did one", cwd=cwd, scope="w1")
    cflow_engine.next_step(cwd=cwd, scope="w1")     # the run moves
    assert clock.scan(1100.0) == []                 # new position, new baseline
    assert clock.scan(1200.0) == []                 # and it has not changed


def test_reminders_off_but_awaits_declared_leaves_a_clock_that_says_only_news(
    proj, tmp_path
):
    """``enabled`` is the master switch for everything this clock says. A zero
    interval turns off only the clock half — which is how a run asks for
    signals and nothing else."""
    cwd = str(proj)
    marker = _marker_flow(proj, tmp_path)
    clock = cflow_clock.ReminderClock(_FakeManager({}))
    # a run override cannot say 0 (the engine floors it), so this is the
    # machine default's door -- the one place "no clock at all" is spelled
    store.set_daemon_field("cflow_reminder_interval", 0)
    assert clock.scan(1000.0) == []
    assert clock.scan(1700.0) == []                 # no clock reminder at all
    marker.write_text("here", encoding="utf-8")
    due = clock.scan(1800.0)
    assert len(due) == 1 and due[0][3] == "signal"  # ...but news still lands

    # and `enabled: false` silences the signal too: it is one switch
    cflow_engine.set_reminder(False, None, cwd=cwd, scope="w1")
    marker.unlink()
    assert clock.scan(1900.0) == []
    assert clock.scan(2000.0) == []


def test_a_hung_probe_is_cut_at_its_timeout_and_not_at_its_own_pace(tmp_path):
    """The ceiling has to actually cut, or it is decoration.

    ``subprocess.run(timeout=...)`` is not enough with ``shell=True``: killing
    the shell leaves the grandchild holding the pipes, so the read after the
    kill blocks until the real process finishes on its own — a ten-second cap
    that waits five minutes. What that would mean here is a workflow able to
    hold the daemon's clock thread open with a slow probe, which is precisely
    the thing ``awaits``'s limits exist to make impossible. So the elapsed
    time, not the return value, is the assertion.
    """
    probe = _probe_script(
        tmp_path,
        """
        import time
        time.sleep(60)
        """,
    )
    started = time.monotonic()
    assert cflow_engine.run_probe(probe, str(tmp_path), 1.0) is None
    assert time.monotonic() - started < 20     # generous; the bug took 60s
