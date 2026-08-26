"""The ``new-session --wizard`` form: what it offers, and what it answers with.

The terminal half (raw mode, ConsoleMode, the alternate screen) needs a real
console, so it is driven through the same seams :mod:`test_attach` uses: keys
are fed in as bytes and the form is read back as the lines it would paint.
Everything else is the form itself, which is deliberately free of I/O.
"""

from __future__ import annotations

import argparse

import pytest

from claude_launcher import attach as attach_mod
from claude_launcher import cli_sessions, wizard, worktree
from claude_launcher.cflow import model, state as cflow_state


class FakeSources(wizard.Sources):
    """Everything the daemon would publish, decided by the test instead."""

    def __init__(self, *, repo=True, workflows=None, meshes=True, issues=None):
        self._repo = repo
        self._workflows = workflows if workflows is not None else {}
        self._meshes = meshes
        # The board as the daemon publishes it: each row already carries the
        # verdict the creation path would take (see daemon/beads.adoption).
        self._issues = issues if issues is not None else [
            {"id": "cl-1", "title": "wire the rail", "status": "open",
             "assignee": "", "mode": "assigned", "held_by": None},
            {"id": "cl-2", "title": "the leader's own", "status": "in_progress",
             "assignee": "lead", "mode": "joined", "held_by": "lead"},
        ]
        self.workflow_calls = []
        self.issue_calls = []

    def harnesses(self):
        return [
            {"name": "claude", "available": True, "description": "Claude Code"},
            {"name": "codex", "available": False, "description": "Codex"},
        ]

    def profiles(self):
        return ["work", "ds4"]

    def workspaces(self):
        return [
            {"name": "api", "path": "/srv/api", "exists": True},
            {"name": "gone", "path": "/srv/gone", "exists": False},
        ]

    def roles(self):
        return [
            {"name": "worker", "aliases": ["hand"], "stance": "do the work"},
            {"name": "leader", "aliases": [], "stance": "steer the fleet"},
        ]

    def resumable(self):
        return [{"name": "old", "status": "exited", "conversation_id": "u1"}]

    def meshes(self):
        return [{"name": "team"}] if self._meshes else []

    def members(self, mesh):
        return ["lead", "api"] if mesh == "team" else []

    def workflows(self, cwd):
        self.workflow_calls.append(cwd)
        return self._workflows.get(cwd, [])

    def issues(self, cwd, parent=""):
        self.issue_calls.append((cwd, parent))
        return list(self._issues)

    def git(self, cwd):
        if not self._repo:
            return {"repo": False, "branch": "", "branches": [], "worktrees": []}
        # A branch per directory, so a test can tell "the branch this checkout
        # is cut from" apart from any other.
        branch = {"/srv/api": "api-main"}.get(cwd, "master")
        return {
            "repo": True, "branch": branch,
            "branches": [branch, "topic", "old-thing"],
            "worktrees": ["review"],
        }


def form(**kw) -> wizard.Wizard:
    sources = kw.pop("sources", None) or FakeSources(**kw)
    wiz = wizard.Wizard(sources, cwd="/work/repo")
    wiz.color = False
    return wiz


def focus_on(wiz: wizard.Wizard, key: str) -> None:
    """Put the cursor on a field the way a person would: with the arrow keys."""
    for _ in range(len(wiz.fields)):
        wiz.handle("up")
    for _ in range(len(wiz.fields)):
        if wiz.current.key == key:
            return
        wiz.handle("down")
    raise AssertionError(f"never reached {key!r} (hidden or disabled?)")


def pick(wiz: wizard.Wizard, key: str, label: str) -> None:
    """Open ``key``'s picker and choose the option labelled ``label``."""
    focus_on(wiz, key)
    wiz.handle("enter")
    assert wiz.mode == wizard.PICK
    wiz.handle("home")
    for _ in range(len(wiz.current.options)):
        if wiz.current.options[wiz.pick].label.startswith(label):
            wiz.handle("enter")
            return
        wiz.handle("down")
    raise AssertionError(f"no option {label!r} in {key!r}")


def type_into(wiz: wizard.Wizard, key: str, text: str) -> None:
    """Fill a text row the way a person would: open it, type, commit."""
    focus_on(wiz, key)
    wiz.handle("enter")
    assert wiz.mode == wizard.EDIT
    for ch in text:
        wiz.handle(ch)
    wiz.handle("enter")
    assert wiz.value(key) == text


# --------------------------------------------------------------------------- #
# keyboard
# --------------------------------------------------------------------------- #
def test_decode_keys_names_the_keys_a_form_needs():
    assert wizard.decode_keys("\x1b[A") == ["up"]
    assert wizard.decode_keys("\x1bOB") == ["down"]
    assert wizard.decode_keys("\x1b[3~") == ["delete"]
    assert wizard.decode_keys("\r") == ["enter"]
    assert wizard.decode_keys("\x13") == ["submit"]
    assert wizard.decode_keys("\x03") == ["cancel"]
    assert wizard.decode_keys("\x1b") == ["escape"]
    # text arrives a character at a time, mixed freely with keys
    assert wizard.decode_keys("ab\x1b[Dc") == ["a", "b", "left", "c"]


def test_decode_keys_swallows_sequences_it_does_not_know():
    """A mouse report or an unmapped key must not be typed into a field."""
    assert wizard.decode_keys("\x1b[<0;1;1M") == []
    assert wizard.decode_keys("\x1b[15~x") == ["x"]


def test_width_counts_wide_cells_twice():
    assert wizard.width("abc") == 3
    assert wizard.width("한글") == 4
    assert wizard.fit("abcdef", 5) == "ab..."
    assert wizard.fit("abc", 5) == "abc"
    assert wizard.width(wizard.pad("한글", 8)) == 8


# --------------------------------------------------------------------------- #
# what the form offers
# --------------------------------------------------------------------------- #
def test_every_closed_set_is_a_picker_not_a_text_box():
    wiz = form()
    for key in ("harness", "profile", "cwd", "worktree", "role", "resume",
                "mesh", "workflow", "restore", "attach"):
        assert isinstance(wiz.field(key), wizard.ChoiceField), key
    for key in ("name", "task", "args", "handle", "context", "worktree_name"):
        assert isinstance(wiz.field(key), wizard.TextField), key


def test_harness_is_read_only_and_derived_from_the_profile():
    wiz = form()
    assert not wiz.field("harness").selectable
    assert "profile" in wiz.field("harness").disabled_note.lower()
    assert wiz.value("harness") == "claude"


def test_a_missing_workspace_is_shown_and_unpickable():
    wiz = form()
    gone = [o for o in wiz.field("cwd").options if o.value == "/srv/gone"][0]
    assert gone.disabled and "missing" in gone.detail


def test_the_directory_starts_where_the_command_was_typed():
    """The CLI's default is the directory you are standing in; the wizard must
    not quietly move a launch into the workspace registry instead."""
    wiz = form()
    assert wiz.value("cwd") == wizard.os.path.abspath("/work/repo")
    assert wiz.field("cwd").options[0].label == "this directory"


def test_the_form_defaults_to_a_real_profile():
    wiz = form()
    assert wiz.value("profile") == "work"
    assert wiz.field("profile").options[0].value == ""  # empty placeholder


def test_profile_selector_picker_is_qualified_but_borrow_stays_base_profile():
    class SelectorSources(FakeSources):
        def profile_selectors(self):
            return ["work:claude", "work:pi", "ds4:claude", "ds4:pi"]

        def profile_details(self):
            return [
                {"name": "work:claude", "harness": "claude", "harness_available": True},
                {"name": "work:pi", "harness": "pi", "harness_available": True},
                {"name": "ds4:claude", "harness": "claude", "harness_available": True},
                {"name": "ds4:pi", "harness": "pi", "harness_available": True},
            ]

    wiz = form(sources=SelectorSources())
    profile_values = [o.value for o in wiz.field("profile").options]
    borrow_values = [o.value for o in wiz.field("borrow").options]

    assert "work:pi" in profile_values
    assert "work:pi" not in borrow_values
    assert "work" in borrow_values
    pick(wiz, "profile", "work:pi")
    assert wiz.value("harness") == "pi"


# --------------------------------------------------------------------------- #
# fields that depend on other fields
# --------------------------------------------------------------------------- #
def test_role_and_resume_belong_to_claude_only():
    class PiSources(FakeSources):
        def profile_details(self):
            return [
                {"name": "work", "harness": "claude", "harness_available": True},
                {"name": "ds4", "harness": "pi", "harness_available": True},
            ]

    wiz = form(sources=PiSources())
    assert wiz.field("role").selectable
    pick(wiz, "profile", "ds4")
    assert wiz.value("harness") == "pi"
    assert not wiz.field("role").selectable
    assert not wiz.field("resume").selectable
    assert not wiz.field("fork_session").selectable
    assert not wiz.field("borrow").selectable
    assert not wiz.field("null_token").selectable


def test_the_borrow_picker_offers_the_profiles():
    """--borrow names a profile, and profiles are a closed set the daemon
    publishes -- so it is a picker, never typed."""
    wiz = form()
    labels = [o.label for o in wiz.field("borrow").options]
    assert labels[0].startswith("(this profile's own token)")
    assert "work" in labels and "ds4" in labels
    assert wiz.value("borrow") == ""  # borrowing is an answer somebody gives


def test_null_takes_the_borrow_with_it():
    """`run` refuses --null --borrow outright; the form never offers the
    pair -- saying yes to null greys the borrow row and resets it."""
    wiz = form()
    pick(wiz, "borrow", "ds4")
    assert wiz.value("borrow") == "ds4"
    pick(wiz, "null_token", "yes")
    assert not wiz.field("borrow").selectable
    assert wiz.value("borrow") == ""
    pick(wiz, "null_token", "no")
    assert wiz.field("borrow").selectable


