"""What ``--wizard`` remembers between runs, and what it deliberately does not.

The recall exists so the same three answers are not retyped every launch, so
these tests pin the round trip (submit -> file -> next form's defaults) and,
just as importantly, its limits: a typed flag still beats it, a directory is
never remembered, and a stale file can neither crash a launch nor make a form
claim an option that no longer exists.
"""

from __future__ import annotations

import argparse
import os

from claude_launcher import wizard, wizard_recall

from test_wizard import FakeSources, FakeSpawnSources, _NoTerminal, pick


def drive(monkeypatch, keys: bytes, args: argparse.Namespace, **kw) -> bool:
    """Run the real loop with a scripted keyboard and no real terminal."""
    from claude_launcher import attach as attach_mod

    chunks = [keys, b""]
    form = kw.pop("form", None)
    sources = kw.pop("sources", None)
    monkeypatch.setattr(wizard, "available", lambda: True)
    monkeypatch.setattr(attach_mod, "_read_stdin", lambda: chunks.pop(0))
    monkeypatch.setattr(attach_mod, "_RawTerminal", _NoTerminal)
    monkeypatch.setattr(wizard, "_paint", lambda *a, **k: None)
    monkeypatch.setattr(wizard, "_write", lambda *a, **k: None)
    return wizard.run(
        args, sources=sources or FakeSources(**kw), cwd="/work/repo", form=form
    )


def new_args(**kw) -> argparse.Namespace:
    """The namespace ``new-session`` hands the wizard (flags left unanswered)."""
    base = dict(
        name=None, harness=None, profile=None, borrow=None, null_token=False,
        cwd=None, role=None, resume=None, fork_session=False, args=[],
        mesh=None, handle=None, connect=[], workflow=None, context=None,
        task=None, restore=None, attach=False,
    )
    base.update(kw)
    return argparse.Namespace(**base)


SUBMIT = b"\x13"  # ctrl-s: create, from wherever the cursor stands


# --------------------------------------------------------------------------- #
# the file
# --------------------------------------------------------------------------- #
def test_recall_lives_under_the_launcher_home_not_the_canonical_store(home):
    """Convenience state, kept away from the hand-edited, synced config."""
    from claude_launcher import store

    assert wizard_recall.path() == home / wizard_recall.FILENAME
    assert wizard_recall.path() != store.path()
    assert wizard_recall.load("new") == {}  # nothing remembered yet


def test_save_then_load_round_trips_per_form(home):
    wizard_recall.save("new", {"profile": "work", "role": "worker"})
    wizard_recall.save("spawn", {"profile": "ds4"})
    assert wizard_recall.load("new") == {"profile": "work", "role": "worker"}
    assert wizard_recall.load("spawn") == {"profile": "ds4"}
    # a section is replaced wholesale: an answer taken back is not kept alive
    wizard_recall.save("new", {"profile": "work"})
    assert wizard_recall.load("new") == {"profile": "work"}
    assert wizard_recall.load("spawn") == {"profile": "ds4"}  # untouched


def test_an_unreadable_recall_is_an_empty_one(home):
    wizard_recall.path().write_text("{{{ not yaml", encoding="utf-8")
    assert wizard_recall.load("new") == {}
    # and it is repaired by the next successful submit rather than raising
    wizard_recall.save("new", {"profile": "work"})
    assert wizard_recall.load("new") == {"profile": "work"}


def test_saving_never_breaks_a_launch(home, monkeypatch):
    """A disk that refuses the write must not refuse the session it records."""
    def boom(*a, **k):
        raise OSError("read-only")

    monkeypatch.setattr(wizard_recall.Path, "write_text", boom)
    wizard_recall.save("new", {"profile": "work"})  # no raise
    assert wizard_recall.load("new") == {}


# --------------------------------------------------------------------------- #
# precedence
# --------------------------------------------------------------------------- #
def test_a_typed_flag_beats_the_recall():
    args = new_args(profile="ds4")
    d = wizard_recall.defaults(args, {"profile": "work", "role": "worker"})
    assert d.profile == "ds4"  # typed
    assert d.role == "worker"  # unanswered: remembered


def test_an_explicit_no_restore_is_not_overruled():
    """``--no-restore`` says False outright; a remembered True must not win."""
    d = wizard_recall.defaults(new_args(restore=False), {"restore": True})
    assert d.restore is False
    # while an untyped restore does take the remembered answer, false included
    assert wizard_recall.defaults(new_args(), {"restore": False}).restore is False
    assert wizard_recall.defaults(new_args(), {"restore": True}).restore is True


def test_with_nothing_remembered_the_namespace_passes_through():
    args = new_args(profile="ds4")
    assert wizard_recall.defaults(args, {}) is args


