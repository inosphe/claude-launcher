"""A session keeps the model it is actually answering on across a restart.

The failure this covers: a session created on one model and switched to
another inside the harness (claude's ``/model``) came back on the *creation*
model at the next daemon restart. ``SessionDef.model`` is what the relaunch
puts on the command line, nothing wrote it after creation, and the explicit
``--model`` flag outranks whatever the harness had persisted for itself. The
dashboard showed the new model the whole time, because it reads the transcript
rather than the definition -- so the two disagreed with nothing saying so.

``docs/session-model-persistence.md`` is the design these tests pin. The cases
below are its section 4, in its order, plus the two edges that decide whether
this is safe to run on a timer: an id nobody can read must leave the
definition alone, and a pass that finds nothing must not write.
"""

from __future__ import annotations

import pytest

from claude_launcher import harnesses
from claude_launcher.daemon import harness as harness_mod
from claude_launcher.daemon import ctxsize, manager as manager_mod
from claude_launcher.daemon.harness import HarnessError, SessionDef


# --------------------------------------------------------------------------
# reading a backend model id back into the alias a launch takes
# --------------------------------------------------------------------------

#: Ids observed on this machine's profiles, with the alias each belongs to.
#: Two per alias for opus and fable on purpose: one alias covering several ids
#: is exactly why an exact ``model_aliases`` table cannot answer alone.
OBSERVED = [
    ("claude-opus-5", "opus"),
    ("claude-opus-4-7", "opus"),
    ("claude-sonnet-5", "sonnet"),
    ("claude-fable-5", "fable"),
    ("claude-fable-5-1", "fable"),
    ("claude-haiku-4-5-20251001", "haiku"),
]


@pytest.mark.parametrize("model_id,alias", OBSERVED)
def test_an_observed_model_id_reads_back_as_its_alias(home, model_id, alias):
    entry = harnesses.registry()["claude"]
    assert harness_mod.alias_for_model_id(entry, model_id) == alias


@pytest.mark.parametrize(
    "model_id",
    [
        "",
        "   ",
        "gpt-5.6-luna",          # another harness's id
        "deepseek-v4",           # a backend this registry does not name
        "claude",                # the prefix with no alias after it
        "claude-opusx-1",        # 'opus' must not claim a longer stem
        "opus",                  # the alias itself is not an id
    ],
)
def test_an_id_it_cannot_read_is_left_unanswered(home, model_id):
    """No guess. The caller writes this into the launch command, and a wrong
    alias would relaunch the session on a model nobody chose."""
    entry = harnesses.registry()["claude"]
    assert harness_mod.alias_for_model_id(entry, model_id) is None


def test_an_exact_alias_table_answers_where_the_prefix_cannot(home):
    """Codex puts the version *inside* the id (``gpt-5.6-luna``), so the
    ``<prefix>-<alias>`` shape does not fit it at all. Its declared
    ``model_aliases`` is what answers, and ids outside that table stay
    unanswered rather than being pattern-matched into one."""
    entry = harnesses.registry()["codex"]
    assert harness_mod.alias_for_model_id(entry, "gpt-5.6-luna") == "luna"
    assert harness_mod.alias_for_model_id(entry, "gpt-6-astra") == "astra"
    assert harness_mod.alias_for_model_id(entry, "gpt-5.6-sol-2") is None


def test_a_harness_with_no_models_never_answers(home):
    """A harness that declares no model choices has no alias to return, and
    must not fall through to the prefix branch on an empty list."""
    entry = harnesses.registry()["claude"]
    bare = type(entry)(name="bare")
    assert harness_mod.alias_for_model_id(bare, "claude-opus-5") is None


# --------------------------------------------------------------------------
# carrying the observed model into the session definition
# --------------------------------------------------------------------------

class _FakeSession:
    """The three things ``reconcile_models`` reads off a session."""

    def __init__(self, sdef, *, exited: bool = False):
        self.sdef = sdef
        self.exited = exited
        self.unmapped_model_id = None


def _manager_with(monkeypatch, sdef, reading, *, exited=False):
    """A manager holding one session, with its context reading stubbed.

    ``persist`` is stubbed out too and counted: these cases are about what the
    reconciliation decides, and a real persist would need a daemon directory.
    """
    manager = manager_mod.SessionManager(
        idle_threshold=60.0, scrollback=100, restore_default=True
    )
    session = _FakeSession(sdef, exited=exited)
    manager._sessions[sdef.name] = session
    monkeypatch.setattr(ctxsize, "for_session", lambda _sdef: reading)
    writes = []
    monkeypatch.setattr(manager, "persist", lambda: writes.append(1))
    return manager, session, writes


def _sdef(model, *, name="s1", harness="claude"):
    return SessionDef(name=name, harness=harness, profile="p", cwd=".", model=model)


def test_the_saved_model_follows_the_one_the_session_answers_on(monkeypatch, home):
    """The whole point: created on fable, answering on opus, so the next
    launch must be an opus launch."""
    manager, session, _ = _manager_with(
        monkeypatch, _sdef("fable"), {"model": "claude-opus-5"}
    )
    assert manager.reconcile_models() == ["s1"]
    assert session.sdef.model == "opus"


