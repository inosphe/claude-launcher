"""One board per workspace, and where each board's database is.

The board used to be derived from one rule — the repository root of the
caller's directory — so a registered workspace that was not its own git
checkout had no board, a workspace with no ``.beads/`` had none either, and
everything the dashboard filed went to the daemon's own directory. These
tests pin the three parts of the replacement: the registry
(:mod:`claude_launcher.beads_db`), the resolution order
(:func:`claude_launcher.cli_beads.resolve`), and what the two of them make
``br`` do.

``br`` never runs here: :func:`claude_launcher.cli_beads.plan` is pure and
answers the argv, which is the whole of what this change alters about the
commands.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from claude_launcher import beads_db, cli_beads, store, workspaces


@pytest.fixture
def ws(tmp_path, monkeypatch):
    """Two registered workspaces, one of them inside the other.

    ``inner`` is the case the old rule could not express: a workspace
    registered under another checkout, which git would hand the outer
    repository's board.
    """
    outer = tmp_path / "trees" / "outer"
    inner = outer / "sub" / "inner"
    inner.mkdir(parents=True)
    rows = {
        "outer": workspaces.Workspace(name="outer", path=str(outer)),
        "inner": workspaces.Workspace(name="inner", path=str(inner)),
    }
    monkeypatch.setattr(workspaces, "list_all", lambda doc=None: list(rows.values()))
    monkeypatch.setattr(workspaces, "get", lambda name, doc=None: rows.get(name))

    def owning(path, doc=None):
        best = None
        here = os.path.normcase(os.path.abspath(str(path)))
        for row in rows.values():
            root = os.path.normcase(os.path.abspath(row.path))
            if here == root or here.startswith(root + os.sep):
                if best is None or len(root) > len(os.path.normcase(best.path)):
                    best = row
        return best

    monkeypatch.setattr(workspaces, "owning", owning)
    monkeypatch.setattr(cli_beads.workspaces, "owning", owning)
    return rows


# --------------------------------------------------------------------------- #
# the default path
# --------------------------------------------------------------------------- #
def test_a_workspace_board_is_the_database_under_it(ws):
    ref = beads_db.workspace_ref(ws["outer"])
    assert ref.name == "outer"
    assert Path(ref.db) == Path(ws["outer"].path) / ".beads" / "beads.db"
    assert Path(ref.root) == Path(ws["outer"].path)
    assert ref.configured is False


def test_a_workspace_inside_another_gets_its_own_board(ws):
    """The case the git rule could not express: both workspaces are in one
    checkout, and each files on its own database."""
    outer = beads_db.workspace_ref(ws["outer"])
    inner = beads_db.workspace_ref(ws["inner"])
    assert outer.db != inner.db


# --------------------------------------------------------------------------- #
# the override, and what it refuses
# --------------------------------------------------------------------------- #
def test_a_stored_path_is_what_the_board_reads(ws, tmp_path):
    elsewhere = tmp_path / "boards"
    elsewhere.mkdir()
    target = elsewhere / "inner.db"
    beads_db.set_db("inner", str(target))
    ref = beads_db.workspace_ref(ws["inner"])
    assert Path(ref.db) == target
    assert ref.configured is True
    # A database outside a .beads/ stands alone: its own directory is where
    # br runs, not the workspace it was set for.
    assert Path(ref.root) == elsewhere


def test_clearing_the_path_puts_the_board_back_on_its_default(ws, tmp_path):
    target = tmp_path / "x.db"
    beads_db.set_db("outer", str(target))
    assert beads_db.clear_db("outer") is True
    ref = beads_db.workspace_ref(ws["outer"])
    assert Path(ref.db) == Path(ws["outer"].path) / ".beads" / "beads.db"
    assert ref.configured is False


# Absolute on whichever platform runs this (C:\ on Windows, / elsewhere): a
# drive-letter path is relative on POSIX and would be refused for that before
# its suffix was read.
_ROOT = Path(os.path.abspath(os.sep))


@pytest.mark.parametrize(
    "bad, says",
    [
        ("", "needs a path"),
        ("boards/x.db", "absolute"),
        (str(_ROOT / "boards"), "does not end in"),
        (str(_ROOT / "boards" / "x.sqlite"), "does not end in"),
    ],
)
def test_a_path_that_is_not_a_db_file_is_refused(bad, says):
    with pytest.raises(beads_db.BoardPathError, match=says):
        beads_db.check_path(bad)


def test_a_directory_typed_where_the_file_goes_is_refused(tmp_path):
    """The mistake the .db suffix exists to catch, in the one shape the
    suffix check alone would let through: an existing directory named .db."""
    d = tmp_path / "looks-like.db"
    d.mkdir()
    with pytest.raises(beads_db.BoardPathError, match="is a directory"):
        beads_db.check_path(str(d))


def test_a_parent_that_is_not_there_is_refused_now_not_at_the_next_read(tmp_path):
    with pytest.raises(beads_db.BoardPathError, match="no such directory"):
        beads_db.check_path(str(tmp_path / "nope" / "x.db"))


def test_a_path_that_has_no_file_yet_is_accepted(tmp_path):
    """The board is made on first use, so a path with nothing at it is a
    normal state rather than an error."""
    assert beads_db.check_path(str(tmp_path / "new.db")) == str(tmp_path / "new.db")


# --------------------------------------------------------------------------- #
# the default board
# --------------------------------------------------------------------------- #
def test_the_default_board_is_pinned_once_and_then_stays(tmp_path):
    root = tmp_path / "daemon-repo"
    root.mkdir()
    first = beads_db.ensure_default(root)
    assert Path(first) == root / ".beads" / "beads.db"
    # A second daemon start, standing somewhere else, must not move it: the
    # issues filed before this existed are on the first answer.
    other = tmp_path / "elsewhere"
    other.mkdir()
    assert beads_db.ensure_default(other) == first


def test_the_default_root_is_the_first_candidate_holding_a_database(tmp_path):
    """The daemon's working directory is often not a checkout at all (it was
    launched from the home directory), and a checkout with no board is not
    where anything was filed. Both are skipped."""
    empty = tmp_path / "no-board"
    empty.mkdir()
    real = tmp_path / "has-board"
    (real / ".beads").mkdir(parents=True)
    (real / ".beads" / "beads.db").write_bytes(b"")
    assert beads_db.pick_default_root([None, empty, real]) == real
    assert beads_db.pick_default_root([None, empty]) is None
    assert beads_db.pick_default_root([]) is None


def test_a_daemon_outside_any_checkout_still_pins_its_source_checkouts_board(
    tmp_path, monkeypatch
):
    """The case that shipped broken: the daemon ran from a directory git does
    not claim, the first candidate was None, and nothing was pinned -- so
    Settings had no claunch-default row and the old issues had no name."""
    src = tmp_path / "checkout"
    (src / ".beads").mkdir(parents=True)
    (src / ".beads" / "beads.db").write_bytes(b"")
    monkeypatch.setattr(beads_db, "source_checkout", lambda: src)
    pinned = beads_db.ensure_default(
        beads_db.pick_default_root([None, beads_db.source_checkout()])
    )
    assert Path(pinned) == src / ".beads" / "beads.db"
    assert beads_db.default_ref(None).name == beads_db.DEFAULT_BOARD


def test_the_source_checkout_is_found_from_the_package():
    found = beads_db.source_checkout()
    assert found is not None
    assert (found / "src" / "claude_launcher" / "beads_db.py").is_file()


def test_without_a_pinned_default_and_without_a_root_there_is_no_board():
    assert beads_db.default_ref(None) is None


def test_the_default_board_is_reported_in_the_listing(tmp_path, ws):
    beads_db.ensure_default(tmp_path / "daemon-repo")
    names = [row["board"] for row in beads_db.listing(None)]
    assert names[0] == beads_db.DEFAULT_BOARD
    assert set(names[1:]) == {"inner", "outer"}


def test_two_boards_on_one_file_are_named_to_each_other(tmp_path, ws):
    """Expected for the default board, which is pinned to a workspace's, and
    a mistake anywhere else — so the row says it rather than the page having
    to work it out."""
    beads_db.ensure_default(Path(ws["outer"].path))
    rows = {row["board"]: row for row in beads_db.listing(None)}
    assert rows[beads_db.DEFAULT_BOARD]["shared_with"] == ["outer"]
    assert rows["outer"]["shared_with"] == [beads_db.DEFAULT_BOARD]
    assert rows["inner"]["shared_with"] == []


# --------------------------------------------------------------------------- #
# resolution order
# --------------------------------------------------------------------------- #
def test_the_workspace_wins_over_the_checkout_it_sits_in(ws, monkeypatch):
    """A worktree of the outer repository would resolve to the outer root
    through git. The inner workspace is registered, so it is answered first
    and files on its own board."""
    monkeypatch.setattr(cli_beads, "repo_root", lambda cwd=None: Path(ws["outer"].path))
    ref = cli_beads.resolve(str(Path(ws["inner"].path) / "deep" / "dir"))
    assert ref.name == "inner"


def test_a_checkout_nobody_registered_keeps_the_board_it_holds(tmp_path, ws, monkeypatch):
    plain = tmp_path / "plain"
    (plain / ".beads").mkdir(parents=True)
    (plain / ".beads" / "beads.db").write_bytes(b"")
    monkeypatch.setattr(cli_beads, "repo_root", lambda cwd=None: plain)
    ref = cli_beads.resolve(str(plain))
    assert ref.name == "plain"
    assert Path(ref.db) == plain / ".beads" / "beads.db"


def test_a_directory_in_the_default_boards_tree_files_on_it(tmp_path, ws, monkeypatch):
    pinned = tmp_path / "pinned"
    pinned.mkdir()
    beads_db.ensure_default(pinned)
    monkeypatch.setattr(cli_beads, "repo_root", lambda cwd=None: None)
    ref = cli_beads.resolve(str(pinned / "sub" / "deeper"))
    assert ref.name == beads_db.DEFAULT_BOARD
    assert Path(ref.db) == pinned / ".beads" / "beads.db"


def test_a_directory_outside_every_board_has_none(tmp_path, ws, monkeypatch):
    """The default board is not a catch-all for the whole machine. A scratch
    directory, or an unrelated checkout with no board, files nowhere — which
    is the answer it got before boards were per workspace, and what keeps its
    issues out of a board that has nothing to do with it."""
    pinned = tmp_path / "pinned"
    pinned.mkdir()
    beads_db.ensure_default(pinned)
    monkeypatch.setattr(cli_beads, "repo_root", lambda cwd=None: None)
    assert cli_beads.resolve(str(tmp_path / "nowhere")) is None


def test_ref_for_root_agrees_with_resolve(ws, monkeypatch):
    """The two answer the same question from different ends — a directory,
    and a root the daemon already holds. A disagreement would have the daemon
    caching one board's listing under another board's key."""
    monkeypatch.setattr(cli_beads, "repo_root", lambda cwd=None: Path(ws["outer"].path))
    for name, row in ws.items():
        ref = cli_beads.resolve(row.path)
        again = beads_db.ref_for_root(Path(ref.root))
        assert (again.name, again.db) == (ref.name, ref.db), name


# --------------------------------------------------------------------------- #
# what br is told
# --------------------------------------------------------------------------- #
def test_the_stored_path_is_the_one_every_command_names(ws, tmp_path):
    target = tmp_path / "moved.db"
    beads_db.set_db("outer", str(target))
    (cmd,) = cli_beads.plan(
        ["list", "--json"], Path(ws["outer"].path), None,
        db_exists=True, jsonl_exists=True,
    )
    assert cmd[:3] == ["br", "--db", str(target)]


def test_a_vouched_board_is_created_where_br_writes_it(ws):
    """``br init`` ignores ``--db`` and writes ``<cwd>/.beads/beads.db``, so a
    board on the default layout is initialised in its own root and nothing has
    to be moved. The prefix is the board's name, so the ids it mints read as
    that workspace's."""
    ref = beads_db.workspace_ref(ws["inner"])
    plan_ = cli_beads.init_plan(ref, default_db_exists=False)
    assert plan_.argv == ["br", "init", "--prefix", "inner"]
    assert Path(plan_.cwd) == Path(ws["inner"].path)
    assert plan_.move_from is None and plan_.discard is None


