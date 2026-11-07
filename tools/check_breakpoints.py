"""Refuse a viewport width in the web UI that is not one of its three breakpoints.

The dashboard's stylesheets once carried eight different ``max-width`` values
(600, 640, 700, 720, 760, 820, 900 and 1100px). The mobile shell switches at
820px (``MOBILE_MQ`` in ``app.js``), so a rule written for "narrow" at 700px
left the 701-820px range with the mobile shell on and the narrow layout off.
claunch-0a5nk.1 reduced them to three steps; this check keeps it that way.

What is checked
---------------
* every ``@media`` condition on ``width`` in ``web/static/*.css``
  (``max-width``, ``min-width``, with or without spaces);
* every ``matchMedia("(...width...)")`` in ``web/static/*.js``.

Comments are removed before the CSS is read, so a comment that quotes an old
value is not reported. ``@container`` queries are not checked: they size to
their container box, which is not the viewport.

The allowed values are ``ALLOWED`` below and the comment at the top of
``style.css``. Change both together.

Exit codes: 0 nothing to report, 1 at least one width outside the steps,
2 the static directory could not be read.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

#: phone / compact / wide. The max side is the step itself; the min side is
#: one pixel past it, so a max rule and its min counterpart never overlap.
STEPS = (600, 820, 1100)

#: Widths a query may name, per feature.
ALLOWED = {
    "max-width": set(STEPS),
    # 1500px: the cflow host page leaves room for its open pane (style.css,
    # .cf2-host-page.pane-open). It is a desktop layout width, not a step.
    "min-width": {s + 1 for s in STEPS} | {1500},
}

STATIC = Path("src") / "claude_launcher" / "web" / "static"

_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_MEDIA = re.compile(r"@media\b[^{;]*")
_MATCH_MEDIA = re.compile(r"""matchMedia\(\s*(["'`])(.*?)\1""")
_WIDTH = re.compile(r"\b(max-width|min-width)\s*:\s*([0-9.]+)\s*(px|em|rem)?")


def _blank_comments(text: str) -> str:
    """Replace each comment with spaces and newlines, keeping line numbers."""
    return _COMMENT.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)


def _check_condition(cond: str) -> list[str]:
    bad = []
    for feature, number, unit in _WIDTH.findall(cond):
        unit = unit or "px"
        if unit != "px":
            bad.append(f"{feature}: {number}{unit} (use px)")
            continue
        value = float(number)
        if value != int(value) or int(value) not in ALLOWED[feature]:
            bad.append(f"{feature}: {number}px")
    return bad


def findings_in(text: str, *, kind: str) -> list[tuple[int, str]]:
    """``(line, message)`` for each disallowed width; kind is 'css' or 'js'."""
    found = []
    if kind == "css":
        body = _blank_comments(text)
        for m in _MEDIA.finditer(body):
            for bad in _check_condition(m.group(0)):
                found.append((body.count("\n", 0, m.start()) + 1, bad))
    else:
        for m in _MATCH_MEDIA.finditer(text):
            for bad in _check_condition(m.group(2)):
                found.append((text.count("\n", 0, m.start()) + 1, bad))
    return found


def scan(static: Path) -> list[tuple[str, int, str]]:
    found = []
    for path in sorted(static.glob("*.css")) + sorted(static.glob("*.js")):
        kind = "css" if path.suffix == ".css" else "js"
        text = path.read_text(encoding="utf-8")
        for line, msg in findings_in(text, kind=kind):
            found.append((path.name, line, msg))
    return found


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--static",
        default="",
        help="directory to scan (default: web/static of the tree this file is in)",
    )
    args = ap.parse_args(argv)
    if args.static:
        static = Path(args.static).resolve()
    else:
        static = Path(__file__).resolve().parent.parent / STATIC
    if not static.is_dir():
        print(f"no such directory: {static}", file=sys.stderr)
        return 2

    findings = scan(static)
    steps = ", ".join(f"{s}px" for s in STEPS)
    if not findings:
        print(f"every viewport width query in {static} uses a breakpoint ({steps})")
        return 0
    print("viewport widths outside the breakpoints:")
    for name, line, msg in findings:
        print(f"  {name}:{line}: {msg}")
    print(
        f"\nUse one of max-width {steps} or min-width "
        + ", ".join(f"{v}px" for v in sorted(ALLOWED["min-width"]))
        + " (tools/check_breakpoints.py ALLOWED, the comment atop style.css)."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
