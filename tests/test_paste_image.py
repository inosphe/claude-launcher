"""A clipboard image pasted into the web session line reaches the harness.

The program in the PTY reads bytes, so nothing here can hand it an
attachment. What it does accept is an image off the clipboard, so the browser
uploads the image, the daemon writes it beside the session's own state, puts
it on the clipboard of its own machine, and sends the keystroke that harness
reads an image with.

Two groups of rules are pinned here. The storage rules decide whether the
file is safe to hand over at all: it is named for the type it actually
claims, it lands outside the session's working directory (a repository is not
a place to drop a screenshot), a body too big is refused rather than written,
and a type the route does not know is refused rather than saved under a
misleading name. The delivery rules decide what the operator is told: the
keystroke comes from the harness declaration, an undeclared harness is said
to be undeclared instead of being sent a guess, and a clipboard that could
not be filled is reported rather than passed over in silence.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from claude_launcher import store
from claude_launcher.daemon import api as api_mod
from claude_launcher.daemon import paths, session_input
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon import session as session_mod
from claude_launcher.daemon.manager import SessionManager

BEARER = {"Authorization": "Bearer sekrit"}

CHILD = (
    "import sys\n"
    "print('READY')\n"
    "for line in sys.stdin:\n"
    "    print('echo:' + line.strip())\n"
)

PNG = bytes.fromhex("89504e470d0a1a0a") + b"a pretend png body"


@pytest.mark.parametrize("ready,failed", [(True, False), (False, False), (True, True)])
@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_image_path_paste_avoids_busy_clipboard(home, tmp_path, monkeypatch,
                                               ready, failed, harness):
    from unittest.mock import AsyncMock

    target = tmp_path / "image with 공백 #1.png"
    target.write_bytes(PNG)
    session = SimpleNamespace(
        sdef=SessionDef(name="s1", harness=harness),
        screen=SimpleNamespace(bracketed_paste=ready),
        paste=AsyncMock(side_effect=RuntimeError("PTY closed") if failed else None),
        send_keys=AsyncMock(),
    )
    put = AsyncMock(side_effect=api_mod.clipboard.ClipboardError("clipboard held"))
    monkeypatch.setattr(api_mod.clipboard, "put_image", put)
    delivered, keys, reason = asyncio.run(
        api_mod._deliver_pasted_image(session, target, "image/png")
    )
    assert delivered is (ready and not failed)
    assert keys == []
    put.assert_not_called()
    session.send_keys.assert_not_called()
    if ready:
        expected = str(target.resolve()) if harness == "claude" else target.resolve().as_uri()
        session.paste.assert_awaited_once_with(expected, enter=False)
        assert reason == ("the image path did not reach the session: PTY closed" if failed else "")
    else:
        session.paste.assert_not_called()
        assert "not ready" in reason


@pytest.fixture(autouse=True)
def _no_settle_carryover(monkeypatch):
    """Each test starts with the clipboard free: the settle window is module
    state, and one test's last paste must not make the next one wait."""
    monkeypatch.setitem(api_mod._paste_settle, "until", 0.0)
    monkeypatch.setattr(api_mod, "PASTE_SETTLE_SECONDS", 0.0)


def _harness(image_paste_keys=None, image_paste_transport="clipboard"):
    body = {"command": [sys.executable, "-u", "-c", CHILD]}
    body["image_paste_transport"] = image_paste_transport
    if image_paste_keys is not None:
        body["image_paste_keys"] = list(image_paste_keys)
    store.update(lambda doc: doc.update({"harnesses": {"py": body}}))


