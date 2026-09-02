"""Both ways out of the worker's ending, driven through the engine.

``improv-worker`` now puts its own ending to the user: ``end-gate`` asks,
an approval falls through to ``end``, and a refusal routes to
``end-hold``. Ending is not a formality here — the daemon reaps a finished
one-shot run's session on sight of ``done`` — so each path needs something
that enforces it, and the two are enforced by different machinery:

* the **approval** path by the engine, which will not deliver a gated step's
  work or accept a report for it until the ask is answered
* the **refusal** path by ``end-hold``'s ``verify``, which asks the daemon
  whether ``keep_alive`` is actually set

That split is why this module exists next to the layer census in
``test_cflow_layers.py``: that one reads the declaration, this one runs it.
The gate this file was written for shipped once with a refusal path that had
no enforcement at all — the step told the agent to run ``claunch keep-alive``
and nothing checked that it did, so a refused ending ended the session
anyway. A test asserting the command string appears in ``instructions`` was
what passed at the time; it is not evidence, and neither is prose.

What each half measures, stated so the seam is visible:

* the approval half runs THIS repository's ``improv-worker`` file, so the
  step ids, the ask and its routing are the real ones
* the refusal half runs the real step with a stand-in verify command, because
  the real one shells out to ``uv`` and a live daemon. That the real command
  is the keep-alive probe is pinned in ``test_project_layer_override.py``
  (the ``GATES`` table), and what that probe answers in each situation is
  pinned in ``test_keepalive_check.py``. The chain is three links and each
  one is measured.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from claude_launcher.cflow import engine, state as state_mod
from claude_launcher.cflow.engine import CflowError

# The roster fake and the driving-session helper are the ones the engine's own
# delegation tests use. Reimplementing them here would be a second answer to
# "who may be asked", and the point of this module is to exercise the real one.
from test_cflow import _driving_session, _mesh  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / ".claunch" / "workflows" / "improv-worker.yaml"

#: Stand-ins for the probe's two answers, written as the yaml line they
#: replace. Neither needs a shell quote of its own: the failing one is git
#: asked for a ref in a directory that is not a repository, the passing one
#: is git reporting its own version.
FAILING_VERIFY = "verify: 'git rev-parse --verify no-such-ref-s231'"
PASSING_VERIFY = "verify: 'git --version'"


@pytest.fixture
def worker_run(home, tmp_path, monkeypatch):
    """This repository's worker workflow, in a throwaway project.

    A parent worker stands above the driver to prove that the ending gate
    ignores session roles and waits for a user.
    """
    proj = tmp_path / "proj"
    wf = proj / ".claunch" / "workflows"
    wf.mkdir(parents=True)
    shutil.copy(WORKER, wf / "improv-worker.yaml")
    monkeypatch.chdir(proj)
    _driving_session(monkeypatch)
    _mesh(monkeypatch, ("worker", "boss"), parent="worker-boss")
    engine.start("improv-worker")
    return proj


def _at(step_id):
    """Put the run on a step and take whatever the engine hands back."""
    engine.goto(step_id)
    return engine.status()


def test_the_ending_holds_for_user_approval(worker_run):
    """The approval path's enforcement is the engine, not the agent's restraint.

    A read-only look at the step before anyone opened the ask describes it
    honestly (``waiting_answer`` / ``approval``, not yet put to anyone). The
    agent's ``next`` opens it, and with no session candidates it falls to a
    human at once: ``waiting_approval`` with nobody asked and no deadline. The
    step's instructions are withheld and a report is refused — so an agent
    that calls ``next`` at ``end-gate`` gets the same wall rather than
    reaching ``end``, which is the kill.
    """
    payload = _at("end-gate")
    assert payload["status"] == "waiting_answer"
    assert payload["reason"] == "approval"
    assert "instructions" not in payload

    payload = engine.next_step()
    assert payload["status"] == "waiting_approval"
    assert payload["reason"] == "ask"
    assert payload["ask"]["asked"] == []
    assert payload["ask"]["deadline"] is None
    assert "instructions" not in payload

    assert engine.next_step()["status"] == "waiting_approval"
    with pytest.raises(CflowError, match="not been delivered"):
        engine.report("the ending is fine, surely")


def test_user_approval_opens_the_ending(worker_run):
    """Only a user's approval can open the ending."""
    _at("end-gate")
    ask_id = engine.next_step()["ask"]["id"]
    with pytest.raises(CflowError, match="was not asked this"):
        engine.answer(ask_id, "approve", by_session="boss")
    engine.approve(by="user")
    payload = engine.next_step()
    assert payload["status"] == "step" and payload["step_id"] == "end-gate"


