"""One colour per state, worn by every surface that draws it.

The rail, the category page and the beads badges each coloured a session's
life and an issue's status on their own, and they drifted: ``delayed`` was
amber on the category page and purple in the hive, ``paused`` purple there
and blue here, ``in_ready`` green on a beads badge and cyan on a hive chip.
A word that means one thing should read as one colour, so the pairs live in
one ``:root`` block and every surface reads them.

These tests pin the three halves of that: the palette exists and is complete,
each surface reads the palette instead of writing its own hex, and the
partitions the page filters by are the ones the palette names — a life the
daemon can report with no colour would render as an unstyled chip nobody
notices.
"""

from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "src" / "claude_launcher" / "web" / "static"

# daemon/session.py session_category, the partitions the category page's filter
# bar offers as boxes (category-page.js CATEGORY_LIVES).
LIVES = ("running", "hold", "paused", "delayed", "failed", "killed", "archived")
# The bead statuses a badge can wear: beadsStatusBadge maps in_progress -> busy
# and closed -> exited, so those are the class names to look for.
ISSUE_STATUSES = ("open", "ready", "busy", "review", "blocked", "exited")
# ...and the palette names them for what they are, which is what the tokens are
# called: the badge's `exited` is a closed issue.
ISSUE_TOKENS = ("open", "ready", "busy", "review", "blocked", "closed")


def css() -> str:
    return (STATIC / "style.css").read_text(encoding="utf-8")


def palette() -> dict[str, str]:
    """Every custom property declared in the `:root` palette block."""
    block = re.search(r"/\* ---- One palette for a session's life.*?\n:root \{(.*?)\n\}", css(), re.S)
    assert block, "the palette block is gone; the surfaces below still read its properties"
    return dict(re.findall(r"(--[a-z0-9-]+):\s*(#[0-9a-fA-F]{3,8})", block.group(1)))


def rule(selector: str) -> str:
    """The declaration block of one exact selector."""
    match = re.search(rf"(?m)^{re.escape(selector)}\s*\{{([^}}]*)\}}", css())
    assert match, f"no rule for {selector}"
    return match.group(1)


def test_the_palette_names_every_life_and_issue_status() -> None:
    tokens = palette()
    # Five life pairs: delayed is a hold with a relaunch scheduled and
    # killed/archived are both "done" -- the surfaces separate those by shape,
    # not by a sixth colour.
    for name in ("running", "hold", "paused", "failed", "done"):
        assert f"--life-{name}" in tokens, name
        assert f"--life-{name}-rim" in tokens, name
    for name in ISSUE_TOKENS:
        assert f"--issue-{name}" in tokens, name
        assert f"--issue-{name}-bg" in tokens, name


def test_every_life_the_page_filters_by_has_a_chip_colour() -> None:
    tokens = palette()
    for life in LIVES:
        # The chip rule names the token; the class itself keeps the daemon's
        # partition name (category-page.js writes category-status-<life>).
        chip = f".category-status-{life}"
        assert chip in css(), f"no chip rule for {life}"
    # And the chip classes the page writes are the ones the rules carry.
    js = (STATIC / "category-page.js").read_text(encoding="utf-8")
    lives = re.search(r"const CATEGORY_LIVES = \[([^\]]*)\]", js)
    assert lives, "CATEGORY_LIVES is gone"
    assert set(re.findall(r'"([a-z]+)"', lives.group(1))) == set(LIVES)


def test_the_three_surfaces_read_the_palette_instead_of_their_own_hex() -> None:
    """The property that carries the state is a token, on every surface.

    Each surface hands the colour to a different property -- a chip says it in
    `color`, a hive chip keeps it in `--core`, a badge paints `background`
    with it -- and shading a rim darker than its core is a depth decision, not
    a second opinion about the state. So this pins the state-bearing property
    rather than banning hex from the block.
    """
    for selector, prop in (
        (".category-status-running", "color"),
        (".category-status-hold, .category-status-delayed", "color"),
        (".category-status-paused", "color"),
        (".category-status-failed", "color"),
        (".category-status-killed, .category-status-archived", "color"),
        (".hive-chip.s-busy", "--core"),
        (".hive-chip.s-hold", "--core"),
        (".hive-chip.s-delayed", "--core"),
        (".hive-chip.s-paused", "--core"),
        (".hive-chip.s-failed", "--core"),
    ):
        body = rule(selector)
        match = re.search(rf"{re.escape(prop)}:\s*([^;]+);", body)
        assert match, f"{selector} has no {prop}"
        assert match.group(1).strip().startswith("var(--life-"), f"{selector} writes its own {prop}"
    for status, token in (("open", "open"), ("ready", "ready"), ("busy", "busy"),
                          ("review", "review"), ("blocked", "blocked"), ("exited", "closed")):
        body = rule(f".badge.beads-status.{status}")
        assert f"color: var(--issue-{token})" in body, status
        assert f"background: var(--issue-{token}-bg)" in body, status


def test_a_state_is_never_colour_alone() -> None:
    """Each surface prints the word beside the colour.

    The rule matters more than the CSS: a reader who cannot separate the hues
    still has to be able to tell a hold from a killed record.
    """
    js = (STATIC / "category-page.js").read_text(encoding="utf-8")
    # The category chip's text is the life itself (or `running · <status>`).
    assert 'const label = info.life === "running" && info.status' in js
    assert '"category-chip category-status-" + info.life' in js
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    # The beads badge prints its status, never a bare dot.
    assert '`badge beads-status ${cls}`' in app