def _run(tmp_path, body, image_paste_keys=None, image_paste_transport="clipboard"):
    """Boot a daemon with one session and hand it to ``body(client)``."""
    from aiohttp.test_utils import TestClient, TestServer

    _harness(image_paste_keys, image_paste_transport)

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200,
                             restore_default=True)
        app = build_app(mgr, "sekrit", started_at=time.monotonic())
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            mgr.create(SessionDef(name="s1", harness="py", cwd=str(tmp_path)))
            await mgr.get("s1").wait_for("idle", timeout=10.0, threshold=0.5)
            await body(client)
            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_path_transport_upload_sends_one_paste_without_submitting(home, tmp_path,
                                                               monkeypatch, harness):
    from dataclasses import replace

    writes = []

    async def capture_write(self, data, **kwargs):
        writes.append(data)

    async def fail_clipboard(*args, **kwargs):
        raise AssertionError("path transport must not access the clipboard")

    monkeypatch.setattr(session_mod.Session, "write_bytes", capture_write)
    monkeypatch.setattr(api_mod.clipboard, "put_image", fail_clipboard)

    async def body(client):
        session = client.server.app["manager"].get("s1")
        session.sdef = replace(session.sdef, harness=harness)
        session.screen.bracketed_paste = True
        for _ in range(2):
            resp = await client.post("/api/sessions/s1/paste-image", data=PNG,
                                     headers={**BEARER, "Content-Type": "image/png"})
            assert resp.status == 200
            doc = await resp.json()
            assert doc["delivered"] is True
            assert doc["keys"] == []
            target = Path(doc["path"])
            assert target.read_bytes() == PNG
            expected = str(target) if harness == "claude" else target.as_uri()
            assert writes[-1] == b"\x1b[200~" + expected.encode() + b"\x1b[201~"
        assert len(writes) == 2  # No Enter, even for consecutive attachments.
        assert writes[0] != writes[1]

    _run(tmp_path, body, image_paste_transport="path")


def test_a_pasted_png_is_stored_beside_the_session_and_its_path_answered(
    home, tmp_path
):
    async def body(client):
        resp = await client.post("/api/sessions/s1/paste-image", data=PNG,
                                 headers={**BEARER, "Content-Type": "image/png"})
        assert resp.status == 200, await resp.text()
        doc = await resp.json()
        assert doc["bytes"] == len(PNG)

        stored = Path(doc["path"])
        assert stored.read_bytes() == PNG
        assert stored.suffix == ".png"

        # beside the session's state, and nothing is dropped in the directory
        # the session works in (here the test's daemon home happens to sit
        # under that directory, so the claim is about the cwd itself)
        assert stored.parent == paths.session_dir("s1") / "pastes"
        assert not list(Path(tmp_path).glob("*.png"))

        # a second paste is its own file, not an overwrite of the first
        again = await client.post("/api/sessions/s1/paste-image", data=PNG,
                                  headers={**BEARER, "Content-Type": "image/png"})
        assert (await again.json())["path"] != doc["path"]

    _run(tmp_path, body)


def test_the_extension_follows_the_type_the_body_declares(home, tmp_path):
    async def body(client):
        for kind, suffix in (("image/jpeg", ".jpg"), ("image/gif", ".gif"),
                             ("image/webp", ".webp")):
            resp = await client.post("/api/sessions/s1/paste-image", data=PNG,
                                     headers={**BEARER, "Content-Type": kind})
            assert resp.status == 200, await resp.text()
            assert Path((await resp.json())["path"]).suffix == suffix

    _run(tmp_path, body)


def test_a_type_the_route_does_not_know_is_refused_rather_than_saved(
    home, tmp_path
):
    """Saving an unknown type would mean naming the file something it is not,
    and the path is handed to a reader that opens it by that name."""

    async def body(client):
        for kind in ("text/plain", "application/pdf", ""):
            resp = await client.post(
                "/api/sessions/s1/paste-image", data=PNG,
                headers={**BEARER, "Content-Type": kind} if kind else BEARER,
            )
            assert resp.status == 415, (kind, await resp.text())
        assert not (paths.session_dir("s1") / "pastes").exists()

    _run(tmp_path, body)


def test_an_empty_body_is_refused(home, tmp_path):
    async def body(client):
        resp = await client.post("/api/sessions/s1/paste-image", data=b"",
                                 headers={**BEARER, "Content-Type": "image/png"})
        assert resp.status == 400
        assert not (paths.session_dir("s1") / "pastes").exists()

    _run(tmp_path, body)


def test_an_image_over_the_ceiling_is_refused_and_nothing_is_written(
    home, tmp_path, monkeypatch
):
    """The route reads its own payload against its own ceiling, so the cap is
    this route's and the app-wide one is left where it is."""
    monkeypatch.setattr(api_mod, "PASTE_IMAGE_MAX_BYTES", 1024)

    async def body(client):
        resp = await client.post(
            "/api/sessions/s1/paste-image", data=b"x" * 4096,
            headers={**BEARER, "Content-Type": "image/png"},
        )
        assert resp.status == 413
        folder = paths.session_dir("s1") / "pastes"
        assert not folder.exists() or not list(folder.iterdir())

    _run(tmp_path, body)