def test_queue_recheck_loops_the_run_back_to_intake(worker_run):
    """``queue-recheck``'s ``next-round`` is a second round of the SAME run.

    The engine is what makes a loop a loop: intake comes back as visit 2
    (the per-step counter that ``max_visits`` guards), on the same run id,
    with no gate between wrapup and the new round. A worker that read
    "loop back to intake" as "start a new run" would leave this run parked
    at queue-recheck forever -- the daemon sees neither done nor progress.
    """
    before = engine.status()["run"]
    payload = _at("queue-recheck")
    assert payload["status"] == "select"
    assert {o["name"] for o in payload["options"]} == {"next-round", "done"}
    with pytest.raises(CflowError, match="requires a reason"):
        engine.select("next-round")
    payload = engine.select("next-round", "queue: claunch-x1 open, assignable")
    assert payload["status"] == "step"
    assert payload["step_id"] == "intake"
    assert payload["visit"] == 2
    assert payload["run"] == before


def test_queue_recheck_done_reaches_the_user_gate(worker_run):
    """``done`` is the only road to ``end-gate`` -- and it is still a gate.

    Draining the queue does not end the session by itself: the step after
    ``done`` is the ask the user answers, withheld until they do.
    """
    _at("queue-recheck")
    payload = engine.select("done", "queue empty: claunch beads list returned 0 rows")
    assert payload["step_id"] == "end-gate"
    # arriving by a selection opens the ask at once: put to nobody but a
    # user, with no deadline, and the step's work withheld
    assert payload["status"] == "waiting_approval"
    assert payload["reason"] == "ask"
    assert payload["ask"]["asked"] == []
    assert payload["ask"]["deadline"] is None
    assert "instructions" not in payload


def _run_with_verify(proj, monkeypatch, replacement):
    """Start a run whose ``end-hold`` carries ``replacement`` as its verify.

    The substitution happens BEFORE ``start`` because a run snapshots its
    workflow: editing the file afterwards changes nothing for the run already
    going, which is the same property that decides which generation of a file
    a promoted run would carry.
    """
    wf = proj / ".claunch" / "workflows"
    wf.mkdir(parents=True)
    text = WORKER.read_text(encoding="utf-8")
    real = "verify: 'uv run --no-sync python tools/keepalive_check.py'"
    assert text.count(real) == 1, "end-hold's verify is not where this expects it"
    (wf / "improv-worker.yaml").write_text(
        text.replace(real, replacement), encoding="utf-8"
    )
    monkeypatch.chdir(proj)
    _driving_session(monkeypatch)
    _mesh(monkeypatch, ("worker", "boss"), parent="worker-boss")
    engine.start("improv-worker")


def test_the_hold_will_not_finish_while_its_check_says_no(home, tmp_path, monkeypatch):
    """The refusal path's enforcement is an exit code, not the step's prose.

    ``end-hold``'s whole job is that the session survives the run's close, and
    the only thing that makes it survive is the ``keep_alive`` flag. So the
    step does not get to finish on its own account of having set it.
    """
    _run_with_verify(tmp_path / "no", monkeypatch, FAILING_VERIFY)
    _at("end-hold")
    engine.next_step()
    engine.report("keep-alive를 세웠다고 적어 두었다")
    assert engine.next_step()["status"] == "verify_failed", (
        "the hold finished on the agent's word; the run reaches done and the "
        "daemon ends the session the refusal was meant to save"
    )


def test_the_hold_finishes_once_its_check_says_yes(home, tmp_path, monkeypatch):
    """The other direction, so the gate is a condition rather than a wall."""
    _run_with_verify(tmp_path / "yes", monkeypatch, PASSING_VERIFY)
    _at("end-hold")
    engine.next_step()
    engine.report("플래그를 세웠다")
    assert engine.next_step()["status"] == "done"