def test_fork_needs_a_conversation_to_fork():
    wiz = form()
    assert not wiz.field("fork_session").selectable
    pick(wiz, "resume", "old")
    assert wiz.field("fork_session").selectable
    pick(wiz, "fork_session", "yes")
    assert wiz.value("fork_session") is True
    # taking the conversation away takes the fork with it
    pick(wiz, "resume", "(new conversation)")
    assert not wiz.field("fork_session").selectable
    assert wiz.value("fork_session") is False


def test_no_worktree_question_outside_a_repository():
    wiz = form(repo=False)
    assert wiz.field("worktree").hidden
    assert wiz.field("worktree_name").hidden


def test_existing_worktrees_are_offered_alongside_a_new_one():
    wiz = form()
    labels = [o.label for o in wiz.field("worktree").options]
    assert labels[0].startswith("(none)")
    assert "new worktree" in labels
    assert "review" in labels  # already on disk: reuse is the common case


def test_naming_a_worktree_lands_on_the_name():
    wiz = form()
    pick(wiz, "worktree", "new worktree, named")
    assert wiz.current.key == "worktree_name"
    for ch in "feature/x":
        wiz.handle(ch)
    wiz.handle("enter")
    assert wiz.value("worktree_name") == "feature/x"
    assert wiz.problems() == []


def test_an_impossible_worktree_name_is_refused_before_anything_is_built():
    wiz = form()
    pick(wiz, "worktree", "new worktree, named")
    for ch in "../etc":
        wiz.handle(ch)
    wiz.handle("enter")
    assert wiz.handle("submit") is None
    assert "invalid worktree name" in wiz.error
    assert wiz.current.key == "worktree_name"


def test_the_mesh_reveals_the_handle_and_the_roster():
    wiz = form()
    assert wiz.field("handle").hidden and wiz.field("connect").hidden
    pick(wiz, "mesh", "team")
    assert not wiz.field("handle").hidden
    connect = wiz.field("connect")
    assert [o.value for o in connect.options] == ["lead", "api"]
    focus_on(wiz, "connect")
    wiz.handle("enter")          # opens the roster
    wiz.handle("space")          # lead
    wiz.handle("down")
    wiz.handle("enter")          # api, and done
    assert wiz.value("connect") == ["lead", "api"]


def test_workflows_follow_the_directory():
    sources = FakeSources(workflows={"/srv/api": ["ship-it"]})
    wiz = wizard.Wizard(sources, cwd="/work/repo")
    assert [o.value for o in wiz.field("workflow").options] == [""]
    pick(wiz, "cwd", "api")
    assert "ship-it" in [o.value for o in wiz.field("workflow").options]
    pick(wiz, "workflow", "ship-it")
    assert not wiz.field("context").hidden


def test_picking_a_role_selects_its_default_workflow():
    sources = FakeSources(workflows={"/srv/api": [
        {"name": "audit", "default_role": "leader", "priority": 9},
        {"name": "improv", "default_role": "worker", "priority": 1},
    ]})
    wiz = wizard.Wizard(sources, cwd="/work/repo")
    pick(wiz, "cwd", "api")
    assert wiz.value("workflow") == ""       # nobody volunteered for no role
    pick(wiz, "role", "worker")
    assert wiz.value("workflow") == "improv"
    pick(wiz, "role", "leader")              # the auto-pick keeps following
    assert wiz.value("workflow") == "audit"
    pick(wiz, "role", "(no role)")
    assert wiz.value("workflow") == ""       # and lets go with the role


def test_bundled_improv_workflows_volunteer_for_their_whitelisted_role():
    """The shipped improv workflows declare ``default_role``, so the wizard's
    Role row auto-selects them. This is the seam the feature used to die on:
    no workflow in any layer declared a ``default_role``, so picking worker
    (or any role) left the Workflow row on \"(none)\" whatever the form
    offered — the auto-select ran, found nobody volunteering, and picked
    nothing."""
    bundled = cflow_state.bundled_workflows_dir()
    entries = []
    for path in sorted(bundled.glob("improv-*.yaml")):
        wf = model.load(path)
        flt = (
            {"type": wf.filter_roles.type, "roles": list(wf.filter_roles.roles)}
            if wf.filter_roles
            else None
        )
        entries.append(
            wizard._workflow_entry(
                {
                    "name": wf.name,
                    "default_role": wf.default_role,
                    "priority": wf.priority,
                    "filter_roles": flt,
                }
            )
        )
    by_name = {e["name"]: e for e in entries}
    assert by_name["improv-worker"]["default_role"] == "worker"
    assert by_name["improv-leader"]["default_role"] == "leader"
    assert wizard._workflow_default(entries, "worker") == "improv-worker"
    assert wizard._workflow_default(entries, "leader") == "improv-leader"
    # a role nobody volunteered for still selects nothing — the mapping is
    # opt-in per workflow, not a blanket role -> workflow table
    assert wizard._workflow_default(entries, "reviewer") == ""


def test_rival_defaults_settle_by_priority_and_sort_the_picker():
    sources = FakeSources(workflows={"/srv/api": [
        {"name": "b-low", "default_role": "worker", "priority": 1},
        {"name": "a-high", "default_role": "worker", "priority": 5},
        {"name": "plain"},
    ]})
    wiz = wizard.Wizard(sources, cwd="/work/repo")
    pick(wiz, "cwd", "api")
    pick(wiz, "role", "worker")
    assert wiz.value("workflow") == "a-high"
    # the arrow keys walk the same ranking the auto-pick used: the role's
    # candidates by descending priority, then everything else
    assert [o.value for o in wiz.field("workflow").options] == [
        "", "a-high", "b-low", "plain",
    ]


def test_a_workflow_a_person_picked_survives_a_role_change():
    sources = FakeSources(workflows={"/srv/api": [
        {"name": "improv", "default_role": "worker", "priority": 1},
        {"name": "other"},
    ]})
    wiz = wizard.Wizard(sources, cwd="/work/repo")
    pick(wiz, "cwd", "api")
    pick(wiz, "workflow", "other")
    pick(wiz, "role", "worker")
    assert wiz.value("workflow") == "other"  # a human's answer, kept


def test_a_default_the_filter_refuses_is_never_volunteered():
    """The form takes its sources' word rather than re-parsing: whatever
    served the list, a workflow whose filter_roles turns the picked role away
    is never auto-selected — only offered, at the bottom of the picker."""
    sources = FakeSources(workflows={"/srv/api": [
        {"name": "trap", "default_role": "worker", "priority": 9,
         "filter_roles": {"type": "whitelist", "roles": ["leader"]}},
        {"name": "safe", "default_role": "worker", "priority": 1},
    ]})
    wiz = wizard.Wizard(sources, cwd="/work/repo")
    pick(wiz, "cwd", "api")
    pick(wiz, "role", "worker")
    assert wiz.value("workflow") == "safe"
    options = wiz.field("workflow").options
    assert options[-1].value == "trap"       # sunk, but still pickable
    assert "turns 'worker' away" in options[-1].detail


def test_the_form_asks_rolefilter_rather_than_guessing_the_rule():
    """Who a filter admits is decided in one place, for both the form and the
    start it precedes.

    The form used to carry its own copy of the whitelist/blacklist reading,
    and the two had already drifted. This pins the form to the model's answer
    wherever the model has one — that is, across the sanctioned vocabulary.
    A ``type`` outside it is not a disagreement to fix but a question with no
    rule behind it; :func:`test_an_unreadable_filter_is_not_volunteered_on`
    states what the form does there, and why that is the form's call alone.
    """
    for ftype in model.FILTER_TYPES:
        for roles in (["worker"], ["leader"], []):
            f = {"type": ftype, "roles": roles}
            entry = wizard._workflow_entry({"name": "w", "filter_roles": f})
            expected = model.RoleFilter(
                type=ftype, roles=tuple(roles)
            ).allows("worker")
            assert wizard._workflow_admits(entry, "worker") is expected, (
                f"the form disagrees with RoleFilter on {f!r}"
            )


def test_an_unreadable_filter_is_not_volunteered_on():
    """A ``type`` the vocabulary does not contain must sink a workflow, not
    float it.

    The parser refuses such a file, so nothing legitimate carries one and
    this cannot be provoked through the daemon — but the default has to lean
    the safe way regardless, because the failure it guards against is
    auto-selecting the very workflow an unreadable filter may have been
    written to keep away. Deferring to :class:`RoleFilter` here would do the
    opposite: its whitelist-or-else reading takes an unknown word as a
    blacklist and admits everyone.
    """
    for ftype in ("bogus", "", "  ", None):
        entry = wizard._workflow_entry(
            {"name": "w", "filter_roles": {"type": ftype, "roles": ["leader"]}}
        )
        assert wizard._workflow_admits(entry, "worker") is False, (
            f"{ftype!r} was volunteered on"
        )
    # Casing and stray space are the vocabulary's, not an unknown word: this
    # one is a whitelist of leaders, refused by the rule rather than for want
    # of one.
    spelled = wizard._workflow_entry(
        {"name": "w", "filter_roles": {"type": " WHITELIST ", "roles": ["worker"]}}
    )
    assert wizard._workflow_admits(spelled, "worker") is True

    # and a sunk workflow is still offered, never auto-picked
    sources = FakeSources(workflows={"/srv/api": [
        {"name": "unreadable", "default_role": "worker", "priority": 9,
         "filter_roles": {"type": "bogus", "roles": ["worker"]}},
    ]})
    wiz = wizard.Wizard(sources, cwd="/work/repo")
    pick(wiz, "cwd", "api")
    pick(wiz, "role", "worker")
    assert wiz.value("workflow") == ""
    assert [o.value for o in wiz.field("workflow").options] == ["", "unreadable"]


