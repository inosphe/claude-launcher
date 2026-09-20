"""A user's own note on a session, on every path it is supposed to reach.

Everything else on a session record is there because of how the session was
launched or because the daemon put it there. The note is the exception: it is
the reader's annotation on a terminal — why this one is being kept — and the
only thing it is for is being read back later, by the person who wrote it.
That makes three properties load-bearing and this file pins all three.

It has to be *durable*: written into the definition, so it survives the record
being saved and read again, which is what a daemon restart does. It has to be
*on the rail*: `/api/sessions?view=rail` answers from an allow-list, so a
field that is not named there reaches no browser at all and the row silently
has nothing to draw. And it has to be *in the search corpus*: a note is how
somebody finds a session whose own name no longer means anything to them, and
the corpus is where "Search anything" looks.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher.daemon import db, manager as manager_mod, paths, search_anything
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.session import DeadSession

BEARER = {"Authorization": "Bearer sekrit"}


# ---------------------------------------------------------------- the record

def test_note_rides_the_session_record():
    sdef = SessionDef(name="work", note="kept for the migration thread")
    assert SessionDef.from_dict(sdef.to_dict()) == sdef


def test_a_session_without_a_note_writes_no_key():
    """The ordinary record on disk is unchanged by this field, so a fleet of
    un-annotated sessions keeps the shape it had before the field existed."""
    sdef = SessionDef(name="work")
    assert "note" not in sdef.to_dict()
    assert SessionDef.from_dict(sdef.to_dict()) == sdef


def test_a_record_written_before_the_field_still_reads():
    assert SessionDef.from_dict({"name": "old", "cwd": "/tmp"}).note is None


def test_a_blank_note_reads_as_no_note():
    """Whitespace is not an annotation, and the rail would otherwise draw an
    empty line that looks like a rendering bug."""
    assert SessionDef.from_dict({"name": "x", "note": "   "}).note is None


# ------------------------------------------------------------- the manager

@pytest.fixture
def mgr(home):
    return SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)


def _dead(mgr, name, cwd, **fields):
    record = DeadSession(
        SessionDef(name=name, harness="claude", cwd=str(cwd)), exit_code=0, **fields
    )
    mgr._sessions[name] = record
    return record


def _client(mgr, scenario):
    async def run():
        app = build_app(mgr, "sekrit", started_at=time.monotonic())
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            return await scenario(client)
        finally:
            await client.close()

    return asyncio.run(run())


def test_set_note_persists_with_the_definition(mgr, tmp_path):
    _dead(mgr, "s1", tmp_path)
    mgr.set_note("s1", "  why I keep this  ")

    # Trimmed on the way in, so what the rail draws and what search indexes are
    # the same string the writer meant.
    assert mgr.get("s1").sdef.note == "why I keep this"
    # And on disk, which is the copy a restarting daemon reads.
    entries = {e["def"]["name"]: e["def"] for e in db.SessionStore(paths.sessions_db()).load_all()}
    assert entries["s1"]["note"] == "why I keep this"


def test_an_empty_note_clears_it(mgr, tmp_path):
    """The editor's Save and its Clear are one call: the field is either there
    or absent, and there is no empty-string note for a renderer to meet."""
    _dead(mgr, "s1", tmp_path)
    mgr.set_note("s1", "first")
    mgr.set_note("s1", "   ")

    assert mgr.get("s1").sdef.note is None
    assert "note" not in mgr.get("s1").sdef.to_dict()


def test_a_note_longer_than_the_cap_is_refused(mgr, tmp_path):
    """A paste that lands in the box by accident must not grow the session
    record — or the search corpus it is copied into — without limit."""
    _dead(mgr, "s1", tmp_path)
    with pytest.raises(ValueError):
        mgr.set_note("s1", "x" * (manager_mod.MAX_NOTE + 1))

    assert mgr.get("s1").sdef.note is None


# -------------------------------------------------------------- the endpoint

def test_the_endpoint_sets_and_clears_the_note(mgr, tmp_path):
    _dead(mgr, "s1", tmp_path)

    async def scenario(client):
        resp = await client.post(
            "/api/sessions/s1/note", headers=BEARER, json={"note": "watch this one"}
        )
        assert resp.status == 200
        written = (await resp.json())["note"]
        cleared = await client.post(
            "/api/sessions/s1/note", headers=BEARER, json={"note": ""}
        )
        assert cleared.status == 200
        return written, (await cleared.json()).get("note")

    assert _client(mgr, scenario) == ("watch this one", None)


def test_the_endpoint_refuses_a_note_that_is_not_text(mgr, tmp_path):
    _dead(mgr, "s1", tmp_path)

    async def scenario(client):
        return (await client.post(
            "/api/sessions/s1/note", headers=BEARER, json={"note": 7}
        )).status

    assert _client(mgr, scenario) == 400


def test_the_endpoint_refuses_a_note_over_the_cap(mgr, tmp_path):
    _dead(mgr, "s1", tmp_path)

    async def scenario(client):
        return (await client.post(
            "/api/sessions/s1/note", headers=BEARER,
            json={"note": "x" * (manager_mod.MAX_NOTE + 1)},
        )).status

    assert _client(mgr, scenario) == 400


def test_the_note_reaches_the_rail_the_list_is_built_from(mgr, tmp_path):
    """`view=rail` answers from an allow-list (api.py's rail_fields). A field
    left out of it never reaches the browser, and the failure is silent: the
    row is simply drawn without the line."""
    _dead(mgr, "s1", tmp_path)
    mgr.set_note("s1", "the one I keep coming back to")

    async def scenario(client):
        resp = await client.get("/api/sessions?view=rail", headers=BEARER)
        rows = {row["name"]: row for row in (await resp.json())["sessions"]}
        return rows["s1"].get("note")

    assert _client(mgr, scenario) == "the one I keep coming back to"


# ---------------------------------------------------------- the search corpus

def test_a_note_is_in_the_search_corpus(tmp_path):
    """A note is a way to find a session again, so it belongs in the text the
    corpus indexes rather than only on the row that draws it."""
    root = tmp_path / "repo"

    class Board:
        async def issues(self, root):
            return []

        async def br(self, root, args):
            return []

    session = SimpleNamespace(sdef=SimpleNamespace(
        name="s1", cwd=str(root), task="task", issue=None,
        note="waiting on the vendor reply",
    ))

    async def resolve(cwd):
        return root

    service = SimpleNamespace(
        manager=SimpleNamespace(list=lambda: [session]), board=Board(),
        known_roots=lambda: [root], resolve_root=resolve,
    )
    obs = SimpleNamespace(load_session=lambda n: {}, reports=SimpleNamespace(rows=lambda n: []))
    docs = asyncio.run(search_anything.Corpus(service, obs).docs())

    session_docs = [d for d in docs if d.meta["kind"] == "session"]
    assert any("waiting on the vendor reply" in d.chunks[0] for d in session_docs)
