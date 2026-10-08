"""The prompt pack source of the session reminder (claunch-pt9ap).

A session's packs are handed in full once, in the opening, beside their text
ids. From then on the reminder names them by id only, on its own switch and
interval, and the agent pulls a text it no longer holds with ``rebrief``.

The copy a pack follows depends on where its recorded text came from: a
workspace copy follows the workspace's file only; a claunch copy follows both
claunch's file and the workspace, and once a workspace copy appears it
follows only that. A change replaces the session's record (so ``rebrief``
serves the new text and calls the old id stale) and the session is told the
new id; a copy that is gone releases the pack from the session.
"""

from __future__ import annotations

import asyncio
import dataclasses

import pytest

from claude_launcher import prompt_packs, workspaces
from claude_launcher.daemon import daemon_prompt, rebrief, session_reminder
from claude_launcher.daemon.harness import SessionDef

CFG = {"prompt_pack_reminder": True, "prompt_pack_reminder_interval": 600.0}


class _Session:
    def __init__(self, sdef: SessionDef) -> None:
        self.exited = False
        self.sdef = sdef
        self.delivered: list = []

    def status(self, threshold=None):
        return "busy"

    async def deliver(self, text: str) -> bool:
        self.delivered.append(text)
        return True

    def reminders_paused(self) -> bool:
        return False


class _Manager:
    def __init__(self, sessions: dict) -> None:
        self._sessions = sessions
        self.persisted = 0

    def get(self, name: str):
        return self._sessions[name]

    def list(self):
        return list(self._sessions.values())

    def live_children(self, name):
        return []

    def persist(self):
        self.persisted += 1


def _pack_file(directory, pack_id, body):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{pack_id}.md").write_text(
        f"---\ntitle: {pack_id}\n---\n{body}\n", encoding="utf-8"
    )


@pytest.fixture
def env(home, tmp_path, monkeypatch):
    """A registered workspace and a stand-in for claunch's shipped packs."""
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    shipped = tmp_path / "shipped"
    shipped.mkdir()
    monkeypatch.setattr(prompt_packs, "PACKAGE_DIR", shipped)
    ws = tmp_path / "ws"
    ws.mkdir()
    workspaces.add(str(ws), "ws")
    return {"ws": ws, "shipped": shipped, "installed": ws / prompt_packs.INSTALL_DIR}


def _session(env, records):
    sdef = SessionDef(name="w1", cwd=str(env["ws"]), prompt_packs=tuple(records))
    sess = _Session(sdef)
    return sess, _Manager({"w1": sess})


def _ws_record(env, pack_id, body):
    _pack_file(env["installed"], pack_id, body)
    pack = prompt_packs.pack_copy(env["ws"], pack_id)
    assert pack.source == prompt_packs.WORKSPACE
    return prompt_packs.records([pack])[0]


def _pkg_record(env, pack_id, body, required_by=None):
    _pack_file(env["shipped"], pack_id, body)
    pack = prompt_packs.pack_copy(env["ws"], pack_id)
    assert pack.source == prompt_packs.PACKAGE
    req = {pack_id: required_by} if required_by else None
    return prompt_packs.records([pack], required=req)[0]


def _tick(service, now):
    asyncio.run(service.tick(now))


# --------------------------------------------------------------------------- #
# policy, section, context
# --------------------------------------------------------------------------- #
def test_the_pack_source_has_its_own_machine_policy():
    assert session_reminder.pack_reminder_policy(CFG) == (True, 600.0)
    assert session_reminder.pack_reminder_policy({
        "prompt_pack_reminder": True, "prompt_pack_reminder_interval": 1,
    })[1] >= session_reminder.cflow_engine.REMINDER_MIN_INTERVAL
    assert session_reminder.pack_reminder_policy(
        {"prompt_pack_reminder": False, "prompt_pack_reminder_interval": 600}
    )[0] is False


def test_the_section_names_ids_and_never_the_text(env):
    rec = _pkg_record(env, "role-worker", "be a producer", required_by=["worker"])
    sess, _ = _session(env, [rec])
    entries = session_reminder.pack_entries(sess.sdef)
    lines = session_reminder.pack_section(entries, [])
    ident = prompt_packs.text_id("be a producer")
    assert lines[0] == (
        f"prompt pack role-worker: text id {ident} (package, required by role worker)"
    )
    assert "rebrief" in lines[-1]
    assert "be a producer" not in "\n".join(lines)


def test_the_context_line_leaves_the_packs_to_their_own_section(env):
    rec = _ws_record(env, "house", "house rules")
    sess, mgr = _session(env, [rec])
    sess.sdef = dataclasses.replace(sess.sdef, task="count the beans")
    lines = session_reminder.context_id_lines("w1", mgr, None)
    assert "(task)" in lines[0]
    assert "prompt pack" not in lines[0]
    assert prompt_packs.text_id("house rules") not in lines[0]


