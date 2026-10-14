"""quick-fork / merge and handoff (daemon/handoff.py) end to end.

``POST /api/sessions/{A}/quick-fork`` copies ``A`` into a child with a marker
block on top; ``POST /api/sessions/{B}/handoff`` is the way back — the
operator's request (an instruction block typed into ``B``) and the agent's
completion (the wrap-up typed into the target, then ``B`` ended). The fork
itself is claude's ``--resume --fork-session`` and needs a transcript on
disk, which the ``py`` harness has none of, so the conversation copy is
stubbed at :func:`spawn.can_fork` / :func:`spawn._fork_parents_conversation`
and everything around it — naming, the marker, the record, the two halves of
the way back, what a kill does to a pending request — is exercised for real.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

import pytest

from claude_launcher import lineage, profile, spawn as spawn_mod, store
from claude_launcher.daemon import handoff as handoff_mod
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


def _register_py_harness():
    store.update(
        lambda doc: doc.update(
            {"harnesses": {"py": {"command": [sys.executable, "-u", "-c", CHILD]}}}
        )
    )
    if not profile.resolve("py").exists():
        lineage.set_harness(profile.create("py"), "py")


async def _serve(mgr, mm):
    from aiohttp import ClientTimeout
    from aiohttp.test_utils import TestClient, TestServer

    app = build_app(mgr, "sekrit", started_at=time.monotonic(), mesh=mm)
    # The server runs on this test's own loop, so a connect to it is late
    # only when the host has not run that loop. aiohttp's default 30 s
    # connect bound turned such a pause under a loaded gate run into a
    # failure of a test about forking (claunch-wt12o.5.1). The total bound
    # (300 s, the default's own) still ends a request nobody answers.
    client = TestClient(TestServer(app), timeout=ClientTimeout(total=300))
    await client.start_server()
    return client


async def _wait_for(cond, what: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.fixture
def forkable(monkeypatch):
    """Let the ``py`` harness stand in for a claude session with a
    conversation on disk: the copy is claude's business, the rest is ours."""
    monkeypatch.setattr(spawn_mod, "can_fork", lambda parent: True)
    monkeypatch.setattr(
        spawn_mod, "_fork_parents_conversation", lambda child, parent, request: None
    )


@pytest.fixture
def typed(monkeypatch):
    """Every block the daemon would type into a session, by session name."""
    seen = []

    async def fake_deliver(self, text, *, force=False):
        seen.append((self.sdef.name, text))
        return True

    monkeypatch.setattr(Session, "deliver", fake_deliver)
    return seen


def _mgr():
    return SessionManager(idle_threshold=0.2, scrollback=200, restore_default=True)


# ---- the blocks --------------------------------------------------------- #

def test_the_marker_block_names_origin_fork_marker_and_the_way_back():
    text = handoff_mod.compose_marker(
        origin="a", fork="a-qf1", marker="qf-0123abcd", forked_at="2026-09-16T05:00:00+00:00"
    )
    assert text.startswith("---\n# claunch quick-fork: --- forked from here ---")
    assert "machine-generated, not typed by the user" in text
    assert "origin: a\n" in text and "fork: a-qf1\n" in text
    assert "marker: qf-0123abcd\n" in text
    assert "claunch quick-fork merge -f <file>" in text
    assert "'handoff' tool" in text
    assert text.endswith("\n---")
    # by default the copy waits for the operator's press
    assert "does NOT merge on its own" in text and "'MERGE requested'" in text
    allowed = handoff_mod.compose_marker(
        origin="a", fork="a-qf1", marker="qf-0123abcd",
        forked_at="2026-09-16T05:00:00+00:00", self_merge_allowed=True,
    )
    assert "merge: when the work is done" in allowed
    assert "does NOT merge" not in allowed


def test_the_report_block_carries_the_agents_text_verbatim_under_a_header():
    merged = handoff_mod.compose_report(
        kind="merge", source="a-qf1", target="a", text="  did X\nleft Y  ",
        marker="qf-1", forked_at="2026-09-16T05:00:00+00:00",
    )
    assert merged.startswith("---\n# claunch quick-fork: merged from a-qf1")
    assert "marker: qf-1 (forked from this conversation at 2026-09-16T05:00:00+00:00)" in merged
    assert merged.endswith("---\ndid X\nleft Y")
    handed = handoff_mod.compose_report(kind="handoff", source="d", target="c", text="state: ...")
    assert handed.startswith("---\n# claunch handoff: from d")
    assert "from: d\n" in handed and handed.endswith("---\nstate: ...")


def test_fork_names_take_the_first_free_suffix():
    assert handoff_mod.fork_name("a", set()) == "a-qf1"
    assert handoff_mod.fork_name("a", {"a-qf1", "a-qf2"}) == "a-qf3"


def test_the_field_is_written_only_when_set_and_read_back():
    plain = SessionDef(name="x").to_dict()
    assert "quick_fork_of" not in plain
    copy = SessionDef(name="x-qf1", quick_fork_of="x")
    assert copy.to_dict()["quick_fork_of"] == "x"
    assert SessionDef.from_dict(copy.to_dict()).quick_fork_of == "x"
    assert SessionDef.from_dict({"name": "y", "quick_fork_of": ""}).quick_fork_of is None
    assert "self_merge_allowed" not in copy.to_dict()
    allowed = SessionDef(name="x-qf2", quick_fork_of="x", self_merge_allowed=True)
    assert allowed.to_dict()["self_merge_allowed"] is True
    assert SessionDef.from_dict(allowed.to_dict()).self_merge_allowed is True


# ---- quick-fork --------------------------------------------------------- #

