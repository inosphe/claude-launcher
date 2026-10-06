"""cflow v2 expression language (docs/cflow-v2.md 2.4)."""

from __future__ import annotations

import pytest

from claude_launcher.cflow2 import expr
from claude_launcher.cflow2.expr import ExprError, evaluate, parse, references, truthy

ENV = {
    "params": {"n": 3, "name": "x", "tags": ["a", "b"]},
    "data": {"x": {"y": 5}, "items": [10, 20, 30], "m": {"k": "v"}},
    "prev": None,
    "item": "b",
}


@pytest.mark.parametrize("text, want", [
    ("1", 1), ("-2", -2), ("1.5", 1.5), ("'a'", "a"), ('"b\\"c"', 'b"c'),
    ("true", True), ("false", False), ("null", None), ("[1, 'a', [2]]", [1, "a", [2]]),
    ("[]", []), ("[1,]", [1]),
])
def test_literals(text, want):
    assert evaluate(text, {}) == want


@pytest.mark.parametrize("text, want", [
    ("$params.n", 3),
    ("$data.x.y", 5),
    ("$data.items[1]", 20),
    ("$data.items.2", 30),
    ("$data.items[-1]", 30),
    ('$data.m["k"]', "v"),
    ("$data.items[len($params.tags)]", 30),
    ("$data.missing.deeper", None),
    ("$nothere", None),
    ("$data.items[9]", None),
    ("$params.n.x", None),
    ("$prev.anything", None),
])
def test_references(text, want):
    assert evaluate(text, ENV) == want


def test_precedence_or_and_not():
    # or < and < not < comparison
    assert evaluate("true or false and false", {}) is True
    assert evaluate("(true or false) and false", {}) is False
    assert evaluate("not false and false", {}) is False
    assert evaluate("not 1 == 2", {}) is True
    assert evaluate("not $item in $params.tags", ENV) is False
    assert evaluate("$params.n > 2 and $item in ['a', 'b'] or false", ENV) is True


@pytest.mark.parametrize("text, want", [
    ("1 == 1", True), ("1 == 1.0", True), ("1 != 2", True), ("'a' < 'b'", True),
    ("2 <= 2", True), ("3 > 4", False), ("3 >= 3", True),
    ("true == 1", False), ("null == null", True), ("$nothere == null", True),
    ("[1, 2] == [1, 2]", True), ("'a' in 'cat'", True), ("'k' in $data.m", True),
    ("1 not in [2, 3]", True), ("'x' in $nothere", False), ("'x' not in $nothere", True),
])
def test_comparisons(text, want):
    assert evaluate(text, ENV) is want


def test_functions():
    assert evaluate("len($data.items)", ENV) == 3
    assert evaluate("len('abc')", ENV) == 3
    assert evaluate("len($nothere)", ENV) == 0
    for value in ("null", "''", "[]", "$data.nope", "$prev"):
        assert evaluate(f"empty({value})", ENV) is True, value
    assert evaluate("empty({})".replace("{}", "$data.empty_map"), {"data": {"empty_map": {}}}) is True
    assert evaluate("empty(0)", {}) is False
    assert evaluate("empty($data.items)", ENV) is False


@pytest.mark.parametrize("text", [
    "1 < 'a'", "null < 3", "$nothere > 1", "[1] < [2]", "true < false",
    "len(3)", "1 in 5", "1 in 'abc'",
])
def test_type_mismatch_raises_at_evaluate(text):
    parse(text)  # parses fine
    with pytest.raises(ExprError):
        evaluate(text, ENV)


@pytest.mark.parametrize("text", [
    "__import__('os')",
    "__import__('os').system('true')",
    "open('/etc/passwd')",
    "eval('1')",
    "$data.__class__.__mro__ + 1",
    "lambda: 1",
    "1 + 1",
    "$x; 1",
    "a",
    "$",
    "",
    "(1",
    "[1, 2",
    "'unterminated",
    "1 == 2 == 3",
    "len(1, 2)",
    "12abc",
    "$x.",
    "not",
])
def test_rejected_at_parse(text):
    with pytest.raises(ExprError):
        parse(text)


def test_dunder_paths_are_plain_keys():
    # attribute access does not exist: a dunder segment is a mapping key.
    assert evaluate("$data.__class__", ENV) is None
    assert evaluate("$s.__len__", {"s": "abc"}) is None


def test_error_has_column():
    with pytest.raises(ExprError) as info:
        parse("$a == __import__('os')")
    assert info.value.column == 7
    assert "column 7" in str(info.value)


def test_short_circuit_avoids_type_error():
    assert evaluate("not empty($x) and $x > 3", {}) is False
    assert evaluate("empty($x) or $x > 3", {}) is True


def test_references_roots():
    assert references("$params.n > 1 and $data.items[$item] in $result.build") == {
        "params", "data", "item", "result"}
    assert references("1 == 1") == set()


def test_parse_returns_frozen_expr():
    e = parse("$a == 1")
    assert isinstance(e, expr.Expr)
    assert e.source == "$a == 1"
    assert parse(e) is e
    with pytest.raises(Exception):
        e.source = "x"  # type: ignore[misc]
    assert evaluate(e, {"a": 1}) is True


@pytest.mark.parametrize("value, want", [
    (None, False), (False, False), (0, False), (0.0, False), ("", False), ([], False),
    ({}, False), (True, True), (1, True), ("a", True), ([0], True), ({"a": 1}, True),
])
def test_truthy(value, want):
    assert truthy(value) is want
