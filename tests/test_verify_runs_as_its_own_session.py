"""A step's verify command knows whose round it is gating.

Sibling of ``test_probe_runs_as_its_own_session.py`` and the same class of
defect, arrived at from the other side. Both a probe and a verify are
subprocesses that a gate under ``tools/`` uses to ask a question about a
session -- "did MY branch land", "is MY report filed" -- and both used to take
that identity from whatever environment happened to be around them. The probe
inherited the daemon's and answered confidently about somebody else's checkout
(``claunch-04ru``, fixed by :func:`engine.probe_env`). The verify inherited its
caller's, which is right exactly while the caller has one.

It does not always. A run's scope is read from the ambient ``CLAUNCH_SESSION``
at ``start``, so a run begun by a process without it is keyed to
``default`` -- a real, drivable run carrying no session identity at all -- and
the verify it launches inherits that same emptiness. Measured on this machine
(issue ``claunch-d7qp``), from the runs' own journals::

    .claude/worktrees/s127-s390-.../.cflow/runs/default/journal.jsonl
      2026-08-31T09:04:24  verify_failed  step wrapup  exit_code 2
        error: no session: run this inside a claunch session
        (CLAUNCH_SESSION is set there) or name one with --session <name>

The same line is in two more worktrees (s305 once, s362 three times), always
under ``runs/default``, never under a session-named slot. s362's round was
blocked on it until a person moved the run by hand.

So the identity is written in rather than inherited: the run's own scope where
it has one, and where it does not, the single live session the daemon says
stands in this checkout. What is asserted here is the name the verify's own
child process actually sees, because that is the only observable that tells
the two apart.
"""

from __future__ import annotations

import sys
import textwrap

import pytest

from claude_launcher.cflow import checkout
from claude_launcher.cflow import engine as cflow_engine
from claude_launcher.cflow import state as state_mod

#: A name that is not the run's. Spelled like a real session because that is
#: what makes the failure quiet: a lookup on it succeeds and answers about the
#: wrong round rather than erroring.
STRANGER = "s127"
RUN_SCOPE = "w1"

FLOW = """
name: gated
steps:
  build:
    instructions: build the thing
    verify:
      command: "{command}"
      timeout: 60
"""


def _reporting_verify(tmp_path, tag: str) -> str:
    """A verify command that writes down the session name it was handed."""
    script = tmp_path / f"saw_{tag}.py"
    script.write_text(
        textwrap.dedent(
            f"""
            import os, pathlib
            pathlib.Path(r"{tmp_path / f'{tag}.txt'}").write_text(
                os.environ.get("CLAUNCH_SESSION", "<unset>"), encoding="utf-8"
            )
            """
        ),
        encoding="utf-8",
    )
    return f'"{sys.executable}" "{script}"'


def _saw(tmp_path, tag: str) -> str:
    return (tmp_path / f"{tag}.txt").read_text(encoding="utf-8").strip()


@pytest.fixture
def proj(home, tmp_path, monkeypatch):
    def build(command: str):
        d = tmp_path / "proj"
        (d / ".claunch" / "workflows").mkdir(parents=True, exist_ok=True)
        (d / ".claunch" / "workflows" / "gated.yaml").write_text(
            FLOW.format(command=command.replace("\\", "\\\\").replace('"', '\\"')),
            encoding="utf-8",
        )
        return d

    return build


def _drive(cwd: str, scope: str) -> dict:
    cflow_engine.start("gated", cwd=cwd, scope=scope)
    cflow_engine.report("built", cwd=cwd, scope=scope)
    return cflow_engine.next_step(cwd=cwd, scope=scope)


# --------------------------------------------------------------------------- #
# the decision
# --------------------------------------------------------------------------- #
def test_the_runs_own_scope_wins_over_the_ambient_name(monkeypatch, tmp_path):
    monkeypatch.setenv(state_mod.SESSION_ENV, STRANGER)
    token = state_mod.push_scope(RUN_SCOPE)
    try:
        assert cflow_engine.verify_scope(str(tmp_path)) == RUN_SCOPE
    finally:
        state_mod.pop_scope(token)


def test_a_scopeless_run_asks_the_daemon_who_stands_here(monkeypatch, tmp_path):
    monkeypatch.delenv(state_mod.SESSION_ENV, raising=False)
    monkeypatch.setattr(checkout, "occupant", lambda cwd=None: "s390")
    assert cflow_engine.verify_scope(str(tmp_path)) == "s390"