def test_quick_fork_makes_a_marked_child_of_the_origin(home, tmp_path, forkable, typed):
    _register_py_harness()

    async def run():
        mgr = _mgr()
        client = await _serve(mgr, MeshManager(mgr, root=tmp_path / "mesh"))
        try:
            mgr.create(SessionDef(name="a", harness="py", cwd=str(tmp_path)))
            resp = await client.post("/api/sessions/a/quick-fork", json={"task": "try the other approach"}, headers=BEARER)
            doc = await resp.json()
            assert resp.status == 201, doc
            assert doc["origin"] == "a" and doc["marker"].startswith("qf-")
            child = doc["session"]
            assert child["name"] == "a-qf1"
            assert child["parent"] == "a"
            assert child["quick_fork_of"] == "a"
            # the marker block is the copy's opening, the task rides under it
            task = child["task"]
            assert task.startswith("---\n# claunch quick-fork: --- forked from here ---")
            assert f"marker: {doc['marker']}\n" in task
            assert task.endswith("try the other approach")
            # the record survives a round trip through the store
            assert mgr.get("a-qf1").sdef.quick_fork_of == "a"
            # a second fork of the same origin takes the next name
            resp = await client.post("/api/sessions/a/quick-fork", headers=BEARER)
            assert resp.status == 201 and (await resp.json())["session"]["name"] == "a-qf2"
            # the origin is not written into: the marker is the copy's alone
            assert [n for n, _ in typed if n == "a"] == []
            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_codex_quick_fork_pins_new_rollout_restores_and_merges(home, tmp_path, typed):
    from claude_launcher import harnesses
    from claude_launcher.daemon import db, harness

    source_id = "11111111-2222-3333-4444-555555555555"
    copy_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    # Stand in for the native CLI, including its new-rollout side effect.
    # Spawn validation, command building, discovery and persistence are real.
    program = (
        "import os,json,pathlib,sys\n"
        "if 'fork' in sys.argv:\n"
        f" assert sys.argv[sys.argv.index('fork')+1] == {source_id!r}\n"
        " assert 'resume' not in sys.argv\n"
        " p=pathlib.Path(os.environ['CODEX_HOME'])/'sessions'/'copy.jsonl'\n"
        f" p.write_text(json.dumps({{'type':'session_meta','payload':{{'id':{copy_id!r},'cwd':os.getcwd()}}}})+'\\n')\n"
        "print('READY',flush=True)\n"
        "for line in sys.stdin: pass\n"
    )
    store.update(lambda doc: doc.update({"harnesses": {"codex": {
        "command": [sys.executable, "-u", "-c", program],
        "home_env": "CODEX_HOME", "restore_args": ["resume", "--last"],
    }}}))
    prof = profile.create("cx")
    lineage.set_harness(prof, "codex")
    path = harnesses.get("codex").profile_home(prof.config_dir) / "sessions" / "source.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    original = json.dumps({"type": "session_meta", "payload": {
        "id": source_id, "cwd": str(tmp_path),
    }}) + "\n"
    path.write_text(original, encoding="utf-8")

    async def run():
        mgr = _mgr()
        client = await _serve(mgr, MeshManager(mgr, root=tmp_path / "mesh"))
        try:
            origin = mgr.create(SessionDef(
                name="cx", profile="cx", cwd=str(tmp_path),
                args=("resume", source_id), conversation_id=source_id,
            ))
            resp = await client.post("/api/sessions/cx/quick-fork", headers=BEARER)
            doc = await resp.json()
            assert resp.status == 201, doc
            child = mgr.get("cx-qf1")
            await _wait_for(lambda: child.sdef.conversation_id == copy_id, "Codex copy ID")
            assert child.sdef.quick_fork_of == "cx"
            assert "--- forked from here ---" in child.sdef.task
            assert origin.sdef.conversation_id == source_id
            saved = {row["def"]["name"]: row["def"] for row in db.open_default().load_all()}
            assert saved["cx-qf1"]["conversation_id"] == copy_id
            argv, _, _ = harness.build_command(child.sdef, restoring=True)
            assert argv[-2:] == ["resume", copy_id]
            assert "fork" not in argv and source_id not in argv
            assert path.read_text(encoding="utf-8") == original
            await client.post("/api/sessions/cx-qf1/handoff", json={}, headers=BEARER)
            resp = await client.post("/api/sessions/cx-qf1/handoff",
                                     json={"text": "Codex fork result"}, headers=BEARER)
            assert resp.status == 200, await resp.text()
            await _wait_for(lambda: child.exited, "merged copy exit")
            assert any(n == "cx" and "Codex fork result" in text for n, text in typed)
            assert not origin.exited
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_quick_fork_refuses_a_session_with_nothing_to_copy(home, tmp_path, monkeypatch):
    _register_py_harness()
    monkeypatch.setattr(spawn_mod, "can_fork", lambda parent: False)

    async def run():
        mgr = _mgr()
        client = await _serve(mgr, MeshManager(mgr, root=tmp_path / "mesh"))
        try:
            mgr.create(SessionDef(name="a", harness="py", cwd=str(tmp_path)))
            resp = await client.post("/api/sessions/a/quick-fork", headers=BEARER)
            assert resp.status == 400
            assert "no conversation to copy" in (await resp.json())["error"]
            resp = await client.post("/api/sessions/nope/quick-fork", headers=BEARER)
            assert resp.status == 404
            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_a_plain_spawn_cannot_claim_to_be_a_quick_fork(home, tmp_path, forkable):
    """``quick_fork_of`` unlocks merge, so only a request that copied the
    conversation (``fork``) may carry it — a spawn body naming it without
    the fork is a claim the record refuses."""
    _register_py_harness()

    async def run():
        mgr = _mgr()
        client = await _serve(mgr, MeshManager(mgr, root=tmp_path / "mesh"))
        try:
            mgr.create(SessionDef(name="a", harness="py", cwd=str(tmp_path)))
            resp = await client.post(
                "/api/sessions/a/children",
                json={"name": "kid", "quick_fork_of": "a", "beads": False},
                headers=BEARER,
            )
            assert resp.status == 201, await resp.text()
            assert mgr.get("kid").sdef.quick_fork_of is None
            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


# ---- merge: request, then completion ------------------------------------ #

