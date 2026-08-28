"""A daemon-launched probe measures ITS OWN run's checkout, never the daemon's.

Every gate under ``tools/`` asks a question about "this session's branch", and
the way it finds that branch is :func:`cflow.checkout.own_checkout`, which
reads the ambient ``CLAUNCH_SESSION``. In a session that variable is the
session's own name and the answer is right. In the daemon it is ONE name --
whichever terminal started the daemon -- for every run on the machine, and a
probe that inherits it asks the question about that one session's checkout
instead of its own.

That is what happened (issue ``claunch-04ru``). ``run_probe`` called
``subprocess.Popen`` with no ``env=``, so the daemon's environment went
straight through. Four sessions in four different worktrees had probes that
measured the repository root, and the gates' own basis string said ``session``
where it should have said ``run cwd``.

It cost what it cost because it is quiet. A probe pointed at a checkout
standing on ``master`` says "cannot tell", which is visible; one pointed at a
checkout standing on any other branch answers 0, 1 or 3 with full confidence,
and ``improv-worker``'s ``await-landing`` step instructs a worker to act on
exactly those codes. A worker unfreezing on somebody else's branch
measurement produces no error anywhere.

So these tests assert on the one observable that separates the two cases: the
name the probe's own child process sees. Nothing here mocks ``run_probe`` or
``own_checkout`` -- a probe is a real subprocess, and the environment it is
handed is the entire subject.

Three layers, because the defect can return at any one of them and only that
layer goes red:

* :func:`cflow.engine.probe_env` -- the decision itself;
* ``ReminderClock`` -- the ``awaits`` probe, the path that flipped a worker's
  landing signal;
* ``ChecklistClock`` -- the checklist item, the path that held a worker's
  ``landed`` gate shut with an ``exit 2`` no flag could work around;
* ``SessionReminderService`` -- the compatibility surface in front of the
  first of those. It is here because it broke: while this fix was in review
  the reminder clock was split, and the forwarding wrapper left behind kept
  the old argument list and would have dropped the scope on the floor. That
  is the same defect one layer up, and a wrapper is where nobody looks.
"""

from __future__ import annotations

import sys
import textwrap

import pytest

from claude_launcher.cflow import engine as cflow_engine
from claude_launcher.cflow import state as state_mod
from claude_launcher.daemon import cflow_clock
from claude_launcher.daemon import session_reminder
from claude_launcher.daemon.harness import SessionDef

#: The name the daemon is holding. Anything that is not the run's scope would
#: expose the defect; this one is spelled like a real session because that is
#: what made the failure silent -- ``own_checkout`` looked it up and got a
#: real directory back, so the gate answered confidently about the wrong tree.
DAEMON_SESSION = "s127"

#: The run under test. Its scope is what every probe below must report.
RUN_SCOPE = "w1"


class _FakeSession:
    def __init__(self, name: str, cwd: str) -> None:
        self.exited = False
        self.delivered: list = []
        self.sdef = SessionDef(name=name, cwd=cwd)

    def status(self, threshold=None):
        return "busy"

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


def _reporting_probe(where, name: str) -> str:
    """A probe whose whole job is to write down who it thinks it is.

    Its exit code is a function of that name, so the same script also serves
    as an ``awaits`` probe whose code MOVES when the name is wrong -- which
    lets a clock-level test observe the defect through the clock's own
    recorded answer and not only through a file the probe wrote.

    Written as a script rather than a shell one-liner for the reason the
    neighbouring suite gives: quoting, not logic, is what would differ between
    ``cmd`` and ``sh``.
    """
    script = where / f"{name}.py"
    seen = where / f"{name}.txt"
    script.write_text(
        textwrap.dedent(
            f"""
            import os
            import pathlib
            import sys

            who = os.environ.get({state_mod.SESSION_ENV!r}, "<unset>")
            pathlib.Path({str(seen)!r}).write_text(who, encoding="utf-8")
            sys.exit(0 if who == {RUN_SCOPE!r} else 3)
            """
        ),
        encoding="utf-8",
    )
    return f'"{sys.executable}" "{script}"'


def _saw(where, name: str) -> str:
    """The name the probe's child process actually read."""
    return (where / f"{name}.txt").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# the decision
# --------------------------------------------------------------------------- #
def test_probe_env_writes_the_runs_scope_over_whatever_the_daemon_holds(monkeypatch):
    """The run's name wins. The whole fix, in one assertion."""
    monkeypatch.setenv(state_mod.SESSION_ENV, DAEMON_SESSION)
    assert cflow_engine.probe_env(RUN_SCOPE)[state_mod.SESSION_ENV] == RUN_SCOPE