def test_a_relocated_board_is_initialised_then_moved_onto_its_path(ws, tmp_path):
    """The same init, run where ``br`` will accept it, and the file it wrote
    moved onto the name the board is set to."""
    elsewhere = tmp_path / "boards"
    elsewhere.mkdir()
    beads_db.set_db("inner", str(elsewhere / "inner.db"))
    ref = beads_db.workspace_ref(ws["inner"])
    plan_ = cli_beads.init_plan(ref, default_db_exists=False)
    assert Path(plan_.cwd) == elsewhere
    assert Path(plan_.move_from) == elsewhere / ".beads" / "beads.db"
    assert plan_.discard is None


def test_a_root_that_already_holds_a_database_is_initialised_in_a_staging_dir(
    ws, tmp_path
):
    """``br init`` refuses outright when ``<cwd>/.beads/beads.db`` is there, so
    a second board under the same root is made beside it and moved in."""
    root = Path(ws["outer"].path)
    (root / ".beads").mkdir(parents=True)
    beads_db.set_db("outer", str(root / ".beads" / "second.db"))
    ref = beads_db.workspace_ref(ws["outer"])
    plan_ = cli_beads.init_plan(ref, default_db_exists=True)
    assert Path(plan_.cwd) == root / cli_beads.STAGING_DIR
    assert Path(plan_.move_from) == root / cli_beads.STAGING_DIR / ".beads" / "beads.db"
    assert Path(plan_.discard) == root / cli_beads.STAGING_DIR