def test_merge_request_types_the_instruction_into_the_fork_and_marks_it_pending(home, tmp_path, forkable, typed):
    _register_py_harness()

    async def run():
        mgr = _mgr()
        client = await _serve(mgr, MeshManager(mgr, root=tmp_path / "mesh"))
        try:
            mgr.create(SessionDef(name="a", harness="py", cwd=str(tmp_path)))
            resp = await client.post("/api/sessions/a/quick-fork", headers=BEARER)
            marker = (await resp.json())["marker"]
            resp = await client.post("/api/sessions/a-qf1/handoff", json={}, headers=BEARER)
            doc = await resp.json()
            assert resp.status == 200, doc
            assert doc["requested"] is True and doc["kind"] == "merge" and doc["target"] == "a"
            assert doc["marker"] == marker
            await _wait_for(lambda: any(n == "a-qf1" for n, _ in typed), "the request block")
            block = [t for n, t in typed if n == "a-qf1"][-1]
            assert block.startswith("---\n# claunch quick-fork: MERGE requested")
            assert f"marker: {marker}\n" in block and "target: a\n" in block
            # the row says so, and the detail panel's meta does too
            resp = await client.get("/api/sessions", headers=BEARER)
            rows = {r["name"]: r for r in (await resp.json())["sessions"]}
            assert rows["a-qf1"]["handoff"]["kind"] == "merge"
            assert "handoff" not in rows["a"]
            resp = await client.get("/api/sessions/a-qf1/meta", headers=BEARER)
            assert (await resp.json())["handoff"]["target"] == "a"
            assert not mgr.get("a-qf1").exited      # a request ends nothing
            # cancel: the row clears and the fork is told
            resp = await client.delete("/api/sessions/a-qf1/handoff", headers=BEARER)
            assert resp.status == 200 and (await resp.json())["cancelled"] is True
            resp = await client.get("/api/sessions", headers=BEARER)
            rows = {r["name"]: r for r in (await resp.json())["sessions"]}
            assert "handoff" not in rows["a-qf1"]
            await _wait_for(lambda: any("request withdrawn" in t for n, t in typed if n == "a-qf1"), "the cancel notice")
            resp = await client.delete("/api/sessions/a-qf1/handoff", headers=BEARER)
            assert resp.status == 404
            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_the_rail_poll_carries_what_the_fork_merge_pair_reads(home, tmp_path, forkable, typed):
    """``/api/sessions?view=rail`` is the only list the dashboard header reads.

    ``sessionsCache`` in app.js is filled from this response and from nothing
    else, and ``forkControlState``/``handoffControlState`` decide the header's
    ``⑂ fork`` / ``↩ merge`` pair off that cache. The rail view answers a
    whitelist of fields, and while ``quick_fork_of`` was not on it the merge
    button was hidden on every quick-fork there was, and the fork button --
    whose guard is ``!s.quick_fork_of`` -- was offered on the copy instead.
    """
    _register_py_harness()

    async def run():
        mgr = _mgr()
        client = await _serve(mgr, MeshManager(mgr, root=tmp_path / "mesh"))
        try:
            mgr.create(SessionDef(name="a", harness="py", cwd=str(tmp_path)))
            resp = await client.post("/api/sessions/a/quick-fork", headers=BEARER)
            assert resp.status == 201, await resp.json()

            async def rail():
                got = await client.get("/api/sessions?view=rail", headers=BEARER)
                return {r["name"]: r for r in (await got.json())["sessions"]}

            rows = await rail()
            # the copy says whose copy it is; the origin has no such field
            assert rows["a-qf1"]["quick_fork_of"] == "a"
            assert "quick_fork_of" not in rows["a"]
            # and the rail view still withholds what only the detail panel wants
            assert "task" not in rows["a-qf1"] and "env" not in rows["a-qf1"]

            # a pending merge rides the same poll: the button reads "merging…"
            # and a second press is "stop now", both off this field
            resp = await client.post("/api/sessions/a-qf1/handoff", json={}, headers=BEARER)
            assert resp.status == 200, await resp.json()
            rows = await rail()
            assert rows["a-qf1"]["handoff"]["kind"] == "merge"
            assert rows["a-qf1"]["handoff"]["target"] == "a"
            assert "handoff" not in rows["a"]
            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_merge_completion_delivers_to_the_origin_then_ends_the_fork(home, tmp_path, forkable, typed):
    _register_py_harness()

    async def run():
        mgr = _mgr()
        client = await _serve(mgr, MeshManager(mgr, root=tmp_path / "mesh"))
        try:
            mgr.create(SessionDef(name="a", harness="py", cwd=str(tmp_path)))
            resp = await client.post("/api/sessions/a/quick-fork", headers=BEARER)
            marker = (await resp.json())["marker"]
            await client.post("/api/sessions/a-qf1/handoff", json={}, headers=BEARER)
            # the agent hands in its wrap-up: no target needed, it is the origin
            resp = await client.post(
                "/api/sessions/a-qf1/handoff",
                json={"text": "## wrap-up\n- did the thing\n- left the other"},
                headers=BEARER,
            )
            doc = await resp.json()
            assert resp.status == 200, doc
            assert doc["completed"] is True and doc["target"] == "a" and doc["ended"] is True
            report = [t for n, t in typed if n == "a"][-1]
            assert report.startswith("---\n# claunch quick-fork: merged from a-qf1")
            assert f"marker: {marker}" in report
            assert report.endswith("---\n## wrap-up\n- did the thing\n- left the other")
            await _wait_for(lambda: mgr.get("a-qf1").exited, "the fork to end")
            assert not mgr.get("a").exited
            resp = await client.get("/api/sessions", headers=BEARER)
            rows = {r["name"]: r for r in (await resp.json())["sessions"]}
            assert "handoff" not in rows["a-qf1"]
            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_a_quick_fork_does_not_merge_without_the_operators_request(home, tmp_path, forkable, typed):
    """claunch-guljk: s15-qf2 merged itself (and was ended) the moment its
    parent asked it to wrap up. A quick-fork's completion now waits for the
    operator's press; neither the copy nor its parent can file that press."""
    _register_py_harness()

    async def run():
        mgr = _mgr()
        client = await _serve(mgr, MeshManager(mgr, root=tmp_path / "mesh"))
        try:
            mgr.create(SessionDef(name="a", harness="py", cwd=str(tmp_path)))
            mgr.create(SessionDef(name="c", harness="py", cwd=str(tmp_path)))
            resp = await client.post("/api/sessions/a/quick-fork", headers=BEARER)
            child = (await resp.json())["session"]
            assert "self_merge_allowed" not in child
            assert "does NOT merge on its own" in child["task"]
            # the copy's own completion, merge or handoff: refused, nothing typed
            for body in ({"text": "wrap"}, {"text": "wrap", "to": "c"}):
                resp = await client.post("/api/sessions/a-qf1/handoff", json=body, headers=BEARER)
                assert resp.status == 400
                assert "no merge was requested" in (await resp.json())["error"]
            assert not any(n in ("a", "c") for n, _ in typed)
            assert not mgr.get("a-qf1").exited
            # a request filed by a session -- the copy or its parent -- is refused
            for actor in ("a-qf1", "a"):
                resp = await client.post("/api/sessions/a-qf1/handoff",
                                         json={"actor": actor}, headers=BEARER)
                assert resp.status == 400
                assert "only by the operator" in (await resp.json())["error"]
            resp = await client.get("/api/sessions", headers=BEARER)
            rows = {r["name"]: r for r in (await resp.json())["sessions"]}
            assert "handoff" not in rows["a-qf1"]
            # the operator's press (no actor): then the completion lands
            resp = await client.post("/api/sessions/a-qf1/handoff", json={}, headers=BEARER)
            assert resp.status == 200
            resp = await client.post("/api/sessions/a-qf1/handoff", json={"text": "wrap"}, headers=BEARER)
            assert resp.status == 200 and (await resp.json())["ended"] is True
            await _wait_for(lambda: mgr.get("a-qf1").exited, "the fork to end")
            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_the_allowed_flag_lets_a_quick_fork_merge_on_its_own(home, tmp_path, forkable, typed):
    _register_py_harness()

    async def run():
        mgr = _mgr()
        client = await _serve(mgr, MeshManager(mgr, root=tmp_path / "mesh"))
        try:
            mgr.create(SessionDef(name="a", harness="py", cwd=str(tmp_path)))
            mgr.create(SessionDef(name="c", harness="py", cwd=str(tmp_path)))
            # given at creation
            resp = await client.post("/api/sessions/a/quick-fork",
                                     json={"self_merge_allowed": True}, headers=BEARER)
            child = (await resp.json())["session"]
            assert child["self_merge_allowed"] is True
            assert "merge: when the work is done" in child["task"]
            assert mgr.get("a-qf1").sdef.self_merge_allowed is True
            resp = await client.post("/api/sessions/a-qf1/handoff", json={"text": "wrap"}, headers=BEARER)
            assert resp.status == 200 and (await resp.json())["ended"] is True
            # set afterwards, by the operator -- not by the copy, and not on a non-fork
            await client.post("/api/sessions/a/quick-fork", headers=BEARER)
            resp = await client.post("/api/sessions/a-qf2/self-merge",
                                     json={"actor": "a-qf2"}, headers=BEARER)
            assert resp.status == 400 and "cannot allow its own" in (await resp.json())["error"]
            resp = await client.post("/api/sessions/c/self-merge", headers=BEARER)
            assert resp.status == 400 and "not a quick-fork" in (await resp.json())["error"]
            resp = await client.post("/api/sessions/a-qf2/self-merge", headers=BEARER)
            assert resp.status == 200 and (await resp.json())["self_merge_allowed"] is True
            resp = await client.post("/api/sessions/a-qf2/self-merge?off=1", headers=BEARER)
            assert (await resp.json())["self_merge_allowed"] is False
            resp = await client.post("/api/sessions/a-qf2/handoff", json={"text": "w"}, headers=BEARER)
            assert resp.status == 400
            await client.post("/api/sessions/a-qf2/self-merge", headers=BEARER)
            resp = await client.post("/api/sessions/a-qf2/handoff", json={"text": "w"}, headers=BEARER)
            assert resp.status == 200
            # a plain session's handoff is untouched: it still completes on its own
            mgr.create(SessionDef(name="d", harness="py", cwd=str(tmp_path)))
            resp = await client.post("/api/sessions/d/handoff",
                                     json={"to": "c", "text": "x"}, headers=BEARER)
            assert resp.status == 200
            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_merge_refuses_a_non_fork_another_target_and_a_gone_origin(home, tmp_path, forkable, typed):
    _register_py_harness()

    async def run():
        mgr = _mgr()
        client = await _serve(mgr, MeshManager(mgr, root=tmp_path / "mesh"))
        try:
            mgr.create(SessionDef(name="a", harness="py", cwd=str(tmp_path)))
            mgr.create(SessionDef(name="c", harness="py", cwd=str(tmp_path)))
            # not a fork: merge has nowhere to go
            resp = await client.post("/api/sessions/c/handoff", json={"kind": "merge"}, headers=BEARER)
            assert resp.status == 400 and "not a quick-fork" in (await resp.json())["error"]
            await client.post("/api/sessions/a/quick-fork", headers=BEARER)
            # a fork's merge goes back to its origin, not elsewhere
            resp = await client.post("/api/sessions/a-qf1/handoff", json={"kind": "merge", "to": "c"}, headers=BEARER)
            assert resp.status == 400 and "goes back there" in (await resp.json())["error"]
            # ...but a handoff from a fork to a third session is allowed
            resp = await client.post("/api/sessions/a-qf1/handoff", json={"to": "c"}, headers=BEARER)
            assert resp.status == 200 and (await resp.json())["kind"] == "handoff"
            # origin gone: the completion keeps the fork alive and says so
            # (the handoff request to c above is still pending, so this is
            # the gone origin's refusal and not the no-request one)
            mgr.kill("a")
            await _wait_for(lambda: mgr.get("a").exited, "a to exit")
            resp = await client.post("/api/sessions/a-qf1/handoff", json={"text": "x", "kind": "merge"}, headers=BEARER)
            assert resp.status == 400 and "has exited" in (await resp.json())["error"]
            assert not mgr.get("a-qf1").exited
            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


