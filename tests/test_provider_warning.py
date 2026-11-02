"""The provider warning's per-session "ignore", and the pair that decides it.

A session running the claude harness on a provider that is not Anthropic's own
pays Claude for the permission classifier its auto mode still calls, so the
screen offers two answers at the top of that session: restart it on yolo mode,
or stop showing the warning. The second is not a browser preference -- it is
recorded on the session's definition (``SessionDef.provider_warning_ignored``),
so it survives a reload, a daemon restart and a respawn, and every record
written before the field existed keeps showing the warning. That default is
what makes the rule retroactive: no write reaches the sessions already on the
machine.

Which sessions match is decided from two facts the session row publishes
(``provider``/``provider_service``): a browser cannot open the config that
resolves the provider, and the provider's *name* does not say whether it is
Anthropic's own (the builtin ``default`` is Anthropic under another name).
"""

from __future__ import annotations

import asyncio
import dataclasses
import sys
import time

from claude_launcher import profile as profile_mod
from claude_launcher import providers, store
from claude_launcher.daemon import harness as harness_mod
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


def _manager() -> SessionManager:
    return SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)


def _fake_claude_build_command(sdef, *, restoring=False, opening=""):
    """Stand in for the claude launch: a real child process, no real claude."""
    return [sys.executable, "-u", "-c", CHILD], {"CLAUNCH_SESSION": sdef.name}, sdef.cwd


