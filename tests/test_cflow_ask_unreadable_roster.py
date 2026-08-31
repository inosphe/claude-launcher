"""A busy daemon must not settle a delegated decision.

Measured (issue ``claunch-ojci``, run ``run-ce9df3c8`` of session s398, journal
event ``ask_unresolved`` at 2026-08-31T09:36:10+00:00): an ``errand`` run's
``end-gate`` opened with ``group: 2``, ``asked: []`` and ``deadline: null`` --
past both agent groups and in front of a person -- and the two ``skipped``
reasons it recorded were not about roles at all::

    worker (ancestor): the claunch daemon did not answer (3 probe(s) over
    3.0s) -- it may be busy; daemon.json says pid 52404 up since ...
    leader (ancestor): ... the same sentence again

Five other runs on the same mesh in the same hour resolved group 1 to the
leader and were answered normally, so nothing about the roster had changed:
the daemon was alive (that pid served the dashboard two minutes later) and had
simply not answered inside :data:`daemon_client.OBSERVATION_BUDGET`.

The defect is a category error, and it is the same one
:func:`daemon_client.unreachable_reason` exists to refuse one layer down --
"announced, alive, and did not answer" must never be phrased as an absence.
:func:`responders.pool` returned a pool with ``problem`` set, ``_open_ask``
read that as every group failing to match, and the ask ran out of candidates.
Running out of candidates is irreversible: it hands the question to
``otherwise``, and under ``otherwise: human`` the result is an ask with no
responder and no deadline, which no agent can move and the daemon's clock
never looks at again. The leader could not ``answer`` it; a person had to.

These tests pin the distinction. A roster that says *nobody holds that role*
is an answer and still spends the groups. A roster that could not be read is
not an answer, and the question waits for one.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from claude_launcher import daemon_client
from claude_launcher.cflow import engine as cflow_engine
from claude_launcher.cflow import responders

# Two agent groups then a person -- the shape ``errand``'s ``end-gate``
# declares, which is the step the defect was measured on.
TWO_GROUPS = """
name: shipit
steps:
  impl:
    instructions: implement the thing
    next: ship
  ship:
    select:
      prompt: which way?
      chooser:
        from:
          - {role: worker, scope: ancestor}
          - {role: leader, scope: ancestor}
        otherwise: human
        timeout: 900
      options:
        go:
          description: ship it
          next: end
        back:
          description: think again
          next: impl
    instructions: decide
"""


@pytest.fixture
def proj(home, tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    d = tmp_path / "proj"
    (d / ".claunch" / "workflows").mkdir(parents=True)
    (d / ".claunch" / "workflows" / "shipit.yaml").write_text(
        TWO_GROUPS, encoding="utf-8"
    )
    return d


def _unresponsive(monkeypatch):
    """The daemon s398 met: announced, pid alive, answering nothing."""
    report = {
        "state": daemon_client.UNRESPONSIVE,
        "pid": 52404,
        "started_at": "2026-08-31T08:30:24+00:00",
        "probes": 3,
        "budget": daemon_client.OBSERVATION_BUDGET,
    }
    monkeypatch.setattr(
        responders.daemon_client,
        "connect_with_diagnosis",
        lambda *a, **k: (None, report),
    )
    return report


def _absent(monkeypatch):
    """No daemon at all -- the settled case, decided from the record."""
    report = {"state": daemon_client.NOT_RUNNING, "probes": 0, "budget": 0.0}
    monkeypatch.setattr(
        responders.daemon_client,
        "connect_with_diagnosis",
        lambda *a, **k: (None, report),
    )
    return report


def _a_leader_is_reachable(monkeypatch):
    """A roster that reads, and holds a leader above this run."""
    monkeypatch.setattr(
        responders,
        "pool",
        lambda **k: responders.Pool(
            mesh="team",
            me="dev1",
            me_role="worker",
            members={
                "boss": responders.Responder(
                    session="lead1",
                    handle="boss",
                    role="leader",
                    local=True,
                    reachability="idle",
                )
            },
            reachable={"boss"},
            ancestors=["boss"],
        ),
    )
    monkeypatch.setattr(responders, "deliver", lambda *a, **k: [])


def _onto_the_ask(cwd: str) -> dict:
    cflow_engine.start("shipit", cwd=cwd, scope="w1")
    cflow_engine.report("done", cwd=cwd, scope="w1")
    return cflow_engine.next_step(cwd=cwd, scope="w1")


def _later(cwd: str):
    """One tick of the daemon's clock, well past any deadline."""
    return cflow_engine.expire_ask(
        now=datetime.now(timezone.utc) + timedelta(hours=1), cwd=cwd, scope="w1"
    )


