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
rail's bulk bar — which of stop/resume/clear/delete is offered on a given
rail, and what each claims it would touch — ``forceclear_check`` on the modal
that replaced the browser dialogs there, and on the forced clear/delete it
can send past a mesh hold — ``owed_check`` on
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
attached session, and whose keyboard it blames for the hold — ``seq_check`` on the
message trace's event list — the order a mesh's traffic is read in, and what
the picture is allowed to claim about where each message got to —
``seqrender_check`` on the sequence that list is drawn into, ``zoom_check`` on
the header's text-size knob — which sizes the session's grid and not merely
this tab's view — ``reconnect_check`` on the terminal's link, the one thing
here that is not a poll and so has to repair itself deliberately,
``wheel_check`` on the terminal's virtual scroll — the wheel on the
alternate screen (where xterm's own scrollback is empty and its fallback
is arrow keys) becoming ``scroll`` controls answered with repaints over
the daemon's history, and the chip that says so, ``termcache_check`` on the
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
(digest first, the opening task as fallback) and the collapsed ⟳ that
refreshes without opening — ``spawnform_check`` on the create form
turned into a spawn — what a child may be asked once a parent is named, and
when the parent's conversation is on offer to fork — and the two
``flow*_check`` harnesses on the flow view — one on the track a workflow
becomes, one on the page those tracks are drawn into — and the two context
harnesses: ``ctxsize_check`` on how full a session's conversation is said to
be (a count with no percentage beside it, because no denominator exists to
make one from, and an absence that must never be drawn as a zero) and
``railctx_check`` on where that ends up on a rail row, which is a separate
failure — a note that stops being hung on anything leaves no trace at all —
and ``detailsplit_check`` on the docked detail rail's width: the drag bar
opposite the session list's, which resizes the ``#sess-view`` column once it
docks and is carried with it and hidden on a phone, and ``railhome_check``
on the rail's own wordmark, which doubles as the link back to the root
route ("#/") and so has to look like a title and not like a browser's
default link.
This wrapper is what makes them run with everything else.

Skipped, not failed, where node is unavailable: node is a convenience for
testing this project, never a requirement for using it.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parent / "web"


@pytest.mark.parametrize(
    "script",
    [
        "layout_check.js",
        "render_check.js",
        "lineage_check.js",
        "railbadge_check.js",
        "sessmesh_check.js",
        "raillayout_check.js",
        "railsplit_check.js",
        "detailsplit_check.js",
        "bulk_check.js",
        "forceclear_check.js",
        "owed_check.js",
        "panel_check.js",
        "sesssend_check.js",
        "sessrun_check.js",
        "wfstart_check.js",
        "wheel_check.js",
        "sesslayout_check.js",
        "rolepanel_check.js",
        "spawnmodal_check.js",
        "queued_check.js",
        "seq_check.js",
        "seqrender_check.js",
        "zoom_check.js",
        "reconnect_check.js",
        "termcache_check.js",
        "briefcard_check.js",
        "briefingtop_check.js",
        "briefrow_check.js",
        "spawnform_check.js",
        "selectpopup_check.js",
        "flowtrack_check.js",
        "flowrender_check.js",
        "ctxsize_check.js",
        "railctx_check.js",
        "wfscroll_check.js",
        "railhome_check.js",
        "typing_check.js",
    ],
)
def test_topology_diagram_logic(script):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    proc = subprocess.run(
        [node, str(WEB / script)],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
