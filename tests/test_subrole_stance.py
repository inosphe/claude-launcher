"""A subrole's stance reaches the member the way the primary's does.

Scenario matrix (claunch-bwukr):

A. The briefing
   A1 the join briefing pastes each subrole's stance after the primary's,
      each with the text id of its whole stance
   A2 the budget fallback (inline=False) and a remote member get the pointer
      only, and the pointer says it covers the subroles
   A3 a subrole whose stance is the primary's word for word, or that has no
      stance, adds nothing

B. The role-change notice
   B1 an added subrole's stance rides the notice with its id; the primary's
      does not, because the member already holds it
   B2 a primary change hands over a subrole stance the briefing had left out
      for repeating the old primary's (claunch-th770)
   B3 a primary change does not repeat a subrole stance already briefed

C. Recovery and reminders
   C1 rebrief serves a subrole stance by its id
   C2 the session reminder's Role entry carries the subrole stance ids, its
      key moves when one changes, and the section names them
   C3 `claunch mesh stance` prints the subroles' stances after the primary's
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace

from claude_launcher import digests
from claude_launcher.daemon import rebrief, session_reminder

from test_mesh_role_edit import _capture, _setup


def _stance(mesh, name):
    return mesh.roleset.get(name).stance.strip()


def _restance(mesh, **stances):
    """Rewrite some roles' stances in the vocabulary this mesh has in force
    (the cached resolution an upload would replace)."""
    roles = dict(mesh.roleset.roles)
    for name, text in stances.items():
        roles[name] = replace(roles[name], stance=text)
    mesh._roleset = replace(mesh.roleset, roles=roles)


# --------------------------------------------------------------------------- #
# A. the briefing
# --------------------------------------------------------------------------- #
def test_the_join_briefing_carries_every_subrole_stance_with_its_id(home, tmp_path):
    async def run():
        mgr, mm, mesh = await _setup(tmp_path)
        member = await mm.join("m", "s1", handle="w1", role="worker",
                               subroles=["reviewer", "analyst"])
        block = mm.briefing_block(mesh, member)

        # A1: primary first, then each subrole in the member's order.
        worker, reviewer, analyst = (
            _stance(mesh, n) for n in ("worker", "reviewer", "analyst"))
        at = [block.index(f"stance ({label}), binding [text id: "
                          f"{digests.text_digest(text)}]:\n")
              for label, text in (("worker", worker),
                                  ("subrole reviewer", reviewer),
                                  ("subrole analyst", analyst))]
        assert at == sorted(at)
        for text in (worker, reviewer, analyst):
            assert text in block
        assert "what a worker and its subroles is on this mesh" in block

        # A2: the budget fallback is the pointer alone.
        lean = mm.briefing_block(mesh, member, inline_stance=False)
        assert "claunch mesh stance m" in lean
        assert "subrole reviewer" not in lean and reviewer not in lean

        await mgr.shutdown_all()

    asyncio.run(run())


def test_a_subrole_with_nothing_further_to_read_adds_nothing(home, tmp_path):
    async def run():
        mgr, mm, mesh = await _setup(tmp_path)
        member = await mm.join("m", "s1", handle="w1", role="worker",
                               subroles=["reviewer", "analyst"])
        # A3: one subrole repeats the primary's stance, one has none.
        _restance(mesh, reviewer=_stance(mesh, "worker"), analyst="")

        assert mm.subrole_stances(mesh, member) == []
        block = mm.briefing_block(mesh, member)
        assert "stance (subrole" not in block
        assert block.count("stance (worker), binding") == 1

        await mgr.shutdown_all()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# B. the role-change notice
# --------------------------------------------------------------------------- #
def test_an_added_subrole_hands_over_its_stance_and_only_its_own(home, tmp_path):
    async def run():
        mgr, mm, mesh = await _setup(tmp_path)
        await mm.join("m", "s1", handle="w1", role="worker")
        sent = _capture(mgr, "s1")

        mm.set_subroles("m", "w1", add=["reviewer"])
        notice = sent[-1]
        reviewer = _stance(mesh, "reviewer")
        assert "subroles added: reviewer" in notice
        assert (f"stance (subrole reviewer), binding [text id: "
                f"{digests.text_digest(reviewer)}]") in notice
        assert reviewer in notice
        # B1: the primary did not move, so its stance is not repeated.
        assert "stance (worker)" not in notice
        assert _stance(mesh, "worker") not in notice

        # Removing it hands over nothing.
        mm.set_subroles("m", "w1", remove=["reviewer"])
        assert "stance (" not in sent[-1]

        await mgr.shutdown_all()

    asyncio.run(run())


def test_a_primary_change_hands_over_a_subrole_stance_it_unmasks(home, tmp_path):
    async def run():
        mgr, mm, mesh = await _setup(tmp_path)
        # B2: reviewer repeats worker's stance, so the join briefing left it out.
        _restance(mesh, reviewer=_stance(mesh, "worker"))
        member = await mm.join("m", "s1", handle="w1", role="worker",
                               subroles=["reviewer"])
        assert mm.subrole_stances(mesh, member) == []
        sent = _capture(mgr, "s1")

        mm.set_member_roles("m", "w1", role="analyst", add=["reviewer"])
        notice = sent[-1]
        reviewer = _stance(mesh, "reviewer")
        assert "primary role: worker -> analyst" in notice
        assert "subroles added" not in notice
        # The member held that text as worker's, under worker's id; it now
        # binds as reviewer's, beside the id the reminder will name.
        assert (f"stance (subrole reviewer), binding [text id: "
                f"{digests.text_digest(reviewer)}]") in notice
        assert f"stance (analyst), binding" in notice

        await mgr.shutdown_all()

    asyncio.run(run())


def test_a_primary_change_does_not_repeat_a_subrole_stance_already_held(home, tmp_path):
    async def run():
        mgr, mm, mesh = await _setup(tmp_path)
        await mm.join("m", "s1", handle="w1", role="worker", subroles=["reviewer"])
        sent = _capture(mgr, "s1")

        # B3: reviewer's stance was briefed at the join; only analyst's is new.
        mm.set_member_roles("m", "w1", role="analyst", add=["reviewer"])
        notice = sent[-1]
        assert "stance (analyst), binding" in notice
        assert "stance (subrole reviewer)" not in notice

        await mgr.shutdown_all()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# C. recovery and reminders
# --------------------------------------------------------------------------- #
def test_rebrief_serves_a_subrole_stance_by_its_id(home, tmp_path):
    async def run():
        mgr, mm, mesh = await _setup(tmp_path)
        await mm.join("m", "s1", handle="w1", role="worker", subroles=["reviewer"])
        reviewer = _stance(mesh, "reviewer")
        ident = digests.text_digest(reviewer)

        # C1
        blocks = rebrief.addressable("s1", manager=mgr, mesh_mgr=mm)
        assert blocks[rebrief.block_digest(reviewer)]["kind"] == (
            "stance (m, subrole reviewer)")
        found = rebrief.recall("s1", ident, manager=mgr, mesh_mgr=mm)
        assert found["status"] == "recalled"
        assert found["text"] == reviewer

        await mgr.shutdown_all()

    asyncio.run(run())


def test_the_role_reminder_names_the_subrole_stance_ids(home, tmp_path):
    async def run():
        mgr, mm, mesh = await _setup(tmp_path)
        await mm.join("m", "s1", handle="w1", role="worker", subroles=["reviewer"])
        _capture(mgr, "s1")
        ident = digests.text_digest(_stance(mesh, "reviewer"))

        # C2
        entries = session_reminder.role_entries("s1", mgr, mm)
        assert entries[0]["subrole_stances"] == [{"name": "reviewer", "digest": ident}]
        lines = session_reminder.role_section(entries, cflow_guidance=False)
        assert any(line.startswith(f"subrole stance text id (reviewer): {ident}")
                   for line in lines)
        # A rewritten subrole stance is a new position: the key moves.
        _restance(mesh, reviewer="review, rewritten")
        again = session_reminder.role_entries("s1", mgr, mm)
        assert session_reminder.role_key(again) != session_reminder.role_key(entries)

        await mgr.shutdown_all()

    asyncio.run(run())


def test_mesh_stance_prints_the_subroles_after_the_primary(monkeypatch, capsys):
    from claude_launcher import cli_mesh

    roles = [{"name": "worker", "stance": "work the slice"},
             {"name": "reviewer", "stance": "judge the branch"},
             {"name": "pm", "stance": "work the slice"},   # same as primary
             {"name": "analyst", "stance": ""}]

    class FakeClient:
        def get(self, path):
            if path.endswith("/roles"):
                return {"roles": roles}
            return {"you": "w1", "members": [
                {"handle": "w1", "role": "worker",
                 "subroles": ["reviewer", "pm", "analyst"]}]}

    monkeypatch.setattr(cli_mesh.daemon_client, "ensure_running", lambda: FakeClient())
    args = argparse.Namespace(mesh="m", handle="w1", session="")
    # C3
    assert cli_mesh._cmd_stance(args) == 0
    out = capsys.readouterr().out
    assert out.index("work the slice") < out.index("# subrole reviewer")
    assert "judge the branch" in out
    assert "# subrole pm" not in out and "# subrole analyst" not in out
