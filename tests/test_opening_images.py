"""Images attached to a new session's opening task (claunch-jmrmc).

The create form uploads each image to ``/api/opening-images`` and names the
returned ids in the create request's ``opening_images``. Pinned here: the
upload stages a file under the daemon (never in a working directory), the
create request moves the named files into the new session's state directory,
a harness with no ``image_paste_keys`` is refused before anything is built,
an unknown id is refused, and the images reach the composer -- clipboard,
then the harness's key -- before the opening text is pasted and submitted,
which is why such an opening is kept off the command line.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

from claude_launcher import lineage, profile, store
from claude_launcher.daemon import api as api_mod
from claude_launcher.daemon import harness as harness_mod
from claude_launcher.daemon import opening_images, paths
from claude_launcher.daemon import session as session_mod
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.manager import SessionManager

BEARER = {"Authorization": "Bearer sekrit"}
CHILD = (
    "import sys\n"
    "print('READY')\n"
    "for line in sys.stdin:\n"
    "    print('echo:' + line.strip())\n"
)
PNG = bytes.fromhex("89504e470d0a1a0a") + b"a pretend png body"


@pytest.fixture(autouse=True)
def _no_settle(monkeypatch):
    monkeypatch.setitem(api_mod._paste_settle, "until", 0.0)
    monkeypatch.setattr(api_mod, "PASTE_SETTLE_SECONDS", 0.0)


def _register(image_paste_keys=None):
    body = {"command": [sys.executable, "-u", "-c", CHILD]}
    if image_paste_keys is not None:
        body["image_paste_keys"] = list(image_paste_keys)
    store.update(lambda doc: doc.update({"harnesses": {"py": body}}))
    if not profile.resolve("py").exists():
        lineage.set_harness(profile.create("py"), "py")


def _run(body, image_paste_keys=None):
    from aiohttp.test_utils import TestClient, TestServer

    _register(image_paste_keys)

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200,
                             restore_default=True)
        app = build_app(mgr, "sekrit", started_at=time.monotonic())
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            await body(client, mgr)
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


async def _upload(client, data=PNG, kind="image/png"):
    return await client.post("/api/opening-images", data=data,
                             headers={**BEARER, "Content-Type": kind})


def test_an_upload_is_staged_under_the_daemon_and_answered_with_an_id(home, tmp_path):
    async def body(client, mgr):
        resp = await _upload(client)
        assert resp.status == 201, await resp.text()
        doc = await resp.json()
        assert doc["bytes"] == len(PNG) and doc["type"] == "image/png"
        staged = opening_images.stage_dir() / doc["id"]
        assert staged.read_bytes() == PNG
        assert staged.suffix == ".png"

        jpg = await (await _upload(client, kind="image/jpeg")).json()
        assert jpg["id"].endswith(".jpg") and jpg["id"] != doc["id"]

        for kind in ("text/plain", ""):
            bad = await client.post(
                "/api/opening-images", data=PNG,
                headers={**BEARER, "Content-Type": kind} if kind else BEARER)
            assert bad.status == 415
        empty = await _upload(client, data=b"")
        assert empty.status == 400

    _run(body, ["M-v"])


def test_an_upload_over_the_ceiling_is_refused(home, monkeypatch):
    monkeypatch.setattr(api_mod, "PASTE_IMAGE_MAX_BYTES", 1024)

    async def body(client, mgr):
        resp = await _upload(client, data=b"x" * 4096)
        assert resp.status == 413
        folder = opening_images.stage_dir()
        assert not folder.exists() or not list(folder.iterdir())

    _run(body, ["M-v"])


def test_a_stale_staged_image_is_swept_by_the_next_upload(home, monkeypatch):
    first = opening_images.stage([PNG], "image/png")
    monkeypatch.setattr(opening_images, "STAGE_TTL_SECONDS", -1)
    second = opening_images.stage([PNG], "image/png")
    names = {p.name for p in opening_images.stage_dir().iterdir()}
    assert first not in names and second in names


def test_a_harness_without_an_image_key_is_refused_and_the_image_kept(home, tmp_path):
    """The images could be stored but never handed over, so the request is
    refused before a session exists -- and the staged file survives, so the
    form can be pointed at another profile and submitted again."""

    async def body(client, mgr):
        image_id = (await (await _upload(client)).json())["id"]
        resp = await client.post("/api/sessions", headers=BEARER, json={
            "name": "o1", "profile": "py", "cwd": str(tmp_path),
            "task": "look", "beads": False, "opening_images": [image_id],
        })
        doc = await resp.json()
        assert resp.status == 400, doc
        assert "image_paste_keys" in doc["error"]
        assert "o1" not in {s.sdef.name for s in mgr.list()}
        assert (opening_images.stage_dir() / image_id).is_file()

    _run(body)


def test_an_id_the_daemon_never_staged_is_refused(home, tmp_path):
    async def body(client, mgr):
        for ids in (["0" * 20 + ".png"], ["../../etc/passwd"], "abc",
                    ["0" * 20 + ".png"] * (opening_images.MAX_IMAGES + 1)):
            resp = await client.post("/api/sessions", headers=BEARER, json={
                "name": "o2", "profile": "py", "cwd": str(tmp_path),
                "task": "look", "beads": False, "opening_images": ids,
            })
            assert resp.status == 400, (ids, await resp.text())
        assert "o2" not in {s.sdef.name for s in mgr.list()}

    _run(body, ["M-v"])


def test_images_are_moved_to_the_session_and_pasted_ahead_of_the_text(
    home, tmp_path, monkeypatch
):
    """The order is the feature: every image goes onto the clipboard and its
    key is sent, then the text is pasted and submitted, so the harness takes
    them as one message. Hence no opening on the command line."""
    events = []

    async def fake_put(path, media_type, **kw):
        events.append(("clipboard", Path(path).name, media_type))

    async def fake_send(self, keys, **kw):
        events.append(("keys", list(keys)))
        return b""

    real_paste = session_mod.Session.paste

    async def spy_paste(self, text, **kw):
        events.append(("paste", text))
        return await real_paste(self, text, **kw)

    openings = {}
    real_build = harness_mod.build_command

    def spy_build(sdef, **kw):
        openings[sdef.name] = kw.get("opening", "")
        return real_build(sdef, **kw)

    monkeypatch.setattr(api_mod.clipboard, "put_image", fake_put)
    monkeypatch.setattr(session_mod.Session, "send_keys", fake_send)
    monkeypatch.setattr(session_mod.Session, "paste", spy_paste)
    monkeypatch.setattr(harness_mod, "build_command", spy_build)

    async def body(client, mgr):
        a = (await (await _upload(client)).json())["id"]
        b = (await (await _upload(client, kind="image/jpeg")).json())["id"]
        resp = await client.post("/api/sessions", headers=BEARER, json={
            "name": "o3", "profile": "py", "cwd": str(tmp_path),
            "task": "describe both pictures", "beads": False,
            "opening_images": [a, b],
        })
        doc = await resp.json()
        assert resp.status == 201, doc

        kept = [Path(p) for p in doc["opening_images"]]
        assert [p.parent for p in kept] == [opening_images.session_dir("o3")] * 2
        assert kept[0].read_bytes() == PNG
        assert not (opening_images.stage_dir() / a).exists()
        assert openings["o3"] == ""

        deadline = time.monotonic() + 15
        while not any(e[0] == "paste" for e in events):
            assert time.monotonic() < deadline, events
            await asyncio.sleep(0.1)

        assert events[0] == ("clipboard", kept[0].name, "image/png")
        assert events[1] == ("keys", ["M-v"])
        assert events[2] == ("clipboard", kept[1].name, "image/jpeg")
        assert events[3] == ("keys", ["M-v"])
        assert events[4][0] == "paste"
        assert "describe both pictures" in events[4][1]

    _run(body, ["M-v"])


def test_an_image_that_could_not_be_pasted_is_named_in_the_opening(
    home, tmp_path, monkeypatch
):
    pasted = []

    async def fake_put(path, media_type, **kw):
        raise api_mod.clipboard.ClipboardError("no clipboard tool")

    real_paste = session_mod.Session.paste

    async def spy_paste(self, text, **kw):
        pasted.append(text)
        return await real_paste(self, text, **kw)

    monkeypatch.setattr(api_mod.clipboard, "put_image", fake_put)
    monkeypatch.setattr(session_mod.Session, "paste", spy_paste)

    async def body(client, mgr):
        image_id = (await (await _upload(client)).json())["id"]
        resp = await client.post("/api/sessions", headers=BEARER, json={
            "name": "o4", "profile": "py", "cwd": str(tmp_path),
            "beads": False, "opening_images": [image_id],
        })
        doc = await resp.json()
        assert resp.status == 201, doc
        deadline = time.monotonic() + 15
        while not pasted:
            assert time.monotonic() < deadline
            await asyncio.sleep(0.1)
        # No task text: the message still says images were attached, and
        # names the file that did not make it with the reason.
        assert "1 image(s) attached" in pasted[0]
        assert doc["opening_images"][0] in pasted[0]
        assert "no clipboard tool" in pasted[0]

    _run(body, ["M-v"])
