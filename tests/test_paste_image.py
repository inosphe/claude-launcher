"""A clipboard image pasted into the web session line becomes a file path.

The program in the PTY reads bytes, so nothing here can hand it an
attachment. What it can be handed is a path: the browser uploads the image,
the daemon writes it beside the session's own state, and the path it answers
with is what the operator's line carries. Claude Code opens an image path
given in a prompt, which is what makes the round trip worth taking.

The rules this pins are the ones that decide whether the path is safe to use:
the file is named for the type it actually claims, it lands outside the
session's working directory (a repository is not a place to drop a
screenshot), a body too big is refused rather than written, and a type the
route does not know is refused rather than saved under a misleading name.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

from claude_launcher import store
from claude_launcher.daemon import api as api_mod
from claude_launcher.daemon import paths
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager

BEARER = {"Authorization": "Bearer sekrit"}

CHILD = (
    "import sys\n"
    "print('READY')\n"
    "for line in sys.stdin:\n"
    "    print('echo:' + line.strip())\n"
)

PNG = bytes.fromhex("89504e470d0a1a0a") + b"a pretend png body"


def _harness():
    store.update(
        lambda doc: doc.update(
            {"harnesses": {"py": {"command": [sys.executable, "-u", "-c", CHILD]}}}
        )
    )


def _run(tmp_path, body):
    """Boot a daemon with one session and hand it to ``body(client)``."""
    from aiohttp.test_utils import TestClient, TestServer

    _harness()

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