# --------------------------------------------------------------------------- #
# the round trip through the real form
# --------------------------------------------------------------------------- #
def capturing(hook=None):
    """A ``Wizard`` that records the instance ``run`` built, and can be scripted.

    A subclass rather than a monkeypatched factory: the recall reads
    ``recall_key``/``recall_fields`` off the class, so the thing ``run`` is
    handed has to be a real form class.
    """
    made: dict = {}

    class Recorded(wizard.Wizard):
        def __init__(self, sources, *, cwd="", defaults=None):
            super().__init__(sources, cwd=cwd, defaults=defaults)
            self.color = False
            made["form"] = self
            if hook is not None:
                hook(self)

    return Recorded, made


def test_the_next_wizard_opens_on_the_last_answers(home, monkeypatch):
    """Pick a profile, a role and a mesh; the next form starts there."""

    def answer(w):
        pick(w, "profile", "ds4")
        pick(w, "role", "worker")
        pick(w, "mesh", "team")

    scripted, _ = capturing(answer)
    args = new_args()
    assert drive(monkeypatch, SUBMIT, args, form=scripted) is True
    assert (args.profile, args.role, args.mesh) == ("ds4", "worker", "team")

    saved = wizard_recall.load("new")
    assert saved["profile"] == "ds4"
    assert saved["role"] == "worker"
    assert saved["mesh"] == "team"
    # identity and directions are NOT remembered
    assert "name" not in saved and "cwd" not in saved
    assert "resume" not in saved and "worktree" not in saved

    # second launch: an untouched form already stands on those answers
    plain, reopened = capturing()
    second = new_args()
    assert drive(monkeypatch, SUBMIT, second, form=plain) is True
    w = reopened["form"]
    assert w.value("profile") == "ds4"
    assert w.value("role") == "worker"
    assert w.value("mesh") == "team"
    # and the directory still starts where the command was typed
    assert w.value("cwd") == os.path.abspath("/work/repo")
    assert second.name in (None, "")  # never remembered


def test_a_cancelled_form_remembers_nothing(home, monkeypatch):
    wizard_recall.save("new", {"profile": "work"})
    assert drive(monkeypatch, b"\x1b", new_args()) is False
    assert wizard_recall.load("new") == {"profile": "work"}  # unchanged


def test_a_remembered_option_that_no_longer_exists_is_ignored(home, monkeypatch):
    """A profile deleted since the last launch must not make the form lie."""
    wizard_recall.save("new", {"profile": "gone-profile", "role": "worker"})
    plain, seen = capturing()
    assert drive(monkeypatch, SUBMIT, new_args(), form=plain) is True
    w = seen["form"]
    assert w.value("profile") == "work"  # the form's own default, not the ghost
    assert w.value("role") == "worker"  # the answer that still exists survives


def test_spawn_remembers_its_own_shorter_list(home):
    """The two forms keep separate sections, and spawn's is narrower."""
    assert wizard.Wizard.recall_key != wizard.SpawnWizard.recall_key
    spawn_fields = set(wizard.SpawnWizard.recall_fields)
    # a child's harness, args and workspace are its parent's and the policy's
    # to offer, so they are not carried across parents; its mesh is the
    # parent's answer, not last launch's
    assert not spawn_fields & {
        "harness", "args", "workspace", "parent", "name", "mesh",
    }
    assert {"profile", "role"} <= spawn_fields


def test_no_form_remembers_a_row_that_takes_no_preset(home):
    """Only fields the form actually re-offers may be remembered.

    The Workflow row is filled by ``sync_workflows`` from the directory and
    the role and reads no preset, so remembering it would promise a default
    that never lands — the same reason spawn's Mesh row is out.
    """
    for form in (wizard.Wizard, wizard.SpawnWizard):
        assert "workflow" not in form.recall_fields
    # the seam itself, so this stays true if the row ever gains a preset:
    # a workflow named in the defaults does not reach the row today
    cwd = os.path.abspath("/work/repo")
    sources = FakeSources(workflows={cwd: ["ship-it"]})
    w = wizard.Wizard(
        sources, cwd=cwd,
        defaults=wizard_recall.defaults(new_args(), {"workflow": "ship-it"}),
    )
    assert "ship-it" in [o.label for o in w.field("workflow").options]
    assert w.value("workflow") == ""


def test_a_policy_locked_row_is_not_remembered(home, monkeypatch):
    """A greyed-out row holds what the form put there, not a person's answer.

    Remembering one would replay a locked choice under the next parent —
    a recall that causes refusals instead of saving typing.
    """
    locked = {
        "can_spawn": True, "blocked_by": [], "depth": 0, "max_depth": 3,
        "children_used": 0, "children_remaining": 3,
        # profile stays the parent's: the row is shown, greyed, with the
        # parent's own answer in it
        "may_choose": [], "spawnable_harnesses": [], "workspaces": [],
        "profiles": ["work", "ds4"],
    }
    made: dict = {}

    class Locked(wizard.SpawnWizard):
        def __init__(self, sources, *, cwd="", defaults=None):
            super().__init__(sources, cwd=cwd, defaults=defaults)
            self.color = False
            made["form"] = self

    assert drive(
        monkeypatch, SUBMIT, spawn_args(), form=Locked,
        sources=FakeSpawnSources(report=locked),
    ) is True
    assert made["form"].field("profile").disabled is True
    assert "profile" not in wizard_recall.load("spawn")
    # the rows the policy left alone are still remembered
    assert "role" in wizard_recall.load("spawn")


