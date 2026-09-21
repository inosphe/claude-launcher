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

from claude_launcher import beads_meta, lineage, profile, store
from claude_launcher.daemon import beads as beads_mod
from claude_launcher.daemon import db, paths
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
    """Enough of ``br`` for the daemon: list/show/comments/dep/create/update/
    close, keyed by the ``--db`` the daemon names, remembering every call."""

    def __init__(self):
        self.issues: dict = {}
        self.comments: dict = {}
        #: edges by the DEPENDING issue, which is where ``br`` stores them
        self.deps: dict = {}
        self.calls: list = []
        self._n = 0

    def link(self, child: str, parent: str, kind: str = "parent-child") -> None:
        """``br dep add <child> <parent> --type <kind>`` -- the child depends."""
        self.deps.setdefault(child, []).append(
            {"issue_id": child, "depends_on_id": parent, "type": kind})
        self.issues[child]["dependency_count"] = len(self.deps[child])
        self.issues[parent]["dependent_count"] = (
            self.issues[parent].get("dependent_count", 0) + 1)

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
            "dependency_count": 0,
            "dependent_count": 0,
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
            opts = _opts(rest)
            rows = list(self.issues.values())
            if "--priority" in opts:
                rows = [r for r in rows if r.get("priority") == int(opts["--priority"])]
            # `--status` is repeatable and the real br ORs the values; a
            # comma-joined single value matches nothing, which is the bug
            # claunch-beads-list-comma-status-tmh records, so the fake is
            # literal about it and only ever matches whole values.
            wanted = _opts_all(rest, "--status")
            if wanted:
                rows = [r for r in rows if r.get("status") in wanted]
            elif "--all" not in rest:
                rows = [r for r in rows if r.get("status") != "closed"]
            # The count br reports is of every MATCHING issue, taken before
            # the window -- it is what a page control counts pages with.
            total = len(rows)
            offset = int(opts.get("--offset", 0))
            limit = int(opts.get("--limit", 0))
            rows = rows[offset:]
            if limit:
                rows = rows[:limit]
            return 0, json.dumps({
                "issues": rows, "total": total,
                "offset": offset, "limit": limit,
            }), ""
        if cmd == "show":
            i = self.issues.get(rest[0])
            return (0, json.dumps([i]), "") if i else (1, "", f"no issue {rest[0]}")
        if cmd == "dep":
            if rest[0] == "list":
                return 0, json.dumps(self.deps.get(rest[1], [])), ""
            if rest[0] == "add":
                kind = _opts(rest[3:]).get("--type", "blocks")
                self.link(rest[1], rest[2], kind)
                return 0, json.dumps({"ok": True}), ""
            return 1, "", f"fake br has no dep {rest[0]}"
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
            if "--description" in opts:
                i["description"] = opts["--description"]
            return 0, json.dumps([i]), ""
        if cmd == "close":
            i = self.issues.get(rest[0])
            if not i:
                return 1, "", f"no issue {rest[0]}"
            i["status"] = "closed"
            i["close_reason"] = _opts(rest[1:]).get("--reason")
            return 0, json.dumps([i]), ""
        return 1, "", f"unknown command {cmd}"


def _opts_all(args, flag):
    """Every value given for a repeatable flag, in order."""
    return [args[i + 1] for i in range(len(args) - 1) if args[i] == flag]


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
        {"id": "f", "status": "in_ready", "created_by": "s1", "updated_at": "2026-01-04T00:00:00Z"},
    ]
    got = beads_mod.match(rows, "s1", issue="c", task="see issue: e")
    assert [i["id"] for i in got] == ["c", "b", "f", "a", "e"]
    via = {i["id"]: i["via"] for i in got}
    assert via["c"] == ["link", "assignee"]
    assert via["b"] == ["created_by"]
    assert via["f"] == ["created_by"]
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
        {"id": "q", "status": "in_ready", "assignee": "s1"},
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
    assert not any("q" in step for step in plan)


def test_sweep_plan_marks_orphaned_followups_and_releases_self_queued_ones():
    """A session's exit also settles the follow-ups IT filed, not just what it
    was assigned: one nobody picked up gets a marker the leader's 3-day sweep
    can key on, one it queued to itself is let go so it does not sit
    invisibly stuck to a name that cannot answer."""
    mine = [
        {"id": "orphan-open", "status": "open", "created_by": "s1"},
        {"id": "orphan-ready", "status": "in_ready", "created_by": "s1"},
        {"id": "self-open", "status": "open", "assignee": "s1",
         "created_by": "s1", "labels": ["found"]},
        {"id": "self-ready", "status": "in_ready", "assignee": "s1",
         "created_by": "s1"},
        # untouched: someone else's follow-up, and one already past triage
        {"id": "others", "status": "open", "created_by": "lead"},
        {"id": "in-review", "status": "in_review", "assignee": "s1",
         "created_by": "s1"},
    ]
    plan = beads_mod.sweep_plan(mine, "s1", exit_code=1)
    assert plan == [
        ["comments", "add", "orphan-open",
         "SESSION ENDED: creator s1 exited (code 1); follow-up left "
         "unassigned (ORPHANED FOLLOW-UP)"],
        ["comments", "add", "orphan-ready",
         "SESSION ENDED: creator s1 exited (code 1); follow-up left "
         "unassigned (ORPHANED FOLLOW-UP)"],
        ["comments", "add", "self-open",
         "SESSION ENDED: creator s1 exited (code 1); self-queued follow-up "
         "released to the pool (SELF-QUEUE RELEASED)"],
        ["update", "self-open", "--assignee", ""],
        ["comments", "add", "self-ready",
         "SESSION ENDED: creator s1 exited (code 1); self-queued follow-up "
         "released to the pool (SELF-QUEUE RELEASED)"],
        ["update", "self-ready", "--assignee", ""],
    ]


def test_self_queued_release_does_not_conflict_with_the_placeholder_close():
    """The two branches are told apart by the ``session`` label alone -- both
    are ``open``, self-assigned, and created by the exited session."""
    mine = [
        {"id": "placeholder", "status": "open", "assignee": "s1",
         "created_by": "s1", "labels": ["session", "user"]},
        {"id": "self-queued", "status": "open", "assignee": "s1",
         "created_by": "s1", "labels": ["found"]},
    ]
    plan = beads_mod.sweep_plan(mine, "s1", exit_code=0)
    assert plan == [
        ["close", "placeholder", "--reason",
         "session s1 ended (code 0) before taking this up"],
        ["comments", "add", "self-queued",
         "SESSION ENDED: creator s1 exited (code 0); self-queued follow-up "
         "released to the pool (SELF-QUEUE RELEASED)"],
        ["update", "self-queued", "--assignee", ""],
    ]


def test_a_joiners_exit_does_not_return_the_holders_issue_to_open():
    """The sweep acts on what a session was ASSIGNED, and a joiner never was.

    Worth pinning rather than reading off the code: a joiner's issue is
    linked to it (its rail shows it, its opening block names it), so every
    view says the issue is "its" — and an exit that swept on that view would
    quietly take a running session's work back to open.
    """
    mine = [
        # the joiner is on this issue, but somebody else holds it
        {"id": "shared", "status": "in_progress", "assignee": "holder",
         "via": ["link"]},
        # ...and this one really is the joiner's own
        {"id": "own", "status": "in_progress", "assignee": "w2"},
    ]
    plan = beads_mod.sweep_plan(mine, "w2", exit_code=0)
    assert [p[1] for p in plan if p[0] == "update"] == ["own"]
    assert not any("shared" in step for step in plan)


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
        # Equality, not containment: the report IS the contract the CLI and
        # the web read back, so a key appearing or vanishing is a change to
        # be made on purpose. ``from_issue_text`` says which of the two boxes
        # the issue was written from.
        assert made == {
            "issue": "t-1", "created": True, "mode": beads_mod.MINTED,
            "held_by": None, "from_issue_text": False,
            "why": "minted from the opening task",
        }
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


def test_a_request_naming_a_free_issue_takes_it_instead_of_minting(repo):
    br = FakeBr()
    br.add(id="claunch-9", title="written by the leader, unassigned")
    board = _board(br, repo)

    async def run():
        me = _Sess(_sdef("w1", repo))
        made = await board.ensure_issue(
            me, body={"task": "go", "context": "issue: claunch-9"}, parent="lead"
        )
        assert made["issue"] == "claunch-9" and made["created"] is False
        assert made["mode"] == beads_mod.TAKE and made["held_by"] is None
        assert br.issues["claunch-9"]["assignee"] == "w1"
        assert len(br.issues) == 1  # nothing minted

        # an explicit field wins too, and an unknown id is not adopted
        made = await board.ensure_issue(
            _Sess(_sdef("w2", repo)), body={"task": "go", "issue": "nope-1"}, parent=None
        )
        assert made is None

    asyncio.run(run())