# --------------------------------------------------------------------------- #
# the source on its own clock
# --------------------------------------------------------------------------- #
def test_packs_remind_a_session_with_no_role_and_no_run(env, monkeypatch):
    monkeypatch.setattr(session_reminder.SessionReminderService, "_config",
                        staticmethod(lambda: dict(CFG)))
    rec = _ws_record(env, "house", "house rules")
    sess, mgr = _session(env, [rec])
    service = session_reminder.SessionReminderService(mgr)

    _tick(service, 1000.0)          # first sight arms: the opening had it
    assert sess.delivered == []
    _tick(service, 1601.0)
    assert len(sess.delivered) == 1
    block = sess.delivered[0]
    assert "## Prompt packs" in block
    assert f"text id {prompt_packs.text_id('house rules')}" in block
    assert "house rules" not in block
    assert "## Role" not in block and "## Cflow" not in block
    assert mgr.persisted == 0       # nothing moved, nothing written


def test_switching_the_source_off_stops_the_reminder(env, monkeypatch):
    monkeypatch.setattr(session_reminder.SessionReminderService, "_config",
                        staticmethod(lambda: {**CFG, "prompt_pack_reminder": False}))
    sess, mgr = _session(env, [_ws_record(env, "house", "house rules")])
    service = session_reminder.SessionReminderService(mgr)
    _tick(service, 1000.0)
    _tick(service, 1601.0)
    assert sess.delivered == []


# --------------------------------------------------------------------------- #
# change detection, by origin
# --------------------------------------------------------------------------- #
def test_an_edited_workspace_copy_is_due_now_with_its_new_id(env, monkeypatch):
    monkeypatch.setattr(session_reminder.SessionReminderService, "_config",
                        staticmethod(lambda: dict(CFG)))
    sess, mgr = _session(env, [_ws_record(env, "house", "house rules")])
    service = session_reminder.SessionReminderService(mgr)
    old = prompt_packs.text_id("house rules")
    _tick(service, 1000.0)

    _pack_file(env["installed"], "house", "new house rules")
    new = prompt_packs.text_id("new house rules")
    _tick(service, 1015.0)          # one poll later, not one interval

    assert sess.sdef.prompt_packs[0]["text"] == "new house rules"
    assert mgr.persisted == 1
    block = sess.delivered[0]
    assert f"changed: prompt pack house was edited; its text id is now {new} (was {old})" in block
    assert "new house rules" not in block    # the id only; rebrief serves it
    assert rebrief.recall("w1", new, manager=mgr, mesh_mgr=None)["text"] == "new house rules"
    assert rebrief.recall("w1", old, manager=mgr, mesh_mgr=None)["status"] == "stale_id"

    # Told once: the next delivery is an ordinary id reminder.
    _tick(service, 1700.0)
    assert len(sess.delivered) == 2
    assert "changed:" not in sess.delivered[1]


def test_a_workspace_copy_never_falls_back_to_claunchs(env, monkeypatch):
    monkeypatch.setattr(session_reminder.SessionReminderService, "_config",
                        staticmethod(lambda: dict(CFG)))
    _pack_file(env["shipped"], "house", "claunch's rules")
    sess, mgr = _session(env, [_ws_record(env, "house", "house rules")])
    service = session_reminder.SessionReminderService(mgr)
    _tick(service, 1000.0)

    (env["installed"] / "house.md").unlink()
    _tick(service, 1015.0)

    assert sess.sdef.prompt_packs == ()
    assert "removed: prompt pack house" in sess.delivered[0]
    old = prompt_packs.text_id("house rules")
    assert rebrief.recall("w1", old, manager=mgr, mesh_mgr=None)["status"] == "stale_id"
    # Released: nothing more to remind of.
    _tick(service, 2000.0)
    _tick(service, 3000.0)
    assert len(sess.delivered) == 1
    assert "w1" not in service._packs


