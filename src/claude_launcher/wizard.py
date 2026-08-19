"""``--wizard``: the same session, chosen from lists instead of typed.

``new-session`` and ``spawn`` spell everything out as flags, which is what
makes them scriptable and what makes them hard to type: a session's harness,
profile, directory, role, mesh, workflow and worktree -- and a child's parent,
workspace and mesh -- are all *closed sets* the daemon already publishes, and
a human typing them from memory is guessing at names a picker could simply
show. The web dashboard has had that form for a while (harness/profile/
directory/role/resume, then the "start it working" block); this is the same
form for a terminal, so a session can be built where the person already is
instead of in a browser.

Two commands, two forms, one engine: :class:`Form` knows about rows, pickers
and eighty columns and nothing about sessions, while :class:`Wizard`
(``new-session``) and :class:`SpawnWizard` (``spawn``) each answer their own
command. Neither posts anything -- both fill in the namespace argparse would
have built and hand it back, so the policy, the refusals and the onboarding
are the daemon's, unchanged.

**Every field that has an answer set is multiple choice.** A mistyped mesh, a
workflow that is not declared in this directory, a workspace that no longer
exists -- each of those is a refusal from the daemon *after* the session was
half arranged, and the cheapest way to never provoke one is to never offer it.
Only what genuinely is free text (a name, an opening task, extra harness
flags) is typed.

The two answers the flags-only door cannot really ask for are here too:
whether to branch off into a git worktree, and whether to attach afterwards.
``--worktree`` is otherwise a yes/no prompt fired *after* the command line is
already committed, and ``-a`` is a flag you remember once you are reading the
"attach:" hint it prints.

Rendering is deliberately ASCII and self-contained: this paints into a Windows
console as often as a Unix terminal, and a box-drawing character on a legacy
code page arrives as a question mark -- or as an encoding error that takes the
form down with it. The raw-terminal and keyboard-read layers are borrowed from
:mod:`attach`, which already solved reading arrow keys on both platforms.
"""

from __future__ import annotations

import os
import re
import shlex
import sys
import unicodedata
from datetime import datetime
from dataclasses import dataclass, field as dataclass_field
from typing import Any, Dict, List, Optional

from . import worktree

#: Value of the worktree picker's "name it myself" entry. Not a name anyone
#: could type (``validate_name`` rejects the space), so it cannot collide with
#: a real worktree.
NAME_IT = "@name it"

#: The resume picker's "let claude ask" entry. The API spells that as the
#: empty string, which the form already spends on "(new conversation)".
PICKER = "@picker"


# --------------------------------------------------------------------------- #
# terminal text
# --------------------------------------------------------------------------- #
def width(text: str) -> int:
    """Columns ``text`` occupies, counting CJK/full-width cells as two.

    Paths and session names are the user's, not ours: a Korean directory name
    in the Directory picker would otherwise push every column right of it out
    of alignment.
    """
    total = 0
    for ch in text:
        if unicodedata.combining(ch):
            continue
        total += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return total


def fit(text: str, cols: int) -> str:
    """``text`` truncated to ``cols`` display columns, with an ASCII ellipsis."""
    if cols <= 0:
        return ""
    if width(text) <= cols:
        return text
    if cols <= 3:
        return "." * cols
    out: List[str] = []
    used = 0
    for ch in text:
        w = 0 if unicodedata.combining(ch) else (
            2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        )
        if used + w > cols - 3:
            break
        out.append(ch)
        used += w
    return "".join(out) + "..."


def pad(text: str, cols: int) -> str:
    """``text`` padded with spaces to ``cols`` display columns."""
    return text + " " * max(0, cols - width(text))


# --------------------------------------------------------------------------- #
# keyboard
# --------------------------------------------------------------------------- #
#: Escape sequences a terminal in raw mode delivers as one chunk, mapped to
#: the key names the form reasons about. Both the CSI (``ESC [``) and SS3
#: (``ESC O``) spellings of the arrows are listed: Windows' VT input emits the
#: first, a Unix terminal in application-cursor mode the second.
_SEQUENCES = {
    "[A": "up", "[B": "down", "[C": "right", "[D": "left",
    "OA": "up", "OB": "down", "OC": "right", "OD": "left",
    "[H": "home", "[F": "end", "OH": "home", "OF": "end",
    "[1~": "home", "[4~": "end", "[7~": "home", "[8~": "end",
    "[5~": "pageup", "[6~": "pagedown",
    "[3~": "delete", "[Z": "backtab",
}

#: Control characters with a name. Enter has both spellings: a raw Unix
#: terminal sends CR, a Windows console can send LF.
_CONTROLS = {
    "\r": "enter", "\n": "enter", "\t": "tab", " ": "space",
    "\x7f": "backspace", "\x08": "backspace",
    "\x03": "cancel",    # Ctrl+C
    "\x04": "cancel",    # Ctrl+D
    "\x13": "submit",    # Ctrl+S
    "\x01": "home",      # Ctrl+A
    "\x05": "end",       # Ctrl+E
    "\x15": "killline",  # Ctrl+U
}


