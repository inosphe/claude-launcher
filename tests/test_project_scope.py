"""A managed session knows its project, and its listings stay inside it.

The incident this pins: s769 (harness devin, project 'gds6', 2026-09-24) ran
``claunch sessions`` and ``claunch mesh ls`` and read a hundred
default-project rows as its own roster. Three things were missing and are
tested here one by one: the project in the session's environment, the
listings narrowing to it inside a managed session, and the join briefing
saying which project the member is in.
"""

from __future__ import annotations

from claude_launcher import cli, daemon_client, profile, projects
from claude_launcher.daemon import harness
from claude_launcher.daemon.harness import SessionDef

from test_mesh_wire import _Manager, _member, _mesh


# --------------------------------------------------------------------------- #
# 1. the environment
# --------------------------------------------------------------------------- #
def test_the_session_env_names_its_project_spelled_out(home, tmp_path):
    profile.create("work")
    for filed, expected in ((None, "default"), ("gds6", "gds6")):
        sdef = harness.normalize(
            SessionDef(name="sx", profile="work", cwd=str(tmp_path), project=filed)
        )
        _, env, _ = harness.build_command(sdef)
        assert env[projects.SESSION_ENV] == expected
        assert env["CLAUNCH_SESSION"] == "sx"


# --------------------------------------------------------------------------- #
# 2. the scope a listing takes
# --------------------------------------------------------------------------- #
def test_outside_a_managed_session_nothing_narrows():
    scope = projects.listing_scope(None, environ={})
    assert scope == projects.ListingScope("", own=False)
    assert projects.scope_footer(scope, hidden=3, noun="session", command="c") == ""


def test_inside_a_managed_session_the_scope_is_its_own_project():
    env = {"CLAUNCH_SESSION": "s769", projects.SESSION_ENV: "gds6"}
    assert projects.listing_scope(None, environ=env) == projects.ListingScope("gds6", own=True)
    # --project wins either way: a name narrows, 'all' widens
    assert projects.listing_scope("hq", environ=env) == projects.ListingScope("hq")
    assert projects.listing_scope(projects.ALL, environ=env) == projects.ListingScope("")


def test_a_session_launched_before_the_variable_asks_the_daemon_for_its_record():
    env = {"CLAUNCH_SESSION": "s769"}
    asked = []

    def fetch(name):
        asked.append(name)
        return "gds6"

    assert projects.listing_scope(None, environ=env, fetch_own=fetch).project == "gds6"
    assert asked == ["s769"]

    # a failing or empty lookup leaves the scope at the default project --
    # never at everything, which is the silent widening this exists to stop
    def boom(name):
        raise RuntimeError("daemon gone")

    assert projects.listing_scope(None, environ=env, fetch_own=boom) == projects.ListingScope(
        "default", own=True
    )
    assert projects.listing_scope(None, environ=env, fetch_own=lambda n: None).project == "default"
    assert projects.listing_scope(None, environ=env).project == "default"


def test_the_footer_always_says_the_list_was_narrowed():
    own = projects.ListingScope("gds6", own=True)
    with_hidden = projects.scope_footer(own, hidden=2, noun="mesh", command="claunch mesh ls")
    assert with_hidden.startswith(
        "project: gds6 (this session's) -- 2 mesh(s) in other projects not shown"
    )
    assert "'claunch mesh ls --project all'" in with_hidden
    none_hidden = projects.scope_footer(own, hidden=0, noun="mesh", command="claunch mesh ls")
    assert none_hidden.startswith("project: gds6 (this session's) -- no meshes in other projects")
    assert "'claunch mesh ls --project all'" in none_hidden
    no_sessions = projects.scope_footer(own, hidden=0, noun="session", command="claunch sessions")
    assert "-- no sessions in other projects;" in no_sessions


