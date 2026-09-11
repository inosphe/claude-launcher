"""The dashboard's client-side logic, run under node.

The dashboard has no build step and no JS test runner, which was fine while
`app.js` was mostly wiring. The clustered diagram is not: it derives a forest
from parent handles, packs it into columns, places boxes on a ring and clips
edges to them, and none of that is checked by anything Python can see. Nor is
the sidebar's own forest, which orders the session list by lineage.

So the harnesses in ``tests/web`` slice the real functions out of the shipped
``app.js`` — not a copy of them — and exercise them: ``layout_check`` on the
maths, ``render_check`` on the SVG the drawing code assembles against a stub
DOM, ``lineage_check`` on the session list's tree ordering, ``railbadge_check`` on
the cflow line each rail row carries — which run it speaks for, and when it is
flagged as the reader's move rather than a peer's — ``sessmesh_check`` on
the mesh tags beside each row's name, which come from the mesh poll rather
than the session one and must not claim another daemon's rooms —
``raillayout_check`` on what those tags cost: a rail row carries every session
at once, so the vertical room one takes is multiplied by twenty, and the check
holds the name, its role and its rooms to a single shrinking line — pinning
which children are allowed to break the row, and that a tag may be
abbreviated but never shrunk to a capsule with nothing in it — ``bulk_check`` on the
rail's bulk bar — which of stop/resume/archive is offered on a given rail,
and what each claims it would touch — ``killstate_check`` on the terminal's
kill action — immediate request feedback, the wind-down escalation action,
the final exit wait, and API error recovery kept identical on desktop and
mobile — ``sessionfilters_check`` on the
current/running/killed/archived partitions and their retained selection — ``owed_check`` on
the Unanswered box and the requests its nudge/dismiss buttons send,
``panel_check`` on where the session detail docks (the rail beside the
terminal, or the page slot on a phone) and what closing it leaves behind,
``sesssend_check`` on the message that panel can put into the mesh — whose
handle it lands on and what it does with a refusal — ``sessrun_check`` on the
run it can fold open in place of a trip to the run page, ``wfstart_check`` on
the workflow picker's option line — clipped so a paragraph-length description
cannot drag the native popup past the viewport edge — ``sesslayout_check``
on the per-session layout — the Details/Workflow radio, the run pane halved
into the terminal's column, and the localStorage both are remembered in —
``rolepanel_check`` on the panels a session gets for its ROLE — which roles
claim which panels, the leader's quick-job dispatch (its pickers now seed the
spawn wizard rather than POSTing, and how a refused policy keeps the button
dead) and its reaping nudge, which reports idle children into the leader's own
terminal rather than killing anything — ``spawnmodal_check`` on the spawn
wizard as a dialog — the rail's +, the detail panel's Spawn button and the
leader's quick job all landing in it with the opener pinned as the parent; the
brain's gating (a policy-locked row greys with the key that opens it) and the
payload it emits reading through the disables, plus what the modal remembers
between spawns — ``queued_check`` on
the queued-deliveries banner — the backlog the daemon is holding for the
attached session, and whose keyboard it blames for the hold —
``window_check`` on the read-only measurement Window page — holders, FIFO
positions, capacity, owner attribution and the two-second poll lifecycle —
``backpressure_check`` on the one state none of those three can show:
past the mesh's cap the daemon stops ACCEPTING mail for a session, so
the backlog stops growing and every field the page had before reads as
calm. Its checks are about precedence (a shut door outranks the timing
holds in the header chip, but never ``exited``, nor a person's own pin,
which the chip is also the button for) and about drawing with nothing to
list — the panel box has to appear on refusals alone, with an empty
queue — ``seq_check`` on the
message trace's event list — the order a mesh's traffic is read in, and what
the picture is allowed to claim about where each message got to —
``seqrender_check`` on the sequence that list is drawn into, ``zoom_check`` on
the header's text-size knob — which sizes the session's grid and not merely
this tab's view — ``reconnect_check`` on the terminal's link, the one thing
here that is not a poll and so has to repair itself deliberately,
``notice_check`` on the corner strip that is the page's only voice — the
daemon restart that used to be repaired in complete silence, told apart
from a link that merely blipped, and kept from stacking a card per flap —
``wheel_check`` on who owns the terminal's wheel — a program that took the
mouse (claude does, and then scrolls its own view far deeper than any
scrollback we could keep) getting its ticks forwarded untouched, the main
buffer scrolling natively out of the scrollback the daemon seeds at attach,
and only the third case — the alternate screen with the mouse left alone —
still becoming ``scroll`` controls answered with repaints over the daemon's
history, with the chip that says so — ``transcript_check`` on the pane that
answers what none of those three can, "what did this session say an hour ago":
claude's own conversation jsonl, served in pages and read in an ordinary
overflow scroller, so the browser owns the wheel. Its checks are mostly about
not disturbing the reader — prepending an older page must leave the text they
are looking at exactly where it was, and the follow-forward must move only
someone already sitting at the bottom — ``termcache_check`` on the
keep-alive that makes switching between sessions cheap — the terminal you
walk away from stays up with its socket shimmed to buffer-only, and
returning to it is a swap of state and a re-fit rather than a new socket
and a full-screen repaint —, ``briefcard_check`` on the rail's briefing
card — the session summary a row
folds open, and which of the daemon's answers (a briefing, unshaped prose,
no LLM, no record) it is showing — ``briefingtop_check`` on the same card's
two other homes, the top header's toggle (whose pane sits between the
header and the terminal) and the detail panel's section, bound to the same
open-set and off-state — ``briefrow_check`` on the row's always-on face —
the one-line job description the /api/sessions poll pours into every row
(the briefing digest — the recorded opening task is detail-panel-only) and
the collapsed ⟳ that refreshes without opening — ``railhold_check`` on why those two glyphs used
to swallow a press: changed session data rebuilds the rail, and a rebuild
landing between a pointerdown and its pointerup takes the pressed node out
of the document, leaving the browser no common ancestor to send the click
to. It pins that a poll arriving mid-press leaves the rows as the SAME
objects rather than equal ones, that the data still lands, that the skipped
redraw is paid back after the click and not before it, that the hold expires
on its own so a lost pointerup cannot freeze the rail, and that a failed poll
is not read as "this daemon has no sessions" — ``railscroll_check`` on what
that same rebuild does to the reader's place in the rail: ``#session-list``
is itself the scrolling element, so emptying it drops its content height to
zero and the browser clamps the scroll position to 0 with it, which is how a
rail scrolled halfway down ended up back at the top every time any session
changed state. It pins that a rebuild keeps the position, that a shorter
fleet lands at the new bottom instead of the top, that an unchanged poll and
a held one move nothing, and that a reduced DOM with no scroll geometry
still draws — ``newform_check`` on that same form's reading
order — the arrangement rows (parent, mesh, role, workflow) asked before the
machinery rows, which fold shut under them and must hold exactly what a child
inherits, and the opening task left alone at the bottom, with the summary line
that keeps the folded directory visible — ``spawnform_check`` on the create form
turned into a spawn — what a child may be asked once a parent is named
(the spawn policy's answer, per parent, rather than the form's own), what
its payload therefore carries, the soft child cap offered as a crossing,
and when the parent's conversation is on offer to fork — ``newflow_check``
on the same form's Role and Workflow rows, where picking a role picks the
workflow that volunteers for it and a pick made by hand outlives every
later role change — ``pollselect_check`` on what the two-second session poll
is allowed to do to that form's resume and parent pickers, which it feeds:
rebuilding a ``<select>`` shuts the native popup, so a user two seconds into
reading a list has it vanish under the cursor unless the rebuild is guarded
by a signature — and the signature cannot simply be the poll's answer, since
a session record carries a pid and a clock that move on their own, so each
picker signs the fields its own rebuild reads (the parent one wider than its
label, because its tail greys rows from the picked parent's harness) —
``spawnsize_check`` on how big the spawn DIALOG is
(the modal, not this form): the
grip the stylesheet draws, the size the browser remembers between opens,
and the shared ``.modal-box`` a dragged-wide form must hand back so the
next confirm dialog is not 900px of prose — and the two
``flow*_check`` harnesses on the flow view — one on the track a workflow
becomes, one on the page those tracks are drawn into — and the two context
harnesses: ``ctxsize_check`` on how full a session's conversation is said to
be (a count with no percentage beside it, because no denominator exists to
make one from, and an absence that must never be drawn as a zero) and
``railctx_check`` on where that ends up on a rail row, which is a separate
failure — a note that stops being hung on anything leaves no trace at all —
and ``railmodel_check`` on the third fact the same reading carries, which
until now only tooltips ever showed: WHICH MODEL the session is answering
on, shortened onto the rail row beside the gauge and spelled out in full in
the detail panel, where a version number torn in half ("haiku 4 5") or a row
that quietly stops being appended would both name the wrong thing in
silence — ``railcwd_check`` on the row's directory line, WHERE the session
runs: a worktree of this launcher's own making reads as "repository ›
checkout" rather than the "…/worktrees/<name>" the tail-of-path shortening
would print (one constant word kept, the one that differs dropped), the
whole path stays a hover away, the line is a full-width child of the row
placed under the name and before the gauge, and the detail panel's head
carries the same line under its name in every arrangement (the Details
list's ``directory`` row is seven rows down and absent from the Workflow
tab) — ``railprofile_check`` on the row's meta line, WHO the session runs
as: the profile (harness fallback, ``PROFILE/HARNESS`` canonical form) plus
a borrow's lender when one is borrowed, with the state — "exit N", or
"winding down" — joined to the identity instead of replacing it (an exited
row used to say only its exit code), the stylesheet cap that keeps the
longer line off the name, and the full text recoverable from the element's
title — and ``detailsplit_check`` on the docked detail rail's width: the drag bar
opposite the session list's, which resizes the ``#sess-view`` column once it
docks and is carried with it and hidden on a phone, and ``railhome_check``
on the rail's own wordmark, which doubles as the link back to the root
route ("#/") and so has to look like a title and not like a browser's
default link, and ``gotocard_check`` on the header's ``⇱ card`` — the one
control up there that moves the READER rather than the session, back to the
rail row for the terminal they are sitting in. Both of its failures are
invisible in a screenshot: scrolling to the wrong row (the lit row and the
attached name are different questions) and the mark being wiped half a
second later by the 2s poll that rebuilds the rail whole — which is why the
mark is held outside the DOM and repainted after every rebuild.

``railseen_check`` on the fourth line a rail row spends, and the only one
about the reader rather than the session: WHO HAS BEEN NEAR IT — last looked
at, last typed into, last moved on its own. Three facts that a lazier line
would fold into one "active" word, and folding them is exactly the failure
worth a harness: a session grinding away with nobody watching and a session
watched all afternoon while its agent has not moved since lunch are the two
rows an operator is actually hunting for, and only the GAP between the three
readings tells them from the eighteen that are fine. So the checks pin each
pair to its own field and several pin what a pair must not react to — that a
viewer count does not make the screen look busy, that "typed" does not drift
onto the visit stamp — plus the two shapes that are not durations at all: the
green "now" that means somebody has the terminal open this second (no stamp
taken in the past can say that), and the dash that means no reading, drawn on
every row whether or not it has one so the three columns stay where the eye
left them. It also holds the red stale state on each reading at its own
threshold: one hour for ``seen``, half an hour for ``typed``, five minutes
for ``moved`` — moved is the earliest because it is the reading an operator
most needs to catch, a session that went quiet mid-task. All three are kept
in a ``railStale`` object rather than fixed constants, editable from a
Settings card and remembered per browser, so the same checks parametrize on
whatever thresholds ``railStale`` currently holds instead of a literal hour
or half hour.

``railtimer_check`` on the nudge countdown's shared vocabulary — the clock
for the daemon's automatic nudge, which is the one thing on this dashboard
nobody could see coming: two clocks can produce it (a reminder into a
session that is working, a stall ping into one that stopped), at most one of
them applies at a time, and the check holds the pick to the clock a reader
needs plus the several silences — off, not running, held, gated — that a
lazier chip would collapse into one word. It also guards the monotony that
made the rail's old strip a duplicate: no timer strip ships in the rail —
the countdown lives on the session's own header. ``termtimer_check`` on
that chip, where the clock has a subject: it shows that session's own run
or nothing, with no fallback to whichever run fires soonest — a stranger's
clock would be a lie beside one session's name — its reading is stamped
with the session it was taken for (a terminal switch repaints a second
before the poll comes round), and the three wirings that make it move at
all — the 2s poll, every attach path, and the one interval that ages it —
are held there too.

``wftime_check`` on the run page's second picture: the same journal the
state graph's neighbours print as prose, laid on a time axis. One lane per
step, one bar per visit, and inside each bar the stretches spent at a door
(a choice presented and not confirmed, a gate, a question with a peer, a
paced option held for its window) drawn apart from the stretches spent
working — because a step that took twenty minutes of work and a step that
took twenty minutes because nobody answered its gate are the same two lines
in the fold and must never be the same bar here. It pins the interval model
(delivery closes the standing bar, since `state_forced` and a resumed run
both leave a step without completing it), the right edge (a live run owns
the axis out to now, a finished one stops where its record does), that a
door is counted INTO its step's total and drawn apart from it, that a door
the record never closes ends with its step rather than at the end of the
axis, and that the lanes are the graph's own rows — both pictures call
``wfStepOrder`` so that a step sits on the same line in each. Its two
sharpest fixtures are transcribed out of this repository's own runs, because
the shape a hand-made journal has is not the shape the engine writes: a door
is journalled BETWEEN two steps and names the step being entered, and a step
that is nothing but a choice is never delivered at all — so the door is the
only thing that ever puts the run there, and a model that waits for a
delivery draws twenty real minutes of a leader's `standby` as "never
entered".

``leak_check`` is the odd one out and deliberately so: instead of slicing a
function it boots the WHOLE of ``app.js`` against a stub browser built by
parsing the shipped ``index.html``, then runs the page the way an unattended
tab runs it — four hundred poll ticks, forty terminals walked between, sixty
sessions spawned and killed, a hundred dropped links — and holds the census
afterwards to the census before. Live DOM nodes, live listeners, live
sockets, live xterm objects, live timers and every cache keyed by session
name. Nothing in a single tick is wrong when this class of bug is present;
what is wrong is that the same correct tick leaves something behind, five
thousand times a day, and only a before/after count can see that.

``checklist_check`` and ``meshmessages_check`` are here late and for a
reason worth leaving on the record: both existed, both passed, and neither
was in the list below, so for weeks nothing ran them. ``checklist_check``
holds the checklist gate's card — that `false` and "could not measure" stay
two different facts on the page, and that the card never grows an Approve
button, since that gate is the one stop on the run page no person may
grant. ``meshmessages_check`` holds the mesh log's tabs and pager: an
archived conversation stays off the default view without the operator losing
the route to older records.

This wrapper is what makes them run with everything else — and
``test_every_harness_is_registered`` is what makes the list below match the
directory, so the next harness cannot arrive the way those two did.

Skipped, not failed, where node is unavailable: node is a convenience for
testing this project, never a requirement for using it.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parent / "web"


# Every harness in tests/web is named here, and `test_every_harness_is_registered`
# below holds that list to the directory: a file nobody listed is a file nobody
# runs, and it stays green in the only way that means nothing. Two files sat that
# way for weeks, both passing when run by hand (claunch-som9.1).
CHECKS = [
        "layout_check.js",
        "render_check.js",
        "lineage_check.js",
        "sessionfilters_check.js",
        "sessiongroupsticky_check.js",
        "sessiongroupstack_check.js",
        "railbadge_check.js",
        "sessmesh_check.js",
        "sesshandle_check.js",
        "raillayout_check.js",
        "railsplit_check.js",
        "detailsplit_check.js",
        "bulk_check.js",
        "killstate_check.js",
        "owed_check.js",
        "meshmessages_check.js",
        "panel_check.js",
        "sesssend_check.js",
        "reborrow_check.js",
        "borrowform_check.js",
        "sessrun_check.js",
        "wfstart_check.js",
        "wheel_check.js",
        "transcript_check.js",
        "sesslayout_check.js",
        "rolepanel_check.js",
        "spawnmodal_check.js",
        "queued_check.js",
        "window_check.js",
        "sendinput_check.js",
        "promptpresets_check.js",
        "holdchip_check.js",
        "backpressure_check.js",
        "seq_check.js",
        "seqrender_check.js",
        "zoom_check.js",
        "reconnect_check.js",
        "snapshot_check.js",
        "notice_check.js",
        "restartgate_check.js",
        "termcache_check.js",
        "briefcard_check.js",
        "briefingtop_check.js",
        "briefrow_check.js",
        "railhold_check.js",
        "railscroll_check.js",
        "railquiet_check.js",
        "spawnform_check.js",
        "newflow_check.js",
        "spawnsize_check.js",
        "selectpopup_check.js",
        "flowtrack_check.js",
        "flowrender_check.js",
        "ctxsize_check.js",
        "tps_check.js",
        "railctx_check.js",
        "railmodel_check.js",
        "railcwd_check.js",
        "wfscroll_check.js",
        "wfhead_check.js",
        "railhome_check.js",
        "gotocard_check.js",
        "gotogate_check.js",
        "wfgoto_check.js",
        "typing_check.js",
        "composition_check.js",
        "leak_check.js",
        "pingbox_check.js",
        "newform_check.js",
        "pollselect_check.js",
        "beads_check.js",
        "ragsearch_check.js",
        "beadskanban_check.js",
        "queues_check.js",
        "railbeads_check.js",
        "railbeadskanban_check.js",
        "wfdpace_check.js",
        "wfdtimer_check.js",
        "wfdarrow_check.js",
        "wftime_check.js",
        "wfsplit_check.js",
        "askdoor_check.js",
        "checklist_check.js",
        "railtimer_check.js",
        "termtimer_check.js",
        "mdrender_check.js",
        "railseen_check.js",
        "reports_check.js",
        "sesscommits_check.js",
        "sesstask_check.js",
        "railprofile_check.js",
        "railcompacting_check.js",
        "railkeys_check.js",
]


@pytest.mark.parametrize("script", CHECKS)
def test_topology_diagram_logic(script):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    proc = subprocess.run(
        [node, str(WEB / script)],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_every_harness_is_registered():
    """Every `*_check.js` in tests/web is named in CHECKS, and every name in
    CHECKS is a file that exists.

    CHECKS is what the suite runs; the directory is what the repository has.
    Nothing kept the two in step, so a harness added without touching this file
    simply never ran -- not skipped, not reported, absent, with the suite green
    because nothing asked for it. Two files sat that way, `checklist_check.js`
    and `meshmessages_check.js`; both passed when run by hand, which is what
    makes the gap expensive rather than obvious.

    The reverse direction costs one more line and is worth it: a name left in
    CHECKS after its file is renamed or deleted fails as `cannot locate ...`,
    and reading that as "the harness broke" sends the next person the wrong way.
    """
    assert len(set(CHECKS)) == len(CHECKS), "a harness is listed twice in CHECKS"
    listed = set(CHECKS)
    present = {path.name for path in WEB.glob("*_check.js")}
    assert not present - listed, (
        "harness files that nothing runs -- add them to CHECKS: "
        + ", ".join(sorted(present - listed))
    )
    assert not listed - present, (
        "CHECKS names a file that is not in tests/web: "
        + ", ".join(sorted(listed - present))
    )
