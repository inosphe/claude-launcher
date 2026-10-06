"""The cflow v2 expression language (docs/cflow-v2.md 2.4).

Guards (``cond.if``, ``loop.while``, ``choose.options.*.when``), ``foreach.in``
and ``with`` values are written in this small language and run by the
interpreter below. User text never reaches Python's ``eval``/``compile``/
``ast``: it is tokenised and parsed by hand into a tuple tree, and the
evaluator only knows the node kinds listed here.

Grammar (lowest precedence first)::

    expr     := or
    or       := and ("or" and)*
    and      := not ("and" not)*
    not      := "not" not | cmp
    cmp      := primary (op primary)?      op: == != < <= > >= in "not in"
    primary  := literal | list | ref | call | "(" expr ")"
    literal  := 'str' | "str" | int | float | true | false | null
    list     := "[" (expr ("," expr)*)? ","? "]"
    ref      := "$" name ("." field | "[" expr "]")*
    call     := ("len" | "empty") "(" expr ")"

A reference whose root or path does not exist evaluates to ``null``. Ordering
comparisons need two numbers or two strings; anything else (``1 < "a"``,
``null < 3``) raises :class:`ExprError` at evaluation.

:class:`ExprError` is the only exception :func:`parse` and :func:`evaluate`
raise, whatever the text or the run data: nesting deeper than
:data:`MAX_DEPTH`, an integer literal longer than :data:`MAX_INT_DIGITS`, an
index that cannot be a key (a list or mapping) and anything Python itself
refuses on the way (``TypeError``, ``ValueError``, ``RecursionError``,
``OverflowError``) all become one.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Dict, List, Optional, Set, Tuple, Union

__all__ = ["ExprError", "Expr", "parse", "evaluate", "references", "truthy"]


class ExprError(Exception):
    """A malformed expression (parse) or an operation on the wrong types
    (evaluate). ``column`` is 1-based when known."""

    def __init__(self, message: str, column: Optional[int] = None,
                 source: Optional[str] = None) -> None:
        self.message = message
        self.column = column
        self.source = source
        text = message if column is None else f"{message} (at column {column})"
        if source is not None:
            text = f"{text} in expression {source!r}"
        super().__init__(text)


@dataclass(frozen=True)
class Expr:
    """A parsed expression: the source text and its tuple AST."""

    source: str
    ast: tuple

    def __str__(self) -> str:
        return self.source


# --- tokens ------------------------------------------------------------------

#: Deepest nesting of brackets/``not``/references the parser accepts.
MAX_DEPTH = 64
#: Longest integer literal (digits) the tokeniser accepts.
MAX_INT_DIGITS = 30
#: Longest expression text accepted.
MAX_LENGTH = 20000

_INTERNAL = (TypeError, ValueError, RecursionError, OverflowError, MemoryError)

_KEYWORDS = {"and", "or", "not", "in", "true", "false", "null"}
_FUNCTIONS = {"len", "empty"}
_CMP_OPS = ("==", "!=", "<=", ">=", "<", ">")
_PUNCT = "()[],."


@dataclass(frozen=True)
class _Tok:
    kind: str   # num str ref ident op punct end
    value: Any
    col: int    # 1-based


def _is_name_start(ch: str) -> bool:
    return ch.isascii() and (ch.isalpha() or ch == "_")


def _is_name_char(ch: str) -> bool:
    return ch.isascii() and (ch.isalnum() or ch == "_")


def _tokenize(text: str) -> List[_Tok]:
    toks: List[_Tok] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        col = i + 1
        if ch.isspace():
            i += 1
            continue
        if ch in "'\"":
            quote, i, buf = ch, i + 1, []
            while True:
                if i >= n:
                    raise ExprError("unterminated string", col, text)
                c = text[i]
                if c == "\\":
                    if i + 1 >= n:
                        raise ExprError("unterminated string", col, text)
                    esc = text[i + 1]
                    buf.append({"n": "\n", "t": "\t", "\\": "\\", "'": "'", '"': '"'}.get(esc, "\\" + esc))
                    i += 2
                    continue
                if c == quote:
                    i += 1
                    break
                buf.append(c)
                i += 1
            toks.append(_Tok("str", "".join(buf), col))
            continue
        if ch.isdigit() or (ch == "-" and i + 1 < n and text[i + 1].isdigit()):
            j = i + 1
            while j < n and text[j].isdigit():
                j += 1
            is_float = False
            if j + 1 < n and text[j] == "." and text[j + 1].isdigit():
                is_float = True
                j += 1
                while j < n and text[j].isdigit():
                    j += 1
            if j < n and _is_name_char(text[j]):
                raise ExprError(f"malformed number {text[i:j + 1]!r}", col, text)
            raw = text[i:j]
            if len(raw.lstrip("-").split(".")[0]) > MAX_INT_DIGITS:
                raise ExprError(f"number literal longer than {MAX_INT_DIGITS} digits", col, text)
            toks.append(_Tok("num", float(raw) if is_float else int(raw), col))
            i = j
            continue
        if ch == "$":
            j = i + 1
            if j >= n or not _is_name_start(text[j]):
                raise ExprError("'$' must be followed by a name", col, text)
            while j < n and _is_name_char(text[j]):
                j += 1
            toks.append(_Tok("ref", text[i + 1:j], col))
            i = j
            continue
        if _is_name_start(ch):
            j = i
            while j < n and _is_name_char(text[j]):
                j += 1
            toks.append(_Tok("ident", text[i:j], col))
            i = j
            continue
        two = text[i:i + 2]
        if two in ("==", "!=", "<=", ">="):
            toks.append(_Tok("op", two, col))
            i += 2
            continue
        if ch in "<>":
            toks.append(_Tok("op", ch, col))
            i += 1
            continue
        if ch in _PUNCT:
            toks.append(_Tok("punct", ch, col))
            i += 1
            continue
        raise ExprError(f"unexpected character {ch!r}", col, text)
    toks.append(_Tok("end", None, n + 1))
    return toks


# --- parser ------------------------------------------------------------------
#
# AST nodes (tuples, first item the kind):
#   ("lit", value)                ("list", [node, ...])
#   ("ref", root, (seg, ...))     seg: ("field", name) | ("index", node)
#   ("not", node)                 ("and", a, b)   ("or", a, b)
#   ("cmp", op, a, b, col)        op in _CMP_OPS + ("in", "not in")
#   ("call", name, arg, col)


class _Parser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.toks = _tokenize(text)
        self.pos = 0
        self.depth = 0

    def deeper(self) -> None:
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise self.error(f"expression nested deeper than {MAX_DEPTH} levels")

    def peek(self, offset: int = 0) -> _Tok:
        return self.toks[min(self.pos + offset, len(self.toks) - 1)]

    def next(self) -> _Tok:
        tok = self.toks[self.pos]
        self.pos += 1
        return tok

    def error(self, message: str, tok: Optional[_Tok] = None) -> ExprError:
        tok = tok or self.peek()
        return ExprError(message, tok.col, self.text)

    def is_kw(self, word: str, offset: int = 0) -> bool:
        tok = self.peek(offset)
        return tok.kind == "ident" and tok.value == word

    def is_punct(self, ch: str) -> bool:
        tok = self.peek()
        return tok.kind == "punct" and tok.value == ch

    def expect_punct(self, ch: str) -> _Tok:
        if not self.is_punct(ch):
            tok = self.peek()
            got = "end of expression" if tok.kind == "end" else repr(tok.value)
            raise self.error(f"expected {ch!r}, got {got}")
        return self.next()

    def parse(self) -> tuple:
        if self.peek().kind == "end":
            raise self.error("empty expression")
        node = self.parse_or()
        tok = self.peek()
        if tok.kind != "end":
            raise self.error(f"unexpected {tok.value!r}")
        return node

    def parse_or(self) -> tuple:
        self.deeper()
        node = self.parse_and()
        while self.is_kw("or"):
            self.next()
            node = ("or", node, self.parse_and())
        self.depth -= 1
        return node

    def parse_and(self) -> tuple:
        node = self.parse_not()
        while self.is_kw("and"):
            self.next()
            node = ("and", node, self.parse_not())
        return node

    def parse_not(self) -> tuple:
        if self.is_kw("not"):
            self.next()
            self.deeper()
            node = ("not", self.parse_not())
            self.depth -= 1
            return node
        return self.parse_cmp()

    def parse_cmp(self) -> tuple:
        left = self.parse_primary()
        tok = self.peek()
        op = None
        if tok.kind == "op":
            op = tok.value
            self.next()
        elif self.is_kw("in"):
            op = "in"
            self.next()
        elif self.is_kw("not") and self.is_kw("in", 1):
            op = "not in"
            self.next()
            self.next()
        if op is None:
            return left
        right = self.parse_primary()
        nxt = self.peek()
        if nxt.kind == "op" or self.is_kw("in") or (self.is_kw("not") and self.is_kw("in", 1)):
            raise self.error("comparisons do not chain; use 'and'", nxt)
        return ("cmp", op, left, right, tok.col)

    def parse_primary(self) -> tuple:
        tok = self.peek()
        if tok.kind in ("num", "str"):
            self.next()
            return ("lit", tok.value)
        if tok.kind == "ref":
            self.next()
            return self.parse_path(tok)
        if tok.kind == "punct" and tok.value == "(":
            self.next()
            node = self.parse_or()
            self.expect_punct(")")
            return node
        if tok.kind == "punct" and tok.value == "[":
            self.next()
            items: List[tuple] = []
            while not self.is_punct("]"):
                items.append(self.parse_or())
                if self.is_punct(","):
                    self.next()
                    continue
                break
            self.expect_punct("]")
            return ("list", tuple(items))
        if tok.kind == "ident":
            word = tok.value
            if word == "true":
                self.next()
                return ("lit", True)
            if word == "false":
                self.next()
                return ("lit", False)
            if word == "null":
                self.next()
                return ("lit", None)
            if word in _FUNCTIONS:
                self.next()
                self.expect_punct("(")
                arg = self.parse_or()
                if self.is_punct(","):
                    raise self.error(f"{word}() takes one argument")
                self.expect_punct(")")
                return ("call", word, arg, tok.col)
            if word in _KEYWORDS:
                raise self.error(f"unexpected keyword {word!r}", tok)
            raise self.error(
                f"unknown name {word!r} (references start with '$'; the "
                f"functions are {', '.join(sorted(_FUNCTIONS))})",
                tok,
            )
        if tok.kind == "end":
            raise self.error("unexpected end of expression")
        raise self.error(f"unexpected {tok.value!r}")

    def parse_path(self, root: _Tok) -> tuple:
        segs: List[tuple] = []
        while True:
            if self.is_punct("."):
                self.next()
                tok = self.peek()
                if tok.kind == "ident":
                    segs.append(("field", tok.value))
                elif tok.kind == "num" and isinstance(tok.value, int) and tok.value >= 0:
                    segs.append(("index", ("lit", tok.value)))
                else:
                    raise self.error("expected a field name after '.'")
                self.next()
            elif self.is_punct("["):
                self.next()
                segs.append(("index", self.parse_or()))
                self.expect_punct("]")
            else:
                break
        return ("ref", root.value, tuple(segs))


@lru_cache(maxsize=512)
def _parse_cached(text: str) -> Expr:
    if len(text) > MAX_LENGTH:
        raise ExprError(f"expression longer than {MAX_LENGTH} characters")
    try:
        return Expr(source=text, ast=_Parser(text).parse())
    except ExprError:
        raise
    except _INTERNAL as exc:
        raise ExprError(f"malformed expression ({type(exc).__name__})", None,
                        text[:200]) from None


def parse(text: str) -> Expr:
    """Parse ``text``; raises :class:`ExprError` (with a column) if malformed."""
    if isinstance(text, Expr):
        return text
    if not isinstance(text, str):
        raise ExprError(f"an expression must be a string, got {type(text).__name__}")
    return _parse_cached(text)


# --- evaluation --------------------------------------------------------------


def truthy(value: Any) -> bool:
    """``null``, ``false``, ``0``, ``""``, ``[]`` and ``{}`` are false."""
    if value is None or value is False:
        return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0
    if isinstance(value, (str, list, tuple, dict, set)):
        return len(value) > 0
    return True


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if _is_number(value):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (list, tuple)):
        return "list"
    if isinstance(value, dict):
        return "mapping"
    return type(value).__name__


def _equal(a: Any, b: Any) -> bool:
    # true == 1 is false here: booleans and numbers are different types.
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if _is_number(a) and _is_number(b):
        return a == b
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_equal(x, y) for x, y in zip(a, b))
    if type(a) is not type(b) and not (isinstance(a, dict) and isinstance(b, dict)):
        return False
    return a == b


def _step(value: Any, key: Any) -> Any:
    if isinstance(key, (list, tuple, dict, set)):
        raise ExprError(f"a {_type_name(key)} cannot be used as an index or key")
    if isinstance(value, dict):
        if key in value:
            return value[key]
        if not isinstance(key, str):
            return value.get(str(key))
        return None
    if isinstance(value, (list, tuple)) and _is_number(key) and int(key) == key:
        idx = int(key)
        if -len(value) <= idx < len(value):
            return value[idx]
        return None
    return None


class _Eval:
    def __init__(self, expr: Expr, env: Dict[str, Any]) -> None:
        self.expr = expr
        self.env = env

    def fail(self, message: str, col: Optional[int] = None) -> ExprError:
        return ExprError(message, col, self.expr.source)

    def run(self, node: tuple) -> Any:
        kind = node[0]
        if kind == "lit":
            return node[1]
        if kind == "list":
            return [self.run(item) for item in node[1]]
        if kind == "ref":
            value = self.env.get(node[1]) if isinstance(self.env, dict) else None
            for seg in node[2]:
                if value is None:
                    return None
                key = seg[1] if seg[0] == "field" else self.run(seg[1])
                value = _step(value, key)
            return value
        if kind == "not":
            return not truthy(self.run(node[1]))
        if kind == "and":
            return truthy(self.run(node[1])) and truthy(self.run(node[2]))
        if kind == "or":
            return truthy(self.run(node[1])) or truthy(self.run(node[2]))
        if kind == "cmp":
            return self.compare(node[1], self.run(node[2]), self.run(node[3]), node[4])
        if kind == "call":
            return self.call(node[1], self.run(node[2]), node[3])
        raise self.fail(f"internal: unknown node {kind!r}")

    def compare(self, op: str, a: Any, b: Any, col: int) -> bool:
        if op == "==":
            return _equal(a, b)
        if op == "!=":
            return not _equal(a, b)
        if op in ("in", "not in"):
            if b is None:
                found = False
            elif isinstance(b, (list, tuple)):
                found = any(_equal(a, item) for item in b)
            elif isinstance(b, dict):
                found = isinstance(a, str) and a in b
            elif isinstance(b, str):
                if not isinstance(a, str):
                    raise self.fail(
                        f"'{op}' a string needs a string on the left, got {_type_name(a)}", col)
                found = a in b
            else:
                raise self.fail(f"'{op}' needs a list, mapping or string, got {_type_name(b)}", col)
            return found if op == "in" else not found
        same = (_is_number(a) and _is_number(b)) or (isinstance(a, str) and isinstance(b, str))
        if not same:
            raise self.fail(
                f"cannot compare {_type_name(a)} {op} {_type_name(b)}", col)
        if op == "<":
            return a < b
        if op == "<=":
            return a <= b
        if op == ">":
            return a > b
        return a >= b

    def call(self, name: str, value: Any, col: int) -> Any:
        if name == "len":
            if value is None:
                return 0
            if isinstance(value, (str, list, tuple, dict)):
                return len(value)
            raise self.fail(f"len() of {_type_name(value)}", col)
        if name == "empty":
            return value is None or (isinstance(value, (str, list, tuple, dict)) and len(value) == 0)
        raise self.fail(f"unknown function {name!r}", col)


def evaluate(expr_or_text: Union[Expr, str], env: Dict[str, Any]) -> Any:
    """Evaluate against ``env`` (root names without ``$``: ``{"data": ...}``).
    Raises :class:`ExprError` and nothing else."""
    expr = parse(expr_or_text)
    try:
        return _Eval(expr, env or {}).run(expr.ast)
    except ExprError as exc:
        if exc.source is None:
            raise ExprError(exc.message, exc.column, expr.source) from None
        raise
    except _INTERNAL as exc:
        raise ExprError(f"cannot evaluate ({type(exc).__name__}: {str(exc)[:120]})", None,
                        expr.source) from None


def references(expr_or_text: Union[Expr, str]) -> Set[str]:
    """The root names (``params``, ``data``, ``item`` ...) the expression reads."""
    expr = parse(expr_or_text)
    out: Set[str] = set()

    def walk(node: Any) -> None:
        if not isinstance(node, tuple) or not node:
            return
        if node[0] == "ref":
            out.add(node[1])
            for seg in node[2]:
                if seg[0] == "index":
                    walk(seg[1])
            return
        if node[0] == "list":
            for item in node[1]:
                walk(item)
            return
        for item in node[1:]:
            walk(item)

    walk(expr.ast)
    return out