def test_a_pass_that_finds_nothing_changes_nothing(monkeypatch, home):
    """Runs on a timer, so the ordinary pass has to be free of writes."""
    manager, session, writes = _manager_with(
        monkeypatch, _sdef("opus"), {"model": "claude-opus-5"}
    )
    assert manager.reconcile_models() == []
    assert session.sdef.model == "opus"
    assert writes == []


def test_an_unreadable_id_leaves_the_saved_model_alone(monkeypatch, home):
    """And is remembered, so the disagreement can be seen and resolved by
    hand instead of being guessed at or silently dropped."""
    manager, session, _ = _manager_with(
        monkeypatch, _sdef("fable"), {"model": "some-other-backend-9"}
    )
    assert manager.reconcile_models() == []
    assert session.sdef.model == "fable"
    assert session.unmapped_model_id == "some-other-backend-9"


def test_a_readable_id_clears_an_earlier_unreadable_one(monkeypatch, home):
    manager, session, _ = _manager_with(
        monkeypatch, _sdef("fable"), {"model": "claude-opus-5"}
    )
    session.unmapped_model_id = "some-other-backend-9"
    manager.reconcile_models()
    assert session.unmapped_model_id is None


def test_no_reading_leaves_the_creation_choice_standing(monkeypatch, home):
    """A session that has not taken a turn has nothing to be read off it. The
    creation-time value is what it was before this feature existed, which is
    the right thing to keep."""
    manager, session, _ = _manager_with(monkeypatch, _sdef("fable"), None)
    assert manager.reconcile_models() == []
    assert session.sdef.model == "fable"


def test_an_exited_session_is_not_touched(monkeypatch, home):
    manager, session, _ = _manager_with(
        monkeypatch, _sdef("fable"), {"model": "claude-opus-5"}, exited=True
    )
    assert manager.reconcile_models() == []
    assert session.sdef.model == "fable"


def test_a_failing_reading_does_not_fail_the_pass(monkeypatch, home):
    """One unreadable transcript must not stop the sessions after it."""
    manager = manager_mod.SessionManager(
        idle_threshold=60.0, scrollback=100, restore_default=True
    )
    bad = _FakeSession(_sdef("fable", name="s1"))
    good = _FakeSession(_sdef("fable", name="s2"))
    manager._sessions["s1"] = bad
    manager._sessions["s2"] = good

    def reading(sdef):
        if sdef.name == "s1":
            raise OSError("transcript is gone")
        return {"model": "claude-opus-5"}

    monkeypatch.setattr(ctxsize, "for_session", reading)
    monkeypatch.setattr(manager, "persist", lambda: None)
    assert manager.reconcile_models() == ["s2"]
    assert good.sdef.model == "opus"
    assert bad.sdef.model == "fable"


# --------------------------------------------------------------------------
# the explicit lever
# --------------------------------------------------------------------------

def test_setting_the_model_by_hand_refuses_one_the_harness_lacks(monkeypatch, home):
    """Refused where it is typed, not at the relaunch it would break long
    afterwards."""
    manager, _, _ = _manager_with(monkeypatch, _sdef("fable"), None)
    with pytest.raises(HarnessError) as exc:
        manager.set_model("s1", "gpt-4")
    assert "unknown model" in str(exc.value)


def test_setting_the_model_by_hand_records_the_choice(monkeypatch, home):
    manager, session, writes = _manager_with(monkeypatch, _sdef("fable"), None)
    session.unmapped_model_id = "some-other-backend-9"
    manager.set_model("s1", "opus")
    assert session.sdef.model == "opus"
    # A person naming the model settles the open question the note stood for.
    assert session.unmapped_model_id is None
    assert writes == [1]


def test_clearing_the_model_returns_the_session_to_the_harness_default(
    monkeypatch, home
):
    manager, session, _ = _manager_with(monkeypatch, _sdef("opus"), None)
    manager.set_model("s1", "")
    assert session.sdef.model is None


def test_the_saved_choice_is_refused_when_the_args_already_pick_one(
    monkeypatch, home
):
    """Two model choices on one command line leaves the winner to harness
    parsing; creation refuses that pairing and so does this."""
    sdef = SessionDef(
        name="s1", harness="claude", profile="p", cwd=".",
        args=["--model=sonnet"],
    )
    manager, _, _ = _manager_with(monkeypatch, sdef, None)
    with pytest.raises(HarnessError) as exc:
        manager.set_model("s1", "opus")
    assert "already select a model" in str(exc.value)


# --------------------------------------------------------------------------
# the registry declaration the two steps rest on
# --------------------------------------------------------------------------

def test_the_claude_harness_declares_the_prefix_its_ids_share(home):
    """Without this line every claude id is unreadable and the reconciliation
    silently does nothing at all -- the exact failure it was written for,
    back again with no error to show for it."""
    reg = harnesses.registry()
    assert reg["claude"].model_id_prefix == "claude"
    assert reg["claude"].to_dict()["model_id_prefix"] == "claude"
