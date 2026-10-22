"""``claunch create`` offers the profile install, and asks only a person.

The profile's install is the one step a new profile cannot get from anywhere
else: a profile is an isolated config dir, so a global install never reaches
it. ``create`` therefore offers it -- but only where an answer can come from
a person, so a scripted create never blocks on stdin.
"""

from __future__ import annotations

import pytest

from claude_launcher import cli, profile, settings


class _Terminal:
    """A stdin that a person could answer on."""

    def isatty(self):
        return True


class _Pipe:
    """A stdin that nothing will ever type into."""

    def isatty(self):
        return False


def run(*argv):
    return cli.main(list(argv))


def _answer(monkeypatch, value):
    """Answer the prompt with ``value``; return the list of prompts asked."""
    asked = []

    def fake_input(prompt=""):
        asked.append(prompt)
        return value

    monkeypatch.setattr("builtins.input", fake_input)
    return asked


def _no_input(*_args):
    raise AssertionError("create must not ask here")


def test_create_asks_and_installs_on_the_default_answer(home, capsys, monkeypatch):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    monkeypatch.setattr("sys.stdin", _Terminal())
    asked = _answer(monkeypatch, "")

    assert run("create", "work", "--no-seed") == 0

    assert asked, "create must ask before installing"
    assert "[Y/n]" in asked[0]
    assert "installed:" in capsys.readouterr().out
    # Evidence that the install actually wrote into this profile: the MCP
    # server entry Claude Code reads lives in its .claude.json.
    assert (profile.resolve("work").config_dir / settings.CLAUDE_JSON).is_file()


@pytest.mark.parametrize("answer", ["y", "Y"])
def test_create_installs_on_an_explicit_yes(home, capsys, monkeypatch, answer):
    # The default answer is covered above; this pins the explicit one, which
    # is what a person types when they do not want to think about defaults.
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    monkeypatch.setattr("sys.stdin", _Terminal())
    asked = _answer(monkeypatch, answer)

    assert run("create", "work", "--no-seed") == 0

    assert asked, "create must ask before installing"
    assert "installed:" in capsys.readouterr().out


def test_create_does_not_ask_when_there_is_no_stdin(home, capsys, monkeypatch):
    # A process can run with sys.stdin = None (pythonw, some launchers). That
    # is not a terminal either, and reaching for .isatty() on it raises
    # AttributeError -- cli_sessions.py guards the same way.
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    monkeypatch.setattr("sys.stdin", None)
    monkeypatch.setattr("builtins.input", _no_input)

    assert run("create", "work", "--no-seed") == 0

    out = capsys.readouterr().out
    assert "stdin is not a terminal" in out
    assert "installed:" not in out


def test_create_asks_to_reinstall_when_reinitializing(home, capsys, monkeypatch):
    # --reinit runs over a directory someone may already have used, and the
    # install rewrites the files it manages. The question has to say that
    # instead of describing a profile that was empty a moment ago.
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    monkeypatch.setattr("sys.stdin", _Pipe())
    assert run("create", "work", "--no-seed", "--no-install") == 0
    capsys.readouterr()

    monkeypatch.setattr("sys.stdin", _Terminal())
    asked = _answer(monkeypatch, "y")

    assert run("create", "work", "--no-seed", "--reinit") == 0

    out = capsys.readouterr().out
    assert asked, "reinit must ask too"
    assert "rewriting the files it manages" in asked[0]
    assert "installed:" in out


def test_create_skips_the_install_when_answered_no(home, capsys, monkeypatch):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    monkeypatch.setattr("sys.stdin", _Terminal())
    _answer(monkeypatch, "n")

    assert run("create", "work", "--no-seed") == 0

    out = capsys.readouterr().out
    assert "installed:" not in out
    assert "claunch install work" in out


def test_create_does_not_ask_without_a_terminal(home, capsys, monkeypatch):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    monkeypatch.setattr("sys.stdin", _Pipe())
    monkeypatch.setattr("builtins.input", _no_input)

    assert run("create", "work", "--no-seed") == 0

    out = capsys.readouterr().out
    assert "stdin is not a terminal" in out
    assert "installed:" not in out


def test_create_does_not_ask_inside_a_managed_session(home, capsys, monkeypatch):
    monkeypatch.setenv("CLAUNCH_SESSION", "s9")
    monkeypatch.setattr("sys.stdin", _Terminal())
    monkeypatch.setattr("builtins.input", _no_input)

    assert run("create", "work", "--no-seed") == 0

    out = capsys.readouterr().out
    assert "managed session" in out
    assert "installed:" not in out


def test_create_skips_the_install_when_the_terminal_gives_no_answer(
    home, capsys, monkeypatch
):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    monkeypatch.setattr("sys.stdin", _Terminal())

    def eof(_prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)

    assert run("create", "work", "--no-seed") == 0

    out = capsys.readouterr().out
    assert "no answer" in out
    assert "installed:" not in out


def test_install_flag_installs_without_asking(home, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", _Pipe())
    monkeypatch.setattr("builtins.input", _no_input)

    assert run("create", "work", "--no-seed", "--install") == 0

    assert "installed:" in capsys.readouterr().out


def test_no_install_flag_skips_without_asking(home, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", _Terminal())
    monkeypatch.setattr("builtins.input", _no_input)

    assert run("create", "work", "--no-seed", "--no-install") == 0

    out = capsys.readouterr().out
    assert "installed:" not in out
    assert "claunch install work" in out


def test_the_two_install_flags_cannot_both_be_given(home, capsys, monkeypatch):
    # Last one wins, the way every other single-valued option on this parser
    # behaves; the point of the test is that neither is silently ignored.
    monkeypatch.setattr("sys.stdin", _Pipe())
    monkeypatch.setattr("builtins.input", _no_input)

    assert run("create", "work", "--no-seed", "--install", "--no-install") == 0
    assert "installed:" not in capsys.readouterr().out