def test_a_body_larger_than_the_app_wide_cap_still_reaches_this_route(
    home, tmp_path
):
    """aiohttp's default body cap is 1 MiB and a screenshot is bigger than
    that. The route reads ``request.content`` rather than ``request.read()``
    for exactly this, so a real paste is not refused by a limit meant for
    JSON bodies."""
    big = b"\x89PNG\r\n\x1a\n" + b"y" * (2 * 1024 * 1024)

    async def body(client):
        resp = await client.post("/api/sessions/s1/paste-image", data=big,
                                 headers={**BEARER, "Content-Type": "image/png"})
        assert resp.status == 200, await resp.text()
        assert (await resp.json())["bytes"] == len(big)

    _run(tmp_path, body)


# ---- delivery: the clipboard and the keystroke -------------------------


def test_a_declared_harness_gets_the_image_on_the_clipboard_then_its_keystroke(
    home, tmp_path, monkeypatch
):
    """The whole point of the route. The file is stored, the daemon's own
    clipboard is filled with it, and the key that harness reads an image with
    is sent — in that order, because the key is what makes the harness read
    what was just put there."""
    seen = []
    sent = []

    async def fake_put(path, media_type, **kw):
        seen.append((Path(path), media_type))

    async def fake_send(self, keys, **kw):
        sent.append((list(keys), kw))
        return b""

    monkeypatch.setattr(api_mod.clipboard, "put_image", fake_put)
    monkeypatch.setattr(session_mod.Session, "send_keys", fake_send)

    async def body(client):
        resp = await client.post("/api/sessions/s1/paste-image", data=PNG,
                                 headers={**BEARER, "Content-Type": "image/png"})
        assert resp.status == 200, await resp.text()
        doc = await resp.json()
        assert doc["delivered"] is True
        assert doc["keys"] == ["M-v"]
        assert doc["reason"] == ""

        # the file that went on the clipboard is the file that was stored
        assert seen == [(Path(doc["path"]), "image/png")]
        assert Path(doc["path"]).read_bytes() == PNG

        # forced, like every other key the operator presses in the session
        # line: an image paste must not turn into a 30s wait on a busy session
        assert sent == [(["M-v"], {"force": True})]

    _run(tmp_path, body, image_paste_keys=["M-v"])


def test_an_undeclared_harness_is_told_so_and_is_sent_nothing(home, tmp_path,
                                                              monkeypatch):
    """A chord that means "paste an image" to one program means something else
    to another, so a harness with no declaration gets no keystroke at all."""
    sent = []

    async def fake_send(self, keys, **kw):
        sent.append(list(keys))
        return b""

    async def fake_put(path, media_type, **kw):
        raise AssertionError("the clipboard must not be touched for this")

    monkeypatch.setattr(api_mod.clipboard, "put_image", fake_put)
    monkeypatch.setattr(session_mod.Session, "send_keys", fake_send)

    async def body(client):
        resp = await client.post("/api/sessions/s1/paste-image", data=PNG,
                                 headers={**BEARER, "Content-Type": "image/png"})
        assert resp.status == 200, await resp.text()
        doc = await resp.json()
        assert doc["delivered"] is False
        assert doc["keys"] == []
        assert "py" in doc["reason"]
        # stored all the same — the store succeeded and the hand-over did not,
        # and those are two answers, not one
        assert Path(doc["path"]).read_bytes() == PNG
        assert sent == []

    _run(tmp_path, body)


def test_a_clipboard_that_could_not_be_filled_is_reported_and_no_key_is_sent(
    home, tmp_path, monkeypatch
):
    """Sending the key after a failed clipboard write would make the harness
    read whatever was on the clipboard before — somebody else's image, or the
    operator's own copied text."""
    sent = []

    async def fake_put(path, media_type, **kw):
        raise api_mod.clipboard.ClipboardError("xclip is not installed")

    async def fake_send(self, keys, **kw):
        sent.append(list(keys))
        return b""

    monkeypatch.setattr(api_mod.clipboard, "put_image", fake_put)
    monkeypatch.setattr(session_mod.Session, "send_keys", fake_send)

    async def body(client):
        resp = await client.post("/api/sessions/s1/paste-image", data=PNG,
                                 headers={**BEARER, "Content-Type": "image/png"})
        assert resp.status == 200, await resp.text()
        doc = await resp.json()
        assert doc["delivered"] is False
        assert doc["reason"] == "xclip is not installed"
        assert sent == []

    _run(tmp_path, body, image_paste_keys=["M-v"])


