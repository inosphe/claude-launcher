"""Switching a session's launcher key from the web UI (claunch-wllke.1).

A profile that holds a key pool picks one key per launch. This is the other
direction: a *session* pinned to one key of that pool, from the session rail,
with the harness restarted on it. Three pieces are held here —

* the pin itself (:attr:`SessionDef.key_id`), which must survive a daemon
  restart like the rest of the definition;
* the launch (:func:`daemon.harness.build_command`), which must write the
  pinned key on every launch — a restore included — and must *not* advance the
  rotation, so pinning one session does not steal another session's turn;
* the two routes: the masked key list the modal draws, and the pin-and-restart.

Nothing here starts a real ``claude``: the harness is a python child, the same
stand-in ``tests/test_reborrow.py`` uses for the restart skeleton.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

import pytest

from claude_launcher import credentials, profile as profile_mod, profile_keys
from claude_launcher.daemon import harness as harness_mod
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import ManagerError, SessionManager
from claude_launcher.daemon.mesh import MeshManager

CHILD = (
    "import sys\n"
    "print('READY')\n"
    "for line in sys.stdin:\n"
    "    print('echo:' + line.strip())\n"
)

TOKENS = ("sk-AAAA1111BBBB", "sk-CCCC2222DDDD")


def _register_py_harness() -> None:
    from claude_launcher import store

    store.update(
        lambda doc: doc.update(
            {"harnesses": {"py": {"command": [sys.executable, "-u", "-c", CHILD]}}}
        )
    )


def _pooled_profile(name: str = "p1"):
    """A profile holding a two-key pool; returns ``(profile, [key ids])``."""
    prof = profile_mod.create(name)
    credentials.save_token(prof, TOKENS[0])
    profile_keys.add(prof, TOKENS[1], label="second")
    pool = profile_keys.load(prof)
    return prof, [key.id for key in pool.keys]


def _manager() -> SessionManager:
    return SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)


def _fake_build_command(sdef, *, restoring=False, opening=""):
    return [sys.executable, "-u", "-c", CHILD], {"CLAUNCH_SESSION": sdef.name}, sdef.cwd


async def _serve(mgr, tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    mm = MeshManager(mgr, root=tmp_path / "mesh")
    app = build_app(mgr, "sekrit", started_at=time.monotonic(), mesh=mm)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


# --------------------------------------------------------------------------- #
# the launch: a pinned key is written, and the rotation is not advanced
# --------------------------------------------------------------------------- #
def test_a_pinned_launch_writes_that_key(home, tmp_path):
    prof, ids = _pooled_profile()
    _register_py_harness()

    selection = profile_keys.materialize(prof, ids[0])

    assert selection is not None
    assert selection.key.id == ids[0]
    assert credentials.stored_token(prof) == TOKENS[0]


def test_a_pinned_launch_does_not_advance_the_rotation(home):
    prof, ids = _pooled_profile()
    before = profile_keys.load(prof)
    assert before.cursor == ids[1]  # `add` left the cursor on the new key

    profile_keys.materialize(prof, ids[0])

    after = profile_keys.load(prof)
    # The cursor is the *strategy's* place in the list, not "what ran last":
    # pinning must not move it, or unpinning would resume from the pinned key
    # instead of from where the rotation really stood.
    assert after.cursor == before.cursor


def test_a_pinned_launch_counts_as_a_use(home):
    prof, ids = _pooled_profile()

    profile_keys.materialize(prof, ids[0])

    pool = profile_keys.load(prof)
    used = next(key for key in pool.keys if key.id == ids[0])
    assert used.use_count == 1
    assert used.last_used_at


def test_materialize_reports_a_key_the_pool_no_longer_holds(home):
    prof, ids = _pooled_profile()
    profile_keys.remove(prof, ids[0])

    assert profile_keys.materialize(prof, ids[0]) is None


def test_materialize_is_inert_without_a_pool(home):
    plain = profile_mod.create("plain")
    credentials.save_token(plain, TOKENS[0])

    assert profile_keys.materialize(plain, "k-whatever") is None
    assert credentials.stored_token(plain) == TOKENS[0]


def test_build_command_materializes_the_pinned_key_on_a_restore(home, tmp_path):
    """The point of the pin: another launch's rotation cannot move it."""
    prof, ids = _pooled_profile()
    _register_py_harness()

    # Somebody else's launch rotated the shared token file in between: the
    # pool's cursor sat on the second key, so rotation moved it to the first.
    profile_keys.select_for_launch(prof)
    assert credentials.stored_token(prof) == TOKENS[0]

    sdef = SessionDef(
        name="pinned", harness="py", profile=prof.selector, cwd=str(tmp_path),
        key_id=ids[1],
    )
    harness_mod.build_command(sdef, restoring=True)

    assert credentials.stored_token(prof) == TOKENS[1]


def test_build_command_leaves_a_stale_pin_to_the_strategy(home, tmp_path):
    """A key removed while a session was pinned to it must not kill the launch."""
    prof, ids = _pooled_profile()
    _register_py_harness()
    profile_keys.remove(prof, ids[0])

    sdef = SessionDef(
        name="stale", harness="py", profile=prof.selector, cwd=str(tmp_path),
        key_id=ids[0],
    )
    harness_mod.build_command(sdef)  # a new launch: falls back, does not raise

    assert credentials.stored_token(prof) in TOKENS