class _Manager:
    """Only what ``adoption`` asks a manager: get(name) -> session, or raise."""

    def __init__(self, **sessions):
        self._by_name = sessions

    def get(self, name):
        if name not in self._by_name:
            raise KeyError(name)
        return self._by_name[name]


def test_adoption_takes_a_free_or_dead_issue_and_only_joins_a_live_holder():
    live = lambda name: {"s1": True, "gone": False}.get(name)  # noqa: E731

    assert beads_mod.adoption({}, session="w1", running=live)["mode"] == beads_mod.TAKE
    assert beads_mod.adoption(
        {"assignee": ""}, session="w1", running=live
    )["mode"] == beads_mod.TAKE
    # already mine: taken, and with no holder to report
    mine = beads_mod.adoption({"assignee": "w1"}, session="w1", running=live)
    assert mine["mode"] == beads_mod.TAKE and mine["held_by"] is None
    # a session of ours that has exited: its sweep already let go
    dead = beads_mod.adoption({"assignee": "gone"}, session="w1", running=live)
    assert dead["mode"] == beads_mod.TAKE and dead["held_by"] == "gone"
    # a running session: joined, never taken
    held = beads_mod.adoption({"assignee": "s1"}, session="w1", running=live)
    assert held["mode"] == beads_mod.JOIN and held["held_by"] == "s1"
    # a name the daemon knows nothing about (a human, another machine)
    other = beads_mod.adoption({"assignee": "alice"}, session="w1", running=live)
    assert other["mode"] == beads_mod.JOIN and other["held_by"] == "alice"


def test_an_issue_a_running_session_holds_is_joined_and_the_assignment_stands(repo):
    br = FakeBr()
    br.add(id="claunch-9", title="the leader's own", assignee="lead")
    board = _board(br, repo)
    mgr = _Manager(lead=_Sess(_sdef("lead", repo), status="busy"))

    async def run():
        made = await board.ensure_issue(
            _Sess(_sdef("w1", repo)),
            body={"task": "go", "issue": "claunch-9"}, parent="lead", manager=mgr,
        )
        assert made["mode"] == beads_mod.JOIN and made["held_by"] == "lead"
        # the board was NOT written with a new assignee...
        assert br.issues["claunch-9"]["assignee"] == "lead"
        assert not any(
            c[:2] == ["update", "claunch-9"] or "--assignee" in c for c in br.calls
        )
        # ...but the join is on the record, so a later reader sees both
        text = br.comments["claunch-9"][0]["text"]
        assert text.startswith("JOINED: session w1")
        assert "lead" in text and "assignee left unchanged" in text

    asyncio.run(run())


def test_an_issue_held_by_an_exited_session_is_taken_over(repo):
    br = FakeBr()
    br.add(id="claunch-9", title="orphaned", assignee="old")
    board = _board(br, repo)
    mgr = _Manager(old=_Sess(_sdef("old", repo), status="exited", exit_code=0))

    async def run():
        made = await board.ensure_issue(
            _Sess(_sdef("w1", repo)),
            body={"task": "go", "issue": "claunch-9"}, parent=None, manager=mgr,
        )
        assert made["mode"] == beads_mod.TAKE and made["held_by"] == "old"
        assert br.issues["claunch-9"]["assignee"] == "w1"
        assert "claunch-9" not in br.comments

    asyncio.run(run())


def test_beads_false_is_the_no_issue_answer(repo):
    br = FakeBr()
    br.add(id="claunch-9", title="named but declined")
    board = _board(br, repo)

    async def run():
        for answer in (False, "none", "none-auto"):
            br.calls.clear()
            assert await board.ensure_issue(
                _Sess(_sdef("w1", repo)),
                body={"task": "go", "issue": "claunch-9", "beads": answer},
                parent=None,
            ) is None, answer
            assert br.calls == [], answer

    asyncio.run(run())


def test_the_two_no_issue_answers_are_read_from_one_key():
    """Both leave the board untouched, so nothing downstream of the mint can
    tell them apart -- and they are opposite instructions to the session.
    Reading them in one place is what keeps the code that skips the mint and
    the code that writes the opening block from disagreeing about what the
    operator said.
    """
    mode = beads_mod.none_mode
    assert mode({"beads": False}) == beads_mod.NONE_WAIT
    assert mode({"beads": "none"}) == beads_mod.NONE_WAIT   # the form's value
    assert mode({"beads": "none-auto"}) == beads_mod.NONE_AUTO
    assert mode({"beads": " None-Auto "}) == beads_mod.NONE_AUTO
    # not a no-issue answer at all
    assert mode({}) is None
    assert mode({"beads": True}) is None
    assert mode({"task": "go", "issue": "x-1"}) is None


def test_each_no_issue_note_forbids_what_the_other_one_asks_for():
    """A block saying only "no issue was created" leaves the session free to
    decide that finding one is helpful, which is the behaviour this splits."""
    wait = beads_mod.compose_none_note(beads_mod.NONE_WAIT)
    auto = beads_mod.compose_none_note(beads_mod.NONE_AUTO, session="w1")
    assert wait.startswith("no issue:") and auto.startswith("no issue:")

    assert "wait for instructions" in wait
    assert "Do NOT search the board" in wait
    assert "wait for the user to type one" in wait

    assert "assign yourself" in auto
    assert "claunch beads update <id> --assignee w1" in auto
    assert "do NOT need anyone to confirm" in auto
    # and it does not turn "take one off the board" into "write a new one"
    assert "rather than minting an issue" in auto


def test_issue_text_writes_the_issue_and_the_task_is_left_alone(repo):
    """The whole point of the box: the board holds the specification and the
    terminal holds the first instruction, and they are no longer one text."""
    br = FakeBr()
    board = _board(br, repo)

    async def run():
        made = await board.ensure_issue(
            _Sess(_sdef("s3", repo)),
            body={
                "task": "read your issue and start",
                "issue_text": (
                    "Rail must answer the board" + chr(10) * 2
                    + "every row, one call"
                ),
            },
            parent=None,
        )
        issue = br.issues[made["issue"]]
        assert issue["title"] == "Rail must answer the board"
        assert "every row, one call" in issue["description"]
        # the task is NOT what was written down, and the record says so
        assert "read your issue and start" not in issue["description"]
        assert "issue text written when session s3 was created" in issue["description"]
        assert made["from_issue_text"] is True

    asyncio.run(run())


def test_issue_text_alone_still_mints(repo):
    """The guard used to read "no task, nothing to write down". With two
    boxes that is no longer the same sentence: a session created with only a
    specification must still get its issue."""
    br = FakeBr()
    board = _board(br, repo)

    async def run():
        made = await board.ensure_issue(
            _Sess(_sdef("s1", repo)), body={"issue_text": "the whole job"},
            parent=None,
        )
        assert made and made["created"] is True
        assert br.issues[made["issue"]]["title"] == "the whole job"

    asyncio.run(run())


def test_an_empty_issue_text_changes_nothing(repo):
    """Every caller that predates the field, and every form left blank."""
    br = FakeBr()
    board = _board(br, repo)

    async def run():
        made = await board.ensure_issue(
            _Sess(_sdef("s1", repo)),
            body={"task": "mint from me", "issue_text": "   "}, parent=None,
        )
        issue = br.issues[made["issue"]]
        assert issue["title"] == "mint from me"
        assert "opening task of session s1" in issue["description"]
        assert made["from_issue_text"] is False

    asyncio.run(run())


def test_a_contradictory_board_answer_is_refused_rather_than_half_applied():
    """``issue_text`` says "write a new one" and each of the other three ways
    of answering says "do something else with the board". Sent together the
    adopt branch would win and the written text would vanish without a word,
    which is the failure this area exists to remove."""
    check = beads_mod.check_request
    assert check({"issue_text": "spec"}) is None          # alone: fine
    assert check({"issue": "x-1", "task": "go"}) is None  # without it: fine
    assert check({"issue_text": "  ", "issue": "x-1"}) is None  # blank is absent

    for body, expect in (
        ({"issue_text": "spec", "issue": "x-1"}, "x-1"),
        ({"issue_text": "spec", "beads": False}, "beads"),
        ({"issue_text": "spec", "beads": "none-auto"}, "beads"),
        ({"issue_text": "spec", "task": "do it" + chr(10) + "issue: x-9"}, "x-9"),
        ({"issue_text": "spec", "context": "issue: x-9"}, "x-9"),
    ):
        with pytest.raises(beads_mod.BoardRequestError) as exc:
            check(body)
        # both halves named, so the caller knows which one to drop
        assert "issue_text" in str(exc.value) and expect in str(exc.value)


