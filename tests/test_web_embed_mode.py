"""The dashboard's ``?embed=1`` display mode.

issue-gen's board (F:/works/gds5/issue-gen) embeds a session's terminal in a
side panel by loading ``/?embed=1#/s/<name>`` in an iframe. That page wants
the terminal alone, so app.js marks <body class="embed"> from the query string
and style.css hides the rail, the phone bars and the detail column under that
class. The two halves live in different files; this pins that both exist and
agree on the class name, so neither can be dropped in a refactor without the
other noticing.
"""

from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "src" / "claude_launcher" / "web" / "static"


def test_app_js_marks_body_from_the_query_string() -> None:
    src = (STATIC / "app.js").read_text(encoding="utf-8")
    assert 'new URLSearchParams(location.search).has("embed")' in src
    assert 'document.body.classList.add("embed")' in src
    # Before boot(), so the class is on <body> before the first route paints.
    assert src.index('classList.add("embed")') < src.rindex("\nboot();")


def test_style_hides_every_non_terminal_column_under_body_embed() -> None:
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    block = re.search(r"body\.embed #sidebar[^{]*\{[^}]*\}", css, re.S)
    assert block, "no body.embed rule"
    selector = block.group(0)
    for el in ("#sidebar", "#rail-split", "#mobile-top", "#mobile-bottom", "#detail-split", "#sess-view"):
        assert f"body.embed {el}" in selector, el
    assert "display: none !important" in selector