def test_probe_env_removes_the_name_for_an_unmanaged_run(monkeypatch):
    """An unmanaged run has no session to name, and LEAVING the daemon's there
    would be the original defect with an extra step. Absence is exactly what
    ``own_checkout`` reads as "fall back to the run's cwd" -- and for a run
    the clock keyed from the registry, that cwd is the right answer."""
    monkeypatch.setenv(state_mod.SESSION_ENV, DAEMON_SESSION)
    for scope in (state_mod.DEFAULT_SCOPE, None, "", "   "):
        assert state_mod.SESSION_ENV not in cflow_engine.probe_env(scope)


def test_probe_env_carries_the_rest_of_the_environment_through(monkeypatch):
    """One variable is decided here and nothing else is. A probe is an
    arbitrary shell command that may need PATH, a proxy or a token, so
    building a fresh environment instead of editing a copy would break every
    probe on the machine."""
    monkeypatch.setenv("CLAUNCH_TEST_MARKER", "kept")
    monkeypatch.setenv(state_mod.SESSION_ENV, DAEMON_SESSION)
    assert cflow_engine.probe_env(RUN_SCOPE)["CLAUNCH_TEST_MARKER"] == "kept"


def test_run_probe_hands_the_scope_to_the_actual_child_process(tmp_path, monkeypatch):
    """End of the decision, start of the evidence: the child, not the dict."""
    monkeypatch.setenv(state_mod.SESSION_ENV, DAEMON_SESSION)
    probe = _reporting_probe(tmp_path, "direct")
    answer = cflow_engine.run_probe(probe, str(tmp_path), 30.0, scope=RUN_SCOPE)
    assert _saw(tmp_path, "direct") == RUN_SCOPE
    assert answer["code"] == 0


def test_run_probe_cannot_be_called_without_saying_whose_run_it_is():
    """``scope`` has no default, on purpose.

    A default is a door for the daemon's environment to come back through,
    and it would come back silently -- which is the property that made this
    defect expensive rather than annoying. A caller that does not know whose
    run it is fails here instead.
    """
    with pytest.raises(TypeError):
        cflow_engine.run_probe("echo hi", None, 5.0)


# --------------------------------------------------------------------------- #
# the two clocks that run probes
# --------------------------------------------------------------------------- #
AWAITS_FLOW = """
name: waiter
description: wait for a thing
steps:
  hold:
    instructions: wait here
    awaits: {{probe: '{probe}', poll: 15}}
    next: done
  done:
    instructions: finished
"""

CHECKLIST_FLOW = """
name: lander
description: land a branch
steps:
  hold:
    instructions: freeze the branch and wait
    done_when: the merge commit is in the history
    checklist:
      prompt: has this branch actually landed?
      then: done
      poll: 15
      items:
        - id: merged
          describe: a merge commit on the target lists my tip as a parent
          check: '{probe}'
  done:
    instructions: finished
"""


@pytest.fixture
def proj(home, tmp_path, monkeypatch):
    """A project driven the way the daemon drives one.

    ``CLAUNCH_SESSION`` is set to another session's name and left set for the
    whole test: this process stands in for the daemon, and the defect is
    precisely that the daemon's value reaches the child.
    """
    d = tmp_path / "proj"
    (d / ".claunch" / "workflows").mkdir(parents=True)
    monkeypatch.chdir(d)
    monkeypatch.setenv(state_mod.SESSION_ENV, DAEMON_SESSION)
    return d


def _declare(proj, name: str, text: str) -> None:
    (proj / ".claunch" / "workflows" / f"{name}.yaml").write_text(
        text, encoding="utf-8"
    )


def _entry(clock, cwd: str) -> dict:
    """The clock's single tracked position, whatever it keyed it under.

    Looked up this way rather than by ``(cwd, scope)`` because the clock takes
    its key from the registry's canonicalised path, and a test that
    reconstructs that key is testing path normalisation.
    """
    entries = list(clock._seen.values())
    assert len(entries) == 1, clock._seen
    return entries[0]


def test_the_awaits_probe_runs_as_the_run_it_is_measuring(proj, tmp_path):
    """The path that flipped a worker's landing signal.

    The clock reads ``(cwd, scope)`` out of the registry and is the only thing
    in the chain that knows whose run each probe is for. If it does not spell
    that out for the subprocess, nothing downstream can recover it.
    """
    probe = _reporting_probe(tmp_path, "awaits")
    _declare(proj, "waiter", AWAITS_FLOW.format(probe=probe))
    cwd = str(proj)
    cflow_engine.start("waiter", cwd=cwd, scope=RUN_SCOPE)

    clock = cflow_clock.ReminderClock(
        _FakeManager({RUN_SCOPE: _FakeSession(RUN_SCOPE, cwd)})
    )
    clock.scan(1000.0)

    assert _saw(tmp_path, "awaits") == RUN_SCOPE
    assert _entry(clock, cwd)["probe"]["code"] == 0


