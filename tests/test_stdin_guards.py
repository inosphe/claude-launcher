"""Every place that asks "is a person there?" survives a missing ``sys.stdin``.

The check these sites share is a tty test, and ``sys.stdin`` can be ``None``
outright -- ``pythonw``, some launchers, a service wrapper. Reaching for
``isatty()`` on ``None`` raises ``AttributeError``, which turns "nobody can
answer, so skip the question" into a crash. ``cli_sessions.py`` and
``cli.py::_install_after_create`` already guard it; these pin the rest so the
contract does not depend on which file a caller walks through.
"""

from __future__ import annotations

import argparse

from claude_launcher import attach as attach_mod
from claude_launcher import cli_mesh, cli_reposync, reposync_flow as flow, worktree


class _Daemon:
    """Answers the two reads ``mesh add`` makes and records the post."""

    def __init__(self):
        self.posted = {}

    def post(self, path, body):
        self.posted[path] = body
        return {"member": {"handle": body.get("handle"), "role": body.get("role")}}

    def get(self, path):
        return {}


def test_reposync_refuses_to_ask_without_stdin(home, monkeypatch):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    monkeypatch.setattr(cli_reposync.sys, "stdin", None)
    monkeypatch.setattr(flow, "approval_mode", lambda: flow.ASK)

    refusal, approval = cli_reposync._approve(
        {"summary": "push 1 commit(s)"}, yes=False, c=None
    )

    assert refusal == {
        "status": "needs_approval",
        "why": "not a terminal -- pass --yes",
    }
    assert approval == {}


def test_mesh_add_refuses_the_wizard_without_stdin(home, monkeypatch, capsys):
    monkeypatch.setattr(cli_mesh.daemon_client, "ensure_running", lambda **k: _Daemon())
    monkeypatch.setattr(cli_mesh.sys, "stdin", None)
    args = argparse.Namespace(
        machine=None, session=None, handle=None, role=None, subroles=None, mesh="m"
    )

    assert cli_mesh._cmd_add(args) == 2

    assert "run interactively" in capsys.readouterr().err


def test_mesh_add_skips_the_handle_prompt_without_stdin(home, monkeypatch):
    daemon = _Daemon()
    monkeypatch.setattr(cli_mesh.daemon_client, "ensure_running", lambda **k: daemon)
    monkeypatch.setattr(cli_mesh.sys, "stdin", None)
    args = argparse.Namespace(
        machine="other", session="s1", handle=None, role=None,
        subroles=None, mesh="m",
    )

    assert cli_mesh._cmd_add(args) == 0

    # No prompt, so the handle stays empty rather than pinning a name nobody
    # typed: the flags stay scriptable with a missing stdin.
    assert daemon.posted["/api/mesh/m/invitations"]["handle"] == ""


def test_attach_reports_no_terminal_instead_of_crashing(home, monkeypatch, capsys):
    monkeypatch.setattr(attach_mod.sys, "stdin", None)

    class Client:
        base_url = "http://h"
        token = "t"

        def get(self, path):  # pragma: no cover - must not be reached
            raise AssertionError("attach must not talk to the daemon")

    assert attach_mod.attach(Client(), "s1") == 1

    assert "needs an interactive terminal" in capsys.readouterr().err


def test_worktree_interactive_is_false_without_stdin(home, monkeypatch):
    monkeypatch.delenv(worktree.SESSION_ENV, raising=False)
    monkeypatch.setattr(worktree.sys, "stdin", None)

    assert worktree.interactive() is False