def test_the_link_note_points_a_session_at_a_record_it_has_not_read():
    """An agent told "registered from this task" reasonably skips a record it
    believes it has already read — which is the half of its instructions it
    would then be missing."""
    plain = beads_mod.compose_link_note("x-1", mode=beads_mod.MINTED)
    assert "registered from this task" in plain
    written = beads_mod.compose_link_note("x-1", mode=beads_mod.MINTED, text=True)
    assert "registered from this task" not in written
    assert "says MORE than it does" in written
    assert "claunch beads show x-1" in written


def test_the_link_note_tells_a_joiner_it_is_not_the_assignee():
    minted = beads_mod.compose_link_note("x-1", mode=beads_mod.MINTED)
    assert "registered from this task" in minted and "claunch beads show x-1" in minted
    took = beads_mod.compose_link_note("x-1", mode=beads_mod.TAKE)
    assert "assigned to you" in took
    joined = beads_mod.compose_link_note(
        "x-1", mode=beads_mod.JOIN, held_by="s9", mesh="m1"
    )
    assert "JOINED" in joined and "NOT its assignee" in joined
    assert "claunch mesh send m1 s9" in joined
    # with no shared mesh the instruction cannot name one, and must not pretend
    alone = beads_mod.compose_link_note("x-1", mode=beads_mod.JOIN, held_by="s9")
    assert "mesh send" not in alone and "s9" in alone


def test_candidates_offer_active_issues_with_the_verdict_the_creation_path_will_take(repo):
    br = FakeBr()
    br.add(id="free", title="nobody's", status="open", priority=2)
    br.add(id="ready", title="triaged", status="in_ready", priority=1)
    br.add(id="held", title="the leader's", status="in_progress", assignee="lead")
    br.add(id="dead", title="orphaned", status="open", assignee="old")
    br.add(id="done", title="finished", status="closed")
    board = _board(br, repo)
    mgr = _Manager(
        lead=_Sess(_sdef("lead", repo), status="busy"),
        old=_Sess(_sdef("old", repo), status="exited", exit_code=0),
    )

    async def run():
        view = await board.candidates(str(repo), mgr)
        assert view["root"] == str(repo) and view["error"] is None
        by_id = {i["id"]: i for i in view["issues"]}
        assert "done" not in by_id  # closed issues are not on offer
        assert by_id["free"]["mode"] == beads_mod.TAKE
        assert by_id["ready"]["mode"] == beads_mod.TAKE
        assert by_id["held"]["mode"] == beads_mod.JOIN
        assert by_id["held"]["held_by"] == "lead"
        assert by_id["dead"]["mode"] == beads_mod.TAKE
        # in_progress ranks above in_ready and open, so the held one leads the list
        assert view["issues"][0]["id"] == "held"
        assert [i["id"] for i in view["issues"]].index("ready") < [
            i["id"] for i in view["issues"]
        ].index("free")

        bare = beads_mod.Board(br, root_for=lambda cwd: None)
        blind = await bare.candidates("nowhere")
        assert blind["issues"] == [] and "no board" in blind["error"]

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


def test_edges_are_read_only_where_the_listing_says_there_are_any(repo):
    """``br`` has no bulk edge dump, so the listing's counts are what keeps the
    read bounded: an issue with no outgoing edge is never asked about."""
    br = FakeBr()
    br.add(id="epic")
    br.add(id="kid")
    br.add(id="lone")
    br.link("kid", "epic")
    board = _board(br, repo)

    async def run():
        rows = await board.issues(repo)
        edges = await board.edges(repo, rows)
        # child first: `br dep add <child> <parent>` stores the edge on the
        # depending side, which is the child.
        assert edges == [{"from": "kid", "to": "epic", "type": "parent-child"}]
        asked = [c[c.index("list") + 1] for c in br.calls if "dep" in c]
        assert asked == ["kid"], asked
        # `epic` has a dependent, not a dependency -- asking it too would see
        # the one edge twice.
        assert "epic" not in asked and "lone" not in asked

    asyncio.run(run())


def test_reading_the_edges_does_not_throw_away_the_listing_it_came_from(repo):
    """``dep list`` is a read. It shares the ``dep`` verb with ``dep add``,
    which is a write -- so a rule written on the verb alone would have the
    dashboard's own poll invalidating the listing it had just paid for."""
    br = FakeBr()
    br.add(id="epic")
    br.add(id="kid")
    br.link("kid", "epic")
    board = _board(br, repo)

    async def run():
        rows = await board.issues(repo)
        await board.edges(repo, rows)
        await board.issues(repo)
        assert sum(1 for c in br.calls if "list" in c and "dep" not in c) == 1
        # ... and a real write still does invalidate both.
        await board.br(repo, ["dep", "add", "kid", "epic"], actor="s1")
        rows = await board.issues(repo)
        assert sum(1 for c in br.calls if "list" in c and "dep" not in c) == 2

    asyncio.run(run())


def test_an_edge_read_that_fails_leaves_the_board_readable(repo):
    """A hierarchy is an ornament over a listing that is already useful. One
    unreadable issue must not cost the reader the whole board."""
    br = FakeBr()
    br.add(id="kid")
    br.add(id="epic")
    br.link("kid", "epic")

    async def broken(argv, cwd):
        if "dep" in argv:
            return 1, "", "br dep list exploded"
        return await br(argv, cwd)

    board = beads_mod.Board(broken, root_for=lambda cwd: repo)

    async def run():
        rows = await board.issues(repo)
        assert await board.edges(repo, rows) == []
        view = await board.fleet_view([], extra_roots=[str(repo)])
        board_entry = next(b for b in view["boards"] if b["root"] == str(repo))
        assert board_entry["deps"] == []
        assert {i["id"] for i in board_entry["issues"]} == {"kid", "epic"}

    asyncio.run(run())


def test_the_fleet_view_carries_the_edges_beside_the_issues(repo):
    br = FakeBr()
    br.add(id="epic")
    br.add(id="kid")
    br.link("kid", "epic")
    br.link("kid", "epic", kind="blocks")   # a second edge on the same issue
    board = _board(br, repo)

    async def run():
        view = await board.fleet_view([], extra_roots=[str(repo)])
        entry = next(b for b in view["boards"] if b["root"] == str(repo))
        assert entry["deps"] == [
            {"from": "kid", "to": "epic", "type": "parent-child"},
            {"from": "kid", "to": "epic", "type": "blocks"},
        ]
        # one `dep list` for the one issue that has any, however many it has
        assert sum(1 for c in br.calls if "dep" in c) == 1

    asyncio.run(run())


def test_stream_view_reads_a_bounded_priority_page(repo):
    """The incremental board endpoint asks ``br`` for one extra row, so it
    can return a continuation marker without serializing the whole board."""
    br = FakeBr()
    for n in range(5):
        br.add(id=f"p1-{n}", priority=1, title=f"P1 {n}")
    br.add(id="p2", priority=2, title="P2")
    board = _board(br, repo)

    async def run():
        view = await board.stream_view([], extra_roots=[str(repo)], limit=2, priority=1)
        entry = view["boards"][0]
        assert [i["id"] for i in entry["issues"]] == ["p1-0", "p1-1"]
        assert entry["has_more"] is True
        assert view["has_more"] is True and view["next_offset"] == 2
        listing = next(c for c in br.calls if "list" in c)
        assert "--limit" in listing and listing[listing.index("--limit") + 1] == "3"
        assert "--priority" in listing and listing[listing.index("--priority") + 1] == "1"

        tail = await board.stream_view(
            [], extra_roots=[str(repo)], offset=4, limit=2, priority=1,
        )
        assert [i["id"] for i in tail["boards"][0]["issues"]] == ["p1-4"]
        assert tail["has_more"] is False and tail["next_offset"] is None

    asyncio.run(run())


@pytest.mark.parametrize("field", ["updated_at", "created_at", "priority", "title"])
def test_stream_sort_direction_and_cache_are_separate(repo, field):
    br = FakeBr()
    br.add(id="a")
    board = _board(br, repo)

    async def run():
        for direction in ("asc", "desc"):
            await board.stream_view([], [str(repo)], sort=field, direction=direction)
            listing = [c for c in br.calls if "list" in c][-1]
            assert listing[listing.index("--sort") + 1] == field
            assert ("--reverse" in listing) == (
                (direction == "asc") == (field in {"created_at", "updated_at"})
            )
        assert len([c for c in br.calls if "list" in c]) == 2
        await board.stream_view([], [str(repo)], sort=field, direction="asc")
        assert len([c for c in br.calls if "list" in c]) == 2
        assert len(board._page_cache) == 2
        assert len(board._page_deps) == 2

    asyncio.run(run())


