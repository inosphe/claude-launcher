"""The test tiers leave no test unrun (claunch-ov4zr).

T0 and T1 run ``-m "not slow_tail"``; T2 (the sweep) runs without ``-m``. The
tiers are only safe if every collected test is in exactly one of "core" and
"tail", and the sweep's command filters nothing. These checks read the
session's own collected items (``request.session.items``), so under a full
sweep they cover the whole suite and under a targeted run the selected modules.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

from _pytest.mark.expression import Expression

from claude_launcher import test_tiers

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _names(item) -> set:
    return {m.name for m in item.iter_markers()}


def _selected_by(expr: str, items) -> set:
    compiled = Expression.compile(expr)
    return {i.nodeid for i in items if compiled.evaluate(lambda n, i=i: n in _names(i))}


def test_every_collected_test_is_in_exactly_one_of_core_and_tail(request):
    items = request.session.items
    everything = {i.nodeid for i in items}
    core = _selected_by(test_tiers.CORE_EXPR, items)
    tail = _selected_by(test_tiers.TAIL_MARKER, items)
    assert core | tail == everything
    assert not core & tail
    assert len(core) + len(tail) == len(everything) == len(items)


def test_every_source_marker_puts_the_test_in_the_tail(request):
    tail = _selected_by(test_tiers.TAIL_MARKER, request.session.items)
    marked = {
        i.nodeid for i in request.session.items
        if set(test_tiers.SOURCE_MARKERS) & _names(i)
    }
    assert marked <= tail
    # and the tail holds nothing that is not marked at its source
    assert tail <= marked


def test_the_tail_marker_is_registered(pytestconfig):
    registered = {line.split(":", 1)[0].strip() for line in pytestconfig.getini("markers")}
    assert {test_tiers.TAIL_MARKER, *test_tiers.SOURCE_MARKERS} <= registered


def test_the_sweep_command_filters_nothing():
    sweep = _load("sweep")
    assert not re.search(r"(^|\s)-m\b|--markers?\b", sweep.DEFAULT_COMMAND), sweep.DEFAULT_COMMAND
    assert test_tiers.TAIL_MARKER not in sweep.DEFAULT_COMMAND


def test_the_targeted_tiers_pass_the_core_expression_and_the_flag_lifts_it():
    ct = _load("changed_tests")
    default = ct.build_command(["tests/test_x.py"], basetemp="b")
    assert default[default.index("-m") + 1] == test_tiers.CORE_EXPR
    assert "-m" not in ct.build_command(["tests/test_x.py"], basetemp="b", with_tail=True)


def test_browser_harness_nodes_are_all_in_the_tail(request):
    harnesses = [i for i in request.session.items if "::test_browser_harness[" in i.nodeid]
    assert all(test_tiers.is_tail(_names(i)) for i in harnesses)
    if harnesses:
        assert len(harnesses) == len(list((ROOT / "tests" / "web").glob("*_browser.cjs")))
