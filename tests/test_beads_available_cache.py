"""Asking whether ``br`` exists must not walk PATH on every request.

``Board.available`` called ``shutil.which`` each time, and it is asked on
every board view, every session view and every session list -- the detail
panel polls one of those every five seconds per open card. On Windows
``which`` stats each PATH entry once per ``PATHEXT`` suffix, so one answer
is dozens of filesystem calls on the event loop. A py-spy sample of the
live daemon (s586, 2026-09-21) put 3.6% of the loop thread's time in
``available -> which``.

The answer is a property of the machine, so it is cached. A negative is
cached only briefly: installing ``br`` while the daemon runs has to be
noticed without a restart.
"""

from __future__ import annotations

import pytest

from claude_launcher.daemon import beads as beads_mod


def _board() -> beads_mod.Board:
    return beads_mod.Board(None)


def test_the_path_is_walked_once_for_repeated_questions(monkeypatch):
    calls = []

    def counted(name):
        calls.append(name)
        return "C:/bin/br.exe"

    monkeypatch.setattr(beads_mod.shutil, "which", counted)
    board = _board()
    assert board.available() is True
    for _ in range(50):
        assert board.available() is True
    assert len(calls) == 1, f"walked PATH {len(calls)} times"


def test_a_missing_binary_is_rechecked(monkeypatch):
    """A daemon that started before ``br`` was installed has to find it
    without being restarted, so a negative does not stick."""
    answers = [None, None, "C:/bin/br.exe"]
    calls = []

    def stepping(name):
        calls.append(name)
        return answers.pop(0) if answers else "C:/bin/br.exe"

    monkeypatch.setattr(beads_mod.shutil, "which", stepping)
    board = _board()
    assert board.available() is False
    board._which_checked = 0.0          # the recheck window elapsed
    assert board.available() is False
    board._which_checked = 0.0
    assert board.available() is True
    assert len(calls) == 3


def test_a_negative_is_still_cached_inside_the_window(monkeypatch):
    calls = []
    monkeypatch.setattr(
        beads_mod.shutil, "which", lambda name: calls.append(name) or None
    )
    board = _board()
    assert board.available() is False
    assert board.available() is False
    assert len(calls) == 1


def test_a_fake_runner_never_asks_the_machine(monkeypatch):
    """The test path: a board given a runner is available by definition."""

    def boom(name):
        raise AssertionError("PATH was walked for a board with a runner")

    monkeypatch.setattr(beads_mod.shutil, "which", boom)

    async def runner(argv, cwd):
        return 0, "", ""

    board = beads_mod.Board(runner)
    assert board.available() is True


def test_the_answer_matches_what_which_says(monkeypatch):
    """The cache is not allowed to change the answer, only its cost: the
    first call and the cached call agree with ``which``."""
    for found, expected in (("C:/bin/br.exe", True), (None, False)):
        monkeypatch.setattr(beads_mod.shutil, "which", lambda name, r=found: r)
        board = _board()
        assert board.available() is expected
        assert board.available() is expected