LOCKED_REPORT = {
    "can_spawn": True, "blocked_by": [], "depth": 0, "max_depth": 3,
    "children_used": 0, "children_remaining": 3,
    "may_choose": [], "spawnable_harnesses": [], "workspaces": [],
    "profiles": ["work", "ds4"],
}


def spawn_run(monkeypatch, args, *, report):
    """One scripted spawn launch; returns the form ``run`` built."""
    made: dict = {}

    class Recorded(wizard.SpawnWizard):
        def __init__(self, sources, *, cwd="", defaults=None):
            super().__init__(sources, cwd=cwd, defaults=defaults)
            self.color = False
            made["form"] = self

    ok = drive(
        monkeypatch, SUBMIT, args, form=Recorded,
        sources=FakeSpawnSources(report=report),
    )
    assert ok is True
    return made["form"]


def test_a_remembered_answer_is_not_injected_into_a_locked_row(home, monkeypatch):
    """Remembered under a permissive parent, replayed under a strict one.

    The recall is injected before the form exists, so the policy's verdict
    on that row arrives later — and when it says "locked", the remembered
    value has to come back out rather than travel to a refusal nobody typed.
    """
    wizard_recall.save("spawn", {"profile": "ds4", "role": "worker"})

    args = spawn_args()
    form = spawn_run(monkeypatch, args, report=LOCKED_REPORT)
    assert form.field("profile").disabled is True
    assert form.value("profile") == ""  # dropped, not greyed-out-and-armed
    assert args.profile is None  # so nothing travels to the daemon
    # the unlocked row keeps its recall
    assert form.value("role") == "worker" and args.role == "worker"


def test_a_typed_flag_still_reaches_a_locked_row(home, monkeypatch):
    """A flag typed this session is the person's current intent.

    It is left where it is on purpose: the daemon's refusal is the loud
    failure that tells them the policy forbids it. Dropping it silently
    would be the recall's rule applied to something that is not a recall.
    """
    wizard_recall.save("spawn", {"profile": "ds4"})
    args = spawn_args(profile="work")
    form = spawn_run(monkeypatch, args, report=LOCKED_REPORT)
    assert form.field("profile").disabled is True
    assert form.value("profile") == "work"
    assert args.profile == "work"


def test_nothing_is_dropped_when_the_policy_allows_the_row(home, monkeypatch):
    wizard_recall.save("spawn", {"profile": "ds4"})
    permissive = {**LOCKED_REPORT, "may_choose": ["profile", "borrow"]}
    args = spawn_args()
    form = spawn_run(monkeypatch, args, report=permissive)
    assert form.field("profile").disabled is False
    assert form.value("profile") == "ds4" and args.profile == "ds4"


def spawn_args(**kw) -> argparse.Namespace:
    """The namespace ``spawn`` hands the wizard, flags left unanswered."""
    base = dict(
        parent=None, name=None, harness=None, profile=None, borrow=None,
        null_token=False, args=[], workspace=None, worktree=None,
        rebase_onto=None, mesh=None, handle=None, role=None, connect=[],
        workflow=None, context=None, task=None, attach=False,
    )
    base.update(kw)
    return argparse.Namespace(**base)


def test_spawn_recall_round_trips_and_stays_out_of_new_sessions_section(
    home, monkeypatch
):
    """The child form remembers a role of its own without disturbing ``new``."""
    wizard_recall.save("new", {"role": "leader"})

    made: dict = {}

    class Scripted(wizard.SpawnWizard):
        def __init__(self, sources, *, cwd="", defaults=None):
            super().__init__(sources, cwd=cwd, defaults=defaults)
            self.color = False
            made.setdefault("forms", []).append(self)
            if len(made["forms"]) == 1:
                pick(self, "role", "worker")

    args = spawn_args()
    assert drive(
        monkeypatch, SUBMIT, args, form=Scripted, sources=FakeSpawnSources()
    ) is True
    assert args.role == "worker"
    assert wizard_recall.load("spawn")["role"] == "worker"
    assert wizard_recall.load("new") == {"role": "leader"}  # its own section

    # the next child form opens on it
    assert drive(
        monkeypatch, SUBMIT, spawn_args(), form=Scripted,
        sources=FakeSpawnSources()
    ) is True
    assert made["forms"][-1].value("role") == "worker"