# ---- handoff between unrelated sessions --------------------------------- #

def test_handoff_needs_a_live_target_that_is_not_the_source(home, tmp_path, typed):
    _register_py_harness()

    async def run():
        mgr = _mgr()
        client = await _serve(mgr, MeshManager(mgr, root=tmp_path / "mesh"))
        try:
            mgr.create(SessionDef(name="c", harness="py", cwd=str(tmp_path)))
            mgr.create(SessionDef(name="d", harness="py", cwd=str(tmp_path)))
            resp = await client.post("/api/sessions/d/handoff", json={}, headers=BEARER)
            assert resp.status == 400 and "'to' is required" in (await resp.json())["error"]
            resp = await client.post("/api/sessions/d/handoff", json={"to": "d"}, headers=BEARER)
            assert resp.status == 400 and "itself" in (await resp.json())["error"]
            resp = await client.post("/api/sessions/d/handoff", json={"to": "zz"}, headers=BEARER)
            assert resp.status == 404
            # the request: typed into d, pending on d
            resp = await client.post("/api/sessions/d/handoff", json={"to": "c"}, headers=BEARER)
            doc = await resp.json()
            assert resp.status == 200 and doc["kind"] == "handoff" and doc["target"] == "c"
            await _wait_for(lambda: any(n == "d" for n, _ in typed), "the request block")
            block = [t for n, t in typed if n == "d"][-1]
            assert block.startswith("---\n# claunch handoff: HANDOFF requested")
            assert "claunch handoff --to c -f <file>" in block
            # the completion: typed into c, d ended
            resp = await client.post("/api/sessions/d/handoff", json={"text": "branch d-work @ abc; left: tests"}, headers=BEARER)
            doc = await resp.json()
            assert resp.status == 200 and doc["completed"] and doc["target"] == "c"
            report = [t for n, t in typed if n == "c"][-1]
            assert report.startswith("---\n# claunch handoff: from d")
            assert report.endswith("---\nbranch d-work @ abc; left: tests")
            await _wait_for(lambda: mgr.get("d").exited, "d to end")
            assert not mgr.get("c").exited
            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_a_completion_whose_delivery_fails_keeps_the_source(home, tmp_path, monkeypatch):
    _register_py_harness()

    async def refuse(self, text, *, force=False):
        return False

    monkeypatch.setattr(Session, "deliver", refuse)

    async def run():
        mgr = _mgr()
        client = await _serve(mgr, MeshManager(mgr, root=tmp_path / "mesh"))
        try:
            mgr.create(SessionDef(name="c", harness="py", cwd=str(tmp_path)))
            mgr.create(SessionDef(name="d", harness="py", cwd=str(tmp_path)))
            resp = await client.post("/api/sessions/d/handoff", json={"to": "c", "text": "x"}, headers=BEARER)
            assert resp.status == 400
            assert "is kept" in (await resp.json())["error"]
            assert not mgr.get("d").exited
            # an empty text is not a completion either
            resp = await client.post("/api/sessions/d/handoff", json={"to": "c", "text": "   "}, headers=BEARER)
            assert resp.status == 200 and (await resp.json())["requested"] is True
            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_a_kill_clears_a_pending_request(home, tmp_path, typed):
    _register_py_harness()

    async def run():
        mgr = _mgr()
        client = await _serve(mgr, MeshManager(mgr, root=tmp_path / "mesh"))
        try:
            mgr.create(SessionDef(name="c", harness="py", cwd=str(tmp_path)))
            mgr.create(SessionDef(name="d", harness="py", cwd=str(tmp_path)))
            await client.post("/api/sessions/d/handoff", json={"to": "c"}, headers=BEARER)
            resp = await client.get("/api/sessions", headers=BEARER)
            assert "handoff" in {r["name"]: r for r in (await resp.json())["sessions"]}["d"]
            resp = await client.post("/api/sessions/d/kill", headers=BEARER)
            assert resp.status == 200
            await _wait_for(lambda: mgr.get("d").exited, "d to end")
            resp = await client.get("/api/sessions", headers=BEARER)
            assert "handoff" not in {r["name"]: r for r in (await resp.json())["sessions"]}["d"]
            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