def test_stream_route_validates_and_returns_the_continuation(home, tmp_path, repo):
    br = FakeBr()
    br.add(id="a", priority=1)
    br.add(id="b", priority=1)
    board = _board(br, repo)

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm, board)
        try:
            resp = await client.get(
                f"/api/beads/stream?cwd={repo}&limit=1&priority=1", headers=BEARER,
            )
            doc = await resp.json()
            assert resp.status == 200, doc
            entry = next(b for b in doc["boards"] if b["root"] == str(repo))
            assert [i["id"] for i in entry["issues"]] == ["a"]
            assert doc["has_more"] is True and doc["next_offset"] == 1
            bad = await client.get("/api/beads/stream?limit=zero", headers=BEARER)
            assert bad.status == 400
            for query in ("sort=status", "direction=sideways"):
                bad = await client.get(f"/api/beads/stream?{query}", headers=BEARER)
                assert bad.status == 400
            resp = await client.get(
                f"/api/beads/stream?cwd={repo}&sort=title&direction=asc", headers=BEARER,
            )
            assert resp.status == 200
            listing = [c for c in br.calls if "list" in c][-1]
            assert listing[listing.index("--sort") + 1] == "title"
            assert "--reverse" not in listing
        finally:
            await client.close()

    asyncio.run(run())


def test_a_page_is_a_page_of_what_the_filter_shows(repo):
    """The status filter is applied in the board, not after the page was cut.

    Filtered afterwards, a page of 2 rows over a board whose closed issues
    outnumber its open ones holds however many of those 2 happened to be
    open -- a different count on every page, and pages that draw nothing
    while the board still has matching issues left.
    """
    br = FakeBr()
    br.add(id="open-1", status="open")
    br.add(id="done-1", status="closed")
    br.add(id="open-2", status="open")
    br.add(id="done-2", status="closed")
    br.add(id="open-3", status="open")
    board = _board(br, repo)

    async def run():
        view = await board.stream_view(
            [], extra_roots=[str(repo)], limit=2,
            statuses=["open", "in_ready", "in_progress", "in_review", "blocked"],
        )
        entry = view["boards"][0]
        assert [i["id"] for i in entry["issues"]] == ["open-1", "open-2"]
        assert entry["has_more"] is True
        # The count is of the MATCHING issues, which is what a page control
        # counts pages with -- three open, not five on the board.
        assert entry["total"] == 3 and view["total"] == 3
        listing = [c for c in br.calls if "list" in c][-1]
        assert _opts_all(listing, "--status") == [
            "open", "in_ready", "in_progress", "in_review", "blocked",
        ]

        board.invalidate(repo)
        tail = await board.stream_view(
            [], extra_roots=[str(repo)], offset=2, limit=2,
            statuses=["open", "in_ready", "in_progress", "in_review", "blocked"],
        )
        assert [i["id"] for i in tail["boards"][0]["issues"]] == ["open-3"]
        assert tail["has_more"] is False and tail["next_offset"] is None

    asyncio.run(run())


def test_every_status_is_the_page_when_none_is_asked_for(repo):
    """No ``statuses`` is every status, closed ones included -- the ``all``
    tab, and what every caller that predates the filter still gets."""
    br = FakeBr()
    br.add(id="open-1", status="open")
    br.add(id="done-1", status="closed")
    board = _board(br, repo)

    async def run():
        view = await board.stream_view([], extra_roots=[str(repo)], limit=10)
        assert [i["id"] for i in view["boards"][0]["issues"]] == ["open-1", "done-1"]
        assert view["boards"][0]["total"] == 2
        listing = [c for c in br.calls if "list" in c][-1]
        assert "--status" not in listing and "--all" in listing

    asyncio.run(run())


def test_the_stream_route_takes_repeated_status_and_refuses_a_typo(home, tmp_path, repo):
    br = FakeBr()
    br.add(id="a", status="open")
    br.add(id="b", status="in_review")
    br.add(id="c", status="closed")
    board = _board(br, repo)

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm, board)
        try:
            resp = await client.get(
                f"/api/beads/stream?cwd={repo}&status=open&status=in_review",
                headers=BEARER,
            )
            doc = await resp.json()
            assert resp.status == 200, doc
            entry = next(b for b in doc["boards"] if b["root"] == str(repo))
            assert [i["id"] for i in entry["issues"]] == ["a", "b"]
            assert entry["total"] == 2
            assert doc["statuses"] == ["open", "in_review"]
            # A status the board cannot hold is a typo, and a typo that
            # silently lists nothing reads as an empty board.
            bad = await client.get(
                f"/api/beads/stream?cwd={repo}&status=in_progres", headers=BEARER,
            )
            assert bad.status == 400
            assert "in_progres" in (await bad.json())["error"]
        finally:
            await client.close()

    asyncio.run(run())


def test_a_listing_carries_an_excerpt_and_the_detail_carries_the_text(repo):
    """A listing draws an excerpt of a description and nothing more, so it
    serializes an excerpt. The whole text stays one request away."""
    body = "## goal\n" + ("x" * 4000)
    br = FakeBr()
    br.add(id="long", description=body)
    br.add(id="short", description="fits")
    board = _board(br, repo)

    async def run():
        view = await board.stream_view([], extra_roots=[str(repo)], limit=10)
        rows = {i["id"]: i for i in view["boards"][0]["issues"]}
        assert len(rows["long"]["description"]) == beads_mod.PREVIEW_CHARS
        assert body.startswith(rows["long"]["description"])
        assert rows["long"]["description_full"] is False
        # A description that fits is untouched and says nothing about it.
        assert rows["short"]["description"] == "fits"
        assert "description_full" not in rows["short"]
        # And the cached listing the daemon's own lifecycle work reads still
        # holds the text whole -- the cut is on the response only.
        cached = await board.issues(repo)
        assert next(r for r in cached if r["id"] == "long")["description"] == body
        # As does the detail read.
        assert (await board.show(repo, "long"))["description"] == body

    asyncio.run(run())


def test_edges_come_from_one_read_of_the_board_database(tmp_path):
    """``br dep list`` per issue is one process each; the same answer is one
    query of the file br already keeps them in."""
    import sqlite3

    root = tmp_path / "repo"
    (root / ".beads").mkdir(parents=True)
    conn = sqlite3.connect(root / ".beads" / "beads.db")
    conn.execute(
        "CREATE TABLE dependencies (issue_id TEXT, depends_on_id TEXT, type TEXT)"
    )
    conn.executemany(
        "INSERT INTO dependencies VALUES (?, ?, ?)",
        [("kid", "epic", "parent-child"), ("kid", "epic", "blocks")],
    )
    conn.commit()
    conn.close()

    br = FakeBr()
    br.add(id="epic")
    br.add(id="kid")
    br.link("kid", "epic")
    br.link("kid", "epic", kind="blocks")
    board = _board(br, root)

    async def run():
        view = await board.fleet_view([], extra_roots=[str(root)])
        entry = next(b for b in view["boards"] if b["root"] == str(root))
        assert entry["deps"] == [
            {"from": "kid", "to": "epic", "type": "parent-child"},
            {"from": "kid", "to": "epic", "type": "blocks"},
        ]
        # and not one fork of `br dep list`
        assert [c for c in br.calls if "dep" in c] == []

    asyncio.run(run())


def test_a_board_without_that_table_still_draws_its_edges(repo):
    """The ``repo`` fixture's database is an empty file: no table to read.
    The edges are an ornament over a listing that is already useful, so the
    reading falls back to ``br`` rather than the page losing them."""
    br = FakeBr()
    br.add(id="epic")
    br.add(id="kid")
    br.link("kid", "epic")
    board = _board(br, repo)

    async def run():
        assert await board._edges_from_db(repo) is None
        view = await board.fleet_view([], extra_roots=[str(repo)])
        entry = next(b for b in view["boards"] if b["root"] == str(repo))
        assert entry["deps"] == [
            {"from": "kid", "to": "epic", "type": "parent-child"},
        ]
        assert sum(1 for c in br.calls if "dep" in c) == 1

    asyncio.run(run())


def test_a_title_is_enough_to_file_an_issue_from_the_board(repo):
    """The form's smallest answer: a title. Everything else has a default,
    and the description is the workflows' four sections so the assignee's
    intake finds the headings it reads."""
    br = FakeBr()
    board = _board(br, repo)

    async def run():
        spec = beads_mod.check_new_issue({"title": "  make the board fast  "})
        made = await board.create_issue(repo, spec)
        assert made["created"] is True
        filed = br.issues[made["issue"]]
        assert filed["title"] == "make the board fast"
        assert filed["priority"] == 2 and filed["issue_type"] == "task"
        # The source label the workflows read, and the actor that says an
        # operator filed this rather than a session claiming the work.
        assert filed["labels"] == ["user"]
        assert filed["created_by"] == beads_mod.DASHBOARD_ACTOR
        for heading in ("## 목표", "## 범위(포함·제외)", "## 완료 증거 기준", "## 출처"):
            assert heading in filed["description"]
        assert "make the board fast" in filed["description"]

    asyncio.run(run())