def test_creating_a_board_runs_the_init_and_leaves_the_database_in_place(
    ws, tmp_path
):
    """The filesystem half, with a runner standing in for ``br``: the staging
    directory goes, the database stays where the board is set."""
    elsewhere = tmp_path / "boards"
    elsewhere.mkdir()
    (elsewhere / ".beads").mkdir()
    (elsewhere / ".beads" / "beads.db").write_bytes(b"other board")
    beads_db.set_db("inner", str(elsewhere / "inner.db"))
    ref = beads_db.workspace_ref(ws["inner"])
    seen = []

    def runner(argv, cwd):
        seen.append((argv, cwd))
        made = Path(cwd) / ".beads" / "beads.db"
        made.parent.mkdir(parents=True, exist_ok=True)
        made.write_bytes(b"new board")
        return 0, "", ""

    cli_beads.create_board(ref, runner)
    assert (elsewhere / "inner.db").read_bytes() == b"new board"
    # The board that was already there is untouched, and nothing is left over.
    assert (elsewhere / ".beads" / "beads.db").read_bytes() == b"other board"
    assert not (elsewhere / cli_beads.STAGING_DIR).exists()


def test_a_failed_init_moves_nothing(ws, tmp_path):
    elsewhere = tmp_path / "boards"
    elsewhere.mkdir()
    beads_db.set_db("inner", str(elsewhere / "inner.db"))
    ref = beads_db.workspace_ref(ws["inner"])
    with pytest.raises(cli_beads.BeadsError, match="creating the board inner"):
        cli_beads.create_board(ref, lambda argv, cwd: (1, "", "boom"))
    assert not (elsewhere / "inner.db").exists()