# ---- the CLI and the MCP tool ------------------------------------------- #

class _FakeClient:
    def __init__(self, replies):
        self.calls = []
        self._replies = replies

    def post(self, path, body=None):
        self.calls.append(("post", path, body))
        return self._replies.get(path, {})

    def delete(self, path):
        self.calls.append(("delete", path, None))
        return self._replies.get(path, {})


@pytest.fixture
def fake_daemon(monkeypatch):
    from claude_launcher import daemon_client

    def install(replies):
        client = _FakeClient(replies)
        monkeypatch.setattr(daemon_client, "ensure_running", lambda: client)
        return client

    return install


def test_cli_quick_fork_creates_the_copy_and_says_how_to_merge(fake_daemon, capsys, monkeypatch):
    from claude_launcher import cli

    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    client = fake_daemon({
        "/api/sessions/a/quick-fork": {
            "session": {"name": "a-qf1", "pid": 42}, "marker": "qf-1", "origin": "a",
        },
        "/api/sessions/b/quick-fork": {
            "session": {"name": "scratch", "pid": 43}, "marker": "qf-2", "origin": "b",
        },
    })
    assert cli.main(["quick-fork", "a", "--task", "try it"]) == 0
    assert client.calls == [("post", "/api/sessions/a/quick-fork", {"task": "try it"})]
    out = capsys.readouterr().out
    assert "quick-fork 'a-qf1' of 'a' started" in out
    # a copy that may not merge on its own: the operator is told how to ask
    assert "claunch quick-fork merge -t a-qf1" in out
    assert "claunch quick-fork merge -f" not in out
    assert cli.main(["quick-fork", "a", "--allow-self-merge"]) == 0
    assert client.calls[-1] == ("post", "/api/sessions/a/quick-fork", {"self_merge_allowed": True})
    # no session at all: refused before any call
    assert cli.main(["quick-fork"]) == 1
    # inside a session the origin defaults to it
    monkeypatch.setenv("CLAUNCH_SESSION", "b")
    assert cli.main(["quick-fork", "--as", "scratch"]) == 0
    assert client.calls[-1] == ("post", "/api/sessions/b/quick-fork", {"name": "scratch"})


def test_cli_quick_fork_merge_hands_in_or_asks(fake_daemon, capsys, monkeypatch, tmp_path):
    from claude_launcher import cli

    monkeypatch.setenv("CLAUNCH_SESSION", "a-qf1")
    client = fake_daemon({
        "/api/sessions/a-qf1/handoff": {"completed": True, "target": "a", "ended": True},
    })
    wrap = tmp_path / "wrap.md"
    wrap.write_text("## done\n- x", encoding="utf-8")
    assert cli.main(["quick-fork", "merge", "-f", str(wrap)]) == 0
    assert client.calls[-1] == ("post", "/api/sessions/a-qf1/handoff", {"kind": "merge", "text": "## done\n- x"})
    assert "merged: the wrap-up was typed into 'a'" in capsys.readouterr().out
    # the operator's press: no text, aimed with -t
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    client = fake_daemon({
        "/api/sessions/a-qf1/handoff": {"requested": True, "target": "a", "kind": "merge"},
    })
    assert cli.main(["quick-fork", "merge", "-t", "a-qf1"]) == 0
    assert client.calls[-1] == ("post", "/api/sessions/a-qf1/handoff", {"kind": "merge"})
    assert "merge requested" in capsys.readouterr().out
    # nowhere to aim: refused
    assert cli.main(["quick-fork", "merge"]) == 1


def test_cli_handoff_hands_in_asks_and_cancels(fake_daemon, capsys, monkeypatch):
    from claude_launcher import cli

    monkeypatch.setenv("CLAUNCH_SESSION", "d")
    client = fake_daemon({
        "/api/sessions/d/handoff": {"completed": True, "target": "c", "ended": True, "kind": "handoff"},
    })
    assert cli.main(["handoff", "--to", "c", "state: branch d-work"]) == 0
    assert client.calls[-1] == ("post", "/api/sessions/d/handoff", {"to": "c", "kind": "handoff", "text": "state: branch d-work"})
    assert "handed off: the handoff was typed into 'c'; session 'd' ended" in capsys.readouterr().out
    client = fake_daemon({
        "/api/sessions/d/handoff": {"requested": True, "target": "c", "kind": "handoff"},
    })
    assert cli.main(["handoff", "-t", "d", "--to", "c"]) == 0
    # filed from inside a session: the request names who filed it
    assert client.calls[-1] == ("post", "/api/sessions/d/handoff", {"to": "c", "kind": "handoff", "actor": "d"})
    assert "handoff requested" in capsys.readouterr().out
    assert cli.main(["handoff", "--cancel"]) == 0
    assert client.calls[-1] == ("delete", "/api/sessions/d/handoff", None)
    assert cli.main(["handoff"]) == 1        # no --to