# --------------------------------------------------------------------------- #
# 3. the two CLI listings, end to end through the parser
# --------------------------------------------------------------------------- #
class _Client:
    """The daemon as the two listings see it: every project's rows, and one
    session record for the pre-variable lookup."""

    def __init__(self):
        self.calls = []

    def get(self, path, **_kw):
        self.calls.append(path)
        if path.startswith("/api/sessions/"):
            return {"name": path.rsplit("/", 1)[1], "project": "gds6"}
        if path.startswith("/api/sessions"):
            return {"sessions": [
                _row("s769", "gds6"), _row("s785", None), _row("s127", "default"),
            ]}
        if path.startswith("/api/mesh"):
            return {"meshes": [
                {"name": "gds6-main", "project": "gds6", "members": [], "messages": 1},
                {"name": "mesh0", "members": [], "messages": 9},
            ], "relay": None}
        if path == "/api/daemon":
            return {"relay": None}
        raise AssertionError(path)


def _row(name, project):
    row = {
        "name": name, "status": "idle", "harness": "claude", "profile": "p",
        "cols": 100, "rows": 30, "cwd": "F:/x", "parent": None,
    }
    if project:
        row["project"] = project
    return row


def _run(monkeypatch, capsys, *argv, env):
    client = _Client()
    monkeypatch.setattr(
        daemon_client, "connect_with_diagnosis", lambda *a, **k: (client, None)
    )
    for key in ("CLAUNCH_SESSION", projects.SESSION_ENV):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert cli.main(list(argv)) == 0
    return capsys.readouterr().out, client


INSIDE = {"CLAUNCH_SESSION": "s769", projects.SESSION_ENV: "gds6"}


def test_claunch_sessions_inside_a_session_shows_its_project_and_says_what_it_hid(
    monkeypatch, capsys
):
    out, client = _run(monkeypatch, capsys, "sessions", env=INSIDE)
    assert "s769" in out
    assert "s785" not in out and "s127" not in out
    assert (
        "project: gds6 (this session's) -- 2 session(s) in other projects not shown"
        in out
    )
    assert "'claunch sessions --project all'" in out
    # the whole roster was read once, unfiltered, so the hidden count is real
    assert client.calls[0] == "/api/sessions"


def test_claunch_sessions_widens_on_project_all_and_outside_a_session(monkeypatch, capsys):
    for env, argv in (({}, ["sessions"]), (INSIDE, ["sessions", "--project", "all"])):
        out, _ = _run(monkeypatch, capsys, *argv, env=env)
        assert "s769" in out and "s785" in out and "s127" in out
        assert "this session's" not in out


def test_claunch_sessions_falls_back_to_the_record_when_the_variable_is_missing(
    monkeypatch, capsys
):
    out, client = _run(monkeypatch, capsys, "sessions", env={"CLAUNCH_SESSION": "s769"})
    assert "/api/sessions/s769" in client.calls
    assert "s127" not in out and "project: gds6 (this session's)" in out


def test_claunch_mesh_ls_inside_a_session_shows_its_project_only(monkeypatch, capsys):
    out, _ = _run(monkeypatch, capsys, "mesh", "ls", env=INSIDE)
    assert "gds6-main" in out and "mesh0" not in out
    assert "project: gds6 (this session's) -- 1 mesh(s) in other projects not shown" in out
    assert "'claunch mesh ls --project all'" in out

    out, _ = _run(monkeypatch, capsys, "mesh", "ls", "--project", "all", env=INSIDE)
    assert "gds6-main" in out and "mesh0" in out and "this session's" not in out

    out, _ = _run(monkeypatch, capsys, "mesh", "ls", env={})
    assert "gds6-main" in out and "mesh0" in out and "this session's" not in out


# --------------------------------------------------------------------------- #
# 4. the join briefing names the project
# --------------------------------------------------------------------------- #
def test_the_join_briefing_carries_the_members_project(home):
    mgr = _Manager()
    mm, mesh = _mesh(mgr)
    me = _member(mesh, "me", mgr.add("self"), role="worker")
    mgr.get("self").sdef.project = "gds6"
    block = mm.briefing_block(mesh, me)
    assert (
        "you: me (role: worker)\n"
        "project: gds6 -- 'claunch sessions' and 'claunch mesh ls' show this "
        "project only; add '--project all' for every project\n"
    ) in block

    # a record that never named one is in the default project, spelled out
    mgr.get("self").sdef.project = None
    assert "\nproject: default -- " in mm.briefing_block(mesh, me)

    # a member whose session this daemon does not hold gets no line
    ghost = _member(mesh, "ghost", "gone", role="worker")
    assert "\nproject:" not in mm.briefing_block(mesh, ghost)
