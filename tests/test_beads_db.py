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


@pytest.mark.parametrize(
    "bad, says",
    [
        ("", "needs a path"),
        ("boards/x.db", "absolute"),
        (r"C:\boards", "does not end in"),
        (r"C:\boards\x.sqlite", "does not end in"),
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