def test_nothing_is_invented_when_nobody_can_say(monkeypatch, tmp_path):
    monkeypatch.delenv(state_mod.SESSION_ENV, raising=False)
    monkeypatch.setattr(checkout, "occupant", lambda cwd=None: "")
    assert cflow_engine.verify_scope(str(tmp_path)) == ""


# --------------------------------------------------------------------------- #
# occupant: one, or none
# --------------------------------------------------------------------------- #
class _Client:
    def __init__(self, rows):
        self._rows = rows

    def get(self, path, timeout=None):
        return {"sessions": self._rows}


def _daemon(monkeypatch, rows):
    monkeypatch.setattr(checkout.daemon_client, "connect", lambda: _Client(rows))


def test_occupant_names_the_one_session_standing_here(monkeypatch, tmp_path):
    here = str(tmp_path)
    _daemon(
        monkeypatch,
        [
            {"name": "s390", "cwd": here},
            {"name": "s127", "cwd": str(tmp_path.parent)},
        ],
    )
    assert checkout.occupant(here) == "s390"


def test_occupant_declines_to_guess_between_two(monkeypatch, tmp_path):
    here = str(tmp_path)
    _daemon(
        monkeypatch,
        [{"name": "s390", "cwd": here}, {"name": "s391", "cwd": here}],
    )
    assert checkout.occupant(here) == ""


def test_occupant_ignores_a_session_that_has_exited(monkeypatch, tmp_path):
    here = str(tmp_path)
    _daemon(
        monkeypatch,
        [
            {"name": "s390", "cwd": here},
            {"name": "s391", "cwd": here, "exited": True},
        ],
    )
    assert checkout.occupant(here) == "s390"


def test_occupant_is_empty_without_a_daemon(monkeypatch, tmp_path):
    monkeypatch.setattr(checkout.daemon_client, "connect", lambda: None)
    assert checkout.occupant(str(tmp_path)) == ""


# --------------------------------------------------------------------------- #
# the child process, which is the whole subject
# --------------------------------------------------------------------------- #
def test_the_verify_child_is_handed_the_runs_own_session(
    proj, monkeypatch, tmp_path
):
    """The ambient name is a stranger's; the child must not see it."""
    monkeypatch.setenv(state_mod.SESSION_ENV, STRANGER)
    d = proj(_reporting_verify(tmp_path, "scoped"))
    payload = _drive(str(d), RUN_SCOPE)
    assert payload.get("status") != "verify_failed", payload
    assert _saw(tmp_path, "scoped") == RUN_SCOPE


def test_a_default_scope_run_is_given_the_session_standing_in_its_checkout(
    proj, monkeypatch, tmp_path
):
    """The measured failure, as a test.

    Before this fix the child saw ``<unset>`` -- which is exactly what
    ``tools/report_check.py`` reported as ``exit 2, error: no session`` in
    three worktrees.
    """
    monkeypatch.delenv(state_mod.SESSION_ENV, raising=False)
    monkeypatch.setattr(checkout, "occupant", lambda cwd=None: "s390")
    d = proj(_reporting_verify(tmp_path, "adopted"))
    payload = _drive(str(d), state_mod.DEFAULT_SCOPE)
    assert payload.get("status") != "verify_failed", payload
    assert _saw(tmp_path, "adopted") == "s390"


def test_the_rest_of_the_environment_still_reaches_the_verify(
    proj, monkeypatch, tmp_path
):
    """A verify is an arbitrary shell command: one variable is decided here
    and nothing else is."""
    monkeypatch.setenv("CLAUNCH_TEST_MARKER", "kept")
    monkeypatch.setenv(state_mod.SESSION_ENV, STRANGER)
    script = tmp_path / "marker.py"
    script.write_text(
        textwrap.dedent(
            f"""
            import os, pathlib, sys
            pathlib.Path(r"{tmp_path / 'marker.txt'}").write_text(
                os.environ.get("CLAUNCH_TEST_MARKER", "<unset>"), encoding="utf-8"
            )
            """
        ),
        encoding="utf-8",
    )
    d = proj(f'"{sys.executable}" "{script}"')
    _drive(str(d), RUN_SCOPE)
    assert (tmp_path / "marker.txt").read_text(encoding="utf-8").strip() == "kept"


def test_run_verify_cannot_be_called_without_saying_whose_run_it_is():
    """No default, for the reason ``run_probe`` has none: a default is a door
    the ambient environment walks back through, silently."""
    with pytest.raises(TypeError):
        cflow_engine._run_verify(object(), None)