def test_mcp_handoff_tool_posts_from_the_calling_session(monkeypatch):
    from claude_launcher import mesh_mcp

    calls = []

    class FakeClient:
        def post(self, path, body=None):
            calls.append((path, body))
            return {"completed": True, "target": "a", "ended": True}

    monkeypatch.setattr(mesh_mcp, "_client", lambda: FakeClient())
    monkeypatch.setenv("CLAUNCH_SESSION", "a-qf1")
    assert [t["name"] for t in mesh_mcp.TOOLS if t["name"] == "handoff"] == ["handoff"]
    out = mesh_mcp.call_tool("handoff", {"text": "wrap"})
    assert out["completed"] is True
    assert calls == [("/api/sessions/a-qf1/handoff", {"text": "wrap"})]
    mesh_mcp.call_tool("handoff", {"text": "wrap", "to": "c"})
    assert calls[-1] == ("/api/sessions/a-qf1/handoff", {"text": "wrap", "to": "c"})
    with pytest.raises(mesh_mcp.MeshMcpError):
        mesh_mcp.call_tool("handoff", {"text": "  "})


# ---- what the copy is told, and what it may be given -------------------- #

def test_the_marker_says_the_copy_shares_the_origins_checkout():
    """A fork cannot be moved out of its origin's directory (claude keeps
    transcripts per cwd, so ``spawn.check`` refuses ``fork`` with
    ``worktree``). Two claude sessions then edit one checkout with no lock
    between them, and the only defence is that both know — so the block says
    it, with the directory named."""
    block = handoff_mod.compose_marker(
        origin="a", fork="a-qf1", marker="qf-1", forked_at="T",
        cwd="F:/works/repo",
    )
    line = [ln for ln in block.split("\n") if ln.startswith("checkout: ")]
    assert len(line) == 1, block
    assert "SAME working directory as a" in line[0]
    assert "F:/works/repo" in line[0]
    # and it says what to do about it, not merely that it is so
    assert "same files" in line[0] and "git state" in line[0]
    # the copy joined nothing, so it is told about nothing
    assert "\nmesh: " not in block and "\nworkflow: " not in block


def test_the_marker_names_a_mesh_or_run_only_when_the_fork_was_given_one():
    plain = handoff_mod.compose_marker(
        origin="a", fork="a-qf1", marker="qf-1", forked_at="T",
    )
    assert "\nmesh: " not in plain and "\nworkflow: " not in plain
    # no cwd to name is not a missing line: the sentence stands without it
    assert "SAME working directory as a --" in plain

    given = handoff_mod.compose_marker(
        origin="a", fork="a-qf1", marker="qf-1", forked_at="T",
        mesh="mesh-9", workflow="improv-worker",
    )
    mesh_line = [ln for ln in given.split("\n") if ln.startswith("mesh: ")][0]
    assert "mesh-9" in mesh_line and "a-qf1" in mesh_line
    # told how to look before it sends, since a delivery is typed into a
    # real session's terminal
    assert "claunch mesh members mesh-9" in mesh_line
    run_line = [ln for ln in given.split("\n") if ln.startswith("workflow: ")][0]
    assert "improv-worker" in run_line and "a-qf1" in run_line
    # the block still ends where it did
    assert given.strip().endswith("---")


def test_quick_fork_carries_a_mesh_and_run_when_asked_and_neither_by_default(
    home, tmp_path, forkable, typed
):
    """The default is the scratch copy; the body is how a caller says
    otherwise. Both answers are checked here because the default is a
    judgement (a copy's work is the origin's) and not a limitation — the
    other one has to actually work."""
    _register_py_harness()

    async def run():
        mgr = _mgr()
        meshes = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, meshes)
        try:
            mgr.create(SessionDef(name="a", harness="py", cwd=str(tmp_path)))
            # The daemon refuses a mesh it does not know, which is the right
            # answer and is why this stands up first.
            meshes.create("mesh-9")

            resp = await client.post("/api/sessions/a/quick-fork", headers=BEARER)
            doc = await resp.json()
            assert resp.status == 201, doc
            # Under its own key: onboarding already answers `mesh` with the
            # join record, and a second `mesh` beside it meaning only the
            # name shadowed it — which side won depended on spread order.
            got = doc["quick_fork"]
            assert got["mesh"] == "" and got["workflow"] == ""
            assert got["shared_checkout"] is True
            assert got["cwd"] == str(tmp_path)
            task = doc["session"]["task"]
            assert "\nmesh: " not in task
            assert f"checkout: this copy runs in the SAME working directory as a ({tmp_path})" in task

            resp = await client.post(
                "/api/sessions/a/quick-fork",
                json={"mesh": "mesh-9", "workflow": "-"},
                headers=BEARER,
            )
            doc = await resp.json()
            assert resp.status == 201, doc
            assert doc["quick_fork"]["mesh"] == "mesh-9"
            # '-' is the spelling for none and is reported as none, not as a
            # workflow literally called '-'
            assert doc["quick_fork"]["workflow"] == ""
            task = doc["session"]["task"]
            assert "mesh: you were put in mesh mesh-9 as a-qf2" in task
            assert "\nworkflow: " not in task
            # the join record itself is still there, unshadowed
            assert (doc.get("mesh") or {}).get("handle") == "a-qf2"

            # '.' is the third answer: the origin's, without naming it. The
            # spawn path's own inheritance settles it, so the request never
            # carries a name that a client cache might have got wrong — and
            # the answer is read back from what onboarding actually did.
            resp = await client.post(
                "/api/sessions/a/quick-fork", json={"mesh": "."}, headers=BEARER,
            )
            doc = await resp.json()
            assert resp.status == 201, doc
            # The origin is in no mesh, and inheriting from a session in none
            # is spawn's "open one for the two of you" — so the copy lands in
            # a room named for the origin, NOT in one named '.'. Asserted
            # because it is the surprising half of the dot: asking for the
            # origin's mesh can create a mesh.
            assert doc["quick_fork"]["mesh"] == "a"
            assert "\nmesh: ." not in doc["session"]["task"]
            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