def test_a_filter_only_speaks_once_a_role_is_picked():
    """The form's own two conditions, kept out of the model: with no filter
    or no role chosen there is no question to put to it. ``RoleFilter``
    itself would refuse an empty role against a whitelist."""
    entry = wizard._workflow_entry({
        "name": "w", "filter_roles": {"type": "whitelist", "roles": ["leader"]},
    })
    assert wizard._workflow_admits(entry, "") is True      # nothing picked yet
    assert wizard._workflow_admits({"name": "w"}, "worker") is True  # no filter


def test_a_priority_that_is_not_a_number_does_not_take_the_picker_down():
    """``_workflow_entry`` is the tolerant edge: it already reads a bare
    string as a workflow and coerces every other field, so the one coercion
    that could raise must not be able to. A daemon of another version serving
    ``priority: soon`` should cost that workflow its rank, not the whole
    form."""
    for junk in ("soon", [], {}, "3.5.1"):
        entry = wizard._workflow_entry({"name": "w", "priority": junk})
        assert entry["priority"] == 0

    sources = FakeSources(workflows={"/srv/api": [
        {"name": "broken", "default_role": "worker", "priority": "soon"},
        {"name": "sound", "default_role": "worker", "priority": 2},
    ]})
    wiz = wizard.Wizard(sources, cwd="/work/repo")
    pick(wiz, "cwd", "api")
    pick(wiz, "role", "worker")          # the form renders instead of raising
    assert wiz.value("workflow") == "sound"   # ranked above the unranked one
    assert [o.value for o in wiz.field("workflow").options] == [
        "", "sound", "broken",
    ]


def test_a_priority_that_is_a_numeric_string_still_ranks():
    """Tolerating junk is not the same as discarding what is readable: JSON
    that spells a number as a string is still a number."""
    assert wizard._workflow_entry({"name": "w", "priority": "7"})["priority"] == 7


def test_the_picker_says_what_a_workflow_does_not_only_its_name():
    """A name is not an answer to "which of these five?".

    The workflow's own ``description`` is the one sentence its author wrote
    for exactly this moment, and the daemon serves it beside the name. It
    used to be dropped on the floor here, leaving a person to choose between
    'improv-worker' and 'delegated-dev' by guessing.
    """
    entry = wizard._workflow_entry({
        "name": "improv-worker", "description": "one goal, one round, one session",
    })
    assert entry["description"] == "one goal, one round, one session"

    [opt] = wizard._workflow_options([entry], "")
    assert opt.detail == "one goal, one round, one session"

    # ...and beside the facts, it comes last -- see _workflow_options.
    ranked = wizard._workflow_options(
        [wizard._workflow_entry({
            "name": "improv-worker", "description": "design -> ship",
            "default_role": "worker", "priority": 5,
        })],
        "worker",
    )
    assert ranked[0].detail == "default for worker, priority 5 -- design -> ship"


def test_a_workflow_that_says_nothing_about_itself_still_lists():
    """The description is optional in the schema, and a daemon old enough to
    serve bare names has none to give -- neither may cost the picker a row or
    leave a dangling separator on it."""
    assert wizard._workflow_entry("bare")["description"] == ""
    assert wizard._workflow_entry({"name": "w"})["description"] == ""

    [bare] = wizard._workflow_options([wizard._workflow_entry("bare")], "")
    assert bare.detail == ""
    [ranked] = wizard._workflow_options(
        [wizard._workflow_entry({"name": "w", "default_role": "worker"})], "worker",
    )
    assert ranked.detail == "default for worker"


def test_a_paragraph_of_description_is_cut_down_to_one_option_row():
    """A description is a paragraph -- folded scalars, newlines and all --
    and an option is one row that is then padded and fit to the terminal. So
    it is flattened and cut here, on the same budget the dashboard's picker
    uses, rather than left to shove the row's facts off a narrow screen."""
    folded = "one\ntwo   three\n\nfour"
    assert wizard._one_line(folded) == "one two three four"

    exact = "x" * wizard.WORKFLOW_DESC_COLS
    assert wizard._one_line(exact) == exact          # cut only when cut
    cut = wizard._one_line(exact + "y")
    assert cut.endswith("...") and wizard.width(cut) == wizard.WORKFLOW_DESC_COLS

    # Columns, not characters: a Korean description is twice as wide as it is
    # long, and it is the width that has to fit the row.
    wide = wizard._one_line("가" * 200)
    # A double-width script cannot always land on the budget exactly -- one
    # more character would overshoot it -- so the rule is that the row never
    # exceeds it, not that it always fills it.
    assert wizard.WORKFLOW_DESC_COLS - 1 <= wizard.width(wide) <= wizard.WORKFLOW_DESC_COLS
    # ...and the marker is ASCII, because this is drawn into a console that
    # may not be able to encode an ellipsis character (cp949 replaces it with
    # '?', which reads as a typo rather than as "there is more")
    assert wide.endswith("...")

    para = "a sentence long enough to be cut " * 4
    [opt] = wizard._workflow_options(
        [wizard._workflow_entry({"name": "w", "description": para})], "",
    )
    assert opt.detail.endswith("...")
    assert wizard.width(opt.detail) == wizard.WORKFLOW_DESC_COLS


def test_the_description_survives_the_whole_form():
    """End to end: what the daemon serves reaches the row a person reads."""
    sources = FakeSources(workflows={"/srv/api": [
        {"name": "improv-worker", "description": "one goal, one round",
         "default_role": "worker", "priority": 5},
        {"name": "feature-dev", "description": "design -> ship"},
    ]})
    wiz = wizard.Wizard(sources, cwd="/work/repo")
    pick(wiz, "cwd", "api")
    pick(wiz, "role", "worker")
    details = {o.value: o.detail for o in wiz.field("workflow").options}
    assert details["improv-worker"] == "default for worker, priority 5 -- one goal, one round"
    assert details["feature-dev"] == "design -> ship"


def test_type_to_jump_in_a_long_picker():
    wiz = form()
    focus_on(wiz, "profile")
    wiz.handle("enter")
    wiz.handle("d")
    assert wiz.field("profile").options[wiz.pick].label == "ds4"
    wiz.handle("enter")
    assert wiz.value("profile") == "ds4"


# --------------------------------------------------------------------------- #
# the answers
# --------------------------------------------------------------------------- #
def test_apply_writes_new_sessions_own_spelling():
    wiz = form()
    focus_on(wiz, "name")
    for ch in "api":
        wiz.handle(ch)
    wiz.handle("enter")
    pick(wiz, "profile", "ds4")
    pick(wiz, "borrow", "work")
    pick(wiz, "worktree", "review")
    pick(wiz, "role", "worker")
    pick(wiz, "resume", "old")
    pick(wiz, "fork_session", "yes")
    pick(wiz, "mesh", "team")
    pick(wiz, "attach", "yes")
    focus_on(wiz, "args")
    for ch in "--verbose":
        wiz.handle(ch)
    wiz.handle("enter")

    args = argparse.Namespace()
    wiz.apply(args)
    assert args.name == "api"
    assert args.harness is None  # profile is the only submitted source
    assert args.profile == "ds4"
    assert args.borrow == "work"
    assert args.null_token is False
    assert args.worktree == "review"
    assert args.role == "worker"
    assert args.resume == "old"
    assert args.fork_session is True
    assert args.mesh == "team"
    assert args.attach is True
    assert args.args == ["--verbose"]


def test_a_null_launch_travels_as_null_token():
    wiz = form()
    pick(wiz, "null_token", "yes")
    args = argparse.Namespace()
    wiz.apply(args)
    assert args.null_token is True
    assert args.borrow is None


def test_a_new_conversation_leaves_resume_unset():
    """``None`` and ``""`` are different answers to the API: a missing key is
    a new conversation, an empty one opens claude's picker."""
    wiz = form()
    args = argparse.Namespace()
    wiz.apply(args)
    assert args.resume is None
    pick(wiz, "resume", "pick in the harness")
    wiz.apply(args)
    assert args.resume == ""


def test_answering_no_to_the_worktree_is_not_the_same_as_not_answering():
    """ASK would put the old y/N prompt on the screen right after a form that
    just asked -- so the wizard always leaves a decided value."""
    wiz = form()
    args = argparse.Namespace()
    wiz.apply(args)
    assert args.worktree is worktree.NEVER
    wiz2 = form(repo=False)
    wiz2.apply(args)
    assert args.worktree is worktree.NEVER


def test_a_new_worktree_travels_as_the_bare_flag():
    """"new worktree" with nobody naming it is ``--worktree`` bare -- the
    empty string, which :func:`worktree.resolve` fills in with
    ``default_name``. The two neighbouring answers mean the opposite (NEVER
    is "no", None is "nobody asked"), and both leave the session in the
    directory as it stands, so an empty string is the only spelling that
    actually cuts one. SpawnWizard pins its own auto-name; this pins the
    form ``claunch new --wizard`` opens.
    """
    wiz = form()
    assert wiz.auto_worktree_name() == ""  # new-session lets the cutter decide
    pick(wiz, "worktree", "new worktree")
    args = argparse.Namespace()
    wiz.apply(args)
    assert args.worktree == ""
    assert args.worktree is not worktree.NEVER and args.worktree is not None
    assert args.rebase_onto == ""  # a fresh checkout has nothing to catch up on
    assert "worktree (auto)" in wiz.summary()


