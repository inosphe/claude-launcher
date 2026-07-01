"""Collect a multi-line prompt from the user via an interactive editor.

Used by ``claunch run --add-prompt``: opens the user's ``$VISUAL``/``$EDITOR``
(falling back to a platform default) on a temporary file, then returns the text
they saved. The result is forwarded to ``claude --append-system-prompt`` so the
extra context is *appended* to Claude Code's system prompt, not replacing it.

Everything from a git-style scissors line down is treated as instructions and
stripped, so Markdown ``#`` headings in the user's own text are preserved.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import List

#: Env vars consulted, in order, for the editor command (mirrors git).
_EDITOR_ENVS = ("VISUAL", "EDITOR")

#: Everything from this line down in the temp file is ignored.
_SCISSORS = "# ------------------------ >8 ------------------------"

_INSTRUCTIONS = f"""

{_SCISSORS}
# Type above this line the text to APPEND to Claude Code's system prompt.
# It is passed to `claude --append-system-prompt`, so it adds to (does not
# replace) the built-in prompt. Everything from the scissors line down is
# ignored; Markdown '#' headings in your text above are kept. Save an empty
# body to launch without adding a prompt.
"""


class PromptInputError(Exception):
    """Raised when the editor cannot be launched or exits abnormally."""


def _default_editor() -> List[str]:
    if os.name == "nt":
        return ["notepad"]
    return ["vi"]


def _editor_command() -> List[str]:
    """Resolve the editor command, honoring ``$VISUAL``/``$EDITOR``."""
    for env in _EDITOR_ENVS:
        value = os.environ.get(env)
        if value and value.strip():
            # posix=False on Windows keeps backslash path separators intact.
            return shlex.split(value, posix=os.name != "nt")
    return _default_editor()


def _strip_instructions(text: str) -> str:
    """Drop the scissors line and everything after it, then trim whitespace."""
    kept: List[str] = []
    for line in text.splitlines():
        if line.rstrip() == _SCISSORS:
            break
        kept.append(line)
    return "\n".join(kept).strip()


def collect(initial: str = "") -> str:
    """Open an editor and return the text the user saved (may be empty).

    ``initial`` pre-fills the editable body (e.g. to re-edit a prior prompt).
    """
    editor = _editor_command()
    fd, name = tempfile.mkstemp(prefix="claunch-prompt-", suffix=".md", text=True)
    path = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            if initial:
                fh.write(initial)
                if not initial.endswith("\n"):
                    fh.write("\n")
            fh.write(_INSTRUCTIONS)
        try:
            completed = subprocess.run([*editor, str(path)])
        except (FileNotFoundError, OSError) as exc:
            raise PromptInputError(
                f"could not launch editor {editor[0]!r}: {exc} "
                f"(set $VISUAL or $EDITOR to a working editor)"
            ) from exc
        if completed.returncode != 0:
            raise PromptInputError(
                f"editor {editor[0]!r} exited with status {completed.returncode}; "
                f"prompt not captured"
            )
        text = path.read_text(encoding="utf-8", errors="replace")
    finally:
        try:
            path.unlink()
        except OSError:
            pass
    return _strip_instructions(text)
