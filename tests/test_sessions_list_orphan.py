"""`claunch sessions` tells an orphan from a root (claunch-ikiys).

A session whose parent was removed with nothing above it to take it used to
list exactly like a session a human started. It is drawn at the top level
still -- its parent is gone -- but the row says whose child it was.
"""
from claude_launcher import cli, daemon_client


def _row(name, **extra):
    row = {
        "name": name, "status": "idle", "exit_code": None, "harness": "claude",
        "profile": "p", "cols": 100, "rows": 30, "cwd": "/w", "parent": None,
    }
    row.update(extra)
    return row


class _Client:
    def __init__(self, rows):
        self.rows = rows

    def get(self, path, **_kw):
        if path.startswith("/api/sessions"):
            return {"sessions": self.rows}
        if path == "/api/daemon":
            return {"relay": None}
        raise AssertionError(path)


def _lines(monkeypatch, capsys, rows):
    client = _Client(rows)
    monkeypatch.setattr(
        daemon_client, "connect_with_diagnosis", lambda *a, **k: (client, None)
    )
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    assert cli.main(["sessions", "--project", "all"]) == 0
    return {
        line.split()[0]: line for line in capsys.readouterr().out.splitlines()
        if line.strip() and not line.startswith(" ")
    }


def test_an_orphan_row_names_the_parent_it_lost(monkeypatch, capsys):
    lines = _lines(monkeypatch, capsys, [
        _row("root1"), _row("kid", orphan_of="gone"),
    ])
    assert lines["kid"].endswith("(orphan of gone)")
    assert "orphan" not in lines["root1"]


def test_a_dangling_parent_reads_as_an_orphan_too(monkeypatch, capsys):
    lines = _lines(monkeypatch, capsys, [_row("kid", parent="ghost")])
    assert lines["kid"].endswith("(orphan of ghost)")


def test_a_child_under_its_parent_is_not_marked(monkeypatch, capsys):
    out = _lines(monkeypatch, capsys, [_row("lead"), _row("w1", parent="lead")])
    assert "orphan" not in "".join(out.values())
