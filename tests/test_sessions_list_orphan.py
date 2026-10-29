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
            # The daemon narrows ``?project=`` itself, as the real route does.
            want = path.partition("?project=")[2]
            return {"sessions": [r for r in self.rows
                                 if not want or r.get("project") == want]}
        if path == "/api/daemon":
            return {"relay": None}
        raise AssertionError(path)


def _lines(monkeypatch, capsys, rows, project="all"):
    client = _Client(rows)
    monkeypatch.setattr(
        daemon_client, "connect_with_diagnosis", lambda *a, **k: (client, None)
    )
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    assert cli.main(["sessions", "--project", project]) == 0
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


def test_a_live_parent_in_another_project_does_not_make_an_orphan(
        monkeypatch, capsys):
    """Narrowing to one project hides the parent's row, not the parent."""
    rows = [_row("lead", project="A"), _row("w1", parent="lead", project="B"),
            _row("kid", orphan_of="gone", project="B")]
    lines = _lines(monkeypatch, capsys, rows, project="B")
    assert "orphan" not in lines["w1"]
    # the daemon's own record still reads through the narrowing
    assert lines["kid"].endswith("(orphan of gone)")


def test_a_session_scoped_listing_reads_the_parent_off_the_whole_roster(
        monkeypatch, capsys):
    """Inside a managed session the list narrows to its own project after
    reading every row, so the parent's existence is judged on all of them."""
    from claude_launcher import projects

    client = _Client([_row("lead", project="A"),
                      _row("w1", parent="lead", project="B")])
    monkeypatch.setattr(
        daemon_client, "connect_with_diagnosis", lambda *a, **k: (client, None)
    )
    monkeypatch.setenv("CLAUNCH_SESSION", "w1")
    monkeypatch.setenv(projects.SESSION_ENV, "B")
    assert cli.main(["sessions"]) == 0
    out = capsys.readouterr().out
    row = next(line for line in out.splitlines() if line.startswith("w1"))
    assert "orphan" not in row
