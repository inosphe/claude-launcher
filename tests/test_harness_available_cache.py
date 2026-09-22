"""Asking whether a harness is installed must not walk PATH every time.

``Harness.available`` called ``shutil.which`` on each ask, and ``/api/profiles``
asks it once per declared harness -- seven on this machine -- while the
profiles page and the session-creation form both read that endpoint. On
Windows ``which`` stats each PATH entry once per ``PATHEXT`` suffix, so one
answer is dozens of filesystem calls on the event loop that also pumps the
terminals. Measured on the live daemon (s586, 2026-09-22): the registry cost
18.5 ms per request, and a py-spy sample put 15.9% of the loop thread's
samples under ``h_profiles -> available -> which -> _access_check``.

The answer is a property of the machine, so it is remembered. A negative is
kept only briefly: a harness installed beside a running daemon has to be
noticed without a restart. This is the same rule ``beads.Board.available``
already carries (``tests/test_beads_available_cache.py``), at module level
rather than per instance because ``registry()`` builds fresh
:class:`Harness` objects on every call.
"""

from __future__ import annotations

import pytest

from claude_launcher import harnesses


@pytest.fixture(autouse=True)
def clean_cache():
    """The cache outlives a single registry, so it outlives a single test."""
    harnesses.reset_which_cache()
    yield
    harnesses.reset_which_cache()


def _harness(name: str = "h", command: str = "some-program") -> harnesses.Harness:
    return harnesses.registry({"harnesses": {name: {"command": command}}})[name]


def test_the_path_is_walked_once_for_repeated_questions(monkeypatch):
    calls = []
    monkeypatch.setattr(
        harnesses.shutil, "which", lambda n: calls.append(n) or "C:/bin/h.exe"
    )
    h = _harness()
    assert h.available() is True
    for _ in range(50):
        assert h.available() is True
    assert len(calls) == 1, f"walked PATH {len(calls)} times"


def test_a_fresh_registry_reuses_the_answer(monkeypatch):
    """``registry()`` builds new objects every call -- once per profile row on
    the profiles page -- so the memo cannot live on the instance."""
    calls = []
    monkeypatch.setattr(
        harnesses.shutil, "which", lambda n: calls.append(n) or "C:/bin/h.exe"
    )
    for _ in range(10):
        assert _harness().available() is True
    assert len(calls) == 1


def test_every_declared_harness_is_asked_once(monkeypatch):
    """The registry carries the packaged harnesses as well as the config's,
    so the rule is one walk per program, whatever the set is."""
    calls = []
    monkeypatch.setattr(harnesses.shutil, "which", lambda n: calls.append(n) or None)
    doc = {"harnesses": {"a": {"command": "pa"}, "b": {"command": "pb"}}}
    reg = harnesses.registry(doc)
    for _ in range(5):
        for entry in reg.values():
            entry.available()
    assert {"pa", "pb"} <= set(calls)
    assert len(calls) == len(set(calls)), f"repeated walks: {calls}"
    assert len(calls) == len({e.program() for e in reg.values()})


def test_a_missing_program_is_rechecked_after_the_window(monkeypatch):
    """A daemon that started before the harness was installed has to find it
    without being restarted, so a negative does not stick."""
    answers = [None, None, "C:/bin/h.exe"]
    calls = []

    def stepping(name):
        calls.append(name)
        return answers.pop(0) if answers else "C:/bin/h.exe"

    monkeypatch.setattr(harnesses.shutil, "which", stepping)
    h = _harness()
    assert h.available() is False
    harnesses._which_seen["some-program"] = (False, 0.0)   # the window elapsed
    assert h.available() is False
    harnesses._which_seen["some-program"] = (False, 0.0)
    assert h.available() is True
    assert len(calls) == 3


def test_a_negative_is_still_kept_inside_the_window(monkeypatch):
    calls = []
    monkeypatch.setattr(harnesses.shutil, "which", lambda n: calls.append(n) or None)
    h = _harness()
    assert h.available() is False
    assert h.available() is False
    assert len(calls) == 1


def test_the_answer_matches_what_which_says(monkeypatch):
    """The memo changes the cost, not the answer: the first ask and the
    remembered ask both agree with ``which``."""
    for found, expected in (("C:/bin/h.exe", True), (None, False)):
        harnesses.reset_which_cache()
        monkeypatch.setattr(harnesses.shutil, "which", lambda n, r=found: r)
        h = _harness()
        assert h.available() is expected
        assert h.available() is expected


def test_two_programs_keep_separate_answers(monkeypatch):
    monkeypatch.setattr(
        harnesses.shutil,
        "which",
        lambda n: "C:/bin/there.exe" if n == "there" else None,
    )
    doc = {"harnesses": {"a": {"command": "there"}, "b": {"command": "missing"}}}
    reg = harnesses.registry(doc)
    assert reg["a"].available() is True
    assert reg["b"].available() is False
    assert reg["a"].available() is True


def test_reset_makes_the_machine_be_asked_again(monkeypatch):
    calls = []
    monkeypatch.setattr(
        harnesses.shutil, "which", lambda n: calls.append(n) or "C:/bin/h.exe"
    )
    h = _harness()
    h.available()
    harnesses.reset_which_cache()
    h.available()
    assert len(calls) == 2