def test_flags_typed_before_the_wizard_prefill_it():
    defaults = argparse.Namespace(
        name="api", harness="claude", profile="ds4", cwd="/srv/api",
        borrow="work", null_token=False,
        role="worker", resume="old", fork_session=True, mesh="team",
        handle="apibot", task="ship it", args=["--", "--verbose"],
        connect=["lead"], attach=True, restore=True, workflow=None,
        context=None,
    )
    wiz = wizard.Wizard(FakeSources(), cwd="/work/repo", defaults=defaults)
    assert wiz.value("name") == "api"
    assert wiz.value("profile") == "ds4"
    assert wiz.value("borrow") == "work"
    assert wiz.value("cwd") == wizard.os.path.abspath("/srv/api")
    assert wiz.value("role") == "worker"
    assert wiz.value("resume") == "old"
    assert wiz.value("fork_session") is True
    assert wiz.value("mesh") == "team"
    assert wiz.value("handle") == "apibot"
    assert wiz.value("task") == "ship it"
    assert wiz.value("connect") == ["lead"]
    assert wiz.value("attach") is True
    assert wiz.field("args").text == "--verbose"


def test_a_session_without_any_profile_is_refused_at_the_profile_field():
    class NoProfiles(FakeSources):
        def profiles(self):
            return []

        def profile_details(self):
            return []

    wiz = form(sources=NoProfiles())
    assert wiz.handle("submit") is None      # nothing created
    assert "profile" in wiz.error
    assert wiz.current.key == "profile"


# --------------------------------------------------------------------------- #
# what it paints
# --------------------------------------------------------------------------- #
def test_the_form_reads_like_the_web_one():
    wiz = form()
    screen = "\n".join(wiz.render(90, 40))
    for label in ("Name", "Harness", "Profile", "Directory", "Worktree",
                  "Role", "Resume", "Args", "Mesh", "Workflow",
                  "Opening task", "Attach", "Create session"):
        assert label in screen
    assert "START IT WORKING" in screen
    # hidden until they apply
    assert "Handle" not in screen and "Context" not in screen


def test_the_cursor_and_the_hint_say_where_you_are():
    wiz = form()
    lines = wiz.render(90, 40)
    assert any(line.startswith(" > Name") for line in lines)
    assert any("how every other command refers to it" in line for line in lines)


def test_a_short_terminal_says_there_is_more():
    wiz = form()
    screen = "\n".join(wiz.render(90, 14))
    assert len(wiz.render(90, 14)) == 14
    assert "more below" in screen


def test_the_picker_marks_the_answer_that_stands():
    wiz = form()
    focus_on(wiz, "profile")
    wiz.handle("enter")
    screen = "\n".join(wiz.render(90, 20))
    assert "claunch new-session  /  Profile" in screen
    assert "work *" in screen  # the one currently chosen


# --------------------------------------------------------------------------- #
# the loop, and the command
# --------------------------------------------------------------------------- #
def drive(monkeypatch, keys: bytes, args: argparse.Namespace, **kw):
    """Run the real loop with a scripted keyboard and no real terminal."""
    chunks = [keys, b""]
    monkeypatch.setattr(wizard, "available", lambda: True)
    monkeypatch.setattr(attach_mod, "_read_stdin", lambda: chunks.pop(0))
    monkeypatch.setattr(attach_mod, "_RawTerminal", _NoTerminal)
    monkeypatch.setattr(wizard, "_paint", lambda *a, **k: None)
    monkeypatch.setattr(wizard, "_write", lambda *a, **k: None)
    return wizard.run(args, sources=FakeSources(**kw), cwd="/work/repo")


class _NoTerminal:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_escape_creates_nothing(monkeypatch, capsys):
    args = argparse.Namespace()
    assert drive(monkeypatch, b"\x1b", args) is False
    assert "cancelled" in capsys.readouterr().err
    assert not hasattr(args, "harness")


def test_stdin_closing_under_the_form_is_a_cancel(monkeypatch, capsys):
    args = argparse.Namespace()
    assert drive(monkeypatch, b"", args) is False


def test_ctrl_s_creates_and_says_what_it_is_creating(monkeypatch, capsys):
    args = argparse.Namespace()
    assert drive(monkeypatch, b"\x13", args) is True
    assert args.harness is None
    assert args.profile == "work"
    err = capsys.readouterr().err
    assert "creating:" in err and "profile work" in err


def test_the_wizard_is_refused_where_there_is_nobody_to_fill_it_in(monkeypatch):
    monkeypatch.setattr(wizard, "available", lambda: False)
    with pytest.raises(wizard.WizardUnavailable):
        wizard.run(argparse.Namespace(), sources=FakeSources())


def test_a_scripted_wizard_fails_before_it_starts_a_daemon(monkeypatch):
    """The refusal has to come first: auto-starting a daemon on the way to a
    form that cannot be shown leaves a process behind for nothing."""
    monkeypatch.setattr(wizard, "available", lambda: False)
    monkeypatch.setattr(
        cli_sessions.daemon_client, "ensure_running",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("started a daemon")),
    )
    with pytest.raises(wizard.WizardUnavailable):
        cli_sessions._run_wizard(argparse.Namespace(cwd=None))


def test_new_session_runs_the_wizard_before_it_builds_anything(monkeypatch):
    """The flag is a second way to *answer* new-session, not a second way to
    create a session: a cancelled form must not reach the daemon."""
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    monkeypatch.setattr(
        cli_sessions.daemon_client, "ensure_running",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("the daemon must not be asked to build anything")
        ),
    )
    monkeypatch.setattr(cli_sessions, "_run_wizard", lambda args: False)
    parser = argparse.ArgumentParser()
    from claude_launcher import cli

    args = cli.build_parser().parse_args(["new-session", "--wizard"])
    assert args.wizard is True
    assert cli_sessions._cmd_new_session(args) == 1


def test_an_agent_is_sent_to_spawn_before_the_form_opens(monkeypatch, capsys):
    monkeypatch.setenv("CLAUNCH_SESSION", "parent")
    monkeypatch.setattr(
        cli_sessions, "_run_wizard",
        lambda args: (_ for _ in ()).throw(AssertionError("no form for an agent")),
    )
    from claude_launcher import cli

    args = cli.build_parser().parse_args(["new-session", "--wizard"])
    assert cli_sessions._cmd_new_session(args) == 2
    assert "claunch spawn" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# spawn: a child, and only what a child may be asked
# --------------------------------------------------------------------------- #
class FakeSpawnSources(FakeSources):
    """A daemon with one parent session and a policy the test decides."""

    def __init__(self, *, report=None, sessions=None, **kw):
        super().__init__(**kw)
        self._report = report if report is not None else {
            "can_spawn": True, "blocked_by": [], "depth": 0, "max_depth": 3,
            "children_used": 1, "children_remaining": 3,
            # what the shipped policy allows: a workspace from the vouched
            # list, and a checkout of the repository the parent is already in
            "may_choose": ["workspace", "worktree"], "spawnable_harnesses": [],
            "workspaces": [{"name": "api", "path": "/srv/api", "exists": True}],
            # the run this parent's own workflow pairs its children with:
            # nothing, unless a test says otherwise
            "child_cflow": "",
        }
        self._sessions = sessions if sessions is not None else [
            {"name": "lead", "status": "idle", "harness": "claude",
             "profile": "work", "cwd": "/work/repo"},
            {"name": "old", "status": "exited", "harness": "claude",
             "profile": "work", "cwd": "/work/other", "conversation_id": "u1"},
        ]
        self.report_calls = []

    def sessions(self):
        return self._sessions

    def spawn_report(self, parent):
        self.report_calls.append(parent)
        return self._report

    def mesh_of(self, session):
        return "team" if session == "lead" else ""

    def members(self, mesh):
        return ["lead", "api", "docs"] if mesh == "team" else []


def spawn_form(**kw) -> wizard.SpawnWizard:
    # `defaults` is the namespace argparse already filled in: flags typed
    # alongside --wizard arrive exactly this way, so a test that passes one
    # is exercising the real door rather than the form's insides.
    defaults = kw.pop("defaults", None)
    sources = kw.pop("sources", None) or FakeSpawnSources(**kw)
    wiz = wizard.SpawnWizard(sources, cwd="/work/repo", defaults=defaults)
    wiz.color = False
    return wiz


def test_the_spawn_form_asks_only_what_a_child_may_be_asked():
    """No free-text directory and no resume: a child runs in its parent's
    directory (a registered workspace, or a worktree cut of either, is the
    vouched-for exception) and opens a conversation of its own. Profile and
    the auth rows ARE here now — policy-gated — because the spawn policy can
    unlock whose login a child holds."""
    wiz = spawn_form()
    keys = [f.key for f in wiz.fields]
    assert "parent" in keys
    assert "worktree" in keys
    for present in ("profile", "borrow", "null_token", "args", "attach"):
        assert present in keys, present
    for absent in ("cwd", "resume", "fork_session", "restore"):
        assert absent not in keys, absent


def test_the_parent_is_a_picker_of_the_sessions_that_exist():
    wiz = spawn_form()
    assert [o.value for o in wiz.field("parent").options] == ["lead", "old"]
    assert wiz.value("parent") == "lead"
    assert "idle" in wiz.field("parent").options[0].detail


def test_with_no_sessions_there_is_nothing_to_be_a_child_of():
    wiz = spawn_form(sessions=[])
    assert wiz.value("parent") is None
    assert wiz.handle("submit") is None
    assert "no parent to spawn from" in wiz.error


def test_the_parent_row_says_what_it_has_left_to_spend():
    wiz = spawn_form()
    assert "1 running, 3 left" in wiz.field("parent").hint
    assert "depth 0/3" in wiz.field("parent").hint


