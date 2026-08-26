"""Round reports: one HTML page per round, and the index that is its filename.

Four surfaces, and the tests are grouped by what each of them is responsible
for keeping true:

* :mod:`claude_launcher.reports` — where a report goes, what it may be called,
  and what counts as one at all (a path that exists is not the answer).
* ``claunch report`` — the only handle onto those rules, and the exit code a
  workflow's ``verify`` gates on.
* the daemon — the listing rides in the session's board view, and the page is
  served sandboxed, both of them outliving the session record.
* ``improv-worker`` — the workflow that must actually demand one.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from claude_launcher import cli, reports
from claude_launcher.daemon import paths

PAGE = (
    "<!doctype html><html><head><title>round</title></head><body>"
    + "<p>what happened, at length.</p>" * 20
    + "</body></html>"
)


def write(path: Path, body: str = PAGE) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# the name is the index
# --------------------------------------------------------------------------- #
def test_a_filename_carries_the_time_and_the_issue_back(home):
    at = datetime(2026, 8, 26, 3, 12, 0, tzinfo=timezone.utc)
    name = reports.filename("claunch-j31", at)
    assert name == "20260826T031200Z-claunch-j31.html"
    assert reports.parse(name) == {"at": "2026-08-26T03:12:00Z", "issue": "claunch-j31"}


def test_a_round_with_no_issue_is_still_named_not_refused(home):
    name = reports.filename(None)
    assert name.endswith(f"-{reports.NO_ISSUE}.html")
    # The placeholder is a naming device, not an issue id, so it comes back
    # as None — a reader must not see "no-issue" and go looking for it.
    assert reports.parse(name)["issue"] is None


def test_a_stamp_is_utc_whatever_zone_it_arrives_in(home):
    east = timezone(timedelta(hours=9))
    at = datetime(2026, 8, 26, 12, 12, 0, tzinfo=east)
    assert reports.filename("x", at) == "20260826T031200Z-x.html"


@pytest.mark.parametrize(
    "name",
    [
        "notes.html",                          # no stamp
        "20260826T031200Z-claunch-j31.htm",    # not .html
        "20260826T031200Z.html",               # no issue half
        "20260826T031200Z-a/b.html",           # a separator
        "20261326T031200Z-x.html",             # month 13
    ],
)
def test_a_file_that_is_not_the_indexed_form_is_not_a_report(home, name):
    assert reports.parse(name) is None


def test_an_odd_issue_id_is_squeezed_into_the_name_not_rejected(home):
    # A report is never lost to an argument about naming; the id is coerced.
    assert reports.issue_slug("proj#42 / urgent") == "proj-42-urgent"
    assert reports.issue_slug("///") == reports.NO_ISSUE


# --------------------------------------------------------------------------- #
# where it goes, and what counts as one
# --------------------------------------------------------------------------- #
def test_reports_live_outside_the_session_directory(home):
    # The whole point: `clear-sessions --logs` rmtree's session_dir, and a
    # report has to survive that. If this ever nests, that guarantee is gone.
    inside = paths.session_dir("s1").resolve()
    assert inside not in reports.dir_for("s1").resolve().parents
    assert reports.dir_for("s1").resolve().parent == paths.reports_root().resolve()


def test_a_session_name_cannot_walk_out_of_the_reports_directory(home):
    for bad in ("../s2", "a/b", "", ".hidden", "x" * 65):
        with pytest.raises(reports.ReportError):
            reports.dir_for(bad)


def test_a_filename_cannot_walk_out_either(home):
    for bad in ("../../token", "a/b.html", "..html", "20260826T031200Z-../x.html"):
        with pytest.raises(reports.ReportError):
            reports.resolve("s1", bad)


def test_an_empty_file_at_the_right_path_is_not_a_report(home):
    path = reports.target("s1", "i-1")
    write(path, "")
    assert not reports.is_report(path)
    assert reports.listing("s1") == []


def test_a_stub_page_is_not_a_report_either(home):
    # The failure this is here for: a gate answered by touching a file.
    write(reports.target("s1", "i-1"), "<html></html>")
    assert reports.listing("s1") == []


def test_a_text_note_with_the_right_name_is_not_a_report(home):
    write(reports.target("s1", "i-1"), "round went fine\n" * 60)
    assert reports.listing("s1") == []


def test_a_real_page_is_listed_with_its_time_and_issue(home):
    path = write(reports.target("s1", "claunch-j31"))
    rows = reports.listing("s1")
    assert len(rows) == 1
    assert rows[0]["issue"] == "claunch-j31"
    assert rows[0]["file"] == path.name
    assert rows[0]["url"] == f"/api/sessions/s1/reports/{path.name}"
    assert rows[0]["size"] == len(PAGE)


def test_reports_come_back_newest_first_and_strays_are_ignored(home):
    base = reports.dir_for("s1", create=True)
    for stamp in ("20260824T090000Z", "20260826T090000Z", "20260825T090000Z"):
        write(base / f"{stamp}-i-1.html")
    write(base / "scratch.html")            # not the indexed form
    (base / "notes.txt").write_text("x", encoding="utf-8")
    assert [r["at"][:10] for r in reports.listing("s1")] == [
        "2026-08-26", "2026-08-25", "2026-08-24",
    ]


def test_an_unknown_session_lists_nothing_rather_than_raising(home):
    # A panel drawing this must not break on "no report yet", and a bad name
    # arriving from a URL is the same non-answer.
    assert reports.listing("never-ran") == []
    assert reports.listing("../etc") == []


# --------------------------------------------------------------------------- #
# asking twice for the same round gets the same file
# --------------------------------------------------------------------------- #
def test_the_same_session_and_issue_get_the_same_file_back(home):
    first = reports.target("s1", "i-1")
    write(first)
    assert reports.target("s1", "i-1") == first


def test_a_draft_that_is_not_a_report_yet_still_holds_its_name(home):
    """The common case, not a corner: the first thing written to the path is
    often too thin to pass the gate. If asking again minted a second name, a
    revised report would leave its own stub behind for a reader to find."""
    first = write(reports.target("s1", "i-1"), "<html>half a page</html>")
    assert reports.listing("s1") == []          # not a report yet
    assert reports.target("s1", "i-1") == first  # but the name is taken


def test_a_different_issue_is_a_different_report(home):
    write(reports.target("s1", "i-1"))
    assert reports.target("s1", "i-2") != reports.target("s1", "i-1")


def test_new_forces_a_fresh_file_when_keeping_the_old_one_is_deliberate(home):
    first = write(reports.target("s1", "i-1"))
    later = datetime.now(timezone.utc) + timedelta(seconds=5)
    assert reports.target("s1", "i-1", new=True, at=later) != first


def test_saving_a_page_written_elsewhere_files_it_under_the_rules(home, tmp_path):
    src = write(tmp_path / "somewhere" / "report.html")
    dest = reports.save("s1", src, "i-1")
    assert dest.parent == reports.dir_for("s1")
    assert reports.parse(dest.name)["issue"] == "i-1"
    assert [r["file"] for r in reports.listing("s1")] == [dest.name]


def test_saving_a_missing_file_says_so(home, tmp_path):
    with pytest.raises(reports.ReportError):
        reports.save("s1", tmp_path / "nope.html", "i-1")


# --------------------------------------------------------------------------- #
# the command line — and the exit code a verify gates on
# --------------------------------------------------------------------------- #
def run_cli(*argv) -> int:
    return cli.main(list(argv))


def test_path_prints_where_to_write_and_is_stable(home, capsys):
    assert run_cli("report", "path", "--session", "s1", "--issue", "i-1") == 0
    first = capsys.readouterr().out.strip()
    write(Path(first))
    assert run_cli("report", "path", "--session", "s1", "--issue", "i-1") == 0
    assert capsys.readouterr().out.strip() == first


def test_check_fails_before_a_report_exists(home, capsys):
    assert run_cli("report", "check", "--session", "s1") == 1
    err = capsys.readouterr().err
    # The agent that trips this gate reads this and nothing else, so it has
    # to carry the command that fixes it.
    assert "claunch report path" in err
    assert str(reports.MIN_BYTES) in err


def test_check_passes_once_a_real_page_is_there(home, capsys):
    write(reports.target("s1", "i-1"))
    assert run_cli("report", "check", "--session", "s1") == 0
    assert "report ok" in capsys.readouterr().out


def test_check_still_fails_on_a_stub_at_the_right_path(home, capsys):
    write(reports.target("s1", "i-1"), "<html></html>")
    assert run_cli("report", "check", "--session", "s1") == 1


def test_check_can_be_pinned_to_the_rounds_issue(home, capsys):
    write(reports.target("s1", "i-1"))
    assert run_cli("report", "check", "--session", "s1", "--issue", "i-2") == 1
    assert "issue i-2" in capsys.readouterr().err


def test_the_session_comes_from_the_environment_when_unnamed(home, monkeypatch, capsys):
    monkeypatch.setenv("CLAUNCH_SESSION", "s9")
    assert run_cli("report", "path") == 0
    assert reports.dir_for("s9").resolve() == Path(capsys.readouterr().out.strip()).parent


def test_with_no_session_anywhere_the_command_says_so_rather_than_guessing(
    home, monkeypatch, tmp_path, capsys
):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    monkeypatch.chdir(tmp_path)
    assert run_cli("report", "path") == 2
    assert "no session" in capsys.readouterr().err


def test_a_cflow_run_in_this_directory_names_the_session_for_a_human(
    home, monkeypatch, tmp_path, capsys
):
    """The case this fallback exists for: a person advancing the run from
    their own shell runs the same verify without ``CLAUNCH_SESSION``."""
    from claude_launcher.cflow import state as cflow_state

    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    monkeypatch.chdir(tmp_path)
    token = cflow_state.push_scope("s7")
    try:
        cflow_state.save_state({"run_id": "run-1", "workflow": "w"}, str(tmp_path))
    finally:
        cflow_state.pop_scope(token)
    assert cflow_state.scopes_in(str(tmp_path)) == ["s7"]
    assert run_cli("report", "path") == 0
    assert Path(capsys.readouterr().out.strip()).parent == reports.dir_for("s7")


def test_ls_json_is_the_same_rows_the_api_serves(home, capsys):
    write(reports.target("s1", "i-1"))
    assert run_cli("report", "ls", "--session", "s1", "--json") == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc == {"session": "s1", "reports": reports.listing("s1")}


def test_save_through_the_cli_prints_where_it_landed(home, tmp_path, capsys):
    src = write(tmp_path / "r.html")
    assert run_cli("report", "save", str(src), "--session", "s1", "--issue", "i-1") == 0
    assert Path(capsys.readouterr().out.strip()).exists()


# --------------------------------------------------------------------------- #
# the daemon: indexed in the board view, served sandboxed, outliving both
# --------------------------------------------------------------------------- #
class _Sdef:
    def __init__(self, name):
        self.name = name
        self.cwd = "/nowhere"
        self.issue = None
        self.task = None


class _Session:
    def __init__(self, name):
        self.sdef = _Sdef(name)


def test_the_board_view_carries_the_reports(home):
    from claude_launcher.daemon.beads import Board

    write(reports.target("s1", "i-1"))
    board = Board(runner=None)
    view = asyncio.run(board.session_view(_Session("s1")))
    assert [r["issue"] for r in view["reports"]] == ["i-1"]


def test_reports_survive_the_board_being_unavailable(home, monkeypatch):
    """A machine without ``br`` still has the pages its sessions wrote, and
    hiding them because the board is unreachable hides the only thing the
    panel could still show."""
    from claude_launcher.daemon.beads import Board

    write(reports.target("s1", "i-1"))
    board = Board(runner=None)
    monkeypatch.setattr(board, "available", lambda: False)
    view = asyncio.run(board.session_view(_Session("s1")))
    assert view["error"]
    assert [r["issue"] for r in view["reports"]] == ["i-1"]


async def _client():
    from aiohttp.test_utils import TestClient, TestServer

    from claude_launcher.daemon.api import build_app
    from claude_launcher.daemon.manager import SessionManager
    from claude_launcher.daemon.mesh import MeshManager

    mgr = SessionManager(idle_threshold=0.5, scrollback=50, restore_default=False)
    app = build_app(
        mgr, "sekrit", started_at=time.monotonic(),
        mesh=MeshManager(mgr, root=paths.mesh_root()),
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


BEARER = {"Authorization": "Bearer sekrit"}


def test_the_page_is_served_sandboxed_to_a_session_the_daemon_never_heard_of(home):
    """Both halves matter. The session record is gone (cleared, or another
    machine's) and the report is still served — that is what keeping it out of
    ``sessions/<name>/`` was for. And it is served into an opaque origin, so a
    page an agent wrote cannot call the API as the logged-in operator."""
    path = write(reports.target("s1", "i-1"))

    async def run():
        client = await _client()
        try:
            resp = await client.get(f"/api/sessions/s1/reports/{path.name}", headers=BEARER)
            assert resp.status == 200
            assert resp.headers["Content-Type"].startswith("text/html")
            assert "sandbox" in resp.headers["Content-Security-Policy"]
            assert resp.headers["X-Content-Type-Options"] == "nosniff"
            assert "what happened" in await resp.text()
        finally:
            await client.close()

    asyncio.run(run())


def test_the_index_route_lists_what_the_board_view_lists(home):
    write(reports.target("s1", "i-1"))

    async def run():
        client = await _client()
        try:
            resp = await client.get("/api/sessions/s1/reports", headers=BEARER)
            assert resp.status == 200
            assert await resp.json() == {"session": "s1", "reports": reports.listing("s1")}
        finally:
            await client.close()

    asyncio.run(run())


def test_a_missing_report_is_404_and_a_bad_name_is_400(home):
    async def run():
        client = await _client()
        try:
            resp = await client.get(
                "/api/sessions/s1/reports/20260826T031200Z-i-1.html", headers=BEARER
            )
            assert resp.status == 404
            resp = await client.get("/api/sessions/s1/reports/notes.html", headers=BEARER)
            assert resp.status == 400
        finally:
            await client.close()

    asyncio.run(run())


def test_a_stub_is_404_over_the_route_too(home):
    """The size/HTML check is not only the CLI's — a file that would not pass
    the gate must not be reachable as a report either."""
    path = write(reports.target("s1", "i-1"), "<html></html>")

    async def run():
        client = await _client()
        try:
            resp = await client.get(f"/api/sessions/s1/reports/{path.name}", headers=BEARER)
            assert resp.status == 404
        finally:
            await client.close()

    asyncio.run(run())


def test_the_report_routes_need_authentication(home):
    path = write(reports.target("s1", "i-1"))

    async def run():
        client = await _client()
        try:
            assert (await client.get("/api/sessions/s1/reports")).status == 401
            assert (await client.get(f"/api/sessions/s1/reports/{path.name}")).status == 401
        finally:
            await client.close()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# the workflow that has to demand one
# --------------------------------------------------------------------------- #
BUNDLED = Path("src/claude_launcher/workflows/improv-worker.yaml")
OVERRIDE = Path(".claunch/workflows/improv-worker.yaml")


def wrapup_of(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["steps"]["wrapup"]


@pytest.mark.parametrize("path", [BUNDLED, OVERRIDE], ids=["bundled", "override"])
def test_improv_worker_wrapup_asks_for_the_report_in_both_layers(path):
    text = wrapup_of(path)["instructions"]
    assert "회차 보고서" in text
    assert "claunch report path" in text
    assert "claunch report save" in text
    assert "claunch report ls" in wrapup_of(path)["done_when"]


def test_the_prose_is_identical_in_both_layers():
    """The project copy shadows the bundled one, so prose changed in only one
    of them either does not take effect here or does not ship."""
    a, b = wrapup_of(BUNDLED), wrapup_of(OVERRIDE)
    assert a["instructions"] == b["instructions"]
    assert a["done_when"] == b["done_when"]


def test_only_the_project_layer_arms_the_gate():
    """Where the machine check may live, and why it is not both.

    The canonical file ships to every repository, so it carries no ``verify``
    at all (``test_cflow_layers`` pins that for the whole improv trio) — a
    repository that wants the check grafts one into its project layer. The
    cost is real and is the point of writing it down: elsewhere the report is
    prose, and prose is what this change exists to stop relying on.
    """
    assert wrapup_of(BUNDLED).get("verify") is None
    assert wrapup_of(OVERRIDE)["verify"] == (
        "uv run --no-sync python tools/report_check.py"
    )


def test_the_gate_runs_the_checkout_rather_than_whatever_claunch_is_installed():
    """Two obvious spellings of this gate were tried, and both ran another tree.

    `claunch report check` first: the `claunch` on PATH is an installed copy,
    not this tree, and it was measured answering "invalid choice: 'report'"
    with exit 2 — a gate that fails in every session until the branch lands
    and is reinstalled.

    Then `-m claude_launcher.cli report check`, which reads like it fixed
    that and did not. This is a src layout, so `-m` cannot find the package
    under the working directory and takes it from site-packages — and
    `--no-sync` is a promise that a worktree's .venv is never populated.
    Measured in a worker worktree: ModuleNotFoundError, exit 1.

    So the gate names a file in this checkout, and that file puts this
    checkout's src ahead of every installed copy. The mechanism itself, and
    the silent-green case where an installed copy answers to the same name
    with different behaviour, are pinned in
    tests/test_gates_run_this_checkout.py."""
    verify = wrapup_of(OVERRIDE)["verify"]
    assert verify.startswith("uv run --no-sync python")
    assert "tools/report_check.py" in verify
    assert " -m " not in verify