def test_a_filed_issue_records_the_workspace_its_session_opens_in(repo, home, tmp_path):
    """The workspace goes on in the create, not in a second write: an issue
    must never exist with its directory missing."""
    from claude_launcher import workspaces

    target = tmp_path / "somewhere"
    target.mkdir()
    workspaces.add(str(target), "somewhere")
    br = FakeBr()
    board = _board(br, repo)

    async def run():
        spec = beads_mod.check_new_issue({
            "title": "ship it", "workspace": "somewhere",
            "description": "## 목표\nship it\n",
        })
        made = await board.create_issue(repo, spec)
        assert made["workspace"] == "somewhere"
        filed = br.issues[made["issue"]]
        # Read back the way the Start-a-session block reads it.
        assert beads_meta.workspace_of(filed) == "somewhere"
        # and the operator's own words are still under the block
        assert "ship it" in beads_meta.parse(filed["description"])[1]
        # one create, no follow-up update
        assert [c[0] for c in br.calls if c and c[0] in ("create", "update")] == []
        assert sum(1 for c in br.calls if "create" in c) == 1
        assert sum(1 for c in br.calls if "update" in c) == 0

    asyncio.run(run())


def test_the_board_refuses_a_request_it_cannot_file(repo, home):
    """Every refusal is check_new_issue's, so the route holds none of its
    own and a typo never becomes a category nothing filters on."""
    cases = [
        ({}, "needs a title"),
        ({"title": "x", "priority": 9}, "priority must be"),
        ({"title": "x", "priority": "high"}, "must be a number"),
        ({"title": "x", "type": "tsak"}, "unknown type"),
        ({"title": "x", "workspace": "nowhere"}, "no workspace named"),
        ({"title": "x", "status": "closed"}, "starts open"),
    ]
    for body, reason in cases:
        with pytest.raises(beads_mod.BoardRequestError) as caught:
            beads_mod.check_new_issue(body)
        assert reason in str(caught.value), body
    # And the answers it accepts, normalised: P-spelling, a comma string of
    # labels, the source label added once and only once.
    spec = beads_mod.check_new_issue({
        "title": "x", "priority": "P1", "labels": "user, ui", "type": "bug",
    })
    assert spec["priority"] == 1 and spec["type"] == "bug"
    assert spec["labels"] == ["user", "ui"]


def test_the_create_route_files_an_issue_and_names_its_board(home, tmp_path, repo):
    br = FakeBr()
    board = _board(br, repo)

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm, board)
        try:
            resp = await client.post(
                "/api/beads",
                json={"title": "from the web", "cwd": str(repo), "priority": 1},
                headers=BEARER,
            )
            doc = await resp.json()
            assert resp.status == 201, doc
            assert doc["root"] == str(repo)
            assert br.issues[doc["issue"]]["title"] == "from the web"
            assert br.issues[doc["issue"]]["priority"] == 1

            bad = await client.post(
                "/api/beads", json={"cwd": str(repo)}, headers=BEARER,
            )
            assert bad.status == 400
            assert "title" in (await bad.json())["error"]

        finally:
            await client.close()

    asyncio.run(run())


def test_the_create_route_refuses_a_directory_with_no_board(home, tmp_path):
    """A directory that is not in a repository with a ``.beads/`` has
    nowhere to file an issue, and a create that answered 201 there would
    report success for work the board never received."""
    br = FakeBr()
    board = beads_mod.Board(br, root_for=lambda cwd: None)

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm, board)
        try:
            resp = await client.post(
                "/api/beads",
                json={"title": "x", "cwd": str(tmp_path / "not-a-repo")},
                headers=BEARER,
            )
            assert resp.status == 404
            assert "no board" in (await resp.json())["error"]
            assert br.issues == {}
        finally:
            await client.close()

    asyncio.run(run())


def test_the_form_learns_its_boards_without_listing_a_single_issue(home, tmp_path, repo):
    """What the create form needs before it draws: the boards, who is on
    them, and the workspace registry. Listing issues here would pay the cost
    the paged board exists to avoid."""
    br = FakeBr()
    br.add(id="noise")
    board = _board(br, repo)

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm, board)
        try:
            resp = await client.get(f"/api/beads/boards?cwd={repo}", headers=BEARER)
            doc = await resp.json()
            assert resp.status == 200, doc
            assert doc["default"] == str(repo)
            assert [b["root"] for b in doc["boards"]] == [str(repo)]
            assert "workspaces" in doc
            assert [c for c in br.calls if "list" in c] == []
        finally:
            await client.close()

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
            assert doc["beads"]["issue"] == "t-1"
            assert doc["beads"]["created"] is True
            assert doc["beads"]["mode"] == beads_mod.MINTED
            assert doc["issue"] == "t-1"
            assert mgr.get("lead").sdef.issue == "t-1"
            assert br.issues["t-1"]["assignee"] == "lead"

            # the child takes the free issue its parent hands it, and is told so
            br.add(id="claunch-5", title="worker's job")
            resp = await client.post(
                "/api/sessions/lead/children",
                json={"name": "w1", "task": "do the job\nissue: claunch-5"},
                headers=BEARER,
            )
            doc = await resp.json()
            assert resp.status == 201, doc
            assert doc["beads"]["issue"] == "claunch-5"
            assert doc["beads"]["created"] is False
            assert doc["beads"]["mode"] == beads_mod.TAKE
            assert mgr.get("w1").sdef.issue == "claunch-5"
            assert br.issues["claunch-5"]["assignee"] == "w1"

            # a second child pointed at the SAME issue joins it: w1 is running,
            # so the assignment stays where it is and nothing is duplicated
            resp = await client.post(
                "/api/sessions/lead/children",
                json={"name": "w2", "issue": "claunch-5", "task": "help out"},
                headers=BEARER,
            )
            doc = await resp.json()
            assert resp.status == 201, doc
            assert doc["beads"]["mode"] == beads_mod.JOIN
            assert doc["beads"]["held_by"] == "w1"
            assert br.issues["claunch-5"]["assignee"] == "w1"
            assert mgr.get("w2").sdef.issue == "claunch-5"

            # the rail's view and the page's view
            resp = await client.get("/api/sessions/w1/meta", headers=BEARER)
            meta = await resp.json()
            assert meta["beads"]["issue"] == "claunch-5"
            assert [i["id"] for i in meta["beads"]["issues"]] == ["claunch-5"]
            resp = await client.get("/api/beads", headers=BEARER)
            fleet = await resp.json()
            assert [s["name"] for s in fleet["boards"][0]["sessions"]] == [
                "lead", "w1", "w2",
            ]
            # both sessions show on the joined issue, and the board still
            # names only one of them as its assignee
            joined = next(
                i for i in fleet["boards"][0]["issues"] if i["id"] == "claunch-5"
            )
            assert sorted(s["name"] for s in joined["sessions"]) == ["w1", "w2"]
            assert joined["assignee"] == "w1"
            resp = await client.get(f"/api/beads/t-1?cwd={repo}", headers=BEARER)
            detail = await resp.json()
            assert detail["issue"]["title"] == "Lead the work"
            assert [s["name"] for s in detail["sessions"]] == ["lead"]

            # the record survives a restart: the link is in the definition
            mgr.persist()
            fresh = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=False)
            fresh.restore_all()
            assert fresh.get("w1").sdef.issue == "claunch-5"
            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_a_join_tells_the_holder_over_the_mesh_they_share(home, tmp_path, repo):
    """The daemon's other half of the ownership rule.

    It refuses to move the assignment, so the two sessions have to settle it —
    and the holder cannot see from its own terminal that there is anything to
    settle. The notice goes out as ``fyi`` from the board, not from the joiner:
    nobody owes the daemon a reply and the holder keeps the issue either way.
    """
    _register_py_harness()
    br = FakeBr()
    br.add(id="claunch-7", title="the one they both want")
    board = _board(br, repo)

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        mm.create("team")
        client = await _serve(mgr, mm, board)
        try:
            resp = await client.post(
                "/api/sessions",
                json={"name": "holder", "profile": "py", "cwd": str(repo),
                      "mesh": "team", "handle": "h1", "issue": "claunch-7",
                      "task": "own it"},
                headers=BEARER,
            )
            assert (await resp.json())["beads"]["mode"] == beads_mod.TAKE

            resp = await client.post(
                "/api/sessions",
                json={"name": "joiner", "profile": "py", "cwd": str(repo),
                      "mesh": "team", "handle": "h2", "issue": "claunch-7",
                      "task": "help out"},
                headers=BEARER,
            )
            doc = await resp.json()
            assert doc["beads"]["mode"] == beads_mod.JOIN
            assert doc["beads"]["held_by"] == "holder"
            assert doc["beads"]["notified"] == "team"

            resp = await client.get("/api/mesh/team/messages", headers=BEARER)
            msgs = (await resp.json())["messages"]
            notice = [m for m in msgs if m.get("from") == beads_mod.BOARD_SENDER]
            assert len(notice) == 1
            assert notice[0]["to"] == ["h1"] or notice[0]["to"] == "h1"
            assert notice[0]["type"] == "fyi"
            assert "session joiner was just created on claunch-7" in notice[0]["body"]
            assert "did NOT move the assignment" in notice[0]["body"]
            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_no_shared_mesh_means_no_notice_and_the_session_still_starts(home, tmp_path, repo):
    _register_py_harness()
    br = FakeBr()
    br.add(id="claunch-7", title="held, but out of earshot", assignee="holder")
    board = _board(br, repo)

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm, board)
        try:
            await client.post(
                "/api/sessions",
                json={"name": "holder", "profile": "py", "cwd": str(repo),
                      "task": "own it"},
                headers=BEARER,
            )
            resp = await client.post(
                "/api/sessions",
                json={"name": "joiner", "profile": "py", "cwd": str(repo),
                      "issue": "claunch-7", "task": "help out"},
                headers=BEARER,
            )
            doc = await resp.json()
            assert resp.status == 201, doc
            assert doc["beads"]["mode"] == beads_mod.JOIN
            assert doc["beads"]["notified"] == ""
            assert br.issues["claunch-7"]["assignee"] == "holder"
            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_issue_text_files_the_spec_and_sends_the_session_to_read_it(
    home, tmp_path, repo
):
    """End to end, both doors. The board gets the specification, the terminal
    gets the instruction, and the opening block is what joins the two — a
    session told "registered from this task" would skip the record it has in
    fact never seen."""
    from claude_launcher.daemon import onboard

    _register_py_harness()
    br = FakeBr()
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
            resp = await client.post(
                "/api/sessions",
                json={"name": "lead", "profile": "py", "cwd": str(repo),
                      "task": "start when ready",
                      "issue_text": "Rail must answer the board"
                                    + chr(10) * 2 + "every row, one call"},
                headers=BEARER,
            )
            doc = await resp.json()
            assert resp.status == 201, doc
            assert doc["beads"]["from_issue_text"] is True
            issue = br.issues[doc["beads"]["issue"]]
            assert issue["title"] == "Rail must answer the board"
            assert "every row, one call" in issue["description"]
            assert "start when ready" not in issue["description"]
            # the opening block joins the two: the instruction it was given,
            # then the record it has NOT been given
            assert seen["lead"].startswith("start when ready" + chr(10) * 2)
            assert f"issue: {doc['beads']['issue']}" in seen["lead"]
            assert "says MORE than it does" in seen["lead"]
            assert "registered from this task" not in seen["lead"]

            # a child gets the same door, from its parent's own hand
            resp = await client.post(
                "/api/sessions/lead/children",
                json={"name": "w1", "task": "go",
                      "issue_text": "Wire the picker"},
                headers=BEARER,
            )
            doc = await resp.json()
            assert resp.status == 201, doc
            assert br.issues[doc["beads"]["issue"]]["title"] == "Wire the picker"

            # The issue text is consumed only to write the board record.  A
            # session definition is persisted for restart/rebriefing, so it
            # must retain the issue id and opening task without duplicating
            # the specification outside the board.
            mgr.persist()
            saved = json.dumps(db.open_default().load_all())
            assert "Rail must answer the board" not in saved
            assert "every row, one call" not in saved
            assert "Wire the picker" not in saved
            assert doc["beads"]["issue"] in saved
            await mgr.shutdown_all()
        finally:
            onboard.arrange = real
            await client.close()

    asyncio.run(run())