def test_a_keystroke_that_does_not_reach_the_session_is_reported(
    home, tmp_path, monkeypatch
):
    async def fake_put(path, media_type, **kw):
        return None

    async def fake_send(self, keys, **kw):
        raise RuntimeError("the session is gone")

    monkeypatch.setattr(api_mod.clipboard, "put_image", fake_put)
    monkeypatch.setattr(session_mod.Session, "send_keys", fake_send)

    async def body(client):
        resp = await client.post("/api/sessions/s1/paste-image", data=PNG,
                                 headers={**BEARER, "Content-Type": "image/png"})
        assert resp.status == 200, await resp.text()
        doc = await resp.json()
        assert doc["delivered"] is False
        assert doc["keys"] == ["M-v"]
        assert "the session is gone" in doc["reason"]

    _run(tmp_path, body, image_paste_keys=["M-v"])


def test_two_pastes_do_not_interleave_their_clipboard_and_their_key(
    home, tmp_path, monkeypatch
):
    """The clipboard is one per machine while sessions are many. Without the
    lock the second write can land between the first write and the first key,
    and then a session is handed an image nobody sent it — the one failure of
    this route that produces a wrong result instead of an error."""
    order = []

    async def fake_put(path, media_type, **kw):
        order.append(f"put:{Path(path).name}")
        await asyncio.sleep(0.05)

    async def fake_send(self, keys, **kw):
        order.append("key")
        return b""

    monkeypatch.setattr(api_mod.clipboard, "put_image", fake_put)
    monkeypatch.setattr(session_mod.Session, "send_keys", fake_send)

    async def body(client):
        async def one():
            return await client.post(
                "/api/sessions/s1/paste-image", data=PNG,
                headers={**BEARER, "Content-Type": "image/png"},
            )

        responses = await asyncio.gather(one(), one())
        for resp in responses:
            assert (await resp.json())["delivered"] is True
        # each put is followed by its own key before the next put starts
        assert [step.split(":")[0] for step in order] == ["put", "key", "put", "key"]

    _run(tmp_path, body, image_paste_keys=["M-v"])


def test_the_key_the_harness_declares_is_the_key_that_is_sent(home, tmp_path,
                                                              monkeypatch):
    """No harness-name switch in the route: whatever the declaration says is
    what goes to the PTY, which is how a harness with a different chord is
    added without touching this code."""
    sent = []

    async def fake_put(path, media_type, **kw):
        return None

    async def fake_send(self, keys, **kw):
        sent.append(list(keys))
        return b""

    monkeypatch.setattr(api_mod.clipboard, "put_image", fake_put)
    monkeypatch.setattr(session_mod.Session, "send_keys", fake_send)

    async def body(client):
        resp = await client.post("/api/sessions/s1/paste-image", data=PNG,
                                 headers={**BEARER, "Content-Type": "image/png"})
        assert (await resp.json())["keys"] == ["C-v", "Enter"]
        assert sent == [["C-v", "Enter"]]

    _run(tmp_path, body, image_paste_keys=["C-v", "Enter"])


# ---- the same upload, wherever the browser is ------------------------------
#
# The browser always sends the image's bytes; the daemon always puts them on
# its own clipboard and sends the key. So the only thing that differs between
# a browser on the daemon's PC and one on another PC behind the relay is the
# transport the bytes ride. The relay (mux-relay, `/t/<name>/`) passes a
# request body through untouched once it carries Content-Length; what is
# pinned here is the daemon's own leg of that tunnel -- the uplink splitting
# the body into STREAM_DATA frames and piping them to this route.


def _fake_delivery(monkeypatch, log):
    async def fake_put(path, media_type, **kw):
        log.append(("put", Path(path).read_bytes(),
                    asyncio.get_running_loop().time()))

    async def fake_send(self, keys, **kw):
        log.append(("key", list(keys), asyncio.get_running_loop().time()))
        return b""

    monkeypatch.setattr(api_mod.clipboard, "put_image", fake_put)
    monkeypatch.setattr(session_mod.Session, "send_keys", fake_send)


