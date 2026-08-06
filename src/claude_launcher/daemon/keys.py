"""tmux ``send-keys`` emulation: map key names to the bytes a terminal sends.

Mirrors tmux's argument semantics: each argument that matches a key name
(``Enter``, ``Escape``, ``C-c``, ``M-x``, ``Up`` ...) is translated to its
escape sequence; anything else is sent as literal UTF-8 text. A *literal* flag
(tmux ``-l``) disables name lookup entirely. Key-name lookup is
case-insensitive, like tmux.

Arrow/Home/End encoding depends on DECCKM (application cursor keys), which the
running program controls — the caller passes the session's current mode.
"""

from __future__ import annotations

from typing import Dict, Iterable, Tuple


class KeyError_(Exception):
    """Raised for an unencodable key (e.g. an unsupported modifier combo)."""


#: Keys whose encoding is mode-independent. Values are the byte sequences an
#: xterm-compatible terminal emits.
_PLAIN: Dict[str, bytes] = {
    "enter": b"\r",
    "escape": b"\x1b",
    "tab": b"\t",
    "btab": b"\x1b[Z",  # Shift-Tab
    "space": b" ",
    "bspace": b"\x7f",
    "backspace": b"\x7f",
    "ic": b"\x1b[2~",  # Insert
    "insert": b"\x1b[2~",
    "dc": b"\x1b[3~",  # Delete
    "delete": b"\x1b[3~",
    "ppage": b"\x1b[5~",  # PageUp
    "pageup": b"\x1b[5~",
    "pgup": b"\x1b[5~",
    "npage": b"\x1b[6~",  # PageDown
    "pagedown": b"\x1b[6~",
    "pgdn": b"\x1b[6~",
    "f1": b"\x1bOP",
    "f2": b"\x1bOQ",
    "f3": b"\x1bOR",
    "f4": b"\x1bOS",
    "f5": b"\x1b[15~",
    "f6": b"\x1b[17~",
    "f7": b"\x1b[18~",
    "f8": b"\x1b[19~",
    "f9": b"\x1b[20~",
    "f10": b"\x1b[21~",
    "f11": b"\x1b[23~",
    "f12": b"\x1b[24~",
}

#: Keys with (normal, application-cursor-mode) encodings.
_CURSOR: Dict[str, Tuple[bytes, bytes]] = {
    "up": (b"\x1b[A", b"\x1bOA"),
    "down": (b"\x1b[B", b"\x1bOB"),
    "right": (b"\x1b[C", b"\x1bOC"),
    "left": (b"\x1b[D", b"\x1bOD"),
    "home": (b"\x1b[H", b"\x1bOH"),
    "end": (b"\x1b[F", b"\x1bOF"),
}

#: Shift variants of named keys that have a well-known CSI encoding.
_SHIFTED: Dict[str, bytes] = {
    "tab": b"\x1b[Z",
}


def _encode_name(name: str, app_cursor: bool) -> bytes:
    """Encode a bare (unmodified) key name, or raise ``LookupError``."""
    low = name.lower()
    if low in _PLAIN:
        return _PLAIN[low]
    if low in _CURSOR:
        normal, app = _CURSOR[low]
        return app if app_cursor else normal
    raise LookupError(name)


def _encode_key(arg: str, app_cursor: bool) -> bytes:
    """Encode one tmux key argument, or raise ``LookupError`` if it is not a key."""
    # Modifier prefixes, outermost first (tmux allows e.g. M-C-x).
    if len(arg) > 2 and arg[1] == "-" and arg[0] in ("C", "c", "M", "m", "S", "s"):
        mod, rest = arg[0].upper(), arg[2:]
        if mod == "M":
            try:
                return b"\x1b" + _encode_key(rest, app_cursor)
            except LookupError:
                return b"\x1b" + rest.encode("utf-8")
        if mod == "C":
            if len(rest) == 1:
                ch = rest.lower()
                if "a" <= ch <= "z":
                    return bytes([ord(ch) - 0x60])
                ctrl = {" ": b"\x00", "@": b"\x00", "[": b"\x1b", "\\": b"\x1c",
                        "]": b"\x1d", "^": b"\x1e", "_": b"\x1f", "?": b"\x7f"}
                if ch in ctrl:
                    return ctrl[ch]
                raise KeyError_(f"cannot encode control key {arg!r}")
            if rest.lower() == "space":
                return b"\x00"
            raise KeyError_(f"cannot encode control key {arg!r}")
        if mod == "S":
            low = rest.lower()
            if low in _SHIFTED:
                return _SHIFTED[low]
            if len(rest) == 1:
                return rest.upper().encode("utf-8")
            raise KeyError_(f"unsupported shifted key {arg!r}")
    if len(arg) == 1:
        # A single character is itself, but only *named* multi-char args are
        # keys — mirror tmux, where "a" sends "a".
        raise LookupError(arg)
    return _encode_name(arg, app_cursor)


def encode_keys(args: Iterable[str], *, literal: bool = False, app_cursor: bool = False) -> bytes:
    """Translate ``send-keys`` arguments into the byte stream to write to a PTY.

    ``literal`` sends every argument as text (tmux ``-l``); arguments are
    joined without separators, again like tmux.
    """
    out = bytearray()
    for arg in args:
        if literal:
            out += arg.encode("utf-8")
            continue
        try:
            out += _encode_key(arg, app_cursor)
        except LookupError:
            out += arg.encode("utf-8")
    return bytes(out)