def test_two_board_answers_at_once_are_refused_and_nothing_is_created(
    home, tmp_path, repo
):
    """The adopt branch would win and the written text would go nowhere. A
    400 before anything is staged is the whole of the fix — and the name must
    come back into circulation, or the refusal costs the caller its session
    name as well."""
    _register_py_harness()
    br = FakeBr()
    br.add(id="claunch-5", title="already written")
    board = _board(br, repo)

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm, board)
        try:
            for extra in ({"issue": "claunch-5"}, {"beads": False},
                          {"task": "go" + chr(10) + "issue: claunch-5"}):
                resp = await client.post(
                    "/api/sessions",
                    json={"name": "nope", "profile": "py", "cwd": str(repo),
                          "issue_text": "the real spec", **extra},
                    headers=BEARER,
                )
                doc = await resp.json()
                assert resp.status == 400, doc
                assert "issue_text" in doc["error"]
                # nothing staged, nothing written, and the name is free again
                with pytest.raises(Exception):
                    mgr.get("nope")
                assert br.calls == []

            # the same name creates fine once the request means one thing
            resp = await client.post(
                "/api/sessions",
                json={"name": "nope", "profile": "py", "cwd": str(repo),
                      "issue_text": "the real spec"},
                headers=BEARER,
            )
            assert resp.status == 201, await resp.json()
            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_the_candidates_route_offers_a_boards_open_issues_with_their_verdict(
    home, tmp_path, repo
):
    _register_py_harness()
    br = FakeBr()
    br.add(id="free", title="nobody's", status="open")
    br.add(id="shut", title="finished", status="closed")
    board = _board(br, repo)

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm, board)
        try:
            await client.post(
                "/api/sessions",
                json={"name": "holder", "profile": "py", "cwd": str(repo),
                      "task": "own something"},
                headers=BEARER,
            )
            resp = await client.get(
                f"/api/beads/candidates?cwd={repo}", headers=BEARER
            )
            view = await resp.json()
            assert resp.status == 200, view
            rows = {i["id"]: i for i in view["issues"]}
            assert "shut" not in rows
            assert rows["free"]["mode"] == beads_mod.TAKE
            # the issue the running session just had minted is on offer too,
            # marked as its holder's
            held = [i for i in view["issues"] if i["held_by"] == "holder"]
            assert held and held[0]["mode"] == beads_mod.JOIN

            # a spawn form asks about the board of the directory the CHILD
            # will run in, which it names by its parent rather than by path
            resp = await client.get(
                "/api/beads/candidates?parent=holder", headers=BEARER
            )
            assert (await resp.json())["root"] == str(repo)
            resp = await client.get(
                "/api/beads/candidates?parent=nobody", headers=BEARER
            )
            assert resp.status == 404
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
    br.add(id="claunch-5")
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

            # a JOIN is the exception: `issue: <id>` on its own reads as "this
            # is yours", so the note goes in even though the task names the id
            await client.post(
                "/api/sessions",
                json={"name": "c", "profile": "py", "cwd": str(repo),
                      "task": "second pair of hands\nissue: claunch-5"},
                headers=BEARER,
            )
            assert "NOT its assignee" in seen["c"]
            assert "b" in seen["c"]  # names the session that holds it
            await mgr.shutdown_all()
        finally:
            onboard.arrange = real
            await client.close()

    asyncio.run(run())


def test_the_opening_says_which_no_issue_answer_was_given(home, tmp_path, repo):
    """The answer the operator gave has to reach the session it is about.

    Nothing was appended when there was no issue, so a session could not tell
    a deliberate "no issue" from a mint that failed -- and improv-worker's
    ``issue-check``, which asks it to tell exactly those apart, had no record
    to read. It fell through to the branch that searches the board, which is
    the opposite of what one of the two answers means.
    """
    from claude_launcher.daemon import onboard

    _register_py_harness()
    br = FakeBr()
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
                json={"name": "a", "profile": "py", "cwd": str(repo),
                      "task": "just do this", "beads": False},
                headers=BEARER,
            )
            await client.post(
                "/api/sessions",
                json={"name": "b", "profile": "py", "cwd": str(repo),
                      "beads": "none-auto"},
                headers=BEARER,
            )
            assert br.calls == []                    # neither minted anything

            # the older spelling keeps its meaning, under the task it came with
            assert seen["a"].startswith("just do this\n\nno issue:")
            assert "wait for instructions" in seen["a"]
            assert "Do NOT search the board" in seen["a"]

            # and the auto answer arrives even with no task at all: there the
            # block IS the instruction, and a session that never receives it
            # does the opposite of what was asked
            assert seen["b"].startswith("no issue:")
            assert "assign yourself" in seen["b"]
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
            resp = await client.post("/api/sessions/w1/kill", headers=BEARER)
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
            resp = await client.post("/api/sessions/w1/kill", headers=BEARER)
            assert (await resp.json())["winding_down"] is True
            # the same button again: stop now
            resp = await client.post("/api/sessions/w1/kill", headers=BEARER)
            assert "winding_down" not in await resp.json()
            await _wait_for(lambda: mgr.get("w1").exited, "w1 killed")
            # force never winds down
            resp = await client.post("/api/sessions/w2/kill?force=1", headers=BEARER)
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
            resp = await client.post("/api/sessions/w1/kill", headers=BEARER)
            assert "winding_down" not in await resp.json()
            await _wait_for(lambda: mgr.get("w1").exited, "w1 killed")

            store.update(lambda doc: doc.update({"daemon": {"beads_winddown": False}}))
            mgr.create(SessionDef(name="w2", harness="py", cwd=str(repo)))
            resp = await client.post("/api/sessions/w2/kill", headers=BEARER)
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