def test_a_parent_with_no_slots_is_refused_before_anything_is_arranged():
    """The whole reason the form reads the spawn report: a full parent says so
    on its own row, instead of the daemon refusing a filled-in form."""
    wiz = spawn_form(report={
        "can_spawn": False,
        "blocked_by": ["child limit reached (4/4)"],
        "depth": 1, "max_depth": 3, "children_used": 4, "children_remaining": 0,
        "may_choose": [], "spawnable_harnesses": [],
    })
    assert wiz.handle("submit") is None
    assert "child limit reached (4/4)" in wiz.error
    assert wiz.current.key == "parent"


def test_the_parent_row_names_role_and_directory():
    """A picker of ten s-numbers is a guessing game without them: who a
    session is (its role) and where it stands (its cwd) are what tell two
    workers apart."""
    wiz = spawn_form(sessions=[
        {"name": "lead", "status": "idle", "harness": "claude",
         "profile": "work", "cwd": "/work/repo", "role": "leader"},
    ])
    detail = wiz.field("parent").options[0].detail
    assert "leader" in detail
    assert "/work/repo" in detail


def _full_report(**extra):
    """A parent at its child cap, on a daemon that reports the cap as soft."""
    return {
        "can_spawn": False,
        "blocked_by": ["child limit reached (4/4)"],
        "soft_blocked_by": ["child limit reached (4/4)"],
        "depth": 1, "max_depth": 3, "children_used": 4, "children_remaining": 0,
        "may_choose": [], "spawnable_harnesses": [],
        **extra,
    }


def test_a_soft_child_cap_offers_the_override_row():
    """The cap interrupts fan-out loops, it does not forbid a fifth child
    somebody wanted: the form asks the deliberate yes and only then sends
    over_limit along."""
    wiz = spawn_form(report=_full_report())
    assert not wiz.field("over_limit").hidden
    assert wiz.handle("submit") is None
    assert "soft cap" in wiz.error
    pick(wiz, "over_limit", "yes")
    assert wiz.handle("submit") == "create"
    args = argparse.Namespace()
    wiz.apply(args)
    assert args.over_limit is True
    assert "over the child cap" in wiz.summary()


def test_the_override_waives_only_the_child_cap():
    """Depth (and enabled) stay exactly as refused: recursion is the mistake
    the limits are for, and depth is the axis it runs away on."""
    wiz = spawn_form(report=_full_report(
        blocked_by=["depth limit reached (3/3)", "child limit reached (4/4)"],
    ))
    pick(wiz, "over_limit", "yes")
    assert wiz.handle("submit") is None
    assert "depth limit" in wiz.error
    assert "child limit" not in wiz.error


def test_the_override_row_stays_hidden_while_slots_remain():
    """A row that is always there stops being read — and a yes given to a
    full parent must not travel once the pick moves to one with slots."""
    wiz = spawn_form()
    assert wiz.field("over_limit").hidden
    args = argparse.Namespace()
    wiz.apply(args)
    assert args.over_limit is False


def test_an_older_daemon_keeps_the_cap_hard():
    """No soft_blocked_by in the report means a daemon that would refuse the
    override anyway, so the form does not offer what it cannot deliver."""
    wiz = spawn_form(report={
        "can_spawn": False,
        "blocked_by": ["child limit reached (4/4)"],
        "depth": 1, "max_depth": 3, "children_used": 4, "children_remaining": 0,
        "may_choose": [], "spawnable_harnesses": [],
    })
    assert wiz.field("over_limit").hidden
    assert wiz.handle("submit") is None
    assert "child limit reached (4/4)" in wiz.error


def test_the_policy_decides_which_rows_are_open():
    wiz = spawn_form()
    # Harness is never a policy gate: the selected profile owns it.
    assert not wiz.field("harness").selectable
    assert "profile" in wiz.field("harness").disabled_note
    # allow_workspace is on, so the registry it published is pickable
    assert wiz.field("workspace").selectable
    assert [o.value for o in wiz.field("workspace").options] == ["", "api"]


def test_the_policy_keeps_profile_borrow_and_args_locked_by_default():
    """Greyed with the key that opens them, not hidden: the form is also how
    a person learns what the policy currently is."""
    wiz = spawn_form()
    assert not wiz.field("profile").selectable
    assert "spawn.allow_profile" in wiz.field("profile").disabled_note
    assert not wiz.field("borrow").selectable
    assert "spawn.allow_profile" in wiz.field("borrow").disabled_note
    assert not wiz.field("args").selectable
    assert "spawn.allow_args" in wiz.field("args").disabled_note
    # --null is never gated: it takes a credential away, not grants one
    assert wiz.field("null_token").selectable


def _open_report(**extra):
    return {
        "can_spawn": True, "blocked_by": [], "depth": 0, "max_depth": 3,
        "children_used": 0, "children_remaining": 4,
        "may_choose": ["args", "borrow", "null_token", "profile"],
        "spawnable_harnesses": [],
        "profiles": ["other", "work"],
        **extra,
    }


def test_a_locked_profile_row_never_travels():
    """The rule the other rows already keep: a value standing on a greyed-out
    row is not an answer the user gave, so it must not reach the daemon —
    which would refuse it naming a field this form could not still choose."""
    wiz = spawn_form()  # the stock policy: profile locked
    assert not wiz.field("profile").selectable
    # the row still HOLDS a value (it is greyed, not emptied)...
    wiz.field("profile").select("work")
    args = argparse.Namespace()
    wiz.apply(args)
    assert args.profile is None


def test_a_parent_that_locks_the_row_mid_form_takes_the_value_with_it():
    """The seam apply() is the fix for: the form opened on a permissive
    parent, a profile was picked, and then the parent changed to one whose
    policy forbids it. The row greys, and the answer must grey with it.

    The recall's own guard cannot reach this one — `drop_locked` runs once,
    just after the form is built, and the parent changes long afterwards.
    A verdict taken at apply() time is what covers it, which is why the fix
    lives there."""
    sources = FakeSpawnSources(
        report=_open_report(),
        sessions=[
            {"name": "lead", "status": "idle", "harness": "claude",
             "profile": "work", "cwd": "/work/repo"},
            {"name": "strict", "status": "idle", "harness": "claude",
             "profile": "work", "cwd": "/work/other"},
        ],
    )
    wiz = spawn_form(sources=sources)
    # 'work' on purpose: a locked report names no profiles, so the row falls
    # back to the registry — and a pick that SURVIVES that rebuild is the
    # only way this test sees the greyed row still holding a value. Picking
    # one the fallback list drops would pass whether or not apply() reads
    # through the disable, which is no test at all.
    pick(wiz, "profile", "work")
    assert wiz.value("profile") == "work"
    # ...and now the parent moves to one the policy keeps shut
    sources._report = {**_open_report(), "may_choose": []}
    pick(wiz, "parent", "strict")
    assert not wiz.field("profile").selectable
    assert wiz.field("profile").value == "work"   # the row kept it
    args = argparse.Namespace()
    wiz.apply(args)
    assert args.profile is None


def test_a_profile_TYPED_on_the_command_line_travels_even_onto_a_locked_row():
    """The exception that keeps the rule honest, and the line the recall
    already draws: a value the FORM left on a greyed row may be dropped in
    silence, but a flag this person spelled out is their current intent —
    it travels, and the daemon's refusal is the loud failure it deserves."""
    # --profile work --wizard, on the shipped policy that locks the row
    wiz = spawn_form(defaults=argparse.Namespace(profile="work"))
    assert not wiz.field("profile").selectable
    assert wiz.handle("submit") == "create"
    args = argparse.Namespace()
    wiz.apply(args)
    assert args.profile == "work"


def test_a_REMEMBERED_profile_is_not_mistaken_for_a_typed_one():
    """The two arrive on the same namespace, so the form has to ask which
    it is — a recall behind an unanswered flag must not buy the exception
    above, or every remembered profile would travel to a refusal nobody
    typed."""
    from claude_launcher import wizard_recall

    recalled = wizard_recall.defaults(
        argparse.Namespace(profile=None), {"profile": "ds4"}
    )
    wiz = spawn_form(defaults=recalled)
    assert not wiz.field("profile").selectable
    args = argparse.Namespace()
    wiz.apply(args)
    assert args.profile is None


def test_an_unlocked_row_answers_with_whatever_it_holds():
    """The exception is only about locked rows: with the policy open, the
    typed flag and the picked value are the same kind of answer."""
    wiz = spawn_form(report=_open_report(),   # may_choose includes profile
                     defaults=argparse.Namespace(profile="other"))
    assert wiz.value("profile") == "other"   # the flag pre-filled the row
    assert wiz.handle("submit") == "create"
    args = argparse.Namespace()
    wiz.apply(args)
    assert args.profile == "other"


def test_unlocked_profile_borrow_and_args_travel_on_apply():
    wiz = spawn_form(report=_open_report())
    pick(wiz, "profile", "other")
    pick(wiz, "borrow", "other")
    focus_on(wiz, "args")
    for ch in "--verbose":
        wiz.handle(ch)
    wiz.handle("enter")
    pick(wiz, "attach", "yes")
    args = argparse.Namespace()
    wiz.apply(args)
    assert args.profile == "other"
    assert args.borrow == "other"
    assert args.null_token is False
    assert args.args == ["--verbose"]
    assert args.attach is True


def test_null_needs_no_unlock_and_takes_the_borrow_with_it():
    """Same shape as the other form: saying yes to null greys the borrow row
    instead of provoking the daemon's refusal of the pair — and null itself
    stands open under the stock (all-locked) policy."""
    wiz = spawn_form(report=_open_report())
    pick(wiz, "borrow", "other")
    pick(wiz, "null_token", "yes")
    assert not wiz.field("borrow").selectable
    assert wiz.value("borrow") == ""
    args = argparse.Namespace()
    wiz.apply(args)
    assert args.null_token is True
    assert args.borrow is None

    locked = spawn_form()  # stock policy: profile/borrow/args all locked
    pick(locked, "null_token", "yes")
    locked.apply(args)
    assert args.null_token is True
    assert args.borrow is None and args.profile is None