def test_a_directory_nobody_vouched_for_still_gets_the_refusal(tmp_path):
    with pytest.raises(cli_beads.BeadsError, match="no board"):
        cli_beads.plan(
            ["list"], tmp_path / "typo", None,
            db_exists=False, jsonl_exists=False,
        )


def test_a_tracked_jsonl_is_still_rebuilt_rather_than_started_empty(ws, tmp_path):
    """A fresh clone has the JSONL and no database. Creating an empty board
    there instead of importing would lose every issue the checkout carries,
    so the rebuild keeps precedence over the create."""
    beads = Path(ws["outer"].path) / ".beads"
    beads.mkdir(parents=True)
    (beads / "issues.jsonl").write_text("", encoding="utf-8")
    cmds = cli_beads.plan(
        ["list"], Path(ws["outer"].path), None,
        db_exists=False, jsonl_exists=True,
    )
    assert [c[3] for c in cmds] == ["init", "sync", "list"]
    assert "--import-only" in cmds[1]


def test_only_a_vouched_board_may_be_created(ws, tmp_path, monkeypatch):
    monkeypatch.setattr(cli_beads, "repo_root", lambda cwd=None: None)
    plain = tmp_path / "plain"
    plain.mkdir()
    assert cli_beads.autocreatable(beads_db.plain_ref(plain)) is False
    assert cli_beads.autocreatable(beads_db.workspace_ref(ws["inner"])) is True
    beads_db.set_db(beads_db.DEFAULT_BOARD, str(tmp_path / "d.db"))
    assert cli_beads.autocreatable(beads_db.default_ref(None)) is False


