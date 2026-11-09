"""tools/check_breakpoints.py: the web UI's viewport widths stay on three steps."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECK = ROOT / "tools" / "check_breakpoints.py"
STATIC = ROOT / "src" / "claude_launcher" / "web" / "static"


def _load():
    spec = importlib.util.spec_from_file_location("check_breakpoints_under_test", CHECK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_tree_uses_only_the_breakpoints(capsys):
    assert _load().main([]) == 0, capsys.readouterr().out


def test_mobile_mq_is_the_compact_step():
    """The JS shell switch and the CSS compact step are the same width."""
    mod = _load()
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    m = re.search(r'MOBILE_MQ = window\.matchMedia\("\(max-width: (\d+)px\)"\)', app)
    assert m and int(m.group(1)) == mod.STEPS[1]


def test_style_css_header_names_the_same_steps():
    mod = _load()
    head = (STATIC / "style.css").read_text(encoding="utf-8")[:800]
    for step in mod.STEPS:
        assert f"(max-width: {step}px)" in head
        assert f"(min-width: {step + 1}px)" in head


def test_an_off_step_media_query_is_refused(tmp_path, capsys):
    mod = _load()
    (tmp_path / "a.css").write_text(
        ".x { color: red; }\n@media (max-width: 700px) {\n  .x { color: blue; }\n}\n"
        "@media(min-width:900px){ .y{} }\n",
        encoding="utf-8",
    )
    assert mod.main(["--static", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "a.css:2: max-width: 700px" in out
    assert "a.css:5: min-width: 900px" in out


def test_allowed_values_pass_in_any_spacing(tmp_path):
    mod = _load()
    (tmp_path / "a.css").write_text(
        "@media (max-width: 600px) {}\n@media(max-width:820px){}\n"
        "@media (min-width: 821px) and (hover: hover) {}\n"
        "@media (min-width: 1500px) {}\n@media (prefers-reduced-motion: reduce) {}\n",
        encoding="utf-8",
    )
    assert mod.scan(tmp_path) == []


def test_comments_and_container_queries_are_not_read(tmp_path):
    mod = _load()
    (tmp_path / "a.css").write_text(
        "/* was @media (max-width: 700px) before claunch-0a5nk.1 */\n"
        "@container (max-width: 760px) { .z {} }\n"
        ".w { max-width: 900px; }\n",
        encoding="utf-8",
    )
    assert mod.scan(tmp_path) == []


def test_line_numbers_survive_a_multiline_comment(tmp_path):
    mod = _load()
    (tmp_path / "a.css").write_text(
        "/* one\n two\n three */\n@media (max-width: 640px) {}\n", encoding="utf-8"
    )
    assert mod.scan(tmp_path) == [("a.css", 4, "max-width: 640px")]


def test_non_px_units_are_refused(tmp_path):
    mod = _load()
    (tmp_path / "a.css").write_text("@media (max-width: 40em) {}\n", encoding="utf-8")
    assert mod.scan(tmp_path) == [("a.css", 1, "max-width: 40em (use px)")]


def test_match_media_in_js_is_checked(tmp_path):
    mod = _load()
    (tmp_path / "a.js").write_text(
        'const ok = matchMedia("(max-width: 820px)");\n'
        "const bad = window.matchMedia('(max-width: 700px)');\n"
        'const motion = matchMedia("(prefers-reduced-motion: reduce)");\n',
        encoding="utf-8",
    )
    assert mod.scan(tmp_path) == [("a.js", 2, "max-width: 700px")]


def test_missing_directory_exits_2(tmp_path):
    assert _load().main(["--static", str(tmp_path / "nope")]) == 2