def test_even_an_old_daemon_harness_unlock_stays_read_only():
    wiz = spawn_form(report={
        "can_spawn": True, "blocked_by": [], "depth": 0, "max_depth": 3,
        "children_used": 0, "children_remaining": 4,
        "may_choose": [], "spawnable_harnesses": ["codex"],
    })
    harness = wiz.field("harness")
    assert not harness.selectable
    assert [o.value for o in harness.options] == ["claude"]
    # allow_workspace off means the report carries no workspace list at all
    assert not wiz.field("workspace").selectable


def test_the_form_is_rebuilt_when_the_parent_changes():
    wiz = spawn_form(sessions=[
        {"name": "lead", "status": "idle", "profile": "work", "cwd": "/work/repo"},
        {"name": "solo", "status": "idle", "profile": "work", "cwd": "/work/other"},
    ])
    assert "F:" not in wiz.field("workspace").options[0].label
    assert "/work/repo" in wiz.field("workspace").options[0].label
    pick(wiz, "parent", "solo")
    assert "/work/other" in wiz.field("workspace").options[0].label
    assert wiz.sources.report_calls == ["lead", "solo"]


def test_the_mesh_defaults_to_the_parents_own():
    wiz = spawn_form()
    mesh = wiz.field("mesh")
    assert mesh.options[0].label == "(the parent's: team)"
    assert mesh.value == ""            # "" = inherit, which is what spawn means
    assert mesh.options[1].value == wizard.SpawnWizard.NO_MESH


def test_a_parent_in_no_mesh_says_one_will_be_opened():
    wiz = spawn_form(sessions=[
        {"name": "lead", "status": "idle", "profile": "work", "cwd": "/work/repo"},
        {"name": "solo", "status": "idle", "profile": "work", "cwd": "/work/other"},
    ])
    pick(wiz, "parent", "solo")
    assert "opened for the pair" in wiz.field("mesh").options[0].label


def test_no_mesh_at_all_takes_the_handle_and_the_roster_with_it():
    wiz = spawn_form()
    assert not wiz.field("handle").hidden
    pick(wiz, "mesh", "- no mesh")
    assert wiz.field("handle").hidden
    assert wiz.field("connect").hidden
    args = argparse.Namespace()
    wiz.apply(args)
    assert args.mesh == "-"
    assert args.handle is None and args.connect == []


def test_the_args_row_shows_the_parents_flags_without_proposing_them():
    """Every other override row names what it would inherit -- "(the
    parent's: claude)" and so on -- but Args is free text, so the inherited
    value has nowhere to sit except the hint. It has to be *shown* and not
    *pre-filled*: text put in the box is what the child runs INSTEAD of its
    parent's args, so seeding it would send the parent's own flags back as
    if somebody had typed them."""
    wiz = spawn_form(sessions=[
        {"name": "lead", "status": "idle", "harness": "claude",
         "cwd": "/work/repo", "args": ["--model", "opus", "--verbose"]},
    ])
    field = wiz.field("args")
    assert field.text == ""              # nothing proposed
    assert field.display() == ""         # and the row reads empty, not "(none)"
    assert "the parent's: --model opus --verbose" in field.hint
    # Untouched, the child still inherits: apply sends nothing, and an empty
    # 'args' is what spawn reads as "run what the parent runs".
    args = argparse.Namespace()
    wiz.apply(args)
    assert args.args == []


def test_the_args_row_says_so_when_the_parent_has_no_flags_of_its_own():
    """The blank box would otherwise be ambiguous — nothing inherited, or
    nothing known?"""
    wiz = spawn_form(sessions=[
        {"name": "lead", "status": "idle", "harness": "claude", "cwd": "/work/repo"},
    ])
    assert "none of its own" in wiz.field("args").hint


def test_the_args_hint_follows_the_parent_that_is_picked():
    wiz = spawn_form(sessions=[
        {"name": "lead", "status": "idle", "harness": "claude",
         "cwd": "/work/repo", "args": ["--verbose"]},
        {"name": "other", "status": "idle", "harness": "claude",
         "cwd": "/work/repo", "args": ["--model", "haiku"]},
    ])
    assert "the parent's: --verbose" in wiz.field("args").hint
    pick(wiz, "parent", "other")
    assert "the parent's: --model haiku" in wiz.field("args").hint


def test_the_roster_is_the_parents_mesh_minus_the_parent():
    """It can always reach its parent, so offering that as a connection would
    be offering something that is already true."""
    wiz = spawn_form()
    connect = wiz.field("connect")
    assert [o.value for o in connect.options] == ["api", "docs"]


def test_workflows_follow_the_directory_the_child_will_run_in():
    sources = FakeSpawnSources(workflows={"/srv/api": ["ship-it"]})
    wiz = wizard.SpawnWizard(sources, cwd="/work/repo")
    assert [o.value for o in wiz.field("workflow").options] == [""]
    pick(wiz, "workspace", "api")
    assert "ship-it" in [o.value for o in wiz.field("workflow").options]


def test_a_childs_workflow_comes_from_its_parents_pair_not_from_its_role():
    """The spawn form's Workflow row follows the PARENT, not the role.

    Which run a child drives is a property of the procedure its parent is
    running (``default_child_cflow``, served as ``child_cflow``); a role
    travels across every workflow, so reading the child's run off it handed a
    worker-role child the worker flow even under a parent driving something
    else. The role still ranks the list — it just no longer decides.
    """
    sources = FakeSpawnSources(
        report={**FakeSpawnSources().spawn_report("lead"),
                "child_cflow": "worker-flow"},
        workflows={"/work/repo": [
            {"name": "improv", "default_role": "worker", "priority": 5},
            {"name": "audit", "default_role": "leader", "priority": 9},
            {"name": "worker-flow"},
        ]},
    )
    wiz = wizard.SpawnWizard(sources, cwd="/work/repo")
    assert wiz.value("workflow") == "worker-flow"      # before any role
    pick(wiz, "role", "worker")
    assert wiz.value("workflow") == "worker-flow"      # the role does not steer
    pick(wiz, "role", "leader")
    assert wiz.value("workflow") == "worker-flow"
    args = argparse.Namespace()
    wiz.apply(args)
    assert args.workflow == "worker-flow"


def test_a_parent_that_pairs_with_nothing_preselects_no_workflow():
    """"" is an answer, not a missing one — so no fallback to the role's
    default, which is the very reading this replaced."""
    sources = FakeSpawnSources(workflows={"/work/repo": [
        {"name": "improv", "default_role": "worker", "priority": 5},
    ]})
    wiz = wizard.SpawnWizard(sources, cwd="/work/repo")
    pick(wiz, "role", "worker")
    assert wiz.value("workflow") == ""
    args = argparse.Namespace()
    wiz.apply(args)
    # nothing to refuse, so nothing travels: the daemon has no pair to apply
    assert args.workflow is None


def test_clearing_the_workflow_row_travels_as_a_refusal():
    """An emptied row is not silence. The daemon reads an absent workflow as
    'give the child the pair my run declares', so a person who cleared the
    row has to be heard saying no — or the form hands back the run it was
    just used to take away."""
    sources = FakeSpawnSources(
        report={**FakeSpawnSources().spawn_report("lead"),
                "child_cflow": "worker-flow"},
        workflows={"/work/repo": [{"name": "worker-flow"}]},
    )
    wiz = wizard.SpawnWizard(sources, cwd="/work/repo")
    assert wiz.value("workflow") == "worker-flow"
    pick(wiz, "workflow", "(none)")
    args = argparse.Namespace()
    wiz.apply(args)
    assert args.workflow == wizard.SpawnWizard.NO_WORKFLOW
    assert args.context is None


def test_the_bundled_leader_flow_pairs_its_children_with_the_worker_flow():
    """The pair as shipped: improv-leader hands its children improv-worker,
    and nothing else in the bundle has to remember that."""
    bundled = cflow_state.bundled_workflows_dir()
    paired = {
        path.stem: model.load(path).default_child_cflow
        for path in sorted(bundled.glob("improv-*.yaml"))
    }
    assert paired["improv-leader"] == "improv-worker"
    assert paired["improv-mid"] == "improv-worker"
    assert paired["improv-worker"] == "improv-worker"


def test_spawn_apply_writes_the_flags_spawn_reads():
    wiz = spawn_form()
    focus_on(wiz, "name")
    for ch in "helper":
        wiz.handle(ch)
    wiz.handle("enter")
    pick(wiz, "workspace", "api")
    pick(wiz, "role", "worker")
    focus_on(wiz, "handle")
    for ch in "hand":
        wiz.handle(ch)
    wiz.handle("enter")
    focus_on(wiz, "connect")
    wiz.handle("enter")
    wiz.handle("enter")          # the first member, and done
    focus_on(wiz, "task")
    for ch in "ship it":
        wiz.handle(ch)
    wiz.handle("enter")

    args = argparse.Namespace()
    wiz.apply(args)
    assert args.parent == "lead"
    assert args.name == "helper"
    assert args.harness is None          # inherited, and not ours to send
    assert args.workspace == "api"       # the NAME, which is what -w means
    assert args.role == "worker"
    assert args.mesh is None             # "" = the parent's, sent as nothing
    assert args.handle == "hand"
    assert args.connect == ["api"]
    assert args.task == "ship it"
    assert wiz.handle("submit") == "create"


#: A daemon that says this parent has a conversation worth copying. The
#: stock fixture deliberately does not: most parents have nothing to fork.
def _forkable(**over) -> dict:
    return {
        "can_spawn": True, "blocked_by": [], "depth": 0, "max_depth": 3,
        "children_used": 1, "children_remaining": 3,
        "may_choose": ["workspace", "worktree", "fork"],
        "spawnable_harnesses": [], "workspaces": [
            {"name": "api", "path": "/srv/api", "exists": True}
        ],
        **over,
    }