def test_a_claunch_copy_follows_claunch_until_the_workspace_installs_one(env, monkeypatch):
    monkeypatch.setattr(session_reminder.SessionReminderService, "_config",
                        staticmethod(lambda: dict(CFG)))
    rec = _pkg_record(env, "role-worker", "v1", required_by=["worker"])
    sess, mgr = _session(env, [rec])
    service = session_reminder.SessionReminderService(mgr)
    _tick(service, 1000.0)

    # claunch's copy moves on (an upgrade): followed.
    _pack_file(env["shipped"], "role-worker", "v2")
    _tick(service, 1015.0)
    got = sess.sdef.prompt_packs[0]
    assert got["text"] == "v2" and got["source"] == prompt_packs.PACKAGE
    assert got["required_by"] == ["worker"]
    assert "changed: prompt pack role-worker" in sess.delivered[-1]

    # The workspace installs its own: the pack switches to it.
    _pack_file(env["installed"], "role-worker", "ours")
    _tick(service, 1030.0)
    assert sess.sdef.prompt_packs[0] == {"id": "role-worker", "text": "ours"}
    assert prompt_packs.text_id("ours") in sess.delivered[-1]

    # From then on claunch's copy is not followed...
    count = len(sess.delivered)
    _pack_file(env["shipped"], "role-worker", "v3")
    _tick(service, 1045.0)
    assert sess.sdef.prompt_packs[0]["text"] == "ours"
    assert len(sess.delivered) == count
    # ...and removing the workspace's copy releases it, no fallback.
    (env["installed"] / "role-worker.md").unlink()
    _tick(service, 1060.0)
    assert sess.sdef.prompt_packs == ()
    assert "removed: prompt pack role-worker" in sess.delivered[-1]


def test_a_claunch_copy_installed_unchanged_switches_origin_silently(env, monkeypatch):
    monkeypatch.setattr(session_reminder.SessionReminderService, "_config",
                        staticmethod(lambda: dict(CFG)))
    sess, mgr = _session(env, [_pkg_record(env, "role-worker", "same text")])
    service = session_reminder.SessionReminderService(mgr)
    _tick(service, 1000.0)

    _pack_file(env["installed"], "role-worker", "same text")
    _tick(service, 1015.0)
    assert sess.sdef.prompt_packs[0] == {"id": "role-worker", "text": "same text"}
    assert mgr.persisted == 1
    assert sess.delivered == []     # same id: nothing the agent does not hold


def test_a_claunch_copy_gone_from_both_places_is_released(env, monkeypatch):
    monkeypatch.setattr(session_reminder.SessionReminderService, "_config",
                        staticmethod(lambda: dict(CFG)))
    sess, mgr = _session(env, [_pkg_record(env, "extra", "extra rules")])
    service = session_reminder.SessionReminderService(mgr)
    _tick(service, 1000.0)
    (env["shipped"] / "extra.md").unlink()
    _tick(service, 1015.0)
    assert sess.sdef.prompt_packs == ()
    assert "removed: prompt pack extra" in sess.delivered[0]


def test_the_daemon_prompt_follows_the_daemon(env, monkeypatch):
    monkeypatch.setattr(session_reminder.SessionReminderService, "_config",
                        staticmethod(lambda: dict(CFG)))
    daemon_prompt.write("be kind", by="test")
    rec = prompt_packs.records([daemon_prompt.pack()])[0]
    sess, mgr = _session(env, [rec])
    service = session_reminder.SessionReminderService(mgr)
    _tick(service, 1000.0)

    daemon_prompt.write("be kinder", by="test")
    _tick(service, 1015.0)
    assert sess.sdef.prompt_packs[0]["text"] == "be kinder"
    assert sess.sdef.prompt_packs[0]["source"] == prompt_packs.DAEMON
    assert "changed: prompt pack @daemon" in sess.delivered[0]

    daemon_prompt.write("", by="test")
    _tick(service, 1030.0)
    assert sess.sdef.prompt_packs == ()
    assert "removed: prompt pack @daemon" in sess.delivered[1]


def test_the_record_follows_its_copy_even_with_the_source_off(env, monkeypatch):
    """rebrief answers from the record, so it never serves replaced text."""
    monkeypatch.setattr(session_reminder.SessionReminderService, "_config",
                        staticmethod(lambda: {**CFG, "prompt_pack_reminder": False}))
    sess, mgr = _session(env, [_ws_record(env, "house", "house rules")])
    service = session_reminder.SessionReminderService(mgr)
    _tick(service, 1000.0)
    _pack_file(env["installed"], "house", "new house rules")
    _tick(service, 1015.0)
    assert sess.sdef.prompt_packs[0]["text"] == "new house rules"
    assert sess.delivered == []


def test_a_change_notice_waits_for_a_landed_delivery(env, monkeypatch):
    monkeypatch.setattr(session_reminder.SessionReminderService, "_config",
                        staticmethod(lambda: dict(CFG)))
    sess, mgr = _session(env, [_ws_record(env, "house", "house rules")])
    service = session_reminder.SessionReminderService(mgr)
    _tick(service, 1000.0)
    _pack_file(env["installed"], "house", "new house rules")

    async def refuse(_text):
        return False

    sess.deliver = refuse
    _tick(service, 1015.0)
    assert service._packs["w1"]["notices"]

    async def accept(text):
        sess.delivered.append(text)
        return True

    sess.deliver = accept
    _tick(service, 1030.0)
    assert "changed: prompt pack house" in sess.delivered[0]
    assert service._packs["w1"]["notices"] == []