def test_repeated_scans_of_an_unchanged_run_report_one_code(proj, tmp_path):
    """The observed symptom, pinned as an absence.

    A worker watched its ``await-landing`` probe move 2 -> 1 -> 2 -> 1 over
    three minutes with nothing about its branch changing, and each move was
    delivered to it as news. What made the codes differ between calls was
    never measured, so this test asserts no cause; it asserts the consequence
    the fix is built to guarantee -- that the environment is no longer a
    variable between one call and the next. Every scan reads the run's own
    name, so every scan gets the same code, so there is no signal.
    """
    probe = _reporting_probe(tmp_path, "flap")
    _declare(proj, "waiter", AWAITS_FLOW.format(probe=probe))
    cwd = str(proj)
    cflow_engine.start("waiter", cwd=cwd, scope=RUN_SCOPE)
    clock = cflow_clock.ReminderClock(
        _FakeManager({RUN_SCOPE: _FakeSession(RUN_SCOPE, cwd)})
    )

    codes, signals = [], []
    for i in range(6):
        due = clock.scan(1000.0 + i * 60)
        # asserted inside the loop: a probe that reads the right name once and
        # the daemon's the next time is the failure, and only a per-call check
        # can see it.
        assert _saw(tmp_path, "flap") == RUN_SCOPE
        codes.append(_entry(clock, cwd)["probe"]["code"])
        signals += [d for d in due if d[3] == "signal"]

    assert codes == [0] * 6
    assert signals == []


def test_a_checklist_item_runs_as_the_run_it_is_gating(proj, tmp_path):
    """The path that held a worker's ``landed`` gate shut.

    Worth its own test rather than trusting the one above: the scope reaches
    this probe through ``_scoped_op``'s contextvar and the clock's ``scope=``
    keyword, a different mechanism from the ``awaits`` path. It also failed
    separately in production, and worse -- ``landed_check.py`` takes no
    ``--branch``, so the run had no way to answer the item by hand and could
    only wait for a human ``goto``.
    """
    probe = _reporting_probe(tmp_path, "item")
    _declare(proj, "lander", CHECKLIST_FLOW.format(probe=probe))
    cwd = str(proj)
    cflow_engine.start("lander", cwd=cwd, scope=RUN_SCOPE)

    clock = cflow_clock.ChecklistClock(
        _FakeManager({RUN_SCOPE: _FakeSession(RUN_SCOPE, cwd)})
    )
    clock.scan(now=1000.0)

    assert _saw(tmp_path, "item") == RUN_SCOPE
    items = cflow_engine.status(cwd, scope=RUN_SCOPE)["checklist"]["items"]
    measured = {i["id"]: (i["ok"], i["exit_code"]) for i in items}
    assert measured["merged"] == (True, 0)


# --------------------------------------------------------------------------- #
# the compatibility surface in front of the clock
# --------------------------------------------------------------------------- #
def test_the_reminder_services_wrapper_forwards_the_scope_it_was_given():
    """A forwarding wrapper must forward the scope, not drop it.

    ``SessionReminderService`` wraps :class:`CflowReminderSource` so older
    callers keep working across the reminder split. Its ``_measure`` exists
    only to hand the call through -- which makes it exactly the kind of code
    that gets written from the OLD signature and reviewed as a no-op. It was,
    and for a while the merged tree had a wrapper that would have passed
    ``awaits`` where ``scope`` belongs.

    Nothing in production calls it today (the daemon reaches the source
    directly), so a broken wrapper is quiet until the first caller returns.
    Asserting on the forwarded arguments rather than on a probe's output is
    deliberate: the wrapper's whole contract is what it passes on.
    """
    seen = {}

    class _Source:
        def _measure(self, cwd, scope, awaits, entry, now):
            seen.update(cwd=cwd, scope=scope, awaits=awaits, entry=entry, now=now)
            return {"code": 0, "says": ""}

    service = session_reminder.SessionReminderService.__new__(
        session_reminder.SessionReminderService
    )
    service.cflow = _Source()
    service._measure("C:/somewhere", RUN_SCOPE, {"probe": "x"}, {}, 1000.0)

    assert seen["scope"] == RUN_SCOPE
    assert seen["cwd"] == "C:/somewhere"
    assert seen["awaits"] == {"probe": "x"}
    assert seen["now"] == 1000.0