def _ask(cwd: str) -> dict:
    return cflow_engine.status(cwd, scope="w1")["ask"]


# --------------------------------------------------------------------------- #
# the pool: which problems are answers
# --------------------------------------------------------------------------- #
def test_a_silent_daemon_makes_the_roster_unreadable(proj, monkeypatch):
    _unresponsive(monkeypatch)
    got = responders.pool(session="w1", cwd=str(proj))
    assert got.problem  # still says why, for whoever reads the ask
    assert got.unreadable is True


def test_no_daemon_at_all_is_a_settled_answer(proj, monkeypatch):
    _absent(monkeypatch)
    got = responders.pool(session="w1", cwd=str(proj))
    assert got.problem
    assert got.unreadable is False


def test_a_run_with_no_mesh_identity_is_a_settled_answer(proj):
    got = responders.pool(session="", cwd=str(proj))
    assert got.problem
    assert got.unreadable is False


# --------------------------------------------------------------------------- #
# the ask: an unreadable roster spends no group
# --------------------------------------------------------------------------- #
def test_a_busy_daemon_does_not_hand_the_decision_to_a_person(proj, monkeypatch):
    """The measured failure, as a test: group 2, nobody asked, no deadline."""
    _unresponsive(monkeypatch)
    cwd = str(proj)
    _onto_the_ask(cwd)
    ask = _ask(cwd)
    assert ask["asked"] == []
    assert ask["group"] == 0, "the agent groups must still be untried"
    assert ask["deadline"], "the clock must have something to come back to"
    assert ask["deferred"]["count"] == 1
    assert "did not answer" in ask["deferred"]["reason"]


def test_the_clock_retries_the_same_group_and_routes_once_it_can(
    proj, monkeypatch
):
    """Recovery: the daemon answers on the retry and the leader is asked."""
    _unresponsive(monkeypatch)
    cwd = str(proj)
    _onto_the_ask(cwd)
    was = _ask(cwd)["id"]

    _a_leader_is_reachable(monkeypatch)
    assert _later(cwd) is not None
    ask = _ask(cwd)
    assert ask["id"] == was, "the same decision, not a new one"
    assert [e["handle"] for e in ask["asked"]] == ["boss"]
    assert ask["group"] == 1, "group 0 was tried and genuinely held nobody"
    assert "deferred" not in ask


def test_deferral_is_bounded_so_a_silent_daemon_still_reaches_a_person(
    proj, monkeypatch
):
    _unresponsive(monkeypatch)
    cwd = str(proj)
    _onto_the_ask(cwd)
    for _ in range(cflow_engine.MAX_ROSTER_DEFERRALS + 1):
        _later(cwd)
    ask = _ask(cwd)
    assert ask["asked"] == []
    assert ask["group"] == 2, "the groups are spent once patience runs out"
    assert ask["deadline"] is None
    assert any("did not answer" in s["reason"] for s in ask["skipped"])


def test_a_roster_that_answers_nobody_still_spends_the_groups(proj, monkeypatch):
    """The behaviour this must not change: a read roster holding no such role
    is an answer, and the question goes to the person immediately."""
    monkeypatch.setattr(
        responders,
        "pool",
        lambda **k: responders.Pool(mesh="team", me="dev1", me_role="worker"),
    )
    cwd = str(proj)
    _onto_the_ask(cwd)
    ask = _ask(cwd)
    assert ask["asked"] == []
    assert ask["group"] == 2
    assert ask["deadline"] is None
    assert "deferred" not in ask
