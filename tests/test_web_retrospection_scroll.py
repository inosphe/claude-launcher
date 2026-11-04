"""The retrospection page scrolls inside its own view element (claunch-u0wyo).

``html, body`` are ``overflow: hidden`` and every dashboard page draws into
``#main``, a flex column. A page therefore scrolls only when its own view
element carries ``flex: 1; overflow-y: auto; min-height: 0``. The glossary and
knowledge views had that rule and the ``:is(...)`` scoping that gives their
inputs and buttons the dark control styling; ``#retrospection-view`` reused
their shell classes but was left out of both lists, so the page drew taller
than the window with nothing to scroll.

These assertions pin the three halves together — the view element in
index.html, the scroll rule in style.css, and the class names
retrospection-page.js puts inside it — so dropping ``#retrospection-view``
from the rule again fails a test instead of the page.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "src" / "claude_launcher" / "web" / "static"


def _rules(css: str):
    """Every ``selector { body }`` pair, including ones nested in at-rules."""
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        yield " ".join(match.group(1).split()), match.group(2)


def test_the_view_element_exists() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'id="retrospection-view"' in html


def test_the_view_is_its_own_scroll_container() -> None:
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    for selector, body in _rules(css):
        if "#retrospection-view" not in selector:
            continue
        if "overflow-y: auto" not in body:
            continue
        # The page shell's rule, not the detail pane's.
        if "#glossary-view" not in selector or "#knowledge-view" not in selector:
            continue
        assert "flex: 1" in body, selector
        assert "min-height: 0" in body, selector
        return
    pytest.fail("no rule makes #retrospection-view (with the glossary views) a scroll container")


def test_controls_are_scoped_to_the_view() -> None:
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    scoped = [(s, b) for s, b in _rules(css) if ":is(" in s and "#retrospection-view" in s]
    assert any(".wf-btn" in s for s, _ in scoped), "buttons keep the glossary view's styling"
    assert any("input" in s for s, _ in scoped), "inputs keep the glossary view's styling"


def test_the_page_and_the_css_agree_on_class_names() -> None:
    js = (STATIC / "retrospection-page.js").read_text(encoding="utf-8")
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    for name in (
        "retrospection-card-head",
        "retrospection-excerpt",
        "retrospection-detail",
        "retrospection-body",
    ):
        assert name in js, f"{name} is no longer put on the page"
        assert f".{name}" in css, f"{name} has no styling"


def test_every_scoped_class_is_one_the_page_builds() -> None:
    """A rule naming a class nothing emits styles nothing (claunch-u0wyo).

    The view reuses the glossary shell's markup, so a styling rule has to name
    the class that markup actually carries. Two rules shipped naming
    ``retrospection-*`` classes that ``retrospection-page.js`` never emits —
    invisible in review, invisible in the browser harness (an unmatched
    selector is not an error), and found only by hand.
    """
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    emitted = " ".join(
        (STATIC / name).read_text(encoding="utf-8")
        for name in ("retrospection-page.js", "glossary-page.js", "app.js", "index.html")
    )
    scoped = set()
    for selector, _body in _rules(css):
        if "#retrospection-view" not in selector:
            continue
        scoped.update(re.findall(r"\.([A-Za-z0-9_-]+)", selector))
    assert scoped, "no rule is scoped to #retrospection-view"
    dead = sorted(name for name in scoped if name not in emitted)
    assert dead == [], f"styling rules name classes no page emits: {dead}"
