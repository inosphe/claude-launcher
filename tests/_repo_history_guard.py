"""The premise tree-reuse rests on, watched by a machine instead of by hand.

``tools/sweep.py`` accepts a green receipt recorded for a *different commit
with the same tree* (``find_receipt_by_tree``), and ``tools/changed_tests.py``
keys the worker's gate on the same identity. Both are sound only while one
thing is true:

    **no test's outcome depends on this repository's HEAD or its refs.**

A preview commit and the merge commit that lands the same candidates have
byte-identical trees and different histories. If a test read that history the
two would deserve different verdicts, and the tree key would hand one of them
the other's -- in the direction nobody notices, because the wrong answer is
*green*.

Until now the premise was a sentence in ``tools/sweep.py``'s docstring,
grepped by hand on 2026-08-26 and true that day. A hand check does not
survive the next round. This does.

What is forbidden, exactly
--------------------------
Not "no test may run git in this checkout". That rule is wider than the
premise, and it is already false here. What may not happen is a read of
something two same-tree commits disagree about. Two shapes, one of them safe:

* ``git cat-file -e 41fcfc8^{commit}`` names an object by its hash. The
  object database is shared by every commit and every worktree of this
  repository, so no pair of commits can disagree about the answer.
  ``tests/test_mergecheck.py::test_the_real_commits`` reads the real
  repository in exactly that way, deliberately, and is not a violation --
  which is why this rule is written about revisions and not about paths.
  (``tools/sweep.py`` used to say every git-touching test digs its own
  repository under ``tmp_path``. That test is the counter-example and the
  sentence was already wrong. It was wrong *harmlessly* -- hex object names
  cannot vary -- but nothing was checking which kind of wrong it was.)
* ``git log``, ``git rev-parse HEAD``, ``git describe``, ``git status``
  resolve HEAD or a ref. Those are precisely the parts that differ between
  two commits holding the same tree.

So a git command aimed at this repository is a violation when its subcommand
answers from the refs however it is called (:data:`REF_RELATIVE`), or when
one of its arguments is a *symbolic* revision rather than an object name
(:func:`symbolic`).

What this does NOT see
----------------------
Named here so it is not mistaken for more than it is:

* Only ``subprocess`` in the test process. A test that runs a *script* which
  then reads HEAD (``python tools/x.py`` with ``cwd`` inside this repository)
  spawns its git in a child, where this patch does not reach.
* Only git through ``subprocess``. ``os.system``, a git library, or a direct
  read of ``.git/HEAD`` all pass.
* ``GIT_DIR``/``GIT_WORK_TREE`` in a call's ``env`` are not followed: the
  target is read from ``-C`` or from ``cwd``.

Each of those is a way to break the premise without tripping this. What it
covers is the shape every git-touching test in this suite actually uses, and
the shape a new one would be written in.

And one thing it sees and lets through
--------------------------------------
:data:`EXEMPT_CALLERS` is a short table of places that may make an otherwise
refused read, each with its reason written beside it. There is one entry, and
a full sweep is what put it there: six tests reach ``git rev-parse
--abbrev-ref HEAD`` through ``worktree.current_branch`` because the product
code under them builds a display label. The read is real; the *dependency* is
not, and the evidence is that those six are green under every branch name
this fleet has run them from. The exemption is on the caller, not the
command -- the same command written in a test module is still a hard stop.

Kept as a table rather than a special case for the reason this whole file
exists: an exception you can count is not the same as one that is absent
because nobody looked.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Optional, Sequence, Union


class RepoHistoryRead(AssertionError):
    """A test read this repository's HEAD or refs. See this module's docstring."""


#: Subcommands whose answer comes from HEAD or the refs however they are
#: called, so no argument makes them safe against this repository. ``describe``
#: belongs here rather than below because its answer is decided by the *tags*,
#: which are refs, even when the commit it is describing is named by hash.
ALWAYS_REF_RELATIVE = frozenset(
    {
        "branch",
        "tag",
        "for-each-ref",
        "show-ref",
        "symbolic-ref",
        "reflog",
        "status",
        "describe",
        "blame",
        "bisect",
        "worktree",
        "stash",
        "switch",
        "checkout",
        "merge",
        "rebase",
        "pull",
        "push",
        "fetch",
    }
)

#: Subcommands that walk history but only from where they are pointed, and
#: fall back to **HEAD** when they are pointed nowhere. Naming a commit by
#: hash makes them safe -- the graph above a fixed object is the same in every
#: commit of this repository -- so what is forbidden is the implicit form.
#:
#: This distinction is not pedantry: it is the difference between banning
#: ``tests/test_mergecheck.py::test_the_real_commits`` and allowing it. That
#: test asks ``git merge-base 41fcfc8 744e88d`` of the real repository on
#: purpose, and two commits with one tree cannot answer it differently.
HEAD_BY_DEFAULT = frozenset(
    {"log", "shortlog", "whatchanged", "rev-list", "show", "name-rev"}
)

#: Subcommands that read a revision at all. Only these have their arguments
#: inspected -- ``git config user.name`` and ``git commit -m msg`` carry
#: word-shaped arguments that are not revisions, and a scan that did not know
#: the difference would call them history reads.
REV_TAKING = HEAD_BY_DEFAULT | frozenset(
    {
        "rev-parse",
        "cat-file",
        "diff",
        "diff-tree",
        "difftool",
        "range-diff",
        "cherry",
        "merge-base",
        "ls-tree",
        "archive",
        "merge-tree",
    }
)

#: Flags that take a separate value, so the word after them is not a revision.
_TAKES_VALUE = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace"})

#: An object name -- the thing two same-tree commits cannot disagree about.
_OBJECT_NAME = re.compile(r"^[0-9a-fA-F]{7,40}$")

#: The suffixes that navigate *from* a revision: ``^{commit}``, ``^2``, ``~3``.
#: They are part of the expression, not part of the name, and they do not
#: change which of the two -- object or ref -- decided where it started.
_PEEL = re.compile(r"(\^\{\w*\}|\^\d*|~\d*)+$")

#: Revision spellings that are HEAD or a ref by construction.
_REF_SPELLING = re.compile(r"HEAD|(^|[^\w])@($|[^\w])|^refs/|^origin/")

#: A word that could be a revision at all: anything else (a URL, a glob, a
#: config assignment) is not being resolved against this repository's refs.
_WORDLIKE = re.compile(r"^[\w./~^{}:-]+$")


def endpoints(arg: str) -> list:
    """The commits a revision *expression* starts from.

    A single argument can name more than one, and can bury the name inside
    punctuation that has nothing to do with history. All three of these
    appear in this repository's own code
    (``src/claude_launcher/mergecheck.py``):

    * ``41fcfc8`` -- one endpoint;
    * ``<base>...41fcfc8`` -- two, and a rule that only read the first would
      wave through ``<hash>...master``;
    * ``41fcfc8:tests/test_x.py`` -- one, with a path after it. Reading the
      whole token as a name is how the first version of this guard managed
      to refuse ``test_the_real_commits``, the one test it was written to
      keep allowing.

    An empty endpoint (``..master``, ``master..``) is git's shorthand for
    HEAD, so it is returned as the empty string and refused by the caller.
    """
    rev = arg.split(":", 1)[0]
    if not rev:
        return []                       # ``:path`` is the index, not history
    parts = rev.split("...") if "..." in rev else rev.split("..")
    return [_PEEL.sub("", p) for p in parts]


def names_object(arg: str) -> bool:
    """Does ``arg`` name commits by hash, and only by hash?"""
    if arg.startswith("-"):
        return False
    ends = endpoints(arg)
    return bool(ends) and all(_OBJECT_NAME.match(e or "") for e in ends)


def symbolic(arg: str, repo: Path) -> bool:
    """Is ``arg`` a revision that HEAD or the refs decide?

    Object names are not (``41fcfc8``, ``41fcfc8^{commit}``,
    ``41fcfc8:some/path``). Paths that exist in the tree are not -- that is a
    pathspec. Flags are not. What is left standing in a revision position is
    a branch, a tag, or HEAD, and those are exactly what two commits with one
    tree disagree about.
    """
    if arg.startswith("-"):
        return False
    if (repo / arg).exists():
        return False                    # a pathspec, not a revision
    if not _WORDLIKE.match(arg):
        return False                    # a URL, a glob, a message: not a rev
    for end in endpoints(arg):
        if not end or _REF_SPELLING.search(end) or not _OBJECT_NAME.match(end):
            return True
    return False


def _argv(args: Union[str, Sequence]) -> list:
    """Popen's ``args`` as a list of strings.

    A ``shell=True`` string is split on whitespace, on purpose: the callers
    in this repository that pass a string pass pytest command lines, not git,
    and whitespace is enough to tell those apart. Carrying a shell parser
    here would be more machinery than the question needs.
    """
    if isinstance(args, str):
        return args.split()
    return [str(a) for a in args]


def _is_git(argv: list) -> bool:
    return bool(argv) and Path(argv[0]).stem.lower() == "git"


def _target(argv: list, cwd: Optional[Union[str, Path]]) -> Path:
    """Which repository the command speaks to: ``-C`` if given, else ``cwd``."""
    for i, arg in enumerate(argv[1:], start=1):
        if arg == "-C" and i + 1 < len(argv):
            return Path(argv[i + 1])
        if arg.startswith("--git-dir="):
            return Path(arg.split("=", 1)[1])
    return Path(cwd) if cwd is not None else Path.cwd()


def _inside(path: Path, root: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return resolved == root or root in resolved.parents


def _bare(argv: list) -> list:
    """``argv[1:]`` with the pre-subcommand flags and their values removed."""
    rest, skip = [], False
    for arg in argv[1:]:
        if skip:
            skip = False
            continue
        if arg in _TAKES_VALUE:
            skip = True
            continue
        if arg.startswith("--git-dir=") or arg.startswith("--work-tree="):
            continue
        rest.append(arg)
    return rest


def offending(
    args: Union[str, Sequence], cwd: Optional[Union[str, Path]], root: Path
) -> Optional[str]:
    """The violation this call is, or ``None``. The pure, testable half.

    ``root`` is this repository's working tree (``pytestconfig.rootpath``).
    Every worktree of it lives underneath, so they are covered by the same
    comparison.
    """
    argv = _argv(args)
    if not _is_git(argv):
        return None
    target = _target(argv, cwd)
    if not _inside(target, Path(root).resolve()):
        return None  # a throwaway repository under tmp_path: the good case

    rest = _bare(argv)
    sub = next((a for a in rest if not a.startswith("-")), None)
    after = rest[rest.index(sub) + 1 :] if sub in rest else []
    if "--" in after:
        after = after[: after.index("--")]

    named = (
        next((a for a in after if symbolic(a, Path(root))), None)
        if sub in REV_TAKING
        else None
    )
    if sub in ALWAYS_REF_RELATIVE:
        why = f"`git {sub}` answers from HEAD and the refs"
    elif named is not None:
        why = f"`{named}` is a symbolic revision, not an object name"
    elif sub in HEAD_BY_DEFAULT and not any(names_object(a) for a in after):
        why = f"`git {sub}` with no revision named walks from HEAD"
    else:
        return None

    return (
        f"this test read {target}'s history: {' '.join(argv)}\n"
        f"  {why}, and HEAD and the refs are the one thing two commits with\n"
        f"  the same tree disagree about. tools/sweep.py and\n"
        f"  tools/changed_tests.py both reuse a green receipt across such a\n"
        f"  pair, so a test that reads history gets handed the other commit's\n"
        f"  verdict -- as a GREEN, which is why this is a hard stop and not a\n"
        f"  warning. Dig a repository under tmp_path (tests/test_sweep.py has\n"
        f"  the idiom), or name objects by hash if you really do mean this\n"
        f"  repository. Full reasoning: tests/_repo_history_guard.py."
    )


#: Reads this refuses in general and allows from one named place, with the
#: reason written down next to it. Kept in the idiom ``tools/changed_tests.py``
#: uses for ``EXPLICIT_GUARDS``: derive the rule, and keep the exceptions as a
#: short table you can count, rather than letting them be absent silently.
#:
#: Each entry is ``(module suffix, function, why)``. A refused call is allowed
#: when that function is somewhere on the stack.
EXEMPT_CALLERS = (
    (
        "claude_launcher/worktree.py",
        "current_branch",
        # A full sweep of this branch found six tests (test_prompt_input x2,
        # test_cli x3, test_spawn_api x1) reaching `git rev-parse --abbrev-ref
        # HEAD` through here. None of them asks for it: they exercise
        # cli/attach/api against a session whose cwd is this checkout, and
        # `worktree.pane_label` reads the branch to build a *display label*.
        #
        # The read is real and the guard was right to see it -- a branch name
        # is a ref, and two commits with one tree sit on different branches.
        # What is missing is a dependency: those six are green on master, on
        # every worker branch this fleet has run them from, and on the
        # integration previews -- names that all differ. A test whose outcome
        # turned on the branch name could not have survived that.
        #
        # So the exemption is about the *caller*, not about the command:
        # `rev-parse --abbrev-ref HEAD` written in a test module is still a
        # hard stop. What is allowed is product code reading its own checkout
        # for a label while a test happens to be driving it.
        #
        # Left on the board rather than closed: claunch-l8lh.
        "product code reading the branch for a display label; no assertion "
        "in this suite turns on it -- see claunch-l8lh",
    ),
)


def exempt() -> Optional[tuple]:
    """The :data:`EXEMPT_CALLERS` entry on the current stack, if any."""
    import inspect

    # ``context=0``: only the refused path reaches here, but reading source
    # lines for every frame to answer a yes/no question is work nobody asked
    # for, and it touches the disk from inside a subprocess call.
    for frame in inspect.stack(0):
        name = frame.function
        path = frame.filename.replace("\\", "/")
        for module, func, why in EXEMPT_CALLERS:
            if func == name and path.endswith(module):
                return (module, func, why)
    return None


class Guard:
    """The installed patch, so a test can prove it is armed and take it out."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.seen: list = []
        #: Refused-by-rule reads that an :data:`EXEMPT_CALLERS` entry let
        #: through. Kept so the exceptions can be counted, not just trusted.
        self.exempted: list = []
        self._real = None

    def install(self) -> "Guard":
        guard = self
        real = subprocess.Popen
        self._real = real

        class GuardedPopen(real):  # type: ignore[misc,valid-type]
            def __init__(self, args, *rest, **kw):
                problem = offending(args, kw.get("cwd"), guard.root)
                if problem is not None:
                    allowed = exempt()
                    if allowed is None:
                        guard.seen.append(problem)
                        raise RepoHistoryRead(problem)
                    guard.exempted.append((allowed, problem))
                super().__init__(args, *rest, **kw)

        subprocess.Popen = GuardedPopen  # type: ignore[misc]
        return self

    def uninstall(self) -> None:
        if self._real is not None:
            subprocess.Popen = self._real  # type: ignore[misc]
            self._real = None