def test_an_image_through_the_relay_tunnel_arrives_byte_for_byte_and_is_delivered(
    home, tmp_path, monkeypatch
):
    """A body many relay frames long -- the uplink cuts at MAX_STREAM_DATA --
    is reassembled into the same file and handed over the same way as a
    direct upload."""
    import json
    import os

    from claude_launcher.daemon import relay_wire as w
    from claude_launcher.daemon.relay_uplink import RelayUplink

    log = []
    _fake_delivery(monkeypatch, log)
    image = bytes.fromhex("89504e470d0a1a0a") + os.urandom(300 * 1024)
    assert len(image) > 4 * w.MAX_STREAM_DATA

    class FakeWS:
        def __init__(self):
            self.sent = asyncio.Queue()
            self._dec = w.FrameDecoder()

        async def send_bytes(self, data):
            for _room, payload in self._dec.feed(data):
                m = w.decode_payload(payload)
                if m is not None:
                    await self.sent.put(m)

        async def close(self):
            pass

    async def body(client):
        room = bytes([7] * 16)
        up = RelayUplink(url="ws://relay", token="t", name="pc",
                         local_host="127.0.0.1", local_port=client.server.port)
        up._room = room
        up._ws = FakeWS()
        # the head as mux-relay forwards it: prefix stripped, Connection:
        # close and X-Forwarded-Prefix added, everything else as sent
        head = (
            "POST /api/sessions/s1/paste-image HTTP/1.1\r\n"
            "Host: relay\r\n"
            "Authorization: Bearer sekrit\r\n"
            "Content-Type: image/png\r\n"
            f"Content-Length: {len(image)}\r\n"
            "Connection: close\r\n"
            "X-Forwarded-Prefix: /t/pc\r\n"
            "\r\n"
        ).encode()
        await up._handle(w.stream_open(room, 5)[w.HEADER_LEN:])
        for frame in w.iter_stream_data(room, 5, head + image):
            await up._handle(frame[w.HEADER_LEN:])
        await up._handle(w.stream_eof(room, 5)[w.HEADER_LEN:])

        raw = b""
        while True:
            m = await asyncio.wait_for(up._ws.sent.get(), 15.0)
            if m.sid != 5:
                continue
            if m.kind == w.STREAM_DATA:
                raw += m.data
            elif m.kind in (w.STREAM_EOF, w.STREAM_CLOSE):
                break
        up.stop()
        status_line = raw.split(b"\r\n", 1)[0]
        assert b" 200 " in status_line, raw[:300]
        doc = json.loads(raw.split(b"\r\n\r\n", 1)[1])
        assert doc["delivered"] is True, doc
        assert doc["bytes"] == len(image)
        assert Path(doc["path"]).read_bytes() == image
        assert [entry[0] for entry in log] == ["put", "key"]
        assert log[0][1] == image

    _run(tmp_path, body, image_paste_keys=["M-v"])


def test_back_to_back_images_leave_the_clipboard_to_the_harness_in_between(
    home, tmp_path, monkeypatch
):
    """Several images in a row (the file picker takes many) must not overwrite
    the clipboard before the harness has read the one its key asked for: the
    next write waits out the settle window after the previous key."""
    monkeypatch.setattr(api_mod, "PASTE_SETTLE_SECONDS", 0.4)
    log = []
    _fake_delivery(monkeypatch, log)

    async def body(client):
        for _ in range(3):
            resp = await client.post("/api/sessions/s1/paste-image", data=PNG,
                                     headers={**BEARER, "Content-Type": "image/png"})
            assert (await resp.json())["delivered"] is True

    _run(tmp_path, body, image_paste_keys=["M-v"])
    assert [e[0] for e in log] == ["put", "key"] * 3
    keys = [e[2] for e in log if e[0] == "key"]
    puts = [e[2] for e in log if e[0] == "put"]
    assert puts[1] - keys[0] >= 0.39
    assert puts[2] - keys[1] >= 0.39


def test_a_lone_image_is_not_made_to_wait(home, tmp_path, monkeypatch):
    """The settle window is paid by the paste that follows another one; a
    paste with nothing before it is handed over at once."""
    monkeypatch.setattr(api_mod, "PASTE_SETTLE_SECONDS", 30.0)
    log = []
    _fake_delivery(monkeypatch, log)

    async def body(client):
        started = asyncio.get_running_loop().time()
        resp = await client.post("/api/sessions/s1/paste-image", data=PNG,
                                 headers={**BEARER, "Content-Type": "image/png"})
        assert (await resp.json())["delivered"] is True
        assert asyncio.get_running_loop().time() - started < 10.0

    _run(tmp_path, body, image_paste_keys=["M-v"])