def test_a_restart_sweeps_what_retiring_leaves_behind(home, tmp_path, repo):
    """The complement of the shutdown test above.

    A restart retires what it cannot relaunch — a running ``--no-restore``
    session, a relaunch that failed — and that ending never reaches the
    exit hook: the board does not even exist when restore runs. The retired
    record's in_progress issues are swept at boot instead, once the board
    does exist.
    """
    _register_py_harness()
    br = FakeBr()
    br.add(id="w", assignee="w1", status="in_progress")
    board = _board(br, repo)

    async def run():
        old = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        old.create(SessionDef(name="w1", harness="py", cwd=str(repo), restore=False))
        await old.shutdown_all()  # the record says w1 was running

        fresh = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        # Retire first, the way the daemon boots (restore_all runs before
        # the app — and with it the board — is built).
        fresh.restore_all()
        assert fresh.get("w1").exited  # retired, not relaunched
        mm = MeshManager(fresh, root=tmp_path / "mesh")
        client = await _serve(fresh, mm, board)
        try:
            await _wait_for(
                lambda: br.issues["w"]["status"] == "open", "the boot sweep"
            )
            assert any(
                "SESSION ENDED" in " ".join(c) and "comments" in c
                for c in br.calls
            )
        finally:
            await client.close()

    asyncio.run(run())


def test_an_archived_record_is_left_inert_at_boot(home, tmp_path, repo):
    """Archive files a record away, and the boot sweep leaves it alone.

    A restart retires every exited record it does not relaunch and sweeps the
    board for each — releasing any issue still claiming a dead session as its
    worker. An archived record is the exception: archiving is the operator's
    "this is done, filed away", and its board sweep ran when it first exited.
    Re-sweeping it on every boot re-reads the board once per record, and at
    archive scale (hundreds of records) that is a slow boot for nothing, so a
    filed-away record is inert here: browsable, and no longer processed.

    The trade is that an issue an archived record still holds in_progress — a
    pre-fix daemon that never swept it on exit, then it was archived — is not
    auto-released at boot any more. A live session's normal exit still sweeps,
    so only the archive-then-never-swept edge is affected.
    """
    _register_py_harness()
    br = FakeBr()
    br.add(id="w", assignee="w1", status="in_progress")
    board = _board(br, repo)

    async def run():
        old = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        old.create(SessionDef(name="w1", harness="py", cwd=str(repo)))
        await old.shutdown_all()
        old.archive("w1")  # exited only; persists was_running False
        old.persist()  # and the archived_at stamp

        fresh = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        fresh.restore_all()
        assert fresh.get("w1").exited
        assert fresh.get("w1").archived_at  # the archive flag rode along
        # The archived record is not queued for the boot sweep.
        assert "w1" not in {d.sdef.name for d in fresh._retired_for_sweep}
        mm = MeshManager(fresh, root=tmp_path / "mesh")
        client = await _serve(fresh, mm, board)
        try:
            # Let any wrongly scheduled sweep task run, then assert the archived
            # record's issue was left exactly as it was — untouched, uncommented.
            await asyncio.sleep(0.2)
            assert br.issues["w"]["status"] == "in_progress"
            assert not any("SESSION ENDED" in " ".join(c) for c in br.calls)
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


# --------------------------------------------------------------------------- #
# queues: a session's issues in the order it takes them, and the one write
# --------------------------------------------------------------------------- #
def test_a_queue_is_the_boards_own_reading_in_the_workers_order():
    """The queue is never stored: it is ``assignee == name`` over the active
    statuses, most urgent first and then oldest first — the exact listing the
    worker's ``queue-next`` step runs, so the page and the worker read one
    queue. Closed work and other sessions' work are not in it; ``blocked``
    rides along for the page's own column."""
    rows = [
        {"id": "late", "assignee": "s1", "status": "open", "priority": 2,
         "created_at": "2026-01-02T00:00:00Z"},
        {"id": "early", "assignee": "s1", "status": "in_ready", "priority": 2,
         "created_at": "2026-01-01T00:00:00Z"},
        {"id": "urgent", "assignee": "s1", "status": "open", "priority": 0,
         "created_at": "2026-01-03T00:00:00Z"},
        {"id": "now", "assignee": "s1", "status": "in_progress", "priority": 1},
        {"id": "stuck", "assignee": "s1", "status": "blocked", "priority": 3},
        {"id": "done", "assignee": "s1", "status": "closed", "priority": 0},
        {"id": "theirs", "assignee": "s2", "status": "open", "priority": 0},
        {"id": "free", "status": "open", "priority": 0},
    ]
    queue = beads_mod.queue_of(rows, "s1")
    assert [i["id"] for i in queue] == ["urgent", "now", "early", "late", "stuck"]
    assert beads_mod.queue_summary(queue) == {
        "total": 5, "waiting": 3, "working": 1, "review": 0, "blocked": 1,
        "next": "urgent",
    }
    assert beads_mod.queue_summary([]) == {
        "total": 0, "waiting": 0, "working": 0, "review": 0, "blocked": 0,
        "next": None,
    }
    # a priority the board could not parse sorts last, not first
    odd = beads_mod.queue_of(
        [{"id": "x", "assignee": "s1", "status": "open", "priority": "?"},
         {"id": "y", "assignee": "s1", "status": "open", "priority": 4}], "s1")
    assert [i["id"] for i in odd] == ["y", "x"]


def test_created_of_is_a_sessions_own_follow_ups_it_does_not_hold():
    """``created_of`` reads the opposite gap from ``queue_of``: active issues
    the session filed but does not carry as assignee -- the shape a handoff
    follow-up takes when it is filed without one (see the workflows' wrapup
    rule), and the exact reason it goes untracked once nothing but the board
    remembers who filed it. Closed work, work it still holds itself, and
    other sessions' filings are not in it."""
    rows = [
        {"id": "handed-off", "created_by": "s1", "status": "open", "priority": 1,
         "created_at": "2026-01-01T00:00:00Z"},
        {"id": "urgent-handoff", "created_by": "s1", "status": "in_ready", "priority": 0,
         "created_at": "2026-01-02T00:00:00Z"},
        {"id": "still-mine", "created_by": "s1", "assignee": "s1", "status": "open"},
        {"id": "given-away", "created_by": "s1", "assignee": "s2", "status": "in_progress", "priority": 0},
        {"id": "closed-handoff", "created_by": "s1", "status": "closed"},
        {"id": "someone-elses", "created_by": "s2", "status": "open"},
    ]
    created = beads_mod.created_of(rows, "s1")
    # priority 0 before priority 1; within priority 0, no created_at sorts as
    # oldest (``_ts`` reads a missing timestamp as 0.0), same as ``queue_of``
    assert [i["id"] for i in created] == ["given-away", "urgent-handoff", "handed-off"]


def test_the_assignment_comment_names_both_ends_of_the_move():
    assert beads_mod.assign_note("a", "s2", "s1") == "QUEUED by dashboard: assigned to s2 (was s1)"
    assert beads_mod.assign_note("a", "s2", "") == "QUEUED by dashboard: assigned to s2"
    assert beads_mod.assign_note("a", "", "s1") == "UNQUEUED by dashboard: taken off s1"


