"""The daemon's side of the board (daemon/beads.py): a session's issues.

Three moments and one view. At creation a session with a task gets an issue
minted for it (or adopts the one its request names) and is told the id in
its opening message; at a kill a session holding active issues is wound
down — the block typed in, the turn waited for — before it is terminated; at
exit the board is swept so what it was working on goes back to ``open``.
The view is the match between a session and the board's issues, explained
per issue, which is what the rail and the Beads page draw.

``br`` never runs here: a fake runner answers the argv the daemon composes
through :func:`cli_beads.plan`, keeping an in-memory board, so the tests
pin *what the daemon asks the board to do*, and the API tests drive the
real routes over the real session manager with a stub harness.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

from claude_launcher import lineage, profile, store
from claude_launcher.daemon import beads as beads_mod
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.mesh import MeshManager
from claude_launcher.daemon.session import Session

CHILD = (
    "import sys\n"
    "print('READY')\n"
    "for line in sys.stdin:\n"
    "    print('echo:' + line.strip())\n"
)
BEARER = {"Authorization": "Bearer sekrit"}


# --------------------------------------------------------------------------- #
# a board in memory, answering br's argv
# --------------------------------------------------------------------------- #
class FakeBr:
    """Enough of ``br`` for the daemon: list/show/comments/create/update/close,
    keyed by the ``--db`` the daemon names, remembering every call."""

    def __init__(self):
        self.issues: dict = {}
        self.comments: dict = {}
        self.calls: list = []
        self._n = 0

    def add(self, **kw) -> dict:
        iid = kw.pop("id", None)
        if iid is None:
            self._n += 1
            iid = f"t-{self._n}"
        issue = {
            "id": iid,
            "title": "",
            "status": "open",
            "priority": 2,
            "issue_type": "task",
            "assignee": None,
            "created_by": None,
            "labels": [],
            "updated_at": f"2026-08-25T00:00:{len(self.issues):02d}Z",
            "description": "",
        }
        issue.update(kw)
        self.issues[issue["id"]] = issue
        return issue

    async def __call__(self, argv, cwd):
        self.calls.append(list(argv))
        args = list(argv[1:])
        actor = None
        db = None
        while args and args[0] in ("--db", "--actor"):
            flag = args.pop(0)
            val = args.pop(0)
            if flag == "--db":
                db = val
            else:
                actor = val
        if "--json" in args:
            args.remove("--json")
        cmd, rest = args[0], args[1:]
        if cmd == "list":
            return 0, json.dumps({"issues": list(self.issues.values())}), ""
        if cmd == "show":
            i = self.issues.get(rest[0])
            return (0, json.dumps([i]), "") if i else (1, "", f"no issue {rest[0]}")
        if cmd == "comments":
            if rest[0] == "list":
                return 0, json.dumps(self.comments.get(rest[1], [])), ""
            iid, text = rest[1], rest[2]
            if iid not in self.issues:
                return 1, "", f"no issue {iid}"
            self.comments.setdefault(iid, []).append({"author": actor, "text": text})
            return 0, json.dumps({"ok": True}), ""
        if cmd == "create":
            opts = _opts(rest[1:])
            issue = self.add(
                title=rest[0],
                issue_type=opts.get("--type", "task"),
                priority=int(opts.get("--priority", 2)),
                labels=(opts.get("--labels") or "").split(",") if opts.get("--labels") else [],
                assignee=opts.get("--assignee"),
                description=opts.get("--description", ""),
                created_by=actor,
            )
            return 0, json.dumps(issue), ""
        if cmd == "update":
            i = self.issues.get(rest[0])
            if not i:
                return 1, "", f"no issue {rest[0]}"
            opts = _opts(rest[1:])
            if "--status" in opts:
                i["status"] = opts["--status"]
            if "--assignee" in opts:
                i["assignee"] = opts["--assignee"]
            return 0, json.dumps([i]), ""
        if cmd == "close":
            i = self.issues.get(rest[0])
            if not i:
                return 1, "", f"no issue {rest[0]}"
            i["status"] = "closed"
            i["close_reason"] = _opts(rest[1:]).get("--reason")
            return 0, json.dumps([i]), ""
        return 1, "", f"unknown command {cmd}"


def _opts(args):
    out = {}
    i = 0
    while i < len(args):
        if args[i].startswith("--") and i + 1 < len(args):
            out[args[i]] = args[i + 1]
            i += 2
        else:
            i += 1
    return out


@pytest.fixture
def repo(tmp_path):
    """A directory with a board in it (the db file is all the daemon checks)."""
    root = tmp_path / "repo"
    (root / ".beads").mkdir(parents=True)
    (root / ".beads" / "beads.db").write_bytes(b"")
    return root


def _board(br: FakeBr, root: Path) -> beads_mod.Board:
    return beads_mod.Board(br, root_for=lambda cwd: root if cwd else None)


def _sdef(name: str, cwd: Path, **kw) -> SessionDef:
    return SessionDef(name=name, harness="py", cwd=str(cwd), **kw)


class _Sess:
    """A session as the board sees it: a definition and a status."""

    def __init__(self, sdef, status="idle", exit_code=None):
        self.sdef = sdef
        self._status = status
        self.exit_code = exit_code
        self.exited = status == "exited"

    def status(self):
        return self._status


# --------------------------------------------------------------------------- #
# the pure rules
# --------------------------------------------------------------------------- #
def test_issue_refs_reads_the_workflows_convention():
    assert beads_mod.issue_refs("do it\nissue: claunch-2a3.", None, "issue: x-1") == [
        "claunch-2a3", "x-1",
    ]
    assert beads_mod.issue_refs("no reference here") == []


def test_match_explains_each_link_and_orders_the_linked_issue_first():
    rows = [
        {"id": "a", "status": "open", "assignee": "s1", "updated_at": "2026-01-01T00:00:00Z"},
        {"id": "b", "status": "in_progress", "created_by": "s1", "updated_at": "2026-01-02T00:00:00Z"},
        {"id": "c", "status": "closed", "assignee": "s1", "updated_at": "2026-01-03T00:00:00Z"},
        {"id": "d", "status": "open", "assignee": "s2"},
        {"id": "e", "status": "open"},
    ]
    got = beads_mod.match(rows, "s1", issue="c", task="see issue: e")
    assert [i["id"] for i in got] == ["c", "b", "a", "e"]
    via = {i["id"]: i["via"] for i in got}
    assert via["c"] == ["link", "assignee"]
    assert via["b"] == ["created_by"]
    assert via["e"] == ["task"]
    assert "d" not in via


def test_issue_title_is_the_first_real_line_trimmed():
    assert beads_mod.issue_title("\n# Fix the thing\nmore") == "Fix the thing"
    assert beads_mod.issue_title("x" * 200).endswith("…")
    assert len(beads_mod.issue_title("x" * 200)) == beads_mod.DEFAULT_TITLE_LIMIT
    assert beads_mod.issue_title("   ") == ""


def test_sweep_plan_returns_in_progress_to_open_and_closes_untouched_placeholders():
    mine = [
        {"id": "w", "status": "in_progress", "assignee": "s1"},
        {"id": "r", "status": "in_review", "assignee": "s1"},
        {"id": "p", "status": "open", "assignee": "s1", "created_by": "s1",
         "labels": ["session", "user"]},
        {"id": "h", "status": "open", "assignee": "s1", "created_by": "lead",
         "labels": ["leader"]},
        {"id": "o", "status": "in_progress", "assignee": "s2"},
    ]
    plan = beads_mod.sweep_plan(mine, "s1", exit_code=0)
    assert plan == [
        ["comments", "add", "w",
         "SESSION ENDED: s1 exited (code 0); returned to open by the claunch "
         "daemon — reassign or resume"],
        ["update", "w", "--status", "open"],
        ["close", "p", "--reason", "session s1 ended (code 0) before taking this up"],
    ]


def test_the_winddown_block_names_the_issues_and_the_grace():
    text = beads_mod.compose_winddown(
        "s7", [{"id": "x-1", "status": "in_progress", "title": "Do it"}], 90
    )
    assert "- x-1 [in_progress] Do it" in text
    assert "after 90s regardless" in text
    assert "in_review" in text and "HANDOFF" in text
    assert "close" not in text.lower().replace("closest", "")  # closing stays the leader's


# --------------------------------------------------------------------------- #
# the board, through the fake br
# --------------------------------------------------------------------------- #
def test_every_br_call_names_the_board_and_stamps_the_actor(repo):
    br = FakeBr()
    board = _board(br, repo)

    async def run():
        sess = _Sess(_sdef("s3", repo, task="Build the widget"))
        made = await board.ensure_issue(sess, body={"task": "Build the widget"}, parent=None)
        assert made == {"issue": "t-1", "created": True}
        create = br.calls[-1]
        assert create[:5] == ["br", "--db", str(repo / ".beads" / "beads.db"), "--actor", "s3"]
        assert create[5:7] == ["create", "Build the widget"]
        assert "--json" in create

    asyncio.run(run())


def test_a_task_mints_an_issue_assigned_and_labelled_by_origin(repo):
    br = FakeBr()
    board = _board(br, repo)

    async def run():
        me = _Sess(_sdef("s3", repo))
        made = await board.ensure_issue(
            me, body={"task": "First line is the title\n\nand the body"}, parent=None
        )
        issue = br.issues[made["issue"]]
        assert issue["title"] == "First line is the title"
        assert issue["assignee"] == "s3"
        assert issue["created_by"] == "s3"
        assert issue["labels"] == ["session", "user"]
        assert "## 목표" in issue["description"] and "## 출처" in issue["description"]
        assert "operator (new session)" in issue["description"]

        kid = _Sess(_sdef("s4", repo, parent="s3"))
        made = await board.ensure_issue(kid, body={"task": "child work"}, parent="s3")
        assert br.issues[made["issue"]]["labels"] == ["session", "leader"]
        assert "session s3 (spawn)" in br.issues[made["issue"]]["description"]

    asyncio.run(run())


def test_a_request_naming_an_issue_adopts_it_instead_of_minting(repo):
    br = FakeBr()
    br.add(id="claunch-9", title="assigned by the leader", assignee="lead")
    board = _board(br, repo)

    async def run():
        me = _Sess(_sdef("w1", repo))
        made = await board.ensure_issue(
            me, body={"task": "go", "context": "issue: claunch-9"}, parent="lead"
        )
        assert made == {"issue": "claunch-9", "created": False}
        assert br.issues["claunch-9"]["assignee"] == "w1"
        assert len(br.issues) == 1  # nothing minted

        # an explicit field wins too, and an unknown id is not adopted
        made = await board.ensure_issue(
            _Sess(_sdef("w2", repo)), body={"task": "go", "issue": "nope-1"}, parent=None
        )
        assert made is None

    asyncio.run(run())


def test_no_task_means_no_issue_and_a_missing_board_is_never_an_error(repo, tmp_path):
    br = FakeBr()
    board = _board(br, repo)

    async def run():
        assert await board.ensure_issue(_Sess(_sdef("s1", repo)), body={}, parent=None) is None
        assert br.calls == []
        bare = beads_mod.Board(br, root_for=lambda cwd: None)
        assert await bare.ensure_issue(
            _Sess(_sdef("s1", tmp_path)), body={"task": "x"}, parent=None
        ) is None
        view = await bare.session_view(_Sess(_sdef("s1", tmp_path)))
        assert view["issues"] == [] and "no board" in view["error"]

    asyncio.run(run())


def test_the_setting_turns_creation_off(repo):
    br = FakeBr()
    board = _board(br, repo)
    store.update(lambda doc: doc.update({"daemon": {"beads_auto_issue": False}}))

    async def run():
        assert await board.ensure_issue(
            _Sess(_sdef("s1", repo)), body={"task": "x"}, parent=None
        ) is None
        assert br.calls == []

    asyncio.run(run())


def test_session_view_and_fleet_view_draw_the_same_match(repo):
    br = FakeBr()
    br.add(id="a", title="mine", assignee="s1", status="in_progress")
    br.add(id="b", title="theirs", assignee="s2")
    br.add(id="c", title="referenced", created_by="lead")
    board = _board(br, repo)

    async def run():
        s1 = _Sess(_sdef("s1", repo, issue="a", task="also issue: c"))
        s2 = _Sess(_sdef("s2", repo), status="busy")
        view = await board.session_view(s1)
        assert view["root"] == str(repo) and view["issue"] == "a"
        assert [(i["id"], i["via"]) for i in view["issues"]] == [
            ("a", ["link", "assignee"]), ("c", ["task"]),
        ]
        fleet = await board.fleet_view([s1, s2], extra_roots=[str(repo)])
        assert len(fleet["boards"]) == 1
        b = fleet["boards"][0]
        assert [s["name"] for s in b["sessions"]] == ["s1", "s2"]
        owners = {i["id"]: [(s["name"], s["status"]) for s in i["sessions"]] for i in b["issues"]}
        assert owners == {"a": [("s1", "idle")], "b": [("s2", "busy")], "c": [("s1", "idle")]}

    asyncio.run(run())


def test_listings_are_cached_briefly_and_writes_invalidate(repo):
    br = FakeBr()
    now = [100.0]
    board = beads_mod.Board(br, root_for=lambda cwd: repo, clock=lambda: now[0])

    async def run():
        await board.issues(repo)
        await board.issues(repo)
        assert sum(1 for c in br.calls if "list" in c) == 1
        now[0] += beads_mod.CACHE_TTL + 0.1
        await board.issues(repo)
        assert sum(1 for c in br.calls if "list" in c) == 2
        await board.br(repo, ["create", "x"], actor="s1")
        await board.issues(repo)
        assert sum(1 for c in br.calls if "list" in c) == 3

    asyncio.run(run())


def test_the_sweep_acts_on_what_the_session_was_assigned(repo):
    br = FakeBr()
    br.add(id="w", assignee="s1", status="in_progress")
    br.add(id="p", assignee="s1", created_by="s1", labels=["session", "user"])
    br.add(id="r", assignee="s1", status="in_review")
    br.add(id="o", assignee="s2", status="in_progress")
    board = _board(br, repo)

    async def run():
        done = await board.sweep(_Sess(_sdef("s1", repo), status="exited", exit_code=1))
        assert len(done) == 3
        assert br.issues["w"]["status"] == "open"
        assert br.comments["w"][0]["text"].startswith("SESSION ENDED: s1 exited (code 1)")
        assert br.comments["w"][0]["author"] == "s1"
        assert br.issues["p"]["status"] == "closed"
        assert br.issues["r"]["status"] == "in_review"
        assert br.issues["o"]["status"] == "in_progress"

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# through the API, over real sessions
# --------------------------------------------------------------------------- #
def _register_py_harness():
    store.update(
        lambda doc: doc.update(
            {"harnesses": {"py": {"command": [sys.executable, "-u", "-c", CHILD]}}}
        )
    )
    if not profile.resolve("py").exists():
        lineage.set_harness(profile.create("py"), "py")


async def _serve(mgr, mm, board):
    from aiohttp.test_utils import TestClient, TestServer

    app = build_app(mgr, "sekrit", started_at=time.monotonic(), mesh=mm, beads=board)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def _wait_for(cond, what: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def test_create_and_spawn_link_an_issue_and_tell_the_agent(home, tmp_path, repo):
    _register_py_harness()
    br = FakeBr()
    board = _board(br, repo)

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm, board)
        try:
            resp = await client.post(
                "/api/sessions",
                json={"name": "lead", "profile": "py", "cwd": str(repo), "task": "Lead the work"},
                headers=BEARER,
            )
            doc = await resp.json()
            assert resp.status == 201, doc
            assert doc["beads"] == {"issue": "t-1", "created": True}
            assert doc["issue"] == "t-1"
            assert mgr.get("lead").sdef.issue == "t-1"
            assert br.issues["t-1"]["assignee"] == "lead"

            # the child adopts the issue its parent hands it, and is told so
            br.add(id="claunch-5", title="worker's job", assignee="lead")
            resp = await client.post(
                "/api/sessions/lead/children",
                json={"name": "w1", "task": "do the job\nissue: claunch-5"},
                headers=BEARER,
            )
            doc = await resp.json()
            assert resp.status == 201, doc
            assert doc["beads"] == {"issue": "claunch-5", "created": False}
            assert mgr.get("w1").sdef.issue == "claunch-5"
            assert br.issues["claunch-5"]["assignee"] == "w1"

            # the rail's view and the page's view
            resp = await client.get("/api/sessions/w1/meta", headers=BEARER)
            meta = await resp.json()
            assert meta["beads"]["issue"] == "claunch-5"
            assert [i["id"] for i in meta["beads"]["issues"]] == ["claunch-5"]
            resp = await client.get("/api/beads", headers=BEARER)
            fleet = await resp.json()
            assert [s["name"] for s in fleet["boards"][0]["sessions"]] == ["lead", "w1"]
            resp = await client.get(f"/api/beads/t-1?cwd={repo}", headers=BEARER)
            assert (await resp.json())["issue"]["title"] == "Lead the work"

            # the record survives a restart: the link is in the definition
            mgr.persist()
            fresh = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=False)
            fresh.restore_all()
            assert fresh.get("w1").sdef.issue == "claunch-5"
            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_the_opening_names_the_issue_only_when_the_task_did_not(home, tmp_path, repo):
    """What the agent reads first: a minted issue is announced as a line in
    the task; an adopted one already in the task is not repeated."""
    from claude_launcher.daemon import onboard

    _register_py_harness()
    br = FakeBr()
    br.add(id="claunch-5", assignee="lead")
    board = _board(br, repo)
    seen = {}
    real = onboard.arrange

    async def spy(plan, **kw):
        seen[kw["name"]] = plan.task
        return await real(plan, **kw)

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm, board)
        onboard.arrange = spy
        try:
            await client.post(
                "/api/sessions",
                json={"name": "a", "profile": "py", "cwd": str(repo), "task": "minted"},
                headers=BEARER,
            )
            await client.post(
                "/api/sessions",
                json={"name": "b", "profile": "py", "cwd": str(repo),
                      "task": "adopted\nissue: claunch-5"},
                headers=BEARER,
            )
            assert seen["a"].startswith("minted\n\nissue: t-1 -- your board record")
            assert "claunch beads show t-1 --json" in seen["a"]
            assert seen["b"] == "adopted\nissue: claunch-5"
            await mgr.shutdown_all()
        finally:
            onboard.arrange = real
            await client.close()

    asyncio.run(run())


def test_a_kill_winds_down_first_then_terminates_and_sweeps(home, tmp_path, repo, monkeypatch):
    _register_py_harness()
    store.update(lambda doc: doc.update({"daemon": {"beads_winddown_grace": 5.0}}))
    monkeypatch.setattr(beads_mod, "REACT_WINDOW", 0.3)
    br = FakeBr()
    br.add(id="w", title="the job", assignee="w1", status="in_progress")
    board = _board(br, repo)
    typed = []

    async def fake_deliver(self, text):
        typed.append((self.sdef.name, text))
        return True

    monkeypatch.setattr(Session, "deliver", fake_deliver)

    async def run():
        mgr = SessionManager(idle_threshold=0.2, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm, board)
        try:
            mgr.create(SessionDef(name="w1", harness="py", cwd=str(repo)))
            resp = await client.delete("/api/sessions/w1", headers=BEARER)
            doc = await resp.json()
            assert resp.status == 200 and doc["winding_down"] is True
            assert not mgr.get("w1").exited
            assert "w1" in board.winddowns
            resp = await client.get("/api/sessions", headers=BEARER)
            rows = (await resp.json())["sessions"]
            assert rows[0]["winddown"]["issues"] == ["w"]

            await _wait_for(lambda: typed, "the block to be typed")
            assert typed[0][0] == "w1"
            assert "- w [in_progress] the job" in typed[0][1]
            # the harness never turns busy, so the react window ends the wait
            await _wait_for(lambda: mgr.get("w1").exited, "w1 to be terminated")
            await _wait_for(lambda: br.issues["w"]["status"] == "open", "the sweep")
            assert br.comments["w"][0]["text"].startswith("SESSION ENDED: w1 exited")
            assert "w1" not in board.winddowns
        finally:
            await client.close()

    asyncio.run(run())


def test_a_second_kill_or_force_stops_at_once(home, tmp_path, repo, monkeypatch):
    _register_py_harness()
    store.update(lambda doc: doc.update({"daemon": {"beads_winddown_grace": 60.0}}))
    br = FakeBr()
    br.add(id="w", assignee="w1", status="in_progress")
    br.add(id="v", assignee="w2", status="in_progress")
    board = _board(br, repo)

    async def slow_deliver(self, text):
        await asyncio.sleep(30)
        return True

    monkeypatch.setattr(Session, "deliver", slow_deliver)

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm, board)
        try:
            mgr.create(SessionDef(name="w1", harness="py", cwd=str(repo)))
            mgr.create(SessionDef(name="w2", harness="py", cwd=str(repo)))
            resp = await client.delete("/api/sessions/w1", headers=BEARER)
            assert (await resp.json())["winding_down"] is True
            # the same button again: stop now
            resp = await client.delete("/api/sessions/w1", headers=BEARER)
            assert "winding_down" not in await resp.json()
            await _wait_for(lambda: mgr.get("w1").exited, "w1 killed")
            # force never winds down
            resp = await client.delete("/api/sessions/w2?force=1", headers=BEARER)
            assert "winding_down" not in await resp.json()
            await _wait_for(lambda: mgr.get("w2").exited, "w2 killed")
            await board.cancel_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_no_active_issue_means_the_kill_is_immediate_and_the_setting_turns_it_off(
    home, tmp_path, repo
):
    _register_py_harness()
    br = FakeBr()
    br.add(id="c", assignee="w1", status="closed")
    br.add(id="w", assignee="w2", status="in_progress")
    board = _board(br, repo)

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm, board)
        try:
            mgr.create(SessionDef(name="w1", harness="py", cwd=str(repo)))
            resp = await client.delete("/api/sessions/w1", headers=BEARER)
            assert "winding_down" not in await resp.json()
            await _wait_for(lambda: mgr.get("w1").exited, "w1 killed")

            store.update(lambda doc: doc.update({"daemon": {"beads_winddown": False}}))
            mgr.create(SessionDef(name="w2", harness="py", cwd=str(repo)))
            resp = await client.delete("/api/sessions/w2", headers=BEARER)
            assert "winding_down" not in await resp.json()
            await _wait_for(lambda: mgr.get("w2").exited, "w2 killed")
            # the sweep is not a setting
            await _wait_for(lambda: br.issues["w"]["status"] == "open", "the sweep")
        finally:
            await client.close()

    asyncio.run(run())


def test_a_daemon_shutdown_is_not_an_exit_and_sweeps_nothing(home, tmp_path, repo):
    _register_py_harness()
    br = FakeBr()
    br.add(id="w", assignee="w1", status="in_progress")
    board = _board(br, repo)

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm, board)
        try:
            mgr.create(SessionDef(name="w1", harness="py", cwd=str(repo)))
            await mgr.shutdown_all()
            assert mgr.get("w1").exited
            await asyncio.sleep(0.3)
            assert br.issues["w"]["status"] == "in_progress"
            assert not any("comments" in c for c in br.calls)
        finally:
            await client.close()

    asyncio.run(run())


def test_the_rail_can_register_an_issue_after_the_fact(home, tmp_path, repo):
    _register_py_harness()
    br = FakeBr()
    board = _board(br, repo)

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm, board)
        try:
            mgr.create(SessionDef(name="s1", harness="py", cwd=str(repo)))
            resp = await client.post("/api/sessions/s1/beads", json={}, headers=BEARER)
            assert resp.status == 400
            resp = await client.post(
                "/api/sessions/s1/beads", json={"title": "Look into it"}, headers=BEARER
            )
            doc = await resp.json()
            assert resp.status == 201, doc
            assert doc["issue"] == "t-1" and doc["beads"]["issue"] == "t-1"
            assert mgr.get("s1").sdef.issue == "t-1"
            assert br.issues["t-1"]["assignee"] == "s1"
            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_a_rebriefing_restates_the_issue(home, tmp_path):
    from claude_launcher.daemon import rebrief

    text = rebrief._task_section("go", issue="claunch-2a3")
    assert "issue: claunch-2a3 -- your board record" in text
    assert "claunch beads show claunch-2a3 --json" in text
    assert rebrief._task_section("", issue=None) == ""
    assert "issue: x-1" in rebrief._task_section("", issue="x-1")