def test_an_undelivered_paste_does_not_hold_the_clipboard(home, tmp_path,
                                                          monkeypatch):
    """Only a key that was actually sent opens the settle window: a paste
    whose clipboard write failed left nothing for a harness to read."""
    monkeypatch.setattr(api_mod, "PASTE_SETTLE_SECONDS", 30.0)

    async def failing_put(path, media_type, **kw):
        raise api_mod.clipboard.ClipboardError("no clipboard here")

    monkeypatch.setattr(api_mod.clipboard, "put_image", failing_put)

    async def body(client):
        for _ in range(2):
            started = asyncio.get_running_loop().time()
            resp = await client.post("/api/sessions/s1/paste-image", data=PNG,
                                     headers={**BEARER, "Content-Type": "image/png"})
            doc = await resp.json()
            assert doc["delivered"] is False
            assert "no clipboard here" in doc["reason"]
            assert asyncio.get_running_loop().time() - started < 10.0

    _run(tmp_path, body, image_paste_keys=["M-v"])


@pytest.mark.parametrize("name, keys", [("claude", []), ("codex", [])])
def test_the_packaged_harnesses_declare_the_key_they_read_an_image_with(
    home, name, keys
):
    """Both packaged image-capable harnesses use a path without a key chord."""
    from claude_launcher import harnesses as harness_registry

    assert harness_registry.registry().get(name).image_paste_keys == keys


# ---- staging: the image waits for the line (claunch-wxmcw) --------------
#
# A paste into the session line used to be handed over at once, so a
# screenshot attached to whatever the session was doing and the words about
# it arrived as a separate action. Now the browser uploads it in staging form
# (`?stage=1`), holds it on the line, and names it in the send; the daemon
# attaches it ahead of the text, in the same request. The journal records it
# with the line so the detail panel can show what was sent.


def test_a_staged_upload_is_stored_and_handed_over_to_nothing(
    home, tmp_path, monkeypatch
):
    delivered = []

    async def spy(*args, **kwargs):
        delivered.append(args)
        return True, [], ""

    monkeypatch.setattr(api_mod, "_deliver_pasted_image", spy)

    async def body(client):
        resp = await client.post(
            "/api/sessions/s1/paste-image?stage=1", data=PNG,
            headers={**BEARER, "Content-Type": "image/png"},
        )
        assert resp.status == 200, await resp.text()
        doc = await resp.json()
        assert doc["staged"] is True
        assert "delivered" not in doc and "keys" not in doc
        assert api_mod.PASTE_IMAGE_NAME.match(doc["name"]), doc["name"]
        assert Path(doc["path"]).name == doc["name"]
        assert Path(doc["path"]).read_bytes() == PNG
        assert Path(doc["path"]).parent == paths.session_dir("s1") / "pastes"
        assert delivered == []

    _run(tmp_path, body, image_paste_transport="path")


def test_the_line_send_attaches_the_staged_images_ahead_of_the_text(
    home, tmp_path, monkeypatch
):
    """One request carries both halves: the picture lands in the composer
    first (no Enter — attaching must not submit), then the text and its
    Enter. The journal row is the line, with the image it carried."""
    events = []

    async def fake_paste(self, text, *, enter=False):
        events.append(("paste", str(text), enter))
        return b""

    async def fake_keys(self, keys, **kwargs):
        events.append(("keys", list(keys)))
        return b""

    monkeypatch.setattr(session_mod.Session, "paste", fake_paste)
    monkeypatch.setattr(session_mod.Session, "send_keys", fake_keys)

    async def body(client):
        session = client.server.app["manager"].get("s1")
        session.screen.bracketed_paste = True
        up = await client.post(
            "/api/sessions/s1/paste-image?stage=1", data=PNG,
            headers={**BEARER, "Content-Type": "image/png"},
        )
        name = (await up.json())["name"]
        events.clear()

        resp = await client.post(
            "/api/sessions/s1/keys", headers=BEARER,
            json={"keys": ["look at this", "Enter"], "force": True,
                  "input_id": "line-1", "images": [name]},
        )
        assert resp.status == 200, await resp.text()
        assert (await resp.json())["images_failed"] == []

        assert events[0][0] == "paste", events
        assert events[0][2] is False, "attaching must not submit the composer"
        assert str(events[0][1]).endswith(name), events[0]
        assert events[1] == ("keys", ["look at this", "Enter"]), events

        row = session_input.requests("s1")[-1]
        assert row["status"] == "sent"
        assert row["text"] == "look at this"
        assert [i["name"] for i in row["images"]] == [name]
        assert row["images"][0]["type"] == "image/png"
        assert row["images"][0]["bytes"] == len(PNG)

    _run(tmp_path, body, image_paste_transport="path")


