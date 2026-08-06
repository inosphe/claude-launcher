"""Key-name → byte-sequence encoding (tmux send-keys parity)."""

from __future__ import annotations

import pytest

from claude_launcher.daemon import keys


def enc(*args, **kw):
    return keys.encode_keys(list(args), **kw)


def test_plain_text_is_literal_utf8():
    assert enc("hello") == b"hello"
    assert enc("안녕") == "안녕".encode("utf-8")


def test_named_keys():
    assert enc("Enter") == b"\r"
    assert enc("Escape") == b"\x1b"
    assert enc("Tab") == b"\t"
    assert enc("BSpace") == b"\x7f"
    assert enc("PPage") == b"\x1b[5~"
    assert enc("F1") == b"\x1bOP"
    assert enc("F5") == b"\x1b[15~"


def test_key_names_are_case_insensitive():
    assert enc("enter") == b"\r"
    assert enc("ESCAPE") == b"\x1b"


def test_cursor_keys_respect_application_mode():
    assert enc("Up") == b"\x1b[A"
    assert enc("Up", app_cursor=True) == b"\x1bOA"
    assert enc("Home") == b"\x1b[H"
    assert enc("Home", app_cursor=True) == b"\x1bOH"


def test_control_keys():
    assert enc("C-c") == b"\x03"
    assert enc("C-a") == b"\x01"
    assert enc("C-Space") == b"\x00"
    assert enc("c-z") == b"\x1a"


def test_meta_keys():
    assert enc("M-x") == b"\x1bx"
    assert enc("M-Enter") == b"\x1b\r"
    assert enc("M-C-c") == b"\x1b\x03"


def test_shift_tab():
    assert enc("S-Tab") == b"\x1b[Z"
    assert enc("BTab") == b"\x1b[Z"


def test_mixed_args_concatenate_like_tmux():
    assert enc("ls -la", "Enter") == b"ls -la\r"


def test_literal_mode_disables_lookup():
    assert enc("Enter", literal=True) == b"Enter"
    assert enc("C-c", literal=True) == b"C-c"


def test_single_chars_are_literal():
    assert enc("a") == b"a"
    assert enc("q") == b"q"


def test_unknown_multichar_is_literal_text():
    assert enc("hello Enter world") == b"hello Enter world"


def test_bad_control_key_raises():
    with pytest.raises(keys.KeyError_):
        enc("C-Enter")