def decode_keys(text: str) -> List[str]:
    """Split raw terminal input into key names and literal characters.

    A key is either a name from the tables above or a single printable
    character (which is what a text field inserts). Unknown escape sequences
    are swallowed whole rather than typed: a mouse report or an unmapped
    function key must not end up inside somebody's opening task.
    """
    keys: List[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\x1b":
            rest = text[i + 1:]
            if not rest:
                keys.append("escape")
                i += 1
                continue
            matched, key = "", ""
            for seq, name in _SEQUENCES.items():
                if rest.startswith(seq) and len(seq) > len(matched):
                    matched, key = seq, name
            if matched:
                keys.append(key)
                i += 1 + len(matched)
                continue
            if rest[0] in "[O":
                j = 1
                while j < len(rest) and not ("@" <= rest[j] <= "~"):
                    j += 1
                i += 1 + min(j + 1, len(rest))
                continue
            keys.append("escape")
            i += 1
            continue
        name = _CONTROLS.get(ch)
        if name:
            keys.append(name)
        elif ch >= " ":
            keys.append(ch)
        i += 1
    return keys


# --------------------------------------------------------------------------- #
# fields
# --------------------------------------------------------------------------- #
@dataclass
class Option:
    """One answer in a picker."""

    label: str
    value: Any
    detail: str = ""
    #: Shown but unpickable -- a harness that is not installed, a workspace
    #: whose directory is gone. Hiding those would read as "claunch does not
    #: know about pi", which is the wrong thing to learn.
    disabled: bool = False


@dataclass
class Field:
    key: str
    label: str
    #: Section heading this field opens, if any (drawn above it).
    section: str = ""
    #: What this field is for, one line, shown while it has the cursor.
    hint: str = ""
    hidden: bool = False
    disabled: bool = False
    #: Why it is disabled -- shown in place of the value, so a greyed-out row
    #: never leaves the reader guessing.
    disabled_note: str = ""

    @property
    def selectable(self) -> bool:
        return not self.hidden and not self.disabled

    def display(self) -> str:  # pragma: no cover - overridden
        return ""


@dataclass
class ChoiceField(Field):
    options: List[Option] = dataclass_field(default_factory=list)
    index: int = 0
    #: Rendered when there is nothing to pick from.
    empty: str = "(none available)"

    @property
    def value(self) -> Any:
        if not self.options:
            return None
        return self.options[self.index].value

    def display(self) -> str:
        if not self.options:
            return self.empty
        opt = self.options[self.index]
        return f"{opt.label}  {opt.detail}".rstrip() if opt.detail else opt.label

    def select(self, value: Any) -> bool:
        """Move to the option holding ``value``; False when there is none."""
        for i, opt in enumerate(self.options):
            if opt.value == value and not opt.disabled:
                self.index = i
                return True
        return False

    def cycle(self, step: int) -> None:
        """Next/previous pickable option, without wrapping past the ends."""
        i = self.index
        for _ in range(len(self.options)):
            i += step
            if not 0 <= i < len(self.options):
                return
            if not self.options[i].disabled:
                self.index = i
                return


@dataclass
class MultiField(ChoiceField):
    """A picker where several answers stand at once (mesh ``--connect``)."""

    chosen: List[Any] = dataclass_field(default_factory=list)

    @property
    def value(self) -> List[Any]:
        return list(self.chosen)

    def toggle(self, index: int) -> None:
        if not 0 <= index < len(self.options):
            return
        value = self.options[index].value
        if value in self.chosen:
            self.chosen.remove(value)
        else:
            self.chosen.append(value)

    def display(self) -> str:
        if not self.options:
            return self.empty
        if not self.chosen:
            return "(the whole mesh)"
        return ", ".join(str(v) for v in self.chosen)


@dataclass
class TextField(Field):
    text: str = ""
    placeholder: str = ""

    @property
    def value(self) -> str:
        return self.text.strip()

    def display(self) -> str:
        return self.text if self.text else self.placeholder


@dataclass
class ActionField(Field):
    """The Create row -- a field, so Enter means the same thing everywhere."""

    def display(self) -> str:
        return ""


# --------------------------------------------------------------------------- #
# where the answers come from
# --------------------------------------------------------------------------- #
class Sources:
    """The closed sets the form offers, and where each one is read from.

    An object rather than a dict of lists because two of them depend on an
    answer given *in* the form: the workflows declared in a directory follow
    the Directory picker, and a mesh's roster follows the Mesh picker. Tests
    subclass this; :class:`DaemonSources` is the real one.
    """

    def harnesses(self) -> List[dict]:
        return [{"name": "claude", "available": True, "description": ""}]

    def profiles(self) -> List[str]:
        return []

    def workspaces(self) -> List[dict]:
        return []

    def roles(self) -> List[dict]:
        return []

    def sessions(self) -> List[dict]:
        return []

    def resumable(self) -> List[dict]:
        return []

    def spawn_report(self, parent: str) -> dict:
        """What ``parent`` may spawn right now: the policy, and its budget.

        The daemon answers this per session rather than per machine, and the
        spawn form is built out of it -- which fields are unlockable, which
        workspaces a child may be sent to, and whether there is a slot left at
        all. Empty when it cannot be asked, which reads as "nothing unlocked".
        """
        return {}

    def meshes(self) -> List[dict]:
        return []

    def mesh_of(self, session: str) -> str:
        """The mesh ``session`` is a member of, or "" -- the child's default."""
        return ""

    def members(self, mesh: str) -> List[str]:
        return []

    def workflows(self, cwd: str) -> List[str]:
        return []

    def git(self, cwd: str) -> dict:
        """What ``cwd`` looks like to git: ``repo``, ``branch``, ``branches``,
        ``worktrees``.

        One question, because the worktree rows need all four and they have
        to agree: offering a branch list read from one directory beside a
        worktree list read from another is how a picker starts lying.
        """
        return {"repo": False, "branch": "", "branches": [], "worktrees": []}


class DaemonSources(Sources):
    """The live sets: the daemon's API for everything it knows, git for the
    rest.

    Every list is fetched once and cached. The form is open for seconds, and a
    refetch mid-form would be a picker that changes under the cursor -- the
    web UI polls because it is a dashboard that stays open, which this is not.
    A daemon that cannot answer leaves that one picker empty rather than
    failing the wizard: an old daemon still creates sessions perfectly well.
    """

    def __init__(self, client) -> None:
        self.client = client
        self._cache: Dict[str, Any] = {}

    def _get(self, key: str, path: str, field: str, default):
        if key in self._cache:
            return self._cache[key]
        try:
            doc = self.client.get(path)
            value = doc.get(field) or default
        except Exception:
            value = default
        self._cache[key] = value
        return value

    def harnesses(self) -> List[dict]:
        return self._get(
            "harnesses", "/api/harnesses", "harnesses",
            [{"name": "claude", "available": True, "description": ""}],
        )

    def profiles(self) -> List[str]:
        return self._get("profiles", "/api/profiles", "profiles", [])

    def workspaces(self) -> List[dict]:
        return self._get("workspaces", "/api/workspaces", "workspaces", [])

    def roles(self) -> List[dict]:
        return self._get("roles", "/api/roles", "roles", [])

    def sessions(self) -> List[dict]:
        return self._get("sessions", "/api/sessions", "sessions", [])

    def resumable(self) -> List[dict]:
        # A session with no pinned conversation has nothing to resume; an
        # exited one does, and picking that up elsewhere is the whole point.
        return [s for s in self.sessions() if s.get("conversation_id")]

    def spawn_report(self, parent: str) -> dict:
        key = "spawn:" + parent
        if key not in self._cache:
            from urllib.parse import quote

            try:
                self._cache[key] = self.client.get(
                    f"/api/sessions/{quote(parent)}/children"
                )
            except Exception:
                # An unknown parent, or a daemon too old to report: the form
                # falls back to "nothing unlocked", which is the policy's own
                # default and refuses nothing the daemon would have allowed.
                self._cache[key] = {}
        return self._cache[key]

    def meshes(self) -> List[dict]:
        return self._get("meshes", "/api/mesh", "meshes", [])

    def mesh_of(self, session: str) -> str:
        for m in self.meshes():
            for x in m.get("members") or []:
                if x.get("session") == session:
                    return str(m.get("name") or "")
        return ""

    def members(self, mesh: str) -> List[str]:
        for m in self.meshes():
            if m.get("name") == mesh:
                return [
                    str(x.get("handle") or "")
                    for x in (m.get("members") or [])
                    if x.get("handle")
                ]
        return []

    def workflows(self, cwd: str) -> List[str]:
        key = "wf:" + cwd
        if key in self._cache:
            return self._cache[key]
        try:
            from urllib.parse import quote

            doc = self.client.get(f"/api/cflow/workflows?cwd={quote(cwd)}")
            names = [
                (w.get("name") if isinstance(w, dict) else w)
                for w in (doc.get("workflows") or [])
                if not (isinstance(w, dict) and w.get("error"))
            ]
        except Exception:
            names = []
        self._cache[key] = [n for n in names if n]
        return self._cache[key]

    def git(self, cwd: str) -> dict:
        key = "git:" + cwd
        if key in self._cache:
            return self._cache[key]
        from urllib.parse import quote

        try:
            doc = self.client.get(f"/api/git?cwd={quote(cwd)}")
        except Exception:
            # Asked of the daemon rather than of git directly, because the
            # directory being described is the daemon's -- a parent's cwd, a
            # registered workspace -- and only the same-machine case makes
            # the two readings identical. A daemon too old to answer leaves
            # that case working.
            doc = worktree.info(cwd)
        self._cache[key] = doc
        return doc




# --------------------------------------------------------------------------- #
# the worktree rows, which both forms ask exactly the same way
# --------------------------------------------------------------------------- #
def worktree_fields(auto_detail: str, section: str = "") -> List[Field]:
    """The four rows that put a launch in a checkout of its own.

    Four and not one because they are four different questions and three of
    them only exist once the one above has been answered: *which* checkout,
    *called* what, brought up to date or not, and up to date with *what*.

    They are built here rather than in each form because a child asks them
    identically to a parent -- the directory being branched differs, and
    nothing else does.
    """
    return [
        ChoiceField(
            key="worktree", label="Worktree", section=section,
            hint="a checkout of its own, so this agent cannot collide with "
                 "another working in the same repository",
            options=[],
        ),
        TextField(
            key="worktree_name", label="Worktree name",
            placeholder="(auto)",
            hint="also the branch name; an existing one is returned to, not rebuilt",
        ),
        ChoiceField(
            key="update", label="Update",
            hint="a checkout you come back to is as far behind as the day you "
                 "left it",
            options=[
                Option("(no) - open it exactly as you left it", False),
                Option("yes - rebase it onto a branch first", True),
            ],
        ),
        ChoiceField(
            key="rebase_onto", label="Rebase onto",
            hint="the branch to catch up with; a rebase that cannot be done "
                 "cleanly refuses the launch rather than half-doing it",
            options=[],
        ),
    ]


def sync_worktree(form: "Form", cwd: str, *, allowed: bool = True, note: str = "") -> None:
    """Rebuild the worktree rows for ``cwd``, and hide the ones that do not
    apply yet.

    ``allowed`` is the policy's answer (``spawn.allow_worktree``); a locked
    row is greyed with the key that opens it rather than removed, while a
    directory that is no repository at all takes the rows away entirely --
    there is nothing there to be wrong about.
    """
    wt = form.field("worktree")
    signature = (cwd, allowed)
    if getattr(form, "_worktrees_for", None) != signature:
        form._worktrees_for = signature
        keep = wt.value
        info = form.sources.git(cwd) if cwd else {}
        if not allowed:
            wt.options, wt.hidden, wt.disabled = [], False, True
            wt.disabled_note = note
        elif info.get("repo"):
            wt.options = [
                Option("(none) - work in the directory as it stands", False),
                Option("new worktree", "", auto_detail(form)),
                Option("new worktree, named...", NAME_IT),
            ] + [
                Option(n, n, "existing") for n in (info.get("worktrees") or [])
            ]
            wt.index = 0
            wt.select(keep)
            wt.hidden, wt.disabled = False, False
        else:
            wt.options, wt.hidden, wt.disabled = [], True, False

        # The branch this checkout is cut from is the one worth catching up
        # with, so it leads the list: `master` for a launch from the main
        # checkout, the parent's own branch for a child.
        base = form.field("rebase_onto")
        here = info.get("branch") or ""
        base.options = (
            [Option(here, here, "the branch this one is cut from")] if here else []
        ) + [Option(b, b) for b in (info.get("branches") or []) if b != here]
        base.index = 0

    # Re-read every pass, not only when the list is rebuilt: a child's
    # auto-name is made of answers given further down the form, and a picker
    # showing the name it *would* have had is worse than showing none.
    for opt in wt.options:
        if opt.value == "":
            opt.detail = auto_detail(form)

    reusing = isinstance(wt.value, str) and wt.value not in ("", NAME_IT)
    form.field("worktree_name").hidden = not wt.selectable or wt.value != NAME_IT
    # Only a *reused* checkout can be behind: a new one is cut from the
    # repository as it stands, so there is nothing for it to catch up on.
    form.field("update").hidden = not wt.selectable or not reusing
    form.field("rebase_onto").hidden = (
        form.field("update").hidden or not form.value("update")
    )


def auto_detail(form: "Form") -> str:
    """What "new worktree" would call itself, spelled out in the picker."""
    name = form.auto_worktree_name()
    return f"auto-named: {name}" if name else "auto-named"


def check_worktree(form: "Form") -> List[tuple]:
    """``(field, why)`` for a worktree answer the launch could not carry out."""
    out: List[tuple] = []
    if form.field("worktree_name").selectable and form.value("worktree_name"):
        try:
            worktree.validate_name(form.value("worktree_name"))
        except worktree.WorktreeError as exc:
            out.append(("worktree_name", str(exc)))
    if form.value("update") and not form.value("rebase_onto"):
        out.append((
            "rebase_onto",
            "nothing to rebase onto: this repository reports no branches, so "
            "pick 'no' under Update",
        ))
    return out


def worktree_answer(form: "Form") -> tuple:
    """``(worktree choice, rebase base)`` in the spelling the flags use."""
    wt = form.field("worktree")
    choice = wt.value
    if not wt.selectable or choice is False or choice is None:
        # Answered, and answered "no": leaving it as ASK would put the old
        # y/N prompt on the screen straight after a form that just asked.
        return worktree.NEVER, ""
    if choice == NAME_IT:
        choice = form.value("worktree_name") or form.auto_worktree_name()
    base = str(form.value("rebase_onto") or "") if form.value("update") else ""
    return choice, base

# --------------------------------------------------------------------------- #
# the form
# --------------------------------------------------------------------------- #
#: Width of the label column. The longest label ("Worktree name") plus air.
LABEL_W = 16

#: Modes. "form" moves between fields, "pick" is a picker open over one of
#: them, "edit" is a text field taking characters.
FORM, PICK, EDIT = "form", "pick", "edit"

_HELP = {
    FORM: "up/down move   left/right change   Enter open   Ctrl+S create   Esc cancel",
    PICK: "up/down move   Enter pick   Esc back",
    EDIT: "Enter accept   Esc discard   Ctrl+U clear",
}

_HELP_MULTI = "up/down move   Space toggle   Enter done   Esc back"


class Form:
    """A form's whole state: fields, cursor, mode, and what it says.

    Kept free of terminal I/O on purpose -- :meth:`handle` takes a key name
    and :meth:`render` returns lines. The loop that reads a keyboard and
    paints a console is :func:`run`, and everything worth testing is on this
    side of that line.

    Two commands build a session and neither is the other (see
    :mod:`cli_sessions`): ``new-session`` is the human's, ``spawn`` is a
    session's. They ask for different things, so they are two subclasses --
    what they share is the part with no opinion about sessions at all: rows,
    pickers, a text cursor, and how any of it looks in eighty columns.
    """

    #: Shown top left, and in front of a picker's own title.
    title = "claunch"

    def __init__(self, sources: Sources, *, cwd: str = "", defaults: Any = None) -> None:
        self.sources = sources
        self.cwd = os.path.abspath(cwd or os.getcwd())
        self.mode = FORM
        self.focus = 0
        self.error = ""
        #: Picker state: which option is under the cursor while mode is PICK.
        self.pick = 0
        #: Text editor state while mode is EDIT.
        self.buffer = ""
        self.cursor = 0
        self.fields: List[Field] = self._build(defaults)
        self._sync()
        self.focus = self._next_selectable(-1, +1)

    # -- what a subclass fills in ---------------------------------------- #
    def _build(self, defaults: Any) -> List[Field]:
        """The rows, in the order they are read."""
        raise NotImplementedError

    def _sync(self) -> None:
        """Re-derive what is offered and what is greyed out, after any change."""

    def _check(self) -> List[tuple]:
        """``(field, why)`` for everything that would be refused downstream."""
        return []

    def apply(self, args: Any) -> Any:
        """Write every answer onto the namespace the command would have built."""
        raise NotImplementedError

    def summary(self) -> str:
        """One line naming what is about to be built, as the form closes."""
        return "creating a session"

    def auto_worktree_name(self) -> str:
        """What "new worktree" is called when nobody names it.

        Empty means "whoever cuts it decides" -- which for ``new-session`` is
        :func:`worktree.default_name`, the pane plus the second. A child has
        no pane and is cut by the daemon, so :class:`SpawnWizard` answers
        with a name of its own rather than letting the daemon invent one.
        """
        return ""

    # -- lookups --------------------------------------------------------- #
    def field(self, key: str) -> Field:
        for f in self.fields:
            if f.key == key:
                return f
        raise KeyError(key)

    def value(self, key: str) -> Any:
        return getattr(self.field(key), "value", None)

    @property
    def current(self) -> Field:
        return self.fields[self.focus]

    def problems(self) -> List[str]:
        """What would make the daemon refuse -- checked before anything is built."""
        return [why for _, why in self._check()]

    # -- keys ------------------------------------------------------------ #
    def handle(self, key: str) -> Optional[str]:
        """Apply one key. Returns "create", "cancel", or None to keep going."""
        if self.mode == PICK:
            return self._handle_pick(key)
        if self.mode == EDIT:
            return self._handle_edit(key)
        return self._handle_form(key)

    def _next_selectable(self, start: int, step: int) -> int:
        i = start
        for _ in range(len(self.fields)):
            i += step
            if not 0 <= i < len(self.fields):
                return max(0, min(len(self.fields) - 1, start))
            if self.fields[i].selectable:
                return i
        return max(0, start)

    def _handle_form(self, key: str) -> Optional[str]:
        f = self.current
        if key in ("cancel", "escape"):
            return "cancel"
        if key == "submit":
            return self._submit()
        if key in ("up", "backtab"):
            self.focus = self._next_selectable(self.focus, -1)
        elif key in ("down", "tab"):
            self.focus = self._next_selectable(self.focus, +1)
        elif key in ("left", "right") and isinstance(f, ChoiceField) \
                and not isinstance(f, MultiField):
            f.cycle(+1 if key == "right" else -1)
            self.error = ""
            self._sync()
        elif key in ("enter", "space"):
            if isinstance(f, ActionField):
                return self._submit()
            if isinstance(f, ChoiceField):
                self.mode = PICK
                self.pick = f.index
            elif isinstance(f, TextField):
                self._start_edit(f)
        elif isinstance(f, TextField) and key == "backspace":
            self._start_edit(f)
            self._handle_edit("backspace")
        elif isinstance(f, TextField) and len(key) == 1 and key >= " ":
            self._start_edit(f)
            self._insert(key)
        return None

    def _submit(self) -> Optional[str]:
        found = self._check()
        if not found:
            return "create"
        key, self.error = found[0]
        field = self.field(key)
        if field.selectable:
            self.focus = self.fields.index(field)
        return None

    def _handle_pick(self, key: str) -> Optional[str]:
        f = self.current
        n = len(f.options)
        if key in ("escape", "cancel"):
            self.mode = FORM
        elif key == "up":
            self.pick = max(0, self.pick - 1)
        elif key == "down":
            self.pick = min(n - 1, self.pick + 1)
        elif key == "home":
            self.pick = 0
        elif key == "end":
            self.pick = max(0, n - 1)
        elif key == "pageup":
            self.pick = max(0, self.pick - 10)
        elif key == "pagedown":
            self.pick = min(max(0, n - 1), self.pick + 10)
        elif key == "space" and isinstance(f, MultiField):
            f.toggle(self.pick)
        elif key == "enter":
            if isinstance(f, MultiField):
                f.toggle(self.pick)
                self.mode = FORM
            elif 0 <= self.pick < n and not f.options[self.pick].disabled:
                f.index = self.pick
                self.mode = FORM
                self.error = ""
            elif 0 <= self.pick < n:
                # An unpickable entry that silently ignores Enter reads as a
                # broken key, so it says why it is there instead.
                self.error = (
                    f.options[self.pick].label + " "
                    + (f.options[self.pick].detail or "cannot be chosen")
                )
            hidden_before = [x.hidden for x in self.fields]
            self._sync()
            nxt = self.fields.index(f) + 1
            if (self.mode == FORM and nxt < len(self.fields)
                    and hidden_before[nxt] and self.fields[nxt].selectable):
                # A choice that reveals the field under it gave half an
                # answer: "named..." wants the name, a mesh wants a handle.
                # Landing there beats leaving the new row to be noticed.
                self.focus = nxt
        elif len(key) == 1 and key.isprintable():
            # Type-to-jump: the one thing a long list (resume, workspaces)
            # needs that arrows alone make tedious.
            lowered = key.lower()
            order = list(range(self.pick + 1, n)) + list(range(0, self.pick + 1))
            for i in order:
                if f.options[i].label.lower().startswith(lowered):
                    self.pick = i
                    break
        return None

    # -- text editing ---------------------------------------------------- #
    def _start_edit(self, f: TextField) -> None:
        self.mode = EDIT
        self.buffer = f.text
        self.cursor = len(self.buffer)
        self.error = ""

    def _insert(self, ch: str) -> None:
        self.buffer = self.buffer[: self.cursor] + ch + self.buffer[self.cursor:]
        self.cursor += len(ch)

    def _handle_edit(self, key: str) -> Optional[str]:
        f = self.current
        if key == "enter":
            f.text = self.buffer
            self.mode = FORM
            self._sync()
        elif key in ("escape", "cancel"):
            self.mode = FORM
        elif key == "backspace":
            if self.cursor:
                self.buffer = self.buffer[: self.cursor - 1] + self.buffer[self.cursor:]
                self.cursor -= 1
        elif key == "delete":
            self.buffer = self.buffer[: self.cursor] + self.buffer[self.cursor + 1:]
        elif key == "left":
            self.cursor = max(0, self.cursor - 1)
        elif key == "right":
            self.cursor = min(len(self.buffer), self.cursor + 1)
        elif key == "home":
            self.cursor = 0
        elif key == "end":
            self.cursor = len(self.buffer)
        elif key == "killline":
            self.buffer, self.cursor = "", 0
        elif key == "space":
            self._insert(" ")
        elif len(key) == 1 and key >= " ":
            self._insert(key)
        return None

    # -- rendering ------------------------------------------------------- #
    #: ANSI on. Turned off for tests and for a terminal that cannot colour.
    color = True

    def _sgr(self, text: str, code: str) -> str:
        return text if not self.color else "\x1b[" + code + "m" + text + "\x1b[0m"

    def render(self, cols: int, rows: int) -> List[str]:
        """The whole screen, as at most ``rows`` lines of at most ``cols`` columns.

        A list rather than a blob so a test can read the form the way a person
        would, and so the caller owns how lines are terminated (raw mode wants
        CRLF, a file does not).
        """
        cols = max(24, cols)
        rows = max(8, rows)
        head = self._head(cols)
        foot = self._foot(cols)
        body_rows = rows - len(head) - len(foot)
        body, cursor = (
            self._picker_body(cols) if self.mode == PICK else self._form_body(cols)
        )
        return head + self._window(body, cursor, body_rows) + foot

    def _head(self, cols: int) -> List[str]:
        title = self.title
        if self.mode == PICK:
            title += "  /  " + self.current.label
        # Truncated before it is styled: `fit` measures characters, and an
        # escape sequence measured as text would eat the title it decorates.
        return [self._sgr(fit(title, cols), "1"), ""]

    def _foot(self, cols: int) -> List[str]:
        f = self.current
        if self.mode == PICK and isinstance(f, MultiField):
            help_line = _HELP_MULTI
        else:
            help_line = _HELP[self.mode]
        styled = (
            self._sgr(fit("! " + self.error, cols), "31")
            if self.error
            else self._sgr(fit("  " + f.hint, cols), "2")
        )
        return ["", styled, self._sgr(fit("  " + help_line, cols), "2")]

    def _window(self, body: List[str], cursor: int, height: int) -> List[str]:
        """``height`` lines of ``body`` that keep line ``cursor`` in view."""
        if height <= 0:
            return []
        if len(body) <= height:
            return body + [""] * (height - len(body))
        top = max(0, min(cursor - height // 2, len(body) - height))
        window = body[top:top + height]
        # Say so when there is more above or below: a form that silently ends
        # at the bottom of a short terminal reads as a form with fewer fields.
        if top > 0:
            window[0] = self._sgr("  ^ more above", "2")
        if top + height < len(body):
            window[-1] = self._sgr("  v more below", "2")
        return window

    def _form_body(self, cols: int) -> "tuple":
        lines: List[str] = []
        cursor = 0
        section = ""
        for i, f in enumerate(self.fields):
            if f.hidden:
                continue
            if f.section and f.section != section:
                section = f.section
                lines.append("")
                lines.append(self._sgr("  " + f.section, "2"))
            focused = i == self.focus
            if focused:
                cursor = len(lines)
            lines.append(self._row(f, focused, cols))
        return lines, cursor

    def _row(self, f: Field, focused: bool, cols: int) -> str:
        mark = self._sgr(">", "1;32") if focused else " "
        if isinstance(f, ActionField):
            body = "[ " + f.label + " ]"
            return " " + mark + " " + (
                self._sgr(body, "7") if focused else self._sgr(body, "1")
            )
        label = pad(f.label, LABEL_W)
        if f.disabled:
            return self._sgr(fit("   " + label + (f.disabled_note or "-"), cols), "2")
        if focused and self.mode == EDIT and isinstance(f, TextField):
            value = self._editing(cols - LABEL_W - 4)
        else:
            shown = f.display()
            value = fit(shown, max(4, cols - LABEL_W - 4))
            if isinstance(f, TextField) and not f.text:
                value = self._sgr(value, "2")
        return " " + mark + " " + (self._sgr(label, "1") if focused else label) + value

    def _editing(self, cols: int) -> str:
        """The buffer with a visible caret, scrolled to keep the caret in view."""
        text = self.buffer
        start = 0
        if self.cursor > cols - 1:
            start = self.cursor - (cols - 1)
        shown = text[start:start + cols]
        at = self.cursor - start
        head, ch, tail = shown[:at], shown[at:at + 1] or " ", shown[at + 1:]
        return head + (self._sgr(ch, "7") if self.color else "|" + ch) + tail

    def _picker_body(self, cols: int) -> "tuple":
        f = self.current
        lines: List[str] = []
        for i, opt in enumerate(f.options):
            chosen = isinstance(f, MultiField) and opt.value in f.chosen
            box = ("[x] " if chosen else "[ ] ") if isinstance(f, MultiField) else ""
            here = i == self.pick
            mark = self._sgr(">", "1;32") if here else " "
            label = box + opt.label
            if not isinstance(f, MultiField) and i == f.index:
                label += " *"
            text = fit((pad(label, 34) + (opt.detail or "")).rstrip(), cols - 4)
            if opt.disabled:
                text = self._sgr(text, "2")
            elif here:
                text = self._sgr(text, "1")
            lines.append(" " + mark + " " + text)
        if not lines:
            lines.append(self._sgr("   " + f.empty, "2"))
        return lines, self.pick



class Wizard(Form):
    """``new-session``: the human's door, with every field spelled out.

    Nothing is inherited here -- there is no parent to inherit from -- so the
    form is long, and its length is the honest shape of the command.
    """

    title = "claunch new-session"

    # -- construction ---------------------------------------------------- #
    def _build(self, d: Any) -> List[Field]:
        def get(name, fallback=None):
            return getattr(d, name, fallback) if d is not None else fallback

        # What each conditional list was last built for, so `_sync` refetches
        # only when the answer it follows has actually changed.
        self._workflows_for: Optional[str] = None
        self._members_for: Optional[str] = None
        self._worktrees_for: Optional[str] = None

        harnesses = self.sources.harnesses() or []
        harness = ChoiceField(
            key="harness", label="Harness",
            hint="the program the session runs; one not on PATH cannot be picked",
            options=[
                Option(
                    h.get("name", ""),
                    h.get("name", ""),
                    (h.get("description") or "") if h.get("available", True)
                    else "(not installed)",
                    disabled=not h.get("available", True),
                )
                for h in harnesses
            ],
        )
        harness.index = next(
            (i for i, o in enumerate(harness.options) if not o.disabled), 0
        )
        harness.select(get("harness") or "claude")

        profiles = self.sources.profiles() or []
        profile = ChoiceField(
            key="profile", label="Profile",
            hint="which login and config the harness runs under ('claunch list')",
            options=[Option("(no profile)", "")] + [Option(p, p) for p in profiles],
        )
        # A profile is what the claude harness needs, so the first real one is
        # the default: "(no profile)" should be an answer somebody gave, not
        # the one they get by not looking.
        if profiles:
            profile.index = 1
        if get("profile"):
            profile.select(get("profile"))

        directory = ChoiceField(
            key="cwd", label="Directory",
            hint="where the session runs; register more with 'claunch workspace add'",
            options=self._directory_options(get("cwd")),
        )
        directory.select(os.path.abspath(get("cwd")) if get("cwd") else self.cwd)


        roles = self.sources.roles() or []
        role = ChoiceField(
            key="role", label="Role",
            hint="a stance injected into the session's system prompt at every spawn",
            options=[Option("(no role)", "")]
            + [
                Option(
                    r.get("name", ""), r.get("name", ""),
                    ", ".join(r.get("aliases") or []),
                )
                for r in roles
            ],
        )
        role.select(get("role") or "")

        resume = ChoiceField(
            key="resume", label="Resume",
            hint="open an existing conversation instead of starting a new one",
            options=[
                Option("(new conversation)", None),
                Option("pick in the harness own picker", PICKER),
            ]
            + [
                Option(s.get("name", ""), s.get("name", ""), s.get("status", ""))
                for s in (self.sources.resumable() or [])
            ],
        )
        if get("resume") is not None:
            resume.select(get("resume") or PICKER)

        fork = ChoiceField(
            key="fork_session", label="Fork",
            hint="work on a COPY of that conversation, leaving the original untouched",
            options=[Option("no", False), Option("yes - copy it first", True)],
        )
        if get("fork_session"):
            fork.select(True)

        extra = get("args") or []
        args_field = TextField(
            key="args", label="Args",
            placeholder="(extra harness flags)",
            hint="passed through to the harness verbatim",
            text=" ".join(x for x in extra if x != "--"),
        )

        meshes = self.sources.meshes() or []
        mesh = ChoiceField(
            key="mesh", label="Mesh", section="START IT WORKING",
            hint="a mesh it joins at creation, so it can be talked to",
            options=[Option("(none)", "")]
            + [Option(m.get("name", ""), m.get("name", "")) for m in meshes],
        )
        mesh.select(get("mesh") or "")
        handle = TextField(
            key="handle", label="Handle",
            placeholder="(the session name)",
            hint="what it is called inside that mesh",
            text=get("handle") or "",
        )
        connect = MultiField(
            key="connect", label="Connect",
            hint="members it may message; none chosen leaves it the whole mesh",
            options=[], chosen=list(get("connect") or []),
        )

        workflow = ChoiceField(
            key="workflow", label="Workflow",
            hint="a cflow workflow started for it, from those declared in that "
                 "directory",
            options=[],
        )
        context = TextField(
            key="context", label="Context",
            placeholder="(what this run is about)",
            hint="the context string that workflow run carries",
            text=get("context") or "",
        )
        task = TextField(
            key="task", label="Opening task",
            placeholder="typed in once it has booted - what it is for",
            hint="the first thing it is told, sent when the harness is ready",
            text=get("task") or "",
        )

        restore = ChoiceField(
            key="restore", label="Restore", section="AFTERWARDS",
            hint="whether the daemon relaunches this session when it restarts",
            options=[
                Option("(daemon default)", None),
                Option("relaunch it on daemon restart", True),
                Option("do not relaunch it", False),
            ],
        )
        restore.select(get("restore"))
        attach = ChoiceField(
            key="attach", label="Attach",
            hint="take over this terminal right away (Ctrl+] detaches; the "
                 "session lives on)",
            options=[
                Option("no - leave it running in the daemon", False),
                Option("yes - attach this terminal now", True),
            ],
        )
        attach.select(bool(get("attach")))

        name = TextField(
            key="name", label="Name", placeholder="(auto)",
            hint="the session's name -- how every other command refers to it",
            text=get("name") or "",
        )

        return [
            name, harness, profile, directory, *worktree_fields(""), role,
            resume, fork,
            args_field, mesh, handle, connect, workflow, context, task,
            restore, attach,
            ActionField(key="create", label="Create session"),
        ]

    def _directory_options(self, preset: Optional[str]) -> List[Option]:
        """This directory first, then the registered workspaces.

        The CLI's own default is the directory you are standing in, and the
        wizard must not quietly move a launch somewhere else -- the workspace
        registry is what the *web* form has instead of a current directory,
        not a replacement for one.
        """
        seen = {os.path.normcase(self.cwd)}
        options = [Option("this directory", self.cwd, self.cwd)]
        for w in self.sources.workspaces() or []:
            path = w.get("path") or ""
            if not path or os.path.normcase(path) in seen:
                continue
            seen.add(os.path.normcase(path))
            options.append(
                Option(
                    w.get("name") or path, path,
                    path if w.get("exists", True) else path + " (missing)",
                    disabled=not w.get("exists", True),
                )
            )
        if preset:
            full = os.path.abspath(preset)
            if os.path.normcase(full) not in seen:
                options.append(Option(full, full, ""))
        return options

    # -- dependencies between fields ------------------------------------- #
    def _sync(self) -> None:
        """Re-derive what is offered and what is greyed out.

        Run after every change, because most of this form is conditional: the
        worktree question only exists inside a repository, ``--fork-session``
        is claude's "use with --resume" and is a *rejected* choice without
        one, and the workflows on offer are the ones declared in the directory
        currently picked.
        """
        claude = self.value("harness") == "claude"
        for key in ("role", "resume"):
            f = self.field(key)
            f.disabled = not claude
            f.disabled_note = "the claude harness only"
        fork = self.field("fork_session")
        resuming = self.value("resume") is not None
        fork.disabled = not (claude and resuming)
        fork.disabled_note = "needs a conversation to fork"
        if fork.disabled:
            fork.select(False)

        cwd = self.value("cwd") or self.cwd
        sync_worktree(self, cwd)

        mesh = self.value("mesh") or ""
        self.field("handle").hidden = not mesh
        connect = self.field("connect")
        if self._members_for != mesh:
            self._members_for = mesh
            members = self.sources.members(mesh) if mesh else []
            connect.options = [Option(h, h) for h in members]
            connect.chosen = [c for c in connect.chosen if c in members]
        connect.hidden = not mesh or not connect.options

        wf = self.field("workflow")
        if self._workflows_for != cwd:
            self._workflows_for = cwd
            keep = wf.value
            names = self.sources.workflows(cwd) or []
            wf.options = [Option("(none)", "")] + [Option(n, n) for n in names]
            wf.index = 0
            if keep:
                wf.select(keep)
        self.field("context").hidden = not self.value("workflow")

    # -- validation ------------------------------------------------------ #
    def _check(self) -> List[tuple]:
        """``(field, why)`` for everything that would be refused downstream.

        Paired with the field rather than reported on its own so Create can
        put the cursor where the answer has to change -- a complaint printed
        beside a cursor that is somewhere else is a complaint about nothing.
        """
        out: List[tuple] = []
        if not self.value("harness"):
            out.append((
                "harness",
                "no harness is installed here; 'claunch harnesses' lists them",
            ))
        if self.value("harness") == "claude" and not self.value("profile"):
            out.append((
                "profile",
                "the claude harness needs a profile - pick one, or make one "
                "first with 'claunch add <name>'",
            ))
        out.extend(check_worktree(self))
        return out

    # -- the answers ----------------------------------------------------- #
    def apply(self, args: Any) -> Any:
        """Write every answer onto ``args``, in ``new-session``'s own spelling.

        The namespace argparse would have built, so the wizard adds a door to
        the command rather than a second implementation of it: everything past
        this point -- the refusal from inside a session, the worktree, the
        onboarding payload, the attach -- is the code that has always run.
        """
        args.name = self.value("name")
        args.harness = self.value("harness")
        args.profile = self.value("profile") or None
        args.cwd = self.value("cwd") or self.cwd

        args.worktree, args.rebase_onto = worktree_answer(self)

        args.role = self.value("role") or None
        resume = self.value("resume")
        args.resume = None if resume is None else ("" if resume == PICKER else resume)
        args.fork_session = bool(self.value("fork_session")) and args.resume is not None

        text = self.field("args").value
        try:
            args.args = shlex.split(text, posix=os.name != "nt") if text else []
        except ValueError:
            args.args = text.split()

        args.mesh = self.value("mesh") or None
        args.handle = (self.value("handle") or None) if args.mesh else None
        args.connect = list(self.value("connect") or []) if args.mesh else []
        args.workflow = self.value("workflow") or None
        args.context = (self.value("context") or None) if args.workflow else None
        args.task = self.value("task") or None
        args.restore = self.value("restore")
        args.attach = bool(self.value("attach"))
        return args

    def summary(self) -> str:
        """One line naming what is about to be built, printed as the form closes.

        The form is on the alternate screen and vanishes with it; without this
        the scrollback would show a session appearing out of nothing.
        """
        parts = [
            "harness " + str(self.value("harness")),
            "profile " + str(self.value("profile") or "(none)"),
            "in " + str(self.value("cwd")),
        ]
        wt, base = worktree_answer(self)
        if wt is not worktree.NEVER:
            parts.append("worktree " + (wt or "(auto)"))
            if base:
                parts.append("rebased onto " + base)
        for label, key in (("mesh", "mesh"), ("workflow", "workflow"),
                           ("role", "role")):
            if self.value(key):
                parts.append(label + " " + str(self.value(key)))
        if self.value("resume") is not None:
            parts.append("resuming " + (self.value("resume") or "(picker)"))
        return "creating: " + ", ".join(parts)



def _natural(name: str) -> tuple:
    """Sort key that reads digit runs as numbers, so ``s9`` precedes ``s10``.

    Session names are auto-generated with a counter, so a plain string sort
    puts the tenth session between the first and the second -- which reads as
    no order at all once a fleet passes nine.
    """
    return tuple(
        (1, int(part), "") if part.isdigit() else (0, 0, part)
        for part in re.split(r"(\d+)", name) if part
    )


def _can_parent(session: dict) -> bool:
    """Whether ``session`` can take a child at all.

    The daemon refuses a child of an exited session outright (its record is
    what a respawn would revive, not something to hang more work off), so this
    is the one fact about a parent the form can be sure of without asking.
    Slots and depth it cannot: those are one request per session, and the
    picker would spend a round trip on every row to grey out one of them.
    """
    return session.get("status") != "exited"


def _parent_rank(session: dict, here: str) -> tuple:
    """Order the Parent picker by what a caller is most likely to want.

    Their own session first -- ``spawn`` run inside one means *this* one, and
    the form should open on the answer the bare command would have given.
    Then the sessions that can take a child, then the ones that cannot, so the
    default is never a row the daemon will reject. Names break the tie, read
    as numbers.
    """
    name = session.get("name", "")
    return (name != here, not _can_parent(session), _natural(name))


def _parent_option(session: dict, here: str) -> Option:
    """One row of the Parent picker: the session, and why it can or cannot."""
    name = session.get("name", "")
    live = _can_parent(session)
    # Shown rather than hidden, like every other unpickable row: a session
    # missing from the list reads as gone, and this one is a respawn away.
    status = session.get("status", "") if live else "exited - respawn it first"
    detail = ", ".join(
        x for x in (
            status,
            session.get("profile") or session.get("harness") or "",
            session.get("cwd") or "",
        ) if x
    )
    label = f"{name} (you)" if name and name == here else name
    return Option(label, name, detail, disabled=not live)


class SpawnWizard(Form):
    """``spawn``: a CHILD of a session, and only what a child may be asked.

    The shorter form, and shorter for a reason. A child is a copy of its
    parent -- same harness, same profile, same directory -- and every field
    here is either an override the ``spawn`` policy has unlocked or part of
    the arrangement the child is born into. There is no profile row because a
    child runs under its parent's, no directory row because it inherits one
    (a registered *workspace* is the vouched-for exception), and no worktree
    row because the isolation was bought upstream: the parent is already
    standing in whatever checkout it was launched into.

    The parent picker is the field that has no equivalent in the other form,
    and it is the reason this one exists. ``spawn`` normally reads its parent
    from ``$CLAUNCH_SESSION``, which is set for agents and for nobody else --
    a person at a terminal has to name one, and naming one from memory is
    guessing at a session list the daemon can simply show.
    """

    title = "claunch spawn"

    #: The mesh picker's "none at all" entry. The API spells it exactly so.
    NO_MESH = "-"

    def _build(self, d: Any) -> List[Field]:
        def get(name, fallback=None):
            return getattr(d, name, fallback) if d is not None else fallback

        # Everything below the parent is rebuilt when the parent changes; this
        # remembers which one it was last built for.
        self._parent_for: Optional[str] = None
        self._mesh_for: Optional[str] = None
        self._workflows_for: Optional[str] = None
        self._worktrees_for: Optional[tuple] = None
        # Fixed once, not per render: a name that ticked over between the
        # picker showing it and Create sending it would cut a worktree under
        # a name nobody read.
        self._stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self._report: dict = {}
        #: workspace name -> path, so the workflow list can follow a pick that
        #: travels to the daemon as a name.
        self._paths: Dict[str, str] = {}

        sessions = self.sources.sessions() or []
        here = os.environ.get("CLAUNCH_SESSION") or ""
        parent = ChoiceField(
            key="parent", label="Parent",
            hint="the session this one becomes a child of",
            options=[
                _parent_option(s, here)
                for s in sorted(sessions, key=lambda s: _parent_rank(s, here))
            ],
            empty="(no sessions yet - 'claunch new-session' makes the first)",
        )
        # Land on something that can actually take a child: an exited session
        # is offered but refused, so it must not be what the form defaults to.
        parent.index = next(
            (i for i, o in enumerate(parent.options) if not o.disabled), 0
        )
        parent.select(get("parent") or here or "")

        name = TextField(
            key="name", label="Name", placeholder="(auto)",
            hint="the child's session name, how every other command refers to it",
            text=get("name") or "",
        )
        harness = ChoiceField(
            key="harness", label="Harness",
            hint="a different program for the child (spawn.allow_harness "
                 "decides whether it may be one)",
            options=[],
        )
        workspace = ChoiceField(
            key="workspace", label="Workspace",
            hint="a registered directory to run the child in instead of its "
                 "parent's -- the only way a child changes directory",
            options=[],
        )

        mesh = ChoiceField(
            key="mesh", label="Mesh", section="START IT WORKING",
            hint="the mesh the child is enrolled in, so parent and child can "
                 "talk at all",
            options=[],
        )
        handle = TextField(
            key="handle", label="Handle", placeholder="(the session name)",
            hint="what the child is called inside that mesh",
            text=get("handle") or "",
        )
        role = ChoiceField(
            key="role", label="Role",
            hint="the child's stance, injected into its system prompt",
            options=[Option("(no role)", "")]
            + [
                Option(
                    r.get("name", ""), r.get("name", ""),
                    ", ".join(r.get("aliases") or []),
                )
                for r in (self.sources.roles() or [])
            ],
        )
        role.select(get("role") or "")
        connect = MultiField(
            key="connect", label="Connect",
            hint="other members the child may message; it can always reach "
                 "its parent",
            options=[], chosen=list(get("connect") or []),
        )
        workflow = ChoiceField(
            key="workflow", label="Workflow",
            hint="a cflow workflow started for the child, from those declared "
                 "in the directory it will run in",
            options=[],
        )
        context = TextField(
            key="context", label="Context",
            placeholder="(what this run is about)",
            hint="the context string that workflow run carries",
            text=get("context") or "",
        )
        task = TextField(
            key="task", label="Opening task",
            placeholder="typed into the child once it has booted - what it is for",
            hint="why this child exists; without it, it boots knowing nothing",
            text=get("task") or "",
        )
        return [
            parent, name, harness, workspace, *worktree_fields(""),
            mesh, handle, role, connect, workflow, context, task,
            ActionField(key="create", label="Spawn child"),
        ]

    def auto_worktree_name(self) -> str:
        """A child's own name, or its parent's, plus the second.

        `new-session` names an unnamed worktree after the Herdr pane, which a
        child does not have -- and the daemon, which cuts this one, has no
        pane either and would fall back to a bare constant. The session it is
        *for* is the honest answer, and it makes `git worktree list` afterwards
        say which agent each checkout belongs to.
        """
        who = self.value("name") or self.value("parent") or "child"
        who = re.sub(r"[^A-Za-z0-9._-]+", "-", str(who)).strip("-") or "child"
        return f"{who}-{self._stamp}"

    # -- lookups against the parent -------------------------------------- #
    def _session(self, name: str) -> dict:
        for s in self.sources.sessions() or []:
            if s.get("name") == name:
                return s
        return {}

    def _child_cwd(self) -> str:
        """Where the child will actually run -- a workspace, or the parent's."""
        chosen = self.value("workspace") or ""
        if chosen:
            return self._paths.get(chosen, "")
        return self._session(self.value("parent") or "").get("cwd") or ""

    def _mesh_now(self) -> str:
        """The mesh the child lands in: the one picked, or the parent's own."""
        picked = self.value("mesh") or ""
        if picked == self.NO_MESH:
            return ""
        return picked or self.sources.mesh_of(self.value("parent") or "")

    # -- dependencies between fields ------------------------------------- #
    def _sync(self) -> None:
        """Re-derive the form from the parent, then from what was picked.

        The parent decides most of this form -- what the child would inherit,
        and what the policy lets it override -- so the daemon's spawn report
        is read once per parent and the rows are rebuilt from it. Reading it
        here rather than provoking a refusal is the point of the whole form:
        a session with no slots left says so on the Parent row, before
        anything else has been filled in.
        """
        parent = self.value("parent") or ""
        if self._parent_for != parent:
            self._parent_for = parent
            self._report = self.sources.spawn_report(parent) if parent else {}
            self._rebuild_for_parent(parent)

        mesh_field = self.field("mesh")
        no_mesh = mesh_field.value == self.NO_MESH
        self.field("handle").hidden = no_mesh
        connect = self.field("connect")
        mesh = self._mesh_now()
        if self._mesh_for != mesh:
            self._mesh_for = mesh
            members = [
                h for h in (self.sources.members(mesh) if mesh else [])
                if h != parent
            ]
            connect.options = [Option(h, h) for h in members]
            connect.chosen = [c for c in connect.chosen if c in members]
        connect.hidden = no_mesh or not connect.options

        cwd = self._child_cwd()
        # Cut from where the child will actually run: a worktree of the
        # workspace it was sent to, or of the parent's own checkout.
        sync_worktree(
            self, cwd,
            allowed="worktree" in (self._report.get("may_choose") or []),
            note="a child inherits its parent's directory (spawn.allow_worktree)",
        )
        wf = self.field("workflow")
        if self._workflows_for != cwd:
            self._workflows_for = cwd
            keep = wf.value
            names = self.sources.workflows(cwd) if cwd else []
            wf.options = [Option("(none)", "")] + [Option(n, n) for n in names]
            wf.index = 0
            if keep:
                wf.select(keep)
        self.field("context").hidden = not self.value("workflow")

    def _rebuild_for_parent(self, parent: str) -> None:
        """The three rows whose *options* are the parent's, plus its budget."""
        info = self._session(parent)
        report = self._report

        used = report.get("children_used")
        left = report.get("children_remaining")
        self.field("parent").hint = (
            "the session this one becomes a child of"
            if used is None else
            f"child of this one: {used} running, {left} left "
            f"(depth {report.get('depth')}/{report.get('max_depth')})"
        )

        harness = self.field("harness")
        keep = harness.value
        allowed = report.get("spawnable_harnesses") or []
        harness.options = [
            Option("(the parent's" + (f": {info['harness']}" if info.get("harness") else "") + ")", "")
        ] + [Option(h, h) for h in allowed]
        harness.index = 0
        harness.select(keep)
        harness.disabled = not allowed
        harness.disabled_note = (
            "the child runs what its parent runs (spawn.allow_harness)"
        )

        workspace = self.field("workspace")
        keep = workspace.value
        spaces = report.get("workspaces")
        # Absent, not empty, when the policy has it locked: the report only
        # lists workspaces when a child may be sent to one.
        self._paths = {
            (w.get("name") or ""): (w.get("path") or "") for w in (spaces or [])
        }
        workspace.options = [
            Option(
                "(the parent's directory"
                + (f": {info['cwd']}" if info.get("cwd") else "") + ")",
                "",
            )
        ] + [
            Option(
                w.get("name") or "", w.get("name") or "",
                (w.get("path") or "") + ("" if w.get("exists", True) else " (missing)"),
                disabled=not w.get("exists", True),
            )
            for w in (spaces or [])
        ]
        workspace.index = 0
        workspace.select(keep)
        workspace.disabled = spaces is None
        workspace.disabled_note = (
            "the child inherits its parent's directory (spawn.allow_workspace)"
        )

        mesh = self.field("mesh")
        keep = mesh.value
        theirs = self.sources.mesh_of(parent)
        inherited = (
            f"(the parent's: {theirs})" if theirs
            else "(the parent's - it is in none, so one is opened for the pair)"
        )
        mesh.options = [
            Option(inherited, ""),
            Option("- no mesh at all", self.NO_MESH,
                   "the child cannot be messaged, and cannot report back"),
        ] + [
            Option(m.get("name", ""), m.get("name", ""))
            for m in (self.sources.meshes() or [])
            if m.get("name") and m.get("name") != theirs
        ]
        mesh.index = 0
        mesh.select(keep)

    # -- validation ------------------------------------------------------ #
    def _check(self) -> List[tuple]:
        out: List[tuple] = []
        if not self.value("parent"):
            out.append((
                "parent",
                "no parent to spawn from: 'spawn' makes a session's child, "
                "and there is no session here yet",
            ))
            return out
        blocked = self._report.get("blocked_by") or []
        if blocked:
            out.append((
                "parent",
                "; ".join(blocked) + " - pick another parent, or free a slot "
                "('claunch kill-session <child>')",
            ))
        out.extend(check_worktree(self))
        return out

    # -- the answers ----------------------------------------------------- #
    def apply(self, args: Any) -> Any:
        """Write the answers onto ``spawn``'s own namespace.

        Same reasoning as the other form: this fills in the flags rather than
        posting anything, so the policy, the refusal and the onboarding report
        are the daemon's, unchanged.
        """
        args.parent = self.value("parent")
        args.name = self.value("name")
        args.harness = self.value("harness") or None
        # The workspace travels as a NAME, which is what `-w` means and what
        # the API resolves -- a path would be the free-text directory that
        # spawn.allow_cwd exists to keep an agent away from.
        args.workspace = self.value("workspace") or None
        # A child's worktree is cut by the daemon, from the parent's own
        # repository -- so it travels as a NAME, never a path, and never as
        # `cwd`. An auto-named one is named here rather than there: the
        # daemon has no pane and no session of its own to name it after.
        choice, base = worktree_answer(self)
        args.worktree = (
            None if choice is worktree.NEVER
            else (choice or self.auto_worktree_name())
        )
        args.rebase_onto = base or None
        mesh = self.value("mesh") or ""
        args.mesh = mesh or None
        args.handle = (self.value("handle") or None) if mesh != self.NO_MESH else None
        args.role = self.value("role") or None
        args.connect = (
            list(self.value("connect") or []) if mesh != self.NO_MESH else []
        )
        args.workflow = self.value("workflow") or None
        args.context = (self.value("context") or None) if args.workflow else None
        args.task = self.value("task") or None
        return args

    def summary(self) -> str:
        parts = ["child of " + str(self.value("parent"))]
        for label, key in (
            ("harness", "harness"), ("workspace", "workspace"),
            ("role", "role"), ("workflow", "workflow"),
        ):
            if self.value(key):
                parts.append(label + " " + str(self.value(key)))
        choice, base = worktree_answer(self)
        if choice is not worktree.NEVER:
            parts.append("worktree " + (choice or self.auto_worktree_name()))
            if base:
                parts.append("rebased onto " + base)
        mesh = self.value("mesh") or ""
        parts.append(
            "no mesh" if mesh == self.NO_MESH
            else "mesh " + (mesh or self._mesh_now() or "(a new one for the pair)")
        )
        return "spawning: " + ", ".join(parts)

# --------------------------------------------------------------------------- #
# the loop
# --------------------------------------------------------------------------- #
#: Alternate screen + hidden cursor, so the form leaves no trace in the
#: scrollback of the terminal it borrowed.
_ENTER = "\x1b[?1049h\x1b[?25l"
_LEAVE = "\x1b[?25h\x1b[?1049l"


class WizardUnavailable(Exception):
    """Raised when there is no person at a terminal to fill the form in."""


def available() -> bool:
    """Whether the form can be shown: a real terminal, with a human at it.

    The same test the worktree question uses, and for the same reason -- every
    managed session runs on a PTY, so an agent's stdin is a terminal by every
    check Python can make, and a form painted into one is not filled in, it
    hangs the launch.
    """
    return worktree.interactive()


def require_terminal() -> None:
    """Raise unless there is somebody here to fill the form in.

    Separate from :func:`run` so the caller can refuse *before* it starts a
    daemon on the way to a form that cannot be shown: a scripted
    ``new-session --wizard`` should fail having changed nothing.
    """
    if not available():
        raise WizardUnavailable(
            "--wizard needs an interactive terminal -- it is a form, and there "
            "is nobody here to fill it in. Pass the fields as flags instead "
            "(the command's --help lists them)"
        )


def _write(text: str, stream=None) -> None:
    """Write ``text``, surviving a console that cannot encode all of it.

    A legacy Windows code page (cp949, cp1252) cannot represent every path or
    session name that could reach this screen, and an encoding error mid-frame
    would leave the terminal in raw mode with half a form on it.
    """
    out = stream or sys.stdout
    enc = getattr(out, "encoding", None) or "utf-8"
    try:
        out.write(text)
    except UnicodeEncodeError:
        out.write(text.encode(enc, "replace").decode(enc, "replace"))
    out.flush()


def _paint(wiz: Form, stream=None) -> None:
    import shutil

    size = shutil.get_terminal_size((100, 30))
    lines = wiz.render(size.columns - 1, size.lines - 1)
    # Home, then clear each line as it is drawn and the rest at the end:
    # clearing the whole screen first is what makes a full-redraw form flicker.
    frame = "\x1b[H" + "".join(line + "\x1b[K\r\n" for line in lines) + "\x1b[J"
    _write(frame, stream)


def run(
    args: Any,
    *,
    sources: Optional[Sources] = None,
    cwd: str = "",
    form: type = None,
) -> bool:
    """Fill ``args`` in from the form. False when the user backed out.

    ``args`` is the command's own namespace, so flags typed before
    ``--wizard`` arrive as the form's starting values -- ``claunch new -s api
    --wizard`` opens with the name already filled in. ``form`` picks which
    command is being answered (:class:`Wizard` for ``new-session``,
    :class:`SpawnWizard` for ``spawn``).
    """
    from . import attach as attach_mod

    require_terminal()
    wiz = (form or Wizard)(sources or Sources(), cwd=cwd, defaults=args)
    import codecs

    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    outcome = None
    _write(_ENTER)
    try:
        with attach_mod._RawTerminal():
            while outcome is None:
                _paint(wiz)
                data = attach_mod._read_stdin()
                if not data:  # stdin closed under us: the same as Esc
                    outcome = "cancel"
                    break
                for key in decode_keys(decoder.decode(data)):
                    outcome = wiz.handle(key)
                    if outcome:
                        break
    finally:
        _write(_LEAVE)
    if outcome != "create":
        print("cancelled; nothing was created", file=sys.stderr)
        return False
    wiz.apply(args)
    print(wiz.summary(), file=sys.stderr)
    return True
