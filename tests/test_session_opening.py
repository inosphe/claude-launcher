"""The opening record and the route that hands it over again.

A session's opening is composed once and typed in once. When that delivery
does not land the agent has nothing to work from, and this record is the only
copy of what it should have read: the daemon keeps the composed block beside
the session's output log, the detail panel's Opening task section reads it
back, and "re-inject" types the same text in again
(``GET/POST /api/sessions/<name>/opening``).

Three things are checked here:

* the record is what was handed over -- text, image count, and the fact that
  a later read does not depend on the workspace's packs still existing;
* a session older than the record answers ``recomposed``, built from the
  definition's packs and task, and says so rather than passing a
  reconstruction off as the original;
* the route types the block into the terminal (the py harness echoes it, so
  ``delivered`` is not the only evidence) and refuses an unknown session the
  same way the other session routes do.

Staged sessions for the unit half; the real app for the routes, as in
``test_session_packs.py``, whose harness this borrows.
"""

from __future__ import annotations

import asyncio
import sys
import time

from claude_launcher import lineage, profile, prompt_packs, store
from claude_launcher.daemon import session_opening
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.mesh import MeshManager

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


async def _screen_has(session, needle: str, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await session.screen_synced()
        if needle in "\n".join(session.capture(history=True) + session.capture()):
            return True
        await asyncio.sleep(0.1)
    return False


async def _screen_count(session, needle: str, want: int, timeout: float = 15.0) -> str:
    """The whole screen once ``needle`` appears at least ``want`` times."""
    deadline = time.monotonic() + timeout
    screen = ""
    while time.monotonic() < deadline:
        await session.screen_synced()
        screen = "\n".join(session.capture(history=True) + session.capture())
        if screen.count(needle) >= want:
            return screen
        await asyncio.sleep(0.1)
    return screen


def _sdef(**kw) -> SessionDef:
    return SessionDef(name=kw.pop("name", "w1"), harness="py", cwd=kw.pop("cwd", ""), **kw)


# --------------------------------------------------------------------------- #
# the record
# --------------------------------------------------------------------------- #


def test_the_record_is_the_text_that_was_handed_over(home):
    block = "---\n# pack: beads\n---\n\n# the pack\n\nissuing task"
    assert session_opening.record("w1", block, images=2) is True

    found = session_opening.read("w1")
    assert found["text"] == block
    assert found["images"] == 2 and found["chars"] == len(block)
    assert found["at"]

    view = session_opening.view("w1", _sdef())
    assert view["source"] == session_opening.RECORDED
    assert view["text"] == block


def test_the_record_names_the_files_the_opening_claimed(home):
    rec = {"id": "a" * 20, "name": "frame.utrace", "bytes": 12}
    session_opening.record("w1", "look\n[claunch attachment] ...", files=[rec])
    assert session_opening.read("w1")["files"] == [rec]
    # The file is not on disk here, and the list says so.
    assert session_opening.attachments("w1") == {
        "images": [], "files": [{**rec, "missing": True}]}


def test_a_name_used_again_does_not_inherit_the_old_openings_images(home):
    """A session's state directory outlives it and a person may reuse a
    name; the new opening lists only its own images (s597-rev, @ 133b3281)."""
    from claude_launcher.daemon import opening_images

    png = bytes.fromhex("89504e470d0a1a0a") + b"old"
    old = opening_images.claim([opening_images.stage([png], "image/png")], "w1")[0].path.name
    assert [i["name"] for i in session_opening.attachments("w1")["images"]] == [old]

    # Created again under the same name with no images: nothing listed or served.
    assert opening_images.claim([], "w1") == []
    assert session_opening.attachments("w1")["images"] == []
    assert opening_images.claimed_path("w1", old) is None

    # ...or with images of its own: only those.
    new = opening_images.claim([opening_images.stage([png + b"new"], "image/png")], "w1")[0].path.name
    assert new != old
    assert [i["name"] for i in session_opening.attachments("w1")["images"]] == [new]


def test_the_images_listed_are_the_ones_the_record_names(home):
    """A record that names no image lists none, whatever the folder holds
    (s648-rev, s1236-rev2 @ 133b3281: record images=1 then images=0)."""
    from claude_launcher.daemon import opening_images

    png = bytes.fromhex("89504e470d0a1a0a") + b"old"
    old = opening_images.claim([opening_images.stage([png], "image/png")], "w1")[0].path.name
    session_opening.record("w1", "old", images=1, image_names=[old])
    assert session_opening.image_names("w1") == [old]

    session_opening.record("w1", "new", images=0)
    assert session_opening.read("w1")["image_names"] == []
    assert session_opening.attachments("w1")["images"] == []
    assert session_opening.image_names("w1") == []

    # A name the record keeps but the disk lost is not listed.
    session_opening.record("w1", "gone", images=1, image_names=["01-" + "0" * 20 + ".png"])
    assert session_opening.attachments("w1")["images"] == []


def test_a_record_from_before_the_names_were_kept_reads_the_folder(home):
    import json
    from claude_launcher.daemon import opening_images, paths

    png = bytes.fromhex("89504e470d0a1a0a") + b"x"
    img = opening_images.claim([opening_images.stage([png], "image/png")], "w1")[0].path.name
    (paths.session_dir("w1") / "opening.json").write_text(
        json.dumps({"text": "old", "images": 1}), encoding="utf-8")
    assert session_opening.read("w1")["image_names"] is None
    assert [i["name"] for i in session_opening.attachments("w1")["images"]] == [img]


def test_a_session_without_a_record_lists_no_files(home):
    # Its uploads folder cannot tell an opening's file from a later one.
    assert session_opening.attachments("w1") == {"images": [], "files": []}


def test_an_empty_opening_is_not_recorded(home):
    assert session_opening.record("w1", "   \n") is False
    assert session_opening.read("w1") is None
    assert session_opening.view("w1", _sdef()).get("source") == session_opening.NONE


def test_a_later_read_does_not_need_the_packs_to_still_exist(home):
    block = prompt_packs.opening([{"id": "beads", "text": "the beads pack"}]) + "\n\nwork"
    session_opening.record("w1", block)
    # The definition can change, be re-ticked or lose a pack; the record is
    # what the session was given and does not follow it.
    sdef = _sdef(prompt_packs=(_pack("beads", "a different copy"),), task="another task")
    view = session_opening.view("w1", sdef)
    assert view["source"] == session_opening.RECORDED
    assert view["text"] == block and "a different copy" not in view["text"]


def _pack(pack_id: str, body: str) -> dict:
    return {"id": pack_id, "text": body}


def test_a_session_without_a_record_is_recomposed_and_labelled(home):
    sdef = _sdef(prompt_packs=(_pack("beads", "the beads pack"),), task="do the thing")
    view = session_opening.view("w1", sdef)
    assert view["source"] == session_opening.RECOMPOSED
    assert view["text"].startswith("---\n# claunch prompt pack: beads")
    assert view["text"].endswith("do the thing")
    # packs ahead of the task, as the opening itself ordered them
    assert view["text"].index("the beads pack") < view["text"].index("do the thing")


def test_a_malformed_recorded_pack_does_not_break_the_recomposition(home):
    """A hand-edited record is still a record: one entry with no text must
    not turn the answer into a 500 while somebody is recovering a session."""
    sdef = _sdef(
        prompt_packs=({"id": "beads"}, _pack("beads2", "the good pack")),
        task="do the thing",
    )
    view = session_opening.view("w1", sdef)
    assert view["source"] == session_opening.RECOMPOSED
    assert "the good pack" in view["text"] and "do the thing" in view["text"]


def test_a_session_with_nothing_recorded_has_no_opening(home):
    view = session_opening.view("w1", _sdef())
    assert view["source"] == session_opening.NONE and view["text"] == ""


def test_the_score_goal_leads_a_recomposition(home):
    sdef = _sdef(task="do the thing", score_goal=True)
    view = session_opening.view("w1", sdef)
    assert view["source"] == session_opening.RECOMPOSED
    assert view["text"].startswith("사용자의 요구사항에 따라 목표를")
    assert view["text"].endswith("do the thing")


# --------------------------------------------------------------------------- #
# the routes
# --------------------------------------------------------------------------- #


def test_the_routes_read_and_re_inject_the_recorded_opening(home, tmp_path):
    _register_py_harness()
    mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
    mm = MeshManager(mgr, settle=0.05)

    async def scenario():
        from aiohttp.test_utils import TestClient, TestServer

        app = build_app(mgr, "sekrit", started_at=time.monotonic(), mesh=mm)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.post(
                "/api/sessions", headers=BEARER,
                json={"name": "root", "profile": "py", "cwd": str(tmp_path),
                      "prompt_packs": [], "task": "do the thing"},
            )
            assert resp.status == 201, await resp.text()

            # The create path recorded what it was about to hand over.
            found = session_opening.read("root")
            assert found is not None and "do the thing" in found["text"]

            resp = await client.get("/api/sessions/root/opening", headers=BEARER)
            doc = await resp.json()
            assert resp.status == 200
            assert doc["source"] == "recorded"
            assert doc["text"] == found["text"] and doc["chars"] == len(found["text"])
            # The py harness takes its prompt as a typed paste, not as an
            # argument, so a second hand-over is the same message twice.
            assert doc["transport"] == "typed"

            # The opening with the session was given once. Wait for it to
            # land, then hand it over again through the panel's route.
            assert await _screen_has(mgr.get("root"), "echo:do the thing")
            resp = await client.post("/api/sessions/root/opening", headers=BEARER)
            doc = await resp.json()
            assert resp.status == 200 and doc["ok"] is True
            assert doc["delivered"] is True and doc["source"] == "recorded"
            assert "text" not in doc  # the reply does not repeat the block
            screen = await _screen_count(mgr.get("root"), "echo:do the thing", 2)
            assert screen.count("echo:do the thing") >= 2

            # An unknown session is refused the way every session route does.
            resp = await client.get("/api/sessions/ghost/opening", headers=BEARER)
            assert resp.status == 400
        finally:
            await client.close()
            await mgr.shutdown_all()

    asyncio.run(scenario())


def test_a_session_older_than_the_record_answers_recomposed(home, tmp_path):
    _register_py_harness()
    mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
    mm = MeshManager(mgr, settle=0.05)

    async def stage():
        mgr.stage(SessionDef(
            name="old", harness="py", cwd=str(tmp_path),
            task="dig the trench",
            prompt_packs=({"id": "beads", "text": "the beads pack"},),
        ))

    asyncio.run(stage())

    async def scenario():
        from aiohttp.test_utils import TestClient, TestServer

        app = build_app(mgr, "sekrit", started_at=time.monotonic(), mesh=mm)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.get("/api/sessions/old/opening", headers=BEARER)
            doc = await resp.json()
            assert resp.status == 200 and doc["source"] == "recomposed"
            assert "# claunch prompt pack: beads" in doc["text"]
            assert doc["text"].endswith("dig the trench")
        finally:
            await client.close()

    asyncio.run(scenario())