def test_fork_is_offered_only_when_the_parent_has_a_conversation():
    """The daemon decides — it is the one that can read the parent's pinned
    conversation — and an older daemon that never says so greys the row
    rather than offering something it would refuse."""
    assert not spawn_form().field("fork").selectable
    assert "no claude conversation" in spawn_form().field("fork").disabled_note
    wiz = spawn_form(report=_forkable())
    assert wiz.field("fork").selectable


def test_sending_the_child_elsewhere_takes_the_fork_with_it():
    """Claude keeps transcripts per working directory, so a child in a
    workspace of its own would open a conversation that is not there. The
    form greys the row instead of letting the daemon refuse a filled-in
    form -- and a yes given before the workspace was picked does not travel."""
    wiz = spawn_form(report=_forkable())
    pick(wiz, "fork", "yes")
    assert wiz.value("fork") is True
    pick(wiz, "workspace", "api")
    assert not wiz.field("fork").selectable
    assert "per directory" in wiz.field("fork").disabled_note
    args = argparse.Namespace()
    wiz.apply(args)
    assert args.fork is False
    # ...and putting the child back beside its parent brings it back.
    pick(wiz, "workspace", "(the parent")
    assert wiz.field("fork").selectable
    wiz.apply(args)
    assert args.fork is True


def test_a_worktree_of_its_own_also_takes_the_fork():
    wiz = spawn_form(report=_forkable())
    pick(wiz, "fork", "yes")
    pick(wiz, "worktree", "new worktree")
    assert not wiz.field("fork").selectable
    assert "worktree of its own" in wiz.field("fork").disabled_note


def test_a_fork_is_named_in_the_summary_and_the_flags():
    wiz = spawn_form(report=_forkable())
    pick(wiz, "fork", "yes")
    assert "forking the parent's conversation" in wiz.summary()
    args = argparse.Namespace()
    wiz.apply(args)
    assert args.fork is True


def test_the_spawn_form_says_whose_child_it_is_making():
    wiz = spawn_form()
    assert wiz.summary().startswith("spawning: child of lead")
    assert "mesh team" in wiz.summary()


def test_the_spawn_form_reads_like_the_command():
    wiz = spawn_form()
    screen = "\n".join(wiz.render(90, 30))
    assert "claunch spawn" in screen
    for label in ("Parent", "Name", "Harness", "Workspace", "Mesh", "Role",
                  "Workflow", "Opening task", "Spawn child"):
        assert label in screen


def test_spawn_runs_the_wizard_before_it_asks_the_daemon_for_anything(monkeypatch):
    monkeypatch.setattr(
        cli_sessions.daemon_client, "ensure_running",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("the daemon must not be asked to spawn anything")
        ),
    )
    seen = {}

    def fake(args, *, spawn=False):
        seen["spawn"] = spawn
        return False

    monkeypatch.setattr(cli_sessions, "_run_wizard", fake)
    from claude_launcher import cli

    args = cli.build_parser().parse_args(["spawn", "--wizard"])
    assert args.wizard is True
    assert cli_sessions._cmd_spawn(args) == 1
    assert seen["spawn"] is True


def test_the_spawn_form_is_for_the_person_the_parent_picker_exists_for(monkeypatch):
    """An agent has its parent in $CLAUNCH_SESSION and needs no list; a form
    painted into its PTY would hang the child it was creating."""
    monkeypatch.setenv("CLAUNCH_SESSION", "lead")
    with pytest.raises(wizard.WizardUnavailable):
        cli_sessions._run_wizard(argparse.Namespace(), spawn=True)


# --------------------------------------------------------------------------- #
# reusing a checkout, from either form
# --------------------------------------------------------------------------- #
def test_only_a_reused_worktree_can_be_out_of_date():
    """A fresh one is cut from the repository as it stands, so asking whether
    to update it would be asking about nothing."""
    wiz = form()
    assert wiz.field("update").hidden
    pick(wiz, "worktree", "new worktree")
    assert wiz.field("update").hidden
    pick(wiz, "worktree", "review")
    assert not wiz.field("update").hidden


def test_saying_yes_to_the_update_lands_on_the_branch_to_catch_up_with():
    wiz = form()
    pick(wiz, "worktree", "review")
    pick(wiz, "update", "yes")
    assert wiz.current.key == "rebase_onto"
    assert not wiz.field("rebase_onto").hidden
    # the branch this checkout is cut from leads the list
    assert wiz.field("rebase_onto").options[0].value == "master"
    assert "cut from" in wiz.field("rebase_onto").options[0].detail
    assert [o.value for o in wiz.field("rebase_onto").options] == [
        "master", "topic", "old-thing"
    ]


def test_the_base_is_a_picker_not_a_fixed_branch():
    wiz = form()
    pick(wiz, "worktree", "review")
    pick(wiz, "update", "yes")
    pick(wiz, "rebase_onto", "topic")
    args = argparse.Namespace()
    wiz.apply(args)
    assert args.worktree == "review"
    assert args.rebase_onto == "topic"


def test_no_update_sends_no_base_at_all():
    wiz = form()
    pick(wiz, "worktree", "review")
    args = argparse.Namespace()
    wiz.apply(args)
    assert args.worktree == "review"
    assert args.rebase_onto == ""
    assert "rebased" not in wiz.summary()


def test_the_summary_names_the_branch_it_will_catch_up_with():
    wiz = form()
    pick(wiz, "worktree", "review")
    pick(wiz, "update", "yes")
    assert "worktree review" in wiz.summary()
    assert "rebased onto master" in wiz.summary()


# --------------------------------------------------------------------------- #
# a child's own checkout
# --------------------------------------------------------------------------- #
def test_a_child_can_be_given_a_checkout_of_its_own():
    wiz = spawn_form()
    wt = wiz.field("worktree")
    assert wt.selectable
    labels = [o.label for o in wt.options]
    assert labels[0].startswith("(none)")
    assert "new worktree" in labels
    assert "review" in labels  # already on disk beside the parent's


def test_the_childs_worktree_is_named_here_not_by_the_daemon():
    """`new-session` names an unnamed worktree after the Herdr pane; the
    daemon that cuts a child's has no pane, so the form answers instead."""
    wiz = spawn_form()
    focus_on(wiz, "name")
    for ch in "helper":
        wiz.handle(ch)
    wiz.handle("enter")
    auto = wiz.auto_worktree_name()
    assert auto.startswith("helper-")
    assert "auto-named: helper-" in [
        o.detail for o in wiz.field("worktree").options if o.value == ""
    ][0]
    pick(wiz, "worktree", "new worktree")
    args = argparse.Namespace()
    wiz.apply(args)
    assert args.worktree == auto          # a NAME, never a path
    assert "\\" not in args.worktree and "/" not in args.worktree


def test_without_a_name_the_childs_worktree_is_named_after_its_parent():
    wiz = spawn_form()
    assert wiz.auto_worktree_name().startswith("lead-")


def test_the_policy_can_grey_out_the_childs_worktree():
    wiz = spawn_form(report={
        "can_spawn": True, "blocked_by": [], "depth": 0, "max_depth": 3,
        "children_used": 0, "children_remaining": 4,
        "may_choose": [], "spawnable_harnesses": [],
    })
    wt = wiz.field("worktree")
    assert not wt.selectable
    assert "spawn.allow_worktree" in wt.disabled_note
    args = argparse.Namespace()
    wiz.apply(args)
    assert args.worktree is None


def test_a_childs_update_catches_up_with_its_parents_branch():
    """'rebase onto the parent's branch' is the same rule as new-session's
    'the branch you came from' -- the one the checkout is cut from."""
    wiz = spawn_form()
    pick(wiz, "worktree", "review")
    pick(wiz, "update", "yes")
    assert wiz.field("rebase_onto").options[0].value == "master"
    args = argparse.Namespace()
    wiz.apply(args)
    assert args.rebase_onto == "master"


def test_a_childs_worktree_follows_the_workspace_it_was_sent_to():
    """Cut from where the child will actually run, so the branch to catch up
    with is that repository's, not the parent's."""
    wiz = spawn_form()
    pick(wiz, "workspace", "api")
    pick(wiz, "worktree", "review")
    pick(wiz, "update", "yes")
    assert wiz.field("rebase_onto").options[0].value == "api-main"


def test_a_parent_outside_a_repository_is_offered_no_worktree():
    wiz = spawn_form(repo=False)
    assert wiz.field("worktree").hidden
    assert wiz.field("update").hidden
    assert wiz.field("rebase_onto").hidden


# --------------------------------------------------------------------------- #
# who the Parent picker offers, and in what order
# --------------------------------------------------------------------------- #
FLEET = [
    {"name": "s10", "status": "exited", "profile": "nc", "cwd": "F:/works/gds5"},
    {"name": "s11", "status": "idle", "profile": "nc", "cwd": "F:/works/gds5"},
    {"name": "s9", "status": "busy", "profile": "nc", "cwd": "F:/works/gds5"},
]


def test_a_session_that_cannot_spawn_does_not_lead_the_picker():
    """The daemon refuses a child of an exited session outright, so offering
    one as the default is offering a launch that is already lost."""
    wiz = spawn_form(sessions=FLEET)
    values = [o.value for o in wiz.field("parent").options]
    assert values == ["s9", "s11", "s10"]
    assert wiz.value("parent") == "s9"