def test_the_queues_view_draws_a_lane_per_session_and_the_pool(repo):
    """Sessions first in the daemon's order, then assignees the daemon does
    not know (a card must have a row to be dragged back from); an exited
    session with nothing left draws no lane, one still holding issues or its
    own unclaimed follow-ups does; the pool is what nobody has, in queue
    order."""
    br = FakeBr()
    br.add(id="a", title="s1 now", assignee="s1", status="in_progress", priority=1)
    br.add(id="b", title="s1 next", assignee="s1", status="open", priority=2)
    br.add(id="c", title="left behind", assignee="gone", status="open")
    br.add(id="d", title="a human holds it", assignee="lead", status="in_review")
    br.add(id="e", title="pool, urgent", status="open", priority=0)
    br.add(id="f", title="pool", status="in_ready", priority=3)
    br.add(id="g", title="closed", assignee="s1", status="closed")
    # s1 filed a follow-up but did not take it -- must not double up with
    # its own queue ("a", "b" above)
    br.add(id="h", title="s1's follow-up", created_by="s1", status="open", priority=0)
    # quiet exited having only ever filed a follow-up, never assigned
    # itself -- claunch-yfvf's exact case, so it must still get a lane
    br.add(id="i", title="quiet's follow-up", created_by="quiet", status="open")
    board = _board(br, repo)

    def boom(name, cwd):
        raise OSError("no run")

    async def run():
        s1 = _Sess(_sdef("s1", repo, issue="a"), status="busy")
        s2 = _Sess(_sdef("s2", repo))
        gone = _Sess(_sdef("gone", repo), status="exited")
        quiet = _Sess(_sdef("quiet", repo), status="exited")
        steps = {"s1": {"workflow": "improv-worker", "step": "work"}}
        view = await board.queues_view(
            [s1, s2, gone, quiet], extra_roots=[str(repo)],
            cflow_for=lambda name, cwd: steps.get(name),
        )
        assert view["statuses"] == list(beads_mod.ACTIVE_STATUSES)
        assert len(view["boards"]) == 1
        b = view["boards"][0]
        lanes = {l["session"]: l for l in b["lanes"]}
        assert [l["session"] for l in b["lanes"]] == ["s1", "s2", "gone", "quiet", "lead"]
        assert [i["id"] for i in lanes["s1"]["issues"]] == ["a", "b"]
        assert [i["id"] for i in lanes["s1"]["created"]] == ["h"]
        assert lanes["s1"]["summary"]["next"] == "b"
        assert lanes["s1"]["status"] == "busy" and lanes["s1"]["issue"] == "a"
        assert lanes["s1"]["cflow"] == {"workflow": "improv-worker", "step": "work"}
        assert lanes["s2"]["issues"] == [] and lanes["s2"]["created"] == [] and lanes["s2"]["known"] is True
        assert lanes["gone"]["status"] == "exited"
        assert [i["id"] for i in lanes["gone"]["issues"]] == ["c"]
        assert lanes["gone"]["created"] == []
        # an exited session with an unclaimed follow-up and nothing assigned
        # still draws a lane -- the whole point of the fix
        assert lanes["quiet"]["status"] == "exited"
        assert lanes["quiet"]["issues"] == []
        assert [i["id"] for i in lanes["quiet"]["created"]] == ["i"]
        assert lanes["lead"]["known"] is False and lanes["lead"]["status"] is None
        assert lanes["lead"]["cflow"] is None
        # "h" and "i" are unassigned, so the pool -- a different question
        # ("who could claim this") from the creator's own lane ("who filed
        # this and has not claimed it") -- lists them too; the two are not
        # mutually exclusive
        assert [i["id"] for i in b["unassigned"]] == ["e", "h", "i", "f"]
        # the cflow reader failing costs the lane its step, not the page
        broken = await board.queues_view([s1], cflow_for=boom)
        assert broken["boards"][0]["lanes"][0]["cflow"] is None

    asyncio.run(run())


class _Fleet:
    """A manager as ``adoption`` reads it: which names are running here."""

    def __init__(self, **states):
        self.states = states

    def get(self, name):
        if name not in self.states:
            raise KeyError(name)
        return _Sess(_sdef(name, Path(".")), status=self.states[name])


def test_assign_is_one_update_and_one_comment_and_never_a_status(repo):
    br = FakeBr()
    br.add(id="a", title="free", status="in_ready", priority=1)
    br.add(id="b", title="queued on s1", assignee="s1", status="open")
    board = _board(br, repo)

    async def run():
        moved = await board.assign(repo, "a", "s1", manager=_Fleet(s1="idle"))
        assert moved == {"issue": "a", "assignee": "s1", "was": "", "changed": True}
        assert br.issues["a"]["assignee"] == "s1"
        assert br.issues["a"]["status"] == "in_ready"      # untouched
        update, comment = br.calls[-2], br.calls[-1]
        assert update[3:5] == ["--actor", beads_mod.DASHBOARD_ACTOR]
        assert update[5:9] == ["update", "a", "--assignee", "s1"]
        assert "--status" not in update
        assert comment[5:8] == ["comments", "add", "a"]
        assert br.comments["a"][-1]["text"] == "QUEUED by dashboard: assigned to s1"

        # off the queue: an empty assignee, which br reads as "clear"
        moved = await board.assign(repo, "b", None, manager=_Fleet(s1="idle"))
        assert moved["changed"] is True and moved["was"] == "s1"
        assert br.issues["b"]["assignee"] == ""
        assert br.calls[-2][5:9] == ["update", "b", "--assignee", ""]
        assert br.comments["b"][-1]["text"] == "UNQUEUED by dashboard: taken off s1"

        # already there: nothing is written
        n = len(br.calls)
        moved = await board.assign(repo, "a", "s1", manager=_Fleet(s1="idle"))
        assert moved["changed"] is False
        assert len(br.calls) == n + 1          # the one listing read
        assert "--actor" not in br.calls[-1]   # ...and it was a read

        with pytest.raises(beads_mod.cli_beads.BeadsError, match="no issue"):
            await board.assign(repo, "nope", "s1", manager=_Fleet())

    asyncio.run(run())


def test_assign_refuses_to_move_work_off_a_running_session_unless_forced(repo):
    """The creation-time ownership rule, applied to the drag: an issue that
    is ``in_progress`` under a session that is still running has a branch
    with that work on it. A dead holder, or one that is only queued (not
    in_progress), is moved freely; ``force`` moves it regardless."""
    br = FakeBr()
    br.add(id="w", title="being worked", assignee="s1", status="in_progress")
    br.add(id="q", title="only queued", assignee="s1", status="open")
    br.add(id="o", title="orphan", assignee="dead", status="in_progress")
    board = _board(br, repo)

    async def run():
        fleet = _Fleet(s1="busy", dead="exited")
        with pytest.raises(beads_mod.AssignRefused, match="in_progress under s1"):
            await board.assign(repo, "w", "s2", manager=fleet)
        assert br.issues["w"]["assignee"] == "s1" and not br.comments.get("w")
        assert (await board.assign(repo, "q", "s2", manager=fleet))["changed"] is True
        assert (await board.assign(repo, "o", "s2", manager=fleet))["changed"] is True
        assert (await board.assign(repo, "w", "s2", manager=fleet, force=True))["changed"] is True
        assert br.issues["w"]["assignee"] == "s2"
        assert br.issues["w"]["status"] == "in_progress"   # still not this write to make

    asyncio.run(run())


def test_the_queues_route_and_the_assign_route(home, tmp_path, repo):
    _register_py_harness()
    br = FakeBr()
    br.add(id="free", title="unassigned", status="in_ready", priority=1)
    board = _board(br, repo)

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm, board)
        try:
            resp = await client.post(
                "/api/sessions",
                json={"name": "w1", "profile": "py", "cwd": str(repo), "task": "work"},
                headers=BEARER,
            )
            assert resp.status == 201, await resp.text()
            resp = await client.get("/api/beads/queues", headers=BEARER)
            view = await resp.json()
            assert resp.status == 200, view
            lanes = {l["session"]: l for l in view["boards"][0]["lanes"]}
            assert [i["id"] for i in lanes["w1"]["issues"]] == ["t-1"]
            assert lanes["w1"]["known"] is True
            assert [i["id"] for i in view["boards"][0]["unassigned"]] == ["free"]

            # the drag: the pool's issue onto w1's row
            resp = await client.post(
                "/api/beads/free/assign",
                json={"session": "w1", "cwd": str(repo)}, headers=BEARER,
            )
            doc = await resp.json()
            assert resp.status == 200, doc
            assert doc["assignee"] == "w1" and doc["was"] == "" and doc["root"] == str(repo)
            assert br.issues["free"]["assignee"] == "w1"
            assert br.issues["free"]["status"] == "in_ready"

            # the refusal: w1 is running and has taken t-1 up
            br.issues["t-1"]["status"] = "in_progress"
            resp = await client.post(
                "/api/beads/t-1/assign",
                json={"session": None, "cwd": str(repo)}, headers=BEARER,
            )
            assert resp.status == 409, await resp.text()
            assert "in_progress under w1" in (await resp.json())["error"]
            assert br.issues["t-1"]["assignee"] == "w1"
            resp = await client.post(
                "/api/beads/t-1/assign",
                json={"session": None, "cwd": str(repo), "force": True}, headers=BEARER,
            )
            assert resp.status == 200, await resp.text()
            assert br.issues["t-1"]["assignee"] == ""

            resp = await client.post(
                "/api/beads/nope/assign", json={"session": "w1", "cwd": str(repo)},
                headers=BEARER,
            )
            assert resp.status == 404
            resp = await client.post(
                "/api/beads/free/assign", json={"session": 3, "cwd": str(repo)},
                headers=BEARER,
            )
            assert resp.status == 400
            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())
