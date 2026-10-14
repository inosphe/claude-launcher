"""`claunch sessions` tells a held session from an ended one (claunch-1vqm4).

A hold stops the program, not the session: a mesh message wakes it
(claunch-5l35i). The listing used to print a held record as ``exited(143)``
and count it among the records kept for respawn, which is how an agent read a
held peer as gone and stopped writing to it.
"""
from claude_launcher import cli, daemon_client, mesh_install

STAMP = "2026-10-14T05:00:00+00:00"


def _row(name, **extra):
    row = {
        "name": name, "status": "exited", "exit_code": 143, "harness": "claude",
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


def _list(monkeypatch, capsys, rows):
    client = _Client(rows)
    monkeypatch.setattr(
        daemon_client, "connect_with_diagnosis", lambda *a, **k: (client, None)
    )
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    assert cli.main(["sessions", "--project", "all"]) == 0
    return capsys.readouterr().out


def _state(out, name):
    line = next(l for l in out.splitlines() if l.startswith(name + " "))
    return line.split("[", 1)[1].split("]", 1)[0].strip()


def test_each_stopped_kind_reads_as_itself(monkeypatch, capsys):
    out = _list(monkeypatch, capsys, [
        _row("held1", held_at=STAMP),
        _row("delay1", held_at=STAMP, delayed_at=STAMP),
        _row("fail1", failed_at=STAMP),
        _row("kill1"),
        _row("live1", status="idle", exit_code=None),
    ])
    assert _state(out, "held1") == "held"
    assert _state(out, "delay1") == "delayed"
    assert _state(out, "fail1") == "failed(143)"
    assert _state(out, "kill1") == "exited(143)"
    assert _state(out, "live1") == "idle"


def test_footer_counts_held_apart_from_the_ended(monkeypatch, capsys):
    out = _list(monkeypatch, capsys, [
        _row("held1", held_at=STAMP),
        _row("delay1", held_at=STAMP, delayed_at=STAMP),
        _row("kill1"),
    ])
    assert "2 held/delayed session(s) are asleep, not ended" in out
    assert "a mesh message to one wakes it" in out
    # Only the ended record is offered for respawn; clear-sessions still
    # drops held records too, and the footer says so.
    assert "1 exited session(s) kept for respawn: 'claunch respawn kill1'" in out
    assert "(and the held ones)" in out


def test_only_held_says_nothing_about_respawn(monkeypatch, capsys):
    out = _list(monkeypatch, capsys, [_row("held1", held_at=STAMP)])
    assert "1 held/delayed session(s) are asleep" in out
    assert "kept for respawn" not in out


def test_only_ended_keeps_the_old_footer(monkeypatch, capsys):
    out = _list(monkeypatch, capsys, [_row("kill1")])
    assert "asleep" not in out
    assert (
        "1 exited session(s) kept for respawn: 'claunch respawn kill1' revives "
        "one, 'claunch clear-sessions' drops them all\n"
    ) in out


def test_mesh_skill_says_a_held_member_wakes_on_a_message():
    md = mesh_install.SKILL_MD
    assert "A `held` (or `delayed`) member is asleep, not ended." in md
    assert "A message you send it wakes it" in md