# --------------------------------------------------------------------------- #
# the definition
# --------------------------------------------------------------------------- #
def test_the_pin_round_trips_through_the_record(home):
    sdef = SessionDef(name="s", key_id="k-abc123")

    assert sdef.to_dict()["key_id"] == "k-abc123"
    assert SessionDef.from_dict(sdef.to_dict()).key_id == "k-abc123"


def test_an_unpinned_session_writes_no_key_field(home):
    """The common record on disk stays byte-for-byte what it was."""
    assert "key_id" not in SessionDef(name="s").to_dict()


# --------------------------------------------------------------------------- #
# the manager's restart
# --------------------------------------------------------------------------- #
def test_set_key_pins_and_relaunches(home, tmp_path, monkeypatch):
    prof, ids = _pooled_profile()
    _register_py_harness()
    monkeypatch.setattr(harness_mod, "build_command", _fake_build_command)

    async def run():
        mgr = _manager()
        session = mgr.create(
            SessionDef(name="s1", harness="py", profile=prof.selector, cwd=str(tmp_path))
        )
        relaunched = await mgr.set_key("s1", ids[0])

        assert relaunched.sdef.key_id == ids[0]
        assert not relaunched.exited
        assert relaunched.sdef.conversation_id == session.sdef.conversation_id
        assert mgr.get("s1") is relaunched
        event = mgr.events.rows(relaunched)[-1]
        assert event["kind"] == "key"
        assert event["details"]["current"] == ids[0]
        assert event["details"]["profile"] == prof.name
        await mgr.shutdown_all()

    asyncio.run(run())


def test_set_key_none_unpins(home, tmp_path, monkeypatch):
    prof, ids = _pooled_profile()
    _register_py_harness()
    monkeypatch.setattr(harness_mod, "build_command", _fake_build_command)

    async def run():
        mgr = _manager()
        mgr.create(
            SessionDef(
                name="s1", harness="py", profile=prof.selector, cwd=str(tmp_path),
                key_id=ids[0],
            )
        )
        relaunched = await mgr.set_key("s1", None)
        assert relaunched.sdef.key_id is None
        await mgr.shutdown_all()

    asyncio.run(run())


def test_set_key_refusals_touch_nothing(home, tmp_path, monkeypatch):
    prof, ids = _pooled_profile()
    plain = profile_mod.create("plain")
    _register_py_harness()
    monkeypatch.setattr(harness_mod, "build_command", _fake_build_command)

    async def run():
        mgr = _manager()
        mgr.create(
            SessionDef(name="s1", harness="py", profile=prof.selector, cwd=str(tmp_path))
        )
        cases = [
            ("s1", "k-nope"),          # unknown key
            ("s1", None),              # nothing to clear
        ]
        for name, key_id in cases:
            with pytest.raises(ManagerError):
                await mgr.set_key(name, key_id)
        assert mgr.get("s1").sdef.key_id is None
        assert not mgr.get("s1").exited

        # No pool in the chain.
        mgr.create(
            SessionDef(name="s2", harness="py", profile=plain.selector, cwd=str(tmp_path))
        )
        with pytest.raises(ManagerError):
            await mgr.set_key("s2", ids[0])

        # A --null session has no token to switch.
        mgr.create(
            SessionDef(
                name="s3", harness="py", profile=prof.selector, cwd=str(tmp_path),
                null_token=True,
            )
        )
        with pytest.raises(ManagerError):
            await mgr.set_key("s3", ids[0])

        # A borrowing session authenticates with the lender's token.
        mgr.create(
            SessionDef(
                name="s4", harness="py", profile=prof.selector, cwd=str(tmp_path),
                borrow=prof.name,
            )
        )
        with pytest.raises(ManagerError):
            await mgr.set_key("s4", ids[0])
        await mgr.shutdown_all()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# the routes
# --------------------------------------------------------------------------- #
def test_the_keys_route_masks_every_token(home):
    prof, ids = _pooled_profile()

    class _Req:
        match_info = {"profile": prof.selector}
        app = {}

    body = json.loads(asyncio.run(_api_profile_keys(_Req())).text)

    assert body["pooled"] is True
    assert body["owner"] == prof.name
    assert [row["id"] for row in body["keys"]] == ids
    text = json.dumps(body)
    for token in TOKENS:
        assert token not in text  # masks only, never the plaintext
    assert body["keys"][1]["label"] == "second"


async def _api_profile_keys(request):
    from claude_launcher.daemon import api

    return await api.h_profile_keys(request)


def test_the_keys_route_of_an_inheriting_child_names_the_owner(home):
    """A child with no token of its own rotates its nearest ancestor's pool."""
    from claude_launcher import lineage

    parent, ids = _pooled_profile("parent")
    child = profile_mod.create("child")
    lineage.set_parent(child, "parent")

    class _Req:
        match_info = {"profile": child.selector}
        app = {}

    body = json.loads(asyncio.run(_api_profile_keys(_Req())).text)

    assert body["owner"] == parent.name
    assert body["profile"] == child.name
    assert [row["id"] for row in body["keys"]] == ids