# --------------------------------------------------------------------------- #
# setting a board up in full: claunch beads init --workspace / the Settings button
# --------------------------------------------------------------------------- #
def _making_runner(seen=None):
    """A runner standing in for ``br``: ``init`` writes ``<cwd>/.beads/beads.db``
    as the real one does, anything else succeeds silently."""

    def runner(argv, cwd):
        if seen is not None:
            seen.append((list(argv), cwd))
        if "init" in argv:
            made = Path(cwd) / ".beads" / "beads.db"
            made.parent.mkdir(parents=True, exist_ok=True)
            made.write_bytes(b"new board")
        return 0, "", ""

    return runner


def test_setting_a_board_up_makes_the_database_the_policy_and_the_gitignore(ws):
    """What br 0.7 needs beside a board comes with it: without the policy a
    status filter on in_ready/in_review is refused, without the .gitignore
    lines the engine's files show up for 'git add .'."""
    ref = beads_db.workspace_ref(ws["inner"])
    beads = Path(ref.root) / ".beads"
    seen = []
    result = cli_beads.init_board(ref, _making_runner(seen))
    assert [argv for argv, _cwd in seen] == [["br", "init", "--prefix", "inner"]]
    assert result["created"] is True and result["imported"] is False
    assert result["prefix"] == "inner"
    assert result["policy"] is True and result["gitignore"] is True
    assert result["state"] == {
        "database": True, "policy": cli_beads.POLICY_DECLARED,
        "gitignore": True, "complete": True,
    }
    assert cli_beads.policy_state(beads) == cli_beads.POLICY_DECLARED
    assert ".br-wal-index-*/" in (beads / ".gitignore").read_text(encoding="utf-8")


def test_setting_up_a_board_that_is_there_finishes_it_and_leaves_the_database(ws):
    """A board made before br 0.7 has its database and neither file. Setting
    it up adds the two and never runs br init, which would refuse anyway."""
    ref = beads_db.workspace_ref(ws["outer"])
    beads = Path(ref.root) / ".beads"
    beads.mkdir(parents=True)
    (beads / "beads.db").write_bytes(b"mine")
    (beads / ".gitignore").write_text("*.db\n", encoding="utf-8")

    def refuse(argv, cwd):
        raise AssertionError(f"br ran on a board that exists: {argv}")

    assert cli_beads.setup_state(ref)["complete"] is False
    result = cli_beads.init_board(ref, refuse)
    assert result["created"] is False and result["prefix"] is None
    assert result["policy"] is True and result["gitignore"] is True
    assert (beads / "beads.db").read_bytes() == b"mine"
    assert (beads / ".gitignore").read_text(encoding="utf-8").startswith("*.db\n")
    # Once set up, doing it again changes nothing.
    again = cli_beads.init_board(ref, refuse)
    assert (again["created"], again["policy"], again["gitignore"]) == (False, False, False)
    assert again["state"]["complete"] is True