def test_names_are_ordered_as_numbers_not_as_strings():
    """Past nine sessions a string sort puts the tenth between the first and
    the second, which reads as no order at all."""
    fleet = [
        {"name": f"s{n}", "status": "idle", "cwd": "/w"} for n in (9, 10, 11, 2)
    ]
    wiz = spawn_form(sessions=fleet)
    assert [o.value for o in wiz.field("parent").options] == \
        ["s2", "s9", "s10", "s11"]


def test_an_exited_session_is_shown_and_unpickable_with_the_way_out():
    wiz = spawn_form(sessions=FLEET)
    exited = [o for o in wiz.field("parent").options if o.value == "s10"][0]
    assert exited.disabled
    # Shown, not hidden: it is a respawn away from being a usable parent, and
    # a session missing from the list reads as gone.
    assert "respawn" in exited.detail


def test_the_picker_skips_past_a_session_that_cannot_spawn():
    wiz = spawn_form(sessions=FLEET)
    focus_on(wiz, "parent")
    wiz.handle("right")
    assert wiz.value("parent") == "s11"
    wiz.handle("right")          # s10 is next, and unpickable
    assert wiz.value("parent") == "s11"


def test_your_own_session_leads_and_says_so(monkeypatch):
    """`spawn` inside a session means *this* session, so the form opens on the
    answer the bare command would have given."""
    monkeypatch.setenv("CLAUNCH_SESSION", "s11")
    wiz = spawn_form(sessions=FLEET)
    assert [o.value for o in wiz.field("parent").options][0] == "s11"
    assert wiz.value("parent") == "s11"
    assert wiz.field("parent").options[0].label == "s11 (you)"


def test_your_own_session_does_not_lead_when_it_cannot_spawn(monkeypatch):
    """An exited caller is still refused, so the form must not open on it."""
    monkeypatch.setenv("CLAUNCH_SESSION", "s10")
    wiz = spawn_form(sessions=FLEET)
    assert wiz.value("parent") == "s9"
    assert wiz.field("parent").options[0].value == "s10"  # still first, greyed
    assert wiz.field("parent").options[0].disabled


def test_an_all_exited_fleet_offers_nothing_pickable():
    wiz = spawn_form(sessions=[
        {"name": "s1", "status": "exited", "cwd": "/w"},
        {"name": "s2", "status": "exited", "cwd": "/w"},
    ])
    assert all(o.disabled for o in wiz.field("parent").options)


def test_an_explicit_parent_still_wins_over_the_ordering():
    wiz = spawn_form(sessions=FLEET)
    assert wiz.field("parent").select("s11")
    assert wiz.value("parent") == "s11"


@pytest.mark.parametrize(
    "names, expected",
    [
        (["s10", "s9"], ["s9", "s10"]),
        (["b", "a"], ["a", "b"]),
        (["w2p10", "w2p9"], ["w2p9", "w2p10"]),
        (["x", "x1"], ["x", "x1"]),
    ],
)
def test_natural_sort_key(names, expected):
    assert sorted(names, key=wizard._natural) == expected


# --------------------------------------------------------------------------- #
# the board rows
# --------------------------------------------------------------------------- #
def test_the_board_question_has_three_answers_and_only_one_opens_the_picker():
    wiz = form()
    mode = wiz.field("beads")
    assert isinstance(mode, wizard.ChoiceField)
    assert [o.value for o in mode.options] == [
        wizard.BEADS_NEW, wizard.BEADS_PICK, wizard.BEADS_NONE,
    ]
    # minting from the task is what every release before this did, so it is
    # what a form nobody touched still answers
    assert mode.value == wizard.BEADS_NEW
    assert wiz.field("issue").hidden

    pick(wiz, "beads", "an existing issue")
    assert not wiz.field("issue").hidden
    pick(wiz, "beads", "no issue")
    assert wiz.field("issue").hidden


def test_the_issue_text_row_belongs_to_the_answer_that_mints():
    """Each of the two conditional rows is a question under exactly one
    answer, and the text row's answer is the one the form arrives on — so it
    is the row that is visible before anybody touches anything."""
    wiz = form()
    text = wiz.field("issue_text")
    assert isinstance(text, wizard.TextField)
    assert not text.hidden and wiz.field("issue").hidden

    pick(wiz, "beads", "an existing issue")
    assert text.hidden and not wiz.field("issue").hidden
    pick(wiz, "beads", "no issue")
    assert text.hidden


def test_the_text_row_keeps_what_was_typed_while_the_answer_is_tried_out():
    """Hidden, not cleared. Somebody who writes a specification, looks at the
    other two answers and comes back must find their words where they were —
    a row that emptied itself would lose them without saying so."""
    wiz = form()
    type_into(wiz, "issue_text", "Rail must answer the board")
    pick(wiz, "beads", "no issue")
    pick(wiz, "beads", "new issue")
    assert wiz.value("issue_text") == "Rail must answer the board"
    args = argparse.Namespace()
    wiz.apply(args)
    assert args.issue_text == "Rail must answer the board"


def test_only_the_answer_that_mints_sends_a_text():
    """The daemon refuses a request that carries two board answers at once
    (beads.check_request), so a form that let a hidden row's leftovers ride
    along would turn the other two answers into 400s."""
    for answer in ("an existing issue", "no issue"):
        wiz = form()
        type_into(wiz, "issue_text", "left behind")
        pick(wiz, "beads", answer)
        args = argparse.Namespace()
        wiz.apply(args)
        assert args.issue_text is None


def test_the_closing_line_says_which_of_the_two_boxes_the_issue_came_from():
    """The form vanishes with the alternate screen, so this line is the only
    trace of where to go looking for what the session was actually asked."""
    wiz = form()
    assert "new issue from the task" in wiz.summary()
    type_into(wiz, "issue_text", "the real spec")
    assert "new issue, written here" in wiz.summary()


def test_the_issue_picker_says_which_rows_would_only_be_joined():
    wiz = form()
    pick(wiz, "beads", "an existing issue")
    rows = {o.value: o for o in wiz.field("issue").options}
    assert set(rows) == {"cl-1", "cl-2"}
    assert rows["cl-1"].label.startswith("cl-1")
    assert "wire the rail" in rows["cl-1"].label
    assert "JOIN" not in rows["cl-1"].detail
    # the one a running session holds says so, and says what it means
    assert "held by lead" in rows["cl-2"].detail
    assert "JOIN, not assign" in rows["cl-2"].detail


def test_the_three_answers_travel_as_the_flags_the_command_takes():
    wiz = form()
    args = argparse.Namespace()
    wiz.apply(args)
    assert args.issue is None and args.no_issue is False  # mint one

    wiz = form()
    pick(wiz, "beads", "no issue")
    args = argparse.Namespace()
    wiz.apply(args)
    assert args.issue is None and args.no_issue is True

    wiz = form()
    pick(wiz, "beads", "an existing issue")
    pick(wiz, "issue", "cl-2")
    args = argparse.Namespace()
    wiz.apply(args)
    assert args.issue == "cl-2" and args.no_issue is False
    # and the closing line says it is a JOIN, on this form too — the form
    # vanishes with the alternate screen, so this is the only trace left
    assert "issue cl-2" in wiz.summary() and "held by lead" in wiz.summary()
    assert "issue cl-2" in wiz.summary()
    assert "held by lead" in wiz.summary()


def test_flags_typed_alongside_the_wizard_prefill_the_board_rows():
    wiz = wizard.Wizard(
        FakeSources(), cwd="/work/repo",
        defaults=argparse.Namespace(issue="cl-2", no_issue=False),
    )
    assert wiz.value("beads") == wizard.BEADS_PICK
    assert wiz.value("issue") == "cl-2"

    wiz = wizard.Wizard(
        FakeSources(), cwd="/work/repo",
        defaults=argparse.Namespace(issue=None, no_issue=True),
    )
    assert wiz.value("beads") == wizard.BEADS_NONE
    args = argparse.Namespace()
    wiz.apply(args)
    assert args.no_issue is True


def test_an_id_this_board_does_not_have_is_kept_and_marked_rather_than_dropped():
    """A preset the daemon cannot see must not be silently replaced by
    whatever happens to be first in the list."""
    wiz = wizard.Wizard(
        FakeSources(), cwd="/work/repo",
        defaults=argparse.Namespace(issue="elsewhere-9", no_issue=False),
    )
    row = wiz.field("issue").options[0]
    assert row.value == "elsewhere-9" and row.detail == "not on this board"
    args = argparse.Namespace()
    wiz.apply(args)
    assert args.issue == "elsewhere-9"


def test_the_board_is_read_for_the_directory_that_is_actually_picked():
    src = FakeSources()
    wiz = form(sources=src)
    pick(wiz, "beads", "an existing issue")
    # the Directory row absolutises what it holds, so compare the way the
    # form itself does rather than to the literal the test typed
    assert src.issue_calls[-1] == (wiz.value("cwd"), "")
    pick(wiz, "cwd", "api")
    assert src.issue_calls[-1] == (wiz.value("cwd"), "")
    assert src.issue_calls[-1][0].endswith("api")
    # and not re-read on every keystroke while the answer it follows stands
    before = len(src.issue_calls)
    wiz.handle("down")
    assert len(src.issue_calls) == before


def test_a_spawn_asks_the_board_of_the_directory_the_child_lands_in():
    src = FakeSpawnSources()
    wiz = spawn_form(sources=src)
    pick(wiz, "beads", "an existing issue")
    # no workspace: the child inherits the parent's directory, so the daemon
    # is asked by parent and resolves it the same way the spawn will
    assert src.issue_calls[-1] == ("/work/repo", "lead")
    pick(wiz, "workspace", "api")
    assert src.issue_calls[-1] == ("/srv/api", "")

    pick(wiz, "issue", "cl-2")
    args = argparse.Namespace()
    wiz.apply(args)
    assert args.issue == "cl-2" and args.no_issue is False