async def _serve(mgr, tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    mm = MeshManager(mgr, root=tmp_path / "mesh")
    app = build_app(mgr, "sekrit", started_at=time.monotonic(), mesh=mm)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


def _declare_other_provider() -> None:
    """A provider that is not Anthropic's own service, declared in the config."""
    doc = store.load()
    doc.setdefault("providers", {})["fw"] = {
        "endpoints": {"anthropic": "https://fw.invalid"},
    }
    store.save(doc)


# --------------------------------------------------------------------------- #
# the field itself: absent unless set, and carried by the record
# --------------------------------------------------------------------------- #
def test_the_ignore_is_written_only_when_it_is_set(home):
    """A session that never dismissed the warning writes no key.

    The common record on disk stays what it was, and — the point of the
    default — a record that predates the field reads as "not dismissed", which
    is what makes the warning reach the sessions that already existed.
    """
    plain = SessionDef(name="s1")
    assert plain.provider_warning_ignored is False
    assert "provider_warning_ignored" not in plain.to_dict()
    assert SessionDef.from_dict(plain.to_dict()).provider_warning_ignored is False

    dismissed = dataclasses.replace(plain, provider_warning_ignored=True)
    assert dismissed.to_dict()["provider_warning_ignored"] is True
    assert SessionDef.from_dict(dismissed.to_dict()).provider_warning_ignored is True


# --------------------------------------------------------------------------- #
# persistence: the answer outlives the daemon that recorded it
# --------------------------------------------------------------------------- #
def test_the_ignore_survives_a_daemon_restart(home, tmp_path, monkeypatch):
    """The dismissal is on the definition, so a restart restores it.

    Both answers count: a cleared dismissal must stay cleared, or the warning
    would come back on the next boot for a session whose person asked for it.
    """
    profile_mod.create("p1")
    monkeypatch.setattr(harness_mod, "build_command", _fake_claude_build_command)

    async def run():
        mgr = _manager()
        mgr.create(SessionDef(name="s1", harness="claude", profile="p1", cwd=str(tmp_path)))
        dismissed = mgr.set_provider_warning_ignored("s1", True)
        assert dismissed.sdef.provider_warning_ignored is True
        await mgr.shutdown_all()

        # A fresh manager reads the persisted definitions off disk, the way a
        # restarted daemon does.
        restarted = _manager()
        assert not restarted.restore_all()
        assert restarted.get("s1").sdef.provider_warning_ignored is True

        restarted.set_provider_warning_ignored("s1", False)
        await restarted.shutdown_all()

        again = _manager()
        assert not again.restore_all()
        assert again.get("s1").sdef.provider_warning_ignored is False

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# the route
# --------------------------------------------------------------------------- #
def test_api_records_and_clears_the_ignore(home, tmp_path, monkeypatch):
    profile_mod.create("p1")
    monkeypatch.setattr(harness_mod, "build_command", _fake_claude_build_command)

    async def run():
        mgr = _manager()
        client = await _serve(mgr, tmp_path)
        try:
            mgr.create(SessionDef(name="s1", harness="claude", profile="p1", cwd=str(tmp_path)))
            resp = await client.post(
                "/api/sessions/s1/provider-warning",
                json={"ignore": True},
                headers=BEARER,
            )
            assert resp.status == 200
            assert (await resp.json())["provider_warning_ignored"] is True
            assert mgr.get("s1").sdef.provider_warning_ignored is True
            assert not mgr.get("s1").exited   # a record, not a restart

            # pressing it again is the same answer, not a second record
            resp = await client.post(
                "/api/sessions/s1/provider-warning",
                json={"ignore": True},
                headers=BEARER,
            )
            assert resp.status == 200

            for body in ({"ignore": "yes"}, {}, {"ignore": None}):
                resp = await client.post(
                    "/api/sessions/s1/provider-warning", json=body, headers=BEARER
                )
                assert resp.status == 400
            assert mgr.get("s1").sdef.provider_warning_ignored is True

            resp = await client.post(
                "/api/sessions/s1/provider-warning",
                json={"ignore": False},
                headers=BEARER,
            )
            assert resp.status == 200
            assert (await resp.json())["provider_warning_ignored"] is False
            assert mgr.get("s1").sdef.provider_warning_ignored is False

            resp = await client.post(
                "/api/sessions/nope/provider-warning",
                json={"ignore": True},
                headers=BEARER,
            )
            assert resp.status == 400   # no such session
        finally:
            await client.close()
            await mgr.shutdown_all()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# the pair the screen decides on
# --------------------------------------------------------------------------- #
def test_the_session_row_publishes_the_provider_and_its_service(home, tmp_path, monkeypatch):
    """The row answers "is this Claude's own provider" without the browser
    reading the config: Claude's own profiles say ``anthropic``, a custom
    provider says its own service, and the record's dismissal rides along."""
    _declare_other_provider()
    profile_mod.create("own")
    profile_mod.create("other")
    store.set_profile_field("own", "provider", "claude")
    store.set_profile_field("other", "provider", "fw")
    monkeypatch.setattr(harness_mod, "build_command", _fake_claude_build_command)

    async def run():
        mgr = _manager()
        client = await _serve(mgr, tmp_path)
        try:
            mgr.create(SessionDef(name="own", harness="claude", profile="own:claude",
                                  cwd=str(tmp_path)))
            mgr.create(SessionDef(name="other", harness="claude", profile="other:claude",
                                  cwd=str(tmp_path)))
            mgr.set_provider_warning_ignored("other", True)
            resp = await client.get("/api/sessions", headers=BEARER)
            assert resp.status == 200
            rows = {row["name"]: row for row in (await resp.json())["sessions"]}
            assert (rows["own"]["provider"], rows["own"]["provider_service"]) == (
                "claude", providers.ANTHROPIC_SERVICE)
            assert (rows["other"]["provider"], rows["other"]["provider_service"]) == (
                "fw", providers.CUSTOM_SERVICE)
            # The dismissal is on the record the row comes from, so the screen
            # and the route can never disagree about it.
            assert rows["other"]["provider_warning_ignored"] is True
            assert "provider_warning_ignored" not in rows["own"]
        finally:
            await client.close()
            await mgr.shutdown_all()

    asyncio.run(run())


def test_the_rail_poll_carries_the_warning_fields(home, tmp_path, monkeypatch):
    """The dashboard's only session poll is ``/api/sessions?view=rail``, and
    that view answers with a whitelist (``RAIL_FIELDS``).

    A field the whitelist does not name never reaches the page. The strip was
    therefore silent on every live session -- the fill from ``?view=rail``
    dropped the three keys -- while this module stayed green, because the
    other tests read the full list and the web check feeds stub rows
    (claunch-1ztit.1). So this one asserts the rail answer itself.
    """
    _declare_other_provider()
    profile_mod.create("other")
    store.set_profile_field("other", "provider", "fw")
    monkeypatch.setattr(harness_mod, "build_command", _fake_claude_build_command)

    async def run():
        mgr = _manager()
        client = await _serve(mgr, tmp_path)
        try:
            mgr.create(SessionDef(name="s1", harness="claude", profile="other:claude",
                                  cwd=str(tmp_path)))
            mgr.set_provider_warning_ignored("s1", True)
            resp = await client.get("/api/sessions?view=rail", headers=BEARER)
            assert resp.status == 200
            rows = {row["name"]: row for row in (await resp.json())["sessions"]}
            row = rows["s1"]
            # The three the strip decides on, as the rail really sends them.
            assert row["provider"] == "fw"
            assert row["provider_service"] == providers.CUSTOM_SERVICE
            assert row["provider_warning_ignored"] is True
        finally:
            await client.close()
            await mgr.shutdown_all()

    asyncio.run(run())


def test_a_borrowed_profile_decides_the_provider(home, tmp_path, monkeypatch):
    """``--borrow`` runs on the lender's auth, so the lender's provider is the
    one the classifier's calls belong to; the session's own profile is not."""
    _declare_other_provider()
    profile_mod.create("own")
    profile_mod.create("lender")
    store.set_profile_field("own", "provider", "claude")
    store.set_profile_field("lender", "provider", "fw")
    monkeypatch.setattr(harness_mod, "build_command", _fake_claude_build_command)

    async def run():
        mgr = _manager()
        client = await _serve(mgr, tmp_path)
        try:
            mgr.create(SessionDef(name="s1", harness="claude", profile="own:claude",
                                  borrow="lender", cwd=str(tmp_path)))
            resp = await client.get("/api/sessions", headers=BEARER)
            rows = {row["name"]: row for row in (await resp.json())["sessions"]}
            assert (rows["s1"]["provider"], rows["s1"]["provider_service"]) == (
                "fw", providers.CUSTOM_SERVICE)
        finally:
            await client.close()
            await mgr.shutdown_all()

    asyncio.run(run())