def test_a_clone_with_a_tracked_jsonl_is_rebuilt_under_its_own_prefix(ws):
    """A fresh clone carries issues.jsonl and no database. Its board is filled
    from the JSONL, and new ids continue the prefix the old ones carry rather
    than switching to the board's name -- the rebuild plan() runs."""
    ref = beads_db.workspace_ref(ws["outer"])
    beads = Path(ref.root) / ".beads"
    beads.mkdir(parents=True)
    (beads / "issues.jsonl").write_text('{"id":"legacy-1"}\n', encoding="utf-8")
    (beads / "config.yaml").write_text("issue_prefix: legacy\n", encoding="utf-8")
    seen = []
    result = cli_beads.init_board(ref, _making_runner(seen))
    assert [argv for argv, _cwd in seen] == [
        ["br", "init", "--prefix", "legacy"],
        ["br", "--db", ref.db, "sync", "--import-only"],
    ]
    assert result["created"] is True and result["imported"] is True
    assert result["prefix"] == "legacy"


def test_a_failed_import_is_reported_in_br_s_words(ws):
    ref = beads_db.workspace_ref(ws["outer"])
    beads = Path(ref.root) / ".beads"
    beads.mkdir(parents=True)
    (beads / "issues.jsonl").write_text("{}\n", encoding="utf-8")
    making = _making_runner()

    def runner(argv, cwd):
        return (1, "", "bad line 1") if "sync" in argv else making(argv, cwd)

    with pytest.raises(cli_beads.BeadsError, match="bad line 1"):
        cli_beads.init_board(ref, runner)


def test_a_policy_claunch_cannot_extend_is_reported_and_left_alone(ws):
    """A policy.yaml that is not a mapping is br's to refuse; setting the
    board up neither rewrites it nor calls the board unfinished for it."""
    ref = beads_db.workspace_ref(ws["outer"])
    beads = Path(ref.root) / ".beads"
    beads.mkdir(parents=True)
    (beads / "beads.db").write_bytes(b"")
    (beads / "policy.yaml").write_text("workflow: [a, b]\n", encoding="utf-8")
    assert cli_beads.policy_state(beads) == cli_beads.POLICY_UNREADABLE
    result = cli_beads.init_board(ref, _making_runner())
    assert result["policy"] is False
    assert (beads / "policy.yaml").read_text(encoding="utf-8") == "workflow: [a, b]\n"
    assert result["state"]["complete"] is True


@pytest.mark.parametrize("text, state", [
    (None, cli_beads.POLICY_MISSING),
    ("", cli_beads.POLICY_MISSING),
    ("other: 1\n", cli_beads.POLICY_MISSING),
    ("workflow:\n  statuses: [in_ready]\n", cli_beads.POLICY_MISSING),
    ("workflow:\n  statuses: [in_ready, in_review, x]\n", cli_beads.POLICY_DECLARED),
    ("workflow:\n  statuses: in_review\n", cli_beads.POLICY_UNREADABLE),
    ("[1, 2]\n", cli_beads.POLICY_UNREADABLE),
    ("workflow: {statuses: [\n", cli_beads.POLICY_UNREADABLE),
])
def test_the_policy_state_the_card_shows(tmp_path, text, state):
    if text is not None:
        (tmp_path / "policy.yaml").write_text(text, encoding="utf-8")
    assert cli_beads.policy_state(tmp_path) == state


