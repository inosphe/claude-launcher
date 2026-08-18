"""``claunch new-session --wizard``: the same session, chosen from lists.

``new-session`` spells everything out as flags, which is what makes it
scriptable and what makes it hard to type: a session's harness, profile,
directory, role, mesh, workflow and worktree are all *closed sets* the daemon
already publishes, and a human typing them from memory is guessing at names a
picker could simply show. The web dashboard has had that form for a while
(harness/profile/directory/role/resume, then the "start it working" block);
this is the same form for a terminal, so a session can be built where the
person already is instead of in a browser.

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
import shlex
import sys
import unicodedata
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

    def resumable(self) -> List[dict]:
        return []

    def meshes(self) -> List[dict]:
        return []

    def members(self, mesh: str) -> List[str]:
        return []

    def workflows(self, cwd: str) -> List[str]:
        return []

    def worktrees(self, cwd: str) -> List[str]:
        """Names of worktrees this launch could move into, already on disk."""
        return []

    def is_repo(self, cwd: str) -> bool:
        return False


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

    def resumable(self) -> List[dict]:
        sessions = self._get("sessions", "/api/sessions", "sessions", [])
        # A session with no pinned conversation has nothing to resume; an
        # exited one does, and picking that up elsewhere is the whole point.
        return [s for s in sessions if s.get("conversation_id")]

    def meshes(self) -> List[dict]:
        return self._get("meshes", "/api/mesh", "meshes", [])

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

    def worktrees(self, cwd: str) -> List[str]:
        key = "wt:" + cwd
        if key not in self._cache:
            self._cache[key] = worktree_names(cwd)
        return self._cache[key]

    def is_repo(self, cwd: str) -> bool:
        key = "repo:" + cwd
        if key not in self._cache:
            self._cache[key] = worktree.repo_root(cwd) is not None
        return self._cache[key]


def worktree_names(cwd: str) -> List[str]:
    """Launcher worktrees of ``cwd``'s repository, newest git order kept.

    Offered as choices because reuse is the common case a name exists for:
    ``--worktree=review`` twice returns to that checkout with its branch and
    its uncommitted work intact, and remembering which names are taken is
    exactly what a picker is for.
    """
    root = worktree.repo_root(cwd)
    if root is None:
        return []
    base = worktree.worktrees_dir(root)
    done = worktree._git(["worktree", "list", "--porcelain"], cwd=str(root))
    if done.returncode != 0:
        return []
    names: List[str] = []
    for line in (done.stdout or "").splitlines():
        if not line.startswith("worktree "):
            continue
        path = os.path.abspath(line[len("worktree "):].strip())
        try:
            rel = os.path.relpath(path, str(base))
        except ValueError:  # different drive on Windows
            continue
        if rel.startswith(".."):
            continue
        name = rel.replace(os.sep, "/")
        if name and name != ".":
            names.append(name)
    return names


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


class Wizard:
    """The form's whole state: fields, cursor, mode, and what it says.

    Kept free of terminal I/O on purpose -- :meth:`handle` takes a key name
    and :meth:`render` returns lines. The loop that reads a keyboard and
    paints a console is :func:`run`, and everything worth testing is on this
    side of that line.
    """

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
        self._workflows_for: Optional[str] = None
        self._members_for: Optional[str] = None
        self._worktrees_for: Optional[str] = None
        self.fields: List[Field] = self._build(defaults)
        self._sync()
        self.focus = self._next_selectable(-1, +1)

    # -- construction ---------------------------------------------------- #
    def _build(self, d: Any) -> List[Field]:
        def get(name, fallback=None):
            return getattr(d, name, fallback) if d is not None else fallback

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

        wt = ChoiceField(
            key="worktree", label="Worktree",
            hint="a private checkout, so this agent cannot collide with another "
                 "working in the same repository",
            options=[],
        )
        wt_name = TextField(
            key="worktree_name", label="Worktree name",
            placeholder="(auto: pane + timestamp)",
            hint="also the branch name; an existing one is returned to, not rebuilt",
        )

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
            name, harness, profile, directory, wt, wt_name, role, resume, fork,
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
        wt = self.field("worktree")
        if self._worktrees_for != cwd:
            self._worktrees_for = cwd
            keep = wt.value
            if self.sources.is_repo(cwd):
                wt.options = [
                    Option("(none) - work in the directory as it stands", False),
                    Option("new worktree", "", "auto-named: pane + timestamp"),
                    Option("new worktree, named...", NAME_IT),
                ] + [Option(n, n, "existing") for n in self.sources.worktrees(cwd)]
                wt.index = 0
                wt.select(keep)
                wt.hidden = False
            else:
                wt.options = []
                wt.hidden = True
        self.field("worktree_name").hidden = wt.hidden or wt.value != NAME_IT

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
        if self.value("worktree") == NAME_IT and self.value("worktree_name"):
            try:
                worktree.validate_name(self.value("worktree_name"))
            except worktree.WorktreeError as exc:
                out.append(("worktree_name", str(exc)))
        return out

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
            self._sync()
            if self.mode == FORM and f.key == "worktree" and f.value == NAME_IT:
                # "named..." is only half an answer; land on the name itself.
                nxt = self.fields.index(f) + 1
                if self.fields[nxt].selectable:
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
        title = "claunch new-session"
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

        choice = self.value("worktree")
        if self.field("worktree").hidden or choice is False or choice is None:
            # Answered, and answered "no": leaving it as ASK would put the old
            # y/N prompt on the screen straight after a form that just asked.
            args.worktree = worktree.NEVER
        elif choice == NAME_IT:
            args.worktree = self.value("worktree_name") or ""
        else:
            args.worktree = choice

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
        wt = self.value("worktree")
        if not self.field("worktree").hidden and wt is not False:
            parts.append(
                "worktree " + (self.value("worktree_name") or "(auto)")
                if wt == NAME_IT else "worktree " + (wt or "(auto)")
            )
        for label, key in (("mesh", "mesh"), ("workflow", "workflow"),
                           ("role", "role")):
            if self.value(key):
                parts.append(label + " " + str(self.value(key)))
        if self.value("resume") is not None:
            parts.append("resuming " + (self.value("resume") or "(picker)"))
        return "creating: " + ", ".join(parts)


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
            "('claunch new-session --help')"
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


def _paint(wiz: Wizard, stream=None) -> None:
    import shutil

    size = shutil.get_terminal_size((100, 30))
    lines = wiz.render(size.columns - 1, size.lines - 1)
    # Home, then clear each line as it is drawn and the rest at the end:
    # clearing the whole screen first is what makes a full-redraw form flicker.
    frame = "\x1b[H" + "".join(line + "\x1b[K\r\n" for line in lines) + "\x1b[J"
    _write(frame, stream)


def run(args: Any, *, sources: Optional[Sources] = None, cwd: str = "") -> bool:
    """Fill ``args`` in from the form. False when the user backed out.

    ``args`` is ``new-session``'s own namespace, so flags typed before
    ``--wizard`` arrive as the form's starting values -- ``claunch new -s api
    --wizard`` opens with the name already filled in.
    """
    from . import attach as attach_mod

    require_terminal()
    wiz = Wizard(sources or Sources(), cwd=cwd, defaults=args)
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