def test_the_keys_route_answers_an_unpooled_profile(home):
    plain = profile_mod.create("plain")

    class _Req:
        match_info = {"profile": plain.selector}
        app = {}

    body = json.loads(asyncio.run(_api_profile_keys(_Req())).text)

    assert body["pooled"] is False
    assert body["owner"] is None
    assert body["keys"] == []


def test_the_session_key_route_pins(home, tmp_path, monkeypatch):
    prof, ids = _pooled_profile()
    _register_py_harness()
    monkeypatch.setattr(harness_mod, "build_command", _fake_build_command)

    async def run():
        mgr = _manager()
        mgr.create(
            SessionDef(name="s1", harness="py", profile=prof.selector, cwd=str(tmp_path))
        )
        client = await _serve(mgr, tmp_path)
        auth = {"Authorization": "Bearer sekrit"}
        try:
            resp = await client.post(
                "/api/sessions/s1/key", json={"key_id": ids[0]}, headers=auth
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["key_id"] == ids[0]
            assert mgr.get("s1").sdef.key_id == ids[0]
            # 400s the modal can show: an unknown key and a missing field.
            bad = await client.post(
                "/api/sessions/s1/key", json={"key_id": "k-nope"}, headers=auth
            )
            assert bad.status == 400
            missing = await client.post("/api/sessions/s1/key", json={}, headers=auth)
            assert missing.status == 400
            listed = await client.get(f"/api/profiles/{prof.name}/keys", headers=auth)
            assert listed.status == 200
            assert (await listed.json())["owner"] == prof.name
        finally:
            await client.close()
            await mgr.shutdown_all()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# end to end: the process that comes up carries the chosen key in its env
# --------------------------------------------------------------------------- #
def _register_env_recording_harness(marker):
    """A declared api-key harness that records the token it was launched with.

    The file is written by the child, from its own environment, before it
    waits for input -- so what the test reads is the environment the harness
    actually received, not the token file the daemon wrote a line earlier.
    """
    from claude_launcher import store

    script = (
        "import os, sys\n"
        f"with open({str(marker)!r}, 'w', encoding='utf-8') as fh:\n"
        "    fh.write(str(os.getpid()) + ' ' + (os.environ.get('KEYED_API_KEY') or ''))\n"
        "print('READY')\n"
        "for line in sys.stdin:\n"
        "    pass\n"
    )
    store.update(
        lambda doc: doc.update(
            {
                "harnesses": {
                    "keyed": {
                        "command": [sys.executable, "-u", "-c", script],
                        "auth": "api-key",
                        "token_env": "KEYED_API_KEY",
                    }
                }
            }
        )
    )


def _recorded(marker):
    try:
        text = marker.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, ValueError):
        return None
    if not text:
        return None
    pid, _, token = text.partition(" ")
    return int(pid), token


async def _await_record(marker, *, not_pid=None, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = _recorded(marker)
        if found and (not_pid is None or found[0] != not_pid):
            return found
        await asyncio.sleep(0.05)
    raise AssertionError(f"no launch was recorded in {marker} within {timeout}s")


def test_switching_the_key_restarts_the_harness_on_that_key(home, tmp_path):
    """The end-to-end reading claunch-wllke.1 asks for.

    A real child process is launched for the session and writes its own pid
    and the value of its declared ``token_env``. Switching the key must leave
    a *different* process on the *other* key under the same session name, and
    that process's environment is what the assertions read. This is the daemon
    path a ``claunch sessions`` row walks: same name, new harness, the key the
    operator picked.
    """
    from claude_launcher import store

    prof, ids = _pooled_profile()
    marker = tmp_path / "harness.env"
    _register_env_recording_harness(marker)
    # The profile's declared harness is what the daemon launches; the
    # definition alone does not win over it (tests/test_reborrow.py).
    store.set_profile_field(prof.name, "harness", "keyed")

    async def run():
        mgr = _manager()
        try:
            session = mgr.create(
                SessionDef(name="s1", harness="keyed", profile=prof.selector,
                           cwd=str(tmp_path))
            )
            # No pin: the launch takes the pool's strategy, whose cursor
            # `add` left on the second key, so the rotation lands on the first.
            first_pid, first_token = await _await_record(marker)
            assert first_token == TOKENS[0]

            relaunched = await mgr.set_key("s1", ids[1])

            second_pid, second_token = await _await_record(marker, not_pid=first_pid)
            assert second_pid != first_pid, "the harness was relaunched, not edited"
            assert second_token == TOKENS[1], "the new process runs on the chosen key"
            assert credentials.stored_token(prof) == TOKENS[1]
            assert relaunched.sdef.key_id == ids[1]
            assert relaunched.sdef.conversation_id == session.sdef.conversation_id
            assert not relaunched.exited
            assert mgr.get("s1") is relaunched
        finally:
            await mgr.shutdown_all()

    asyncio.run(run())