@pytest.mark.parametrize("sibling", [False, True])
def test_quick_fork_inherits_team_without_reusing_parent_channel(
    home, tmp_path, forkable, monkeypatch, sibling
):
    _register_py_harness()

    async def run():
        mgr = _mgr()
        meshes = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, meshes)

        async def launch(session, **kw):
            return session

        monkeypatch.setattr(mgr, "launch_async", launch)
        try:
            mgr.stage(SessionDef(name="a", harness="py", cwd=str(tmp_path)))
            if sibling:
                meshes.create("team")
                await meshes.join("team", "a")
            first = await client.post(
                "/api/sessions/a/quick-fork", headers=BEARER,
                json={"mesh": "-", "workflow": "-", "beads": False},
            )
            first_doc = await first.json()
            assert first.status == 201, first_doc
            channel = meshes.get(first_doc["parent_channel"]["mesh"])
            original_members = set(channel.members)
            assert original_members == {"a", "a-qf1"}

            second = await client.post(
                "/api/sessions/a/quick-fork", headers=BEARER,
                json={"mesh": ".", "workflow": "-", "beads": False,
                      "sibling": sibling},
            )
            second_doc = await second.json()
            assert second.status == 201, second_doc
            team_name = "team" if sibling else "a"
            assert second_doc["quick_fork"]["mesh"] == team_name
            assert set(channel.members) == original_members
            assert meshes.member_for_session(meshes.get(team_name), "a-qf2") is not None
            assert not meshes.get(team_name).parent_channel
        finally:
            await client.close()

    asyncio.run(run())


def test_a_fork_cannot_be_given_a_checkout_of_its_own():
    """Not a gap this round left open — the refusal is the reason the marker
    block warns instead. Asserted here so a later change that 'fixes' the
    sharing by handing the fork a worktree fails loudly: what it would
    actually hand over is a copy that resolves no conversation."""
    parent = {"harness": "claude", "conversation_id": "u1", "cwd": "F:/w"}
    with pytest.raises(spawn_mod.SpawnDenied) as got:
        spawn_mod._fork_parents_conversation(
            {"harness": "claude"}, parent, {"worktree": "mine"},
        )
    assert "transcripts per working directory" in str(got.value)


# ---- placement and role (claunch-9vpeq) ---------------------------------- #

def test_the_marker_tells_the_copy_when_its_role_is_not_the_origins():
    """The conversation above the block was held under the origin's role, and
    the copy reads all of it as its own unless the block says otherwise."""
    changed = handoff_mod.compose_marker(
        origin="a", fork="a-qf1", marker="qf-1", forked_at="T",
        mesh="mesh-9", role="worker", origin_role="leader",
    )
    line = [ln for ln in changed.split("\n") if ln.startswith("role: ")]
    assert len(line) == 1, changed
    assert line[0].startswith("role: CHANGED")
    assert "held as leader" in line[0] and "NOT yours" in line[0]
    assert "You are worker in mesh mesh-9" in line[0]
    assert "claunch mesh stance mesh-9" in line[0]

    same = handoff_mod.compose_marker(
        origin="a", fork="a-qf1", marker="qf-1", forked_at="T",
        mesh="mesh-9", role="worker", origin_role="worker",
    )
    line = [ln for ln in same.split("\n") if ln.startswith("role: ")][0]
    assert "CHANGED" not in line and "the same role a holds" in line

    meshless = handoff_mod.compose_marker(
        origin="a", fork="a-qf1", marker="qf-1", forked_at="T",
        origin_role="leader",
    )
    line = [ln for ln in meshless.split("\n") if ln.startswith("role: ")][0]
    assert line.startswith("role: NONE") and "leader" in line

    # nothing known about roles: no line at all
    plain = handoff_mod.compose_marker(origin="a", fork="a-qf1", marker="qf-1", forked_at="T")
    assert "\nrole: " not in plain and "\nplacement: " not in plain


def test_the_marker_says_where_a_sibling_hangs():
    under = handoff_mod.compose_marker(
        origin="a", fork="a-qf1", marker="qf-1", forked_at="T",
        sibling=True, placed_under="g",
    )
    line = [ln for ln in under.split("\n") if ln.startswith("placement: ")][0]
    assert "SIBLING of a" in line and "answers to g" in line
    assert "merge still goes back to a" in line
    root = handoff_mod.compose_marker(
        origin="a", fork="a-qf1", marker="qf-1", forked_at="T", sibling=True,
    )
    line = [ln for ln in root.split("\n") if ln.startswith("placement: ")][0]
    assert "a root session with no parent" in line


def test_quick_fork_takes_a_role_refuses_a_second_leader_and_places_a_sibling(
    home, tmp_path, forkable, typed
):
    _register_py_harness()

    async def run():
        mgr = _mgr()
        meshes = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, meshes)
        try:
            mgr.create(SessionDef(name="g", harness="py", cwd=str(tmp_path)))
            mgr.create(SessionDef(name="a", harness="py", cwd=str(tmp_path), parent="g"))
            meshes.create("mesh-9")
            await meshes.join("mesh-9", "a", handle="a", role="leader")

            # The origin holds leader live, so a copy asking for it is refused
            # by the daemon, whatever the form did or did not check.
            resp = await client.post(
                "/api/sessions/a/quick-fork",
                json={"mesh": ".", "role": "leader"}, headers=BEARER,
            )
            doc = await resp.json()
            assert resp.status == 400, doc
            assert "exclusive" in doc["error"] and "leader" in doc["error"]
            assert not any(s.sdef.name.startswith("a-qf") for s in mgr.list())

            # Another role is taken, and the copy is told the role changed.
            resp = await client.post(
                "/api/sessions/a/quick-fork",
                json={"mesh": ".", "role": "worker"}, headers=BEARER,
            )
            doc = await resp.json()
            assert resp.status == 201, doc
            got = doc["quick_fork"]
            assert (got["mesh"], got["role"], got["sibling"], got["parent"]) == (
                "mesh-9", "worker", False, "a")
            assert doc["session"]["parent"] == "a"
            task = doc["session"]["task"]
            assert "mesh: you were put in mesh mesh-9" in task
            assert "role: CHANGED" in task and "held as leader in mesh mesh-9" in task
            member = [m for m in meshes.meshes_for_session(doc["session"]["name"])][0]
            assert member["role"] == "worker"

            # Sibling: built from a (its conversation, its marker, merge back
            # to a), recorded under a's parent.
            resp = await client.post(
                "/api/sessions/a/quick-fork",
                json={"mesh": ".", "role": "worker", "sibling": True}, headers=BEARER,
            )
            doc = await resp.json()
            assert resp.status == 201, doc
            assert doc["session"]["parent"] == "g"
            assert doc["session"]["quick_fork_of"] == "a"
            assert doc["quick_fork"]["sibling"] is True and doc["quick_fork"]["parent"] == "g"
            assert doc["quick_fork"]["mesh"] == "mesh-9"
            assert "placement: this copy is a SIBLING of a" in doc["session"]["task"]
            assert doc["session"]["name"] not in mgr.children("a")
            assert doc["session"]["name"] in mgr.children("g")

            # A root's sibling is a root.
            resp = await client.post(
                "/api/sessions/g/quick-fork", json={"sibling": True}, headers=BEARER,
            )
            doc = await resp.json()
            assert resp.status == 201, doc
            assert doc["session"]["parent"] is None
            assert doc["quick_fork"]["parent"] == ""
            assert "a root session with no parent" in doc["session"]["task"]

            # A role with no mesh to hold it in is refused, not dropped.
            resp = await client.post(
                "/api/sessions/a/quick-fork", json={"role": "worker"}, headers=BEARER,
            )
            assert resp.status == 400, await resp.json()
            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_cli_quick_fork_passes_role_and_sibling(fake_daemon, capsys, monkeypatch):
    from claude_launcher import cli

    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    client = fake_daemon({
        "/api/sessions/a/quick-fork": {
            "session": {"name": "a-qf1", "pid": 42}, "marker": "qf-1", "origin": "a",
            "quick_fork": {"mesh": "mesh-9", "role": "worker", "sibling": True, "parent": "g"},
        },
    })
    assert cli.main(["quick-fork", "a", "--mesh", ".", "--role", "worker", "--sibling"]) == 0
    assert client.calls == [("post", "/api/sessions/a/quick-fork",
                             {"mesh": ".", "role": "worker", "sibling": True})]
    out = capsys.readouterr().out
    assert "in mesh mesh-9 and role worker, as a sibling under 'g'" in out