def test_a_board_is_named_as_the_settings_page_names_it(ws, monkeypatch):
    """By its workspace's name, by that workspace's directory, and not at all
    for a name no board has."""
    assert cli_beads.board_named("inner").root == ws["inner"].path
    monkeypatch.setattr(
        workspaces, "find",
        lambda token, doc=None: ws["outer"] if token == ws["outer"].path else None,
    )
    assert cli_beads.board_named(ws["outer"].path).name == "outer"
    assert cli_beads.board_named("ghost") is None


@pytest.mark.parametrize("args, wanted", [
    (["init", "--workspace", "inner"], True),
    (["init", "--workspace=inner"], True),
    (["init", "--prefix", "x"], False),
    (["init"], False),
    (["list", "--workspace", "inner"], False),
    ([], False),
])
def test_only_init_with_a_workspace_is_claunch_s_own(args, wanted):
    assert cli_beads.wants_workspace_init(args) is wanted


@pytest.fixture
def fake_br(monkeypatch):
    """``br`` installed, and every command it would run answered in-process."""
    seen = []
    monkeypatch.setattr(cli_beads.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(cli_beads, "_subprocess_runner", _making_runner(seen))
    return seen


def test_claunch_beads_init_with_a_workspace_sets_that_board_up(ws, fake_br, capsys):
    assert cli_beads.run(["init", "--workspace", "inner"]) == 0
    out = capsys.readouterr().out
    assert "board inner:" in out
    assert "created (prefix inner)" in out
    assert "declares in_ready, in_review (written)" in out
    assert "br 0.7 lines added" in out
    assert [argv for argv, _cwd in fake_br] == [["br", "init", "--prefix", "inner"]]
    # A second run finds everything there and says so.
    assert cli_beads.run(["init", "--workspace=inner"]) == 0
    out = capsys.readouterr().out
    assert "already there" in out and "(written)" not in out
    assert len(fake_br) == 1


def test_claunch_beads_init_with_a_workspace_answers_json(ws, fake_br, capsys):
    import json

    assert cli_beads.run(["init", "--workspace", "outer", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["board"]["board"] == "outer"
    assert doc["created"] is True and doc["state"]["complete"] is True


def test_claunch_beads_init_names_the_boards_there_are(ws, fake_br):
    with pytest.raises(cli_beads.BeadsError, match="no board named 'ghost'.*inner"):
        cli_beads.run(["init", "--workspace", "ghost"])
    assert fake_br == []


def test_claunch_beads_init_with_a_workspace_takes_no_prefix(ws, fake_br):
    """The prefix is the board's name, as on the Settings page; br's own
    init, without --workspace, is where a prefix is chosen."""
    with pytest.raises(cli_beads.BeadsError, match="no other options.*--prefix"):
        cli_beads.run(["init", "--workspace", "inner", "--prefix", "x"])
    assert fake_br == []


# --------------------------------------------------------------------------- #
# the setting is machine-local
# --------------------------------------------------------------------------- #
def test_the_section_is_not_synced_between_machines():
    """The values are absolute paths, which mean nothing on another machine —
    the same reason ``workspaces`` is absent from the default sections."""
    from claude_launcher import sync

    assert beads_db.SECTION not in sync.DEFAULT_SECTIONS


def test_the_registry_lives_under_one_section(ws, tmp_path):
    beads_db.set_db("outer", str(tmp_path / "o.db"))
    doc = store.load()
    assert doc[beads_db.SECTION][beads_db.BOARDS_KEY]["outer"] == str(tmp_path / "o.db")


def test_the_command_line_runner_reads_utf8_output_whole():
    """br writes UTF-8. Read with the locale codec (cp949 on this machine),
    the first Korean byte kills subprocess.run's reader thread and
    create_board would judge a partial stdout
    (claunch-gds6-subprocess-decode-cp949-ja5ih)."""
    import sys

    code = "import sys; sys.stdout.buffer.write('보드를 만들었다'.encode('utf-8'))"
    rc, out, _err = cli_beads._subprocess_runner(
        [sys.executable, "-c", code], cwd=os.getcwd()
    )

    assert rc == 0
    assert out == "보드를 만들었다"