def test_a_send_naming_an_image_that_is_not_staged_is_refused(home, tmp_path):
    """A name from a request never becomes a path this module opens: an
    outside name, an unknown one and a well-formed one that was never stored
    are all refused before anything is typed."""
    async def body(client):
        for bad in (["../../etc/passwd"],
                    ["not-a-paste.png"],
                    ["20260101-000000-abcdef.png"]):
            resp = await client.post(
                "/api/sessions/s1/keys", headers=BEARER,
                json={"keys": ["x", "Enter"], "input_id": "line-1",
                      "images": bad},
            )
            assert resp.status == 400, (bad, await resp.text())
        # nothing was recorded for a refused send
        resp = await client.get("/api/sessions/s1/input-journal",
                                headers=BEARER)
        assert (await resp.json())["requests"] == []

    _run(tmp_path, body, image_paste_transport="path")


def test_a_stored_paste_image_is_served_to_the_panel_and_nothing_else(
    home, tmp_path
):
    """The journal shows what the line carried with a <img src>, which cannot
    carry a bearer header — it rides the login cookie like every other
    dashboard fetch. Only names this route writes are opened."""
    async def body(client):
        up = await client.post(
            "/api/sessions/s1/paste-image?stage=1", data=PNG,
            headers={**BEARER, "Content-Type": "image/png"},
        )
        name = (await up.json())["name"]
        got = await client.get(f"/api/sessions/s1/pastes/{name}",
                               headers=BEARER)
        assert got.status == 200, await got.text()
        assert got.headers["Content-Type"] == "image/png"
        assert await got.read() == PNG

        for bad in ("nope.png", "..%2F..%2Fsecret.png",
                    "20260101-000000-abcdef.exe", "20260101-000000-ABCDEF.png"):
            resp = await client.get(f"/api/sessions/s1/pastes/{bad}",
                                    headers=BEARER)
            assert resp.status == 404, (bad, resp.status)

    _run(tmp_path, body, image_paste_transport="path")


def test_a_queued_line_hands_its_images_over_before_the_text(
    home, tmp_path, monkeypatch
):
    """A line sent to an ended session keeps its images in the queue, and the
    flush that types it attaches them first — the picture belongs to the line,
    not to the moment it was pasted."""
    from unittest.mock import AsyncMock

    _harness(image_paste_transport="path")
    folder = paths.session_dir("s1") / "pastes"
    folder.mkdir(parents=True, exist_ok=True)
    image = folder / "20260101-000000-abcdef.png"
    image.write_bytes(PNG)

    events = []
    session = SimpleNamespace(
        sdef=SessionDef(name="s1", harness="py"),
        exited=False,
        pid=None,
        screen=SimpleNamespace(bracketed_paste=True),
        prepare_operator_input=AsyncMock(),
        await_input_ready=AsyncMock(),
        paste=AsyncMock(side_effect=lambda text, enter=False:
                        events.append(("paste", str(text), enter))),
        send_keys=AsyncMock(side_effect=lambda keys, **kw:
                            events.append(("keys", list(keys)))),
        capture=lambda *a, **k: [],
    )
    session_input.queue(
        "s1", request_id="q1", text="look at this",
        images=[{"name": image.name, "type": "image/png", "bytes": len(PNG)}],
    )
    asyncio.run(session_input.flush(session))

    assert events[0][0] == "paste", events
    assert events[0][1].endswith(image.name), events[0]
    assert events[0][2] is False
    assert events[1] == ("keys", ["look at this", "Enter"]), events
    row = session_input.requests("s1")[-1]
    assert row["status"] == "sent"
    assert [i["name"] for i in row["images"]] == [image.name]