# ---- the run the copy was started on (claunch-ychal) --------------------- #

def _lines(block: str, prefix: str) -> list:
    return [ln for ln in block.split("\n") if ln.startswith(prefix)]


def test_the_marker_tells_a_copy_its_v2_run_is_new_and_not_the_origins():
    """s270-qf1: started on improv-worker-v2 while its origin was a
    sync-integrator, it called flow_status once and then did the opening task
    outside the run. The block now says the run is new, that the origin's run
    is another one, and that the task goes in through the first task -- in
    the v2 run's own tool names."""
    block = handoff_mod.compose_marker(
        origin="s270", fork="s270-qf1", marker="qf-1", forked_at="T",
        workflow="improv-worker-v2", workflow_version=2,
        origin_workflow="reposync-merge-push-v2",
    )
    run = _lines(block, "workflow: ")
    assert len(run) == 1, block
    assert "NEW improv-worker-v2 run (cflow v2)" in run[0] and "s270-qf1" in run[0]
    assert "starts at its first task" in run[0]
    theirs = _lines(block, "origin run: ")
    assert len(theirs) == 1, block
    assert "s270's own reposync-merge-push-v2 run stays with s270" in theirs[0]
    assert "a different workflow from yours" in theirs[0]
    assert "does not bind you" in theirs[0]
    first = _lines(block, "first: ")
    assert len(first) == 1, block
    assert "call the flow_status tool" in first[0] and "flow_report" in first[0]
    assert "before reading for the task or editing a file" in first[0]
    assert "run's first task" in first[0] and "outside the run" in first[0]
    # the v1 tool name is the one s270-qf1 was given, and it is not a v2 tool
    assert "cflow 'status'" not in block
    assert block.strip().endswith("---")


def test_the_marker_names_v1_tools_and_a_same_workflow_origin_run():
    block = handoff_mod.compose_marker(
        origin="a", fork="a-qf1", marker="qf-1", forked_at="T",
        workflow="improv-worker", workflow_version=1, origin_workflow="improv-worker",
    )
    first = _lines(block, "first: ")[0]
    assert "the cflow 'status' tool" in first and "'next'" in first
    assert "flow_status" not in block
    theirs = _lines(block, "origin run: ")[0]
    assert "the same workflow, but a separate run" in theirs


def test_the_marker_without_a_version_or_origin_run_still_points_at_both_tools():
    block = handoff_mod.compose_marker(
        origin="a", fork="a-qf1", marker="qf-1", forked_at="T", workflow="w",
    )
    first = _lines(block, "first: ")[0]
    assert "flow_status" in first and "cflow 'status'" in first
    theirs = _lines(block, "origin run: ")[0]
    assert "a drove no run of this workflow" in theirs


def test_quick_fork_run_reads_the_version_and_resolves_an_inherited_name(monkeypatch):
    from claude_launcher.daemon import api as api_mod

    assert api_mod._quick_fork_run("-", 0, parent="a", cwd="/x") == ("", 0)
    assert api_mod._quick_fork_run("w", 2, parent="a", cwd="/x") == ("w", 2)

    monkeypatch.setattr(api_mod.cflow2_web, "choose", lambda *a, **k: ("w", object()))
    assert api_mod._quick_fork_run("w", 0, parent="a", cwd="/x") == ("w", 2)
    monkeypatch.setattr(api_mod.cflow2_web, "choose", lambda *a, **k: None)
    assert api_mod._quick_fork_run("w", 0, parent="a", cwd="/x") == ("w", 1)

    def broken(*a, **k):
        raise ValueError("no such workflow")

    monkeypatch.setattr(api_mod.cflow2_web, "choose", broken)
    assert api_mod._quick_fork_run("w", 0, parent="a", cwd="/x") == ("w", 0)

    seen = {}

    def paired(**kw):
        seen.update(kw)
        return "improv-worker-v2", 2

    monkeypatch.setattr(api_mod.onboard, "_paired", paired)
    assert api_mod._quick_fork_run(".", 0, parent="a", cwd="/x") == ("improv-worker-v2", 2)
    # the origin is the copy's parent, so its run is the one paired against
    assert seen == {"parent": "a", "parent_cwd": "/x", "cwd": "/x"}


def test_origin_run_name_reads_the_origins_v2_run_then_its_v1_run(monkeypatch):
    from claude_launcher.daemon import api as api_mod

    assert api_mod._origin_run_name("a", "") == ""
    monkeypatch.setattr(
        api_mod.cflow2_web, "live_run",
        lambda cwd, scope: {"workflow": "reposync-merge-push-v2"} if scope == "a" else None,
    )
    assert api_mod._origin_run_name("a", "/x") == "reposync-merge-push-v2"

    monkeypatch.setattr(
        api_mod.cflow_engine, "status",
        lambda cwd, scope=None: {"status": "step", "workflow": "improv-worker"},
    )
    assert api_mod._origin_run_name("b", "/x") == "improv-worker"
    monkeypatch.setattr(
        api_mod.cflow_engine, "status", lambda cwd, scope=None: {"status": "idle"},
    )
    assert api_mod._origin_run_name("b", "/x") == ""
