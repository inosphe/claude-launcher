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
refused read, each with its reason written beside it. There are two entries,
and what put each one there is a measurement rather than an argument: replace
the value the product code reads with a sentinel, and if the tests under it
stay green, the read is real but the *dependency* is not.

The first is ``worktree.current_branch``. Six tests reach ``git rev-parse
--abbrev-ref HEAD`` through it because the product code under them builds a
display label; with ``current_branch`` replaced by one that returns a fixed
sentinel for this repository, those three modules are ``83 passed in 35.25s``.
A branch name they depended on could not survive being replaced.

The second is ``daemon/runtime_state.code_snapshot``, which reads ``rev-parse
HEAD`` at boot so ``daemon.json`` can say which commit the daemon loaded --
the only moment that is knowable, since an edit or a checkout afterwards
erases it. Same measurement: with the boot path passing no snapshot,
``test_restart_gate`` goes green (``34 passed``, the remaining three failures
being ``test_daemon_wedge``, red on the untouched base as well). Both rows are
the same shape -- product code reading its own checkout while a test happens
to be driving it -- and neither is a test asking for the value.

An entry names a caller **and** the one command that caller may make. Both
halves are load-bearing. Without the caller, the command is blanket-allowed
and a test asserting on this repository's branch sails through. Without the
command, an exempt frame licenses whatever else runs beneath it -- rule (a)
skips test frames deliberately, so a test that intercepted
``subprocess.run`` and dispatched a different read would ride through on
somebody else's exemption.

Kept as a table rather than a special case for the reason this whole file
exists: an exception you can count is not the same as one that is absent
because nobody looked. Its size is pinned by a test, so it cannot grow
quietly.
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
        # The one command this caller may make. Without it the exemption is
        # about *who* asks and not *what* they asked, and it spreads: a test
        # that intercepts ``subprocess.run`` and dispatches some other read
        # of this repository would ride through on the exempt frame, since
        # rule (a) skips test frames on purpose. Nothing does that today.
        # Naming the command keeps each row's reach inside that row, which is
        # what has to be true before the table is ever allowed to grow.
        ("rev-parse", "--abbrev-ref", "HEAD"),
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
    (
        "claude_launcher/daemon/runtime_state.py",
        "code_snapshot",
        # The one command, for the same reason the row above names one. The
        # caller makes four git calls at boot and this is the only one that
        # reaches the guard: ``rev-parse --show-toplevel`` names no revision,
        # and the dirty set is read with ``diff-index`` against the sha this
        # call returned plus ``ls-files --others`` -- which is the idiom this
        # module recommends (name the object by hash) rather than a second
        # exemption. Written with ``git status`` instead, that would have been
        # a second refused command and a second row.
        ("rev-parse", "HEAD"),
        # A daemon serves the content its source directory had at import, and
        # boot is the only moment that is readable -- an edit or a checkout
        # afterwards erases it. So the daemon records its HEAD in daemon.json
        # and tools/deploy_check.py reads it there. Before that field existed
        # the gate compared two timestamps and was measured wrong in both
        # directions on 2026-08-27 (claunch-tig1: green over a checkout that
        # matched no commit; claunch-33id: red over a commit that changed only
        # .beads). Five test modules write daemon.json in-process while
        # exercising restart, instances or delegation, and the boot path is
        # driven in-process by two of them.
        #
        # The read is real and the guard is right to see it. What is missing
        # is a dependency, and it was measured rather than argued: with
        # daemon/__main__.py changed to pass no snapshot, the affected module
        # goes green -- 3 failed, 34 passed over test_daemon_wedge.py and
        # test_restart_gate.py, where those 3 are test_daemon_wedge failures
        # that are red on the untouched base too (claunch-uf7m, reproduced on
        # a94c39a and 063ca672). Every test_restart_gate failure disappears.
        # Nothing asserts on the recorded value; what the tests depend on is
        # the call not happening, which is not a dependency this table exists
        # to protect.
        #
        # Left on the board rather than closed: claunch-p865.
        "product code recording, at boot, which commit it loaded; no "
        "assertion in this suite turns on the value -- sentinel run 34 "
        "passed with it removed -- see claunch-p865",
    ),
)


def exempt() -> Optional[tuple]:
    """The :data:`EXEMPT_CALLERS` entry on the current stack, if any.

    The exemption covers *product code reading its own checkout*, so it is
    withdrawn the moment a test is the one asking. That is the difference
    between the six the sweep found -- which drive ``cli``/``attach``/``api``
    and never mention the branch -- and the case the guard exists for: a test
    that calls ``current_branch`` against this repository and asserts on what
    comes back. The first cannot depend on the branch name; the second is the
    definition of depending on it.

    Checked here rather than argued in a comment. The claim that "no test
    asks for the branch" was established by grep, and this file exists
    because a hand-grepped premise went stale. So the premise is machinery:
    if a test module is the direct caller, ``None`` comes back and the read
    is a hard stop like any other.
    """
    import inspect

    # ``context=0``: only the refused path reaches here, but reading source
    # lines for every frame to answer a yes/no question is work nobody asked
    # for, and it touches the disk from inside a subprocess call.
    return decide(inspect.stack(0))


class _UnknownCaller:
    """Returned when rule (b) cannot be answered, rather than guessed at.

    Not a bool and not ``None`` on purpose: the three outcomes here are
    "exempt", "refused", and "unanswerable", and collapsing the third into
    either of the others is the bug this object exists to prevent.
    """

    def __repr__(self) -> str:                     # pragma: no cover - display
        return "UNKNOWN_CALLER"


#: Rule (b) asked a question whose answer is not on this stack. See
#: :func:`decide`. Treated as a refusal, with its own message.
UNKNOWN_CALLER = _UnknownCaller()

#: Frames that mean "the caller is on a different stack".
#:
#: A worker thread's stack starts at the thread's entry point, so the frames
#: that would answer rule (b) -- did a *test* drive this? -- are not merely
#: further down, they are on another stack entirely and cannot be reached
#: from here at all.
_THREAD_ENTRY = ("/threading.py", "/concurrent/futures/thread.py")


def _thread_boundary(filename: str) -> bool:
    return any(_posix(filename).endswith(tail) for tail in _THREAD_ENTRY)


def decide(frames: Sequence) -> Optional[tuple]:
    """:func:`exempt`'s rule, over a plain list of frames so it can be tested.

    Anything with ``.filename`` and ``.function`` will do. Kept separate from
    ``inspect`` because every interesting case here is a *shape of stack*, and
    the shapes that matter are awkward to produce for real -- one of them only
    turns up when a test has monkeypatched ``subprocess.run``.

    Two questions, in order, and each was wrong once before it was right:

    (a) **Did this call come out of the exempt module?** Otherwise a deeper,
        unrelated git call is exempt for as long as ``current_branch`` sits on
        the stack. Frames belonging to a *test* are skipped here rather than
        counted: a test that monkeypatches ``subprocess.run`` puts its own
        shim in the middle of the chain (``tests/test_cli.py`` does), and that
        shim re-dispatches the same call rather than making a new one.
        Reading it as a foreign caller turned three green tests red.

    (b) **Did a test ask, or did a test drive product code that asked?** The
        hop is measured to the first frame *outside the exempt module*, not
        to the exempt function's immediate caller -- because the module holds
        a wrapper (``pane_label`` calls ``current_branch``), and measuring to
        the immediate caller let ``test -> pane_label -> current_branch``
        through. That leak matters: the label embeds the branch name, so a
        test asserting on it does depend on which branch is out.

        Do NOT widen this into "a test frame anywhere below". Product code
        driven by a test always has a test frame somewhere below it, so that
        version exempts nothing and the six come back red. The first frame
        outside the module is the only line that separates "a test asked"
        from "a test drove product code that asked".

    (c) **Is that question answerable at all?** (b) reads the caller off this
        stack, which assumes the call is on the *same thread* as whoever
        wanted it. Move the branch read to a worker thread -- the obvious
        next step after ``b08c621`` took it off the event loop -- and the
        stack begins at the thread's entry point: the test frame is on
        another stack and is not visible from here. (b) then reads "no test
        asked" for every call and hands out the exemption unconditionally,
        which is the guard quietly not enforcing itself while still
        reporting green. So an unreachable caller returns
        :data:`UNKNOWN_CALLER` and is refused. "Nobody asked" and "I cannot
        see who asked" are not the same answer, and a guard that spells them
        the same way is not a guard.
    """
    for i, frame in enumerate(frames):
        for module, func, command, why in EXEMPT_CALLERS:
            if frame.function != func or not _posix(frame.filename).endswith(module):
                continue

            between = [
                f
                for f in frames[:i]
                if not _plumbing(f.filename) and not _is_test_file(f.filename)
            ]
            if any(not _posix(f.filename).endswith(module) for f in between):
                continue                              # (a) not this module's call

            outside = next(
                (
                    f
                    for f in frames[i + 1 :]
                    if not _posix(f.filename).endswith(module)
                    and not _plumbing(f.filename)
                ),
                None,
            )
            if outside is None or _thread_boundary(outside.filename):
                return UNKNOWN_CALLER                 # (c) cannot answer (b)
            if _is_test_file(outside.filename):
                return None                           # (b) a test asked

            return (module, func, command, why)
    return None


#: This module's own frames, which sit between the caller and ``Popen`` and
#: are not part of anybody's call chain.
_SELF = "tests/_repo_history_guard.py"


def _posix(filename: str) -> str:
    return filename.replace("\\", "/")


def _plumbing(filename: str) -> bool:
    """Frames nobody wrote: this guard, and ``subprocess.run`` itself.

    ``subprocess.run`` opens ``Popen``, so the stdlib sits in the middle of
    every chain the guard sees. Counting it as an outside caller would make
    the same-module test above reject every real call, exemption or not --
    which is exactly what it did on the first attempt.
    """
    path = _posix(filename)
    return path.endswith(_SELF) or path == _posix(subprocess.__file__)


def _is_test_file(filename: str) -> bool:
    """Anything under ``tests/`` -- not just ``test_*.py``.

    A test that asks through ``tests/conftest.py`` or a ``tests/_helper.py``
    is still a test asking, and helpers are where people actually put this
    kind of call.
    """
    path = _posix(filename)
    return "/tests/" in path and not path.endswith(_SELF)


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
                    if allowed is UNKNOWN_CALLER:
                        # Say which question failed. Whoever moved this read
                        # onto a thread is the one person who can put the
                        # answer back, and they will be reading this line.
                        problem = (
                            f"{problem}\n"
                            "The exempt caller was found, but who drove it "
                            "could not be: the frames above it end at a "
                            "thread entry point, so this call is on a "
                            "different stack from whoever wanted it. The "
                            "exemption asks whether a *test* depends on the "
                            "branch name, and that question cannot be "
                            "answered from here -- so it is refused rather "
                            "than assumed. If a git read was moved to a "
                            "thread or an executor, hand the caller's "
                            "identity down to it, or narrow the exemption to "
                            "the new shape."
                        )
                        guard.seen.append(problem)
                        raise RepoHistoryRead(problem)
                    # The caller AND the command: an exempt frame does not
                    # license whatever else happens to be running under it.
                    if allowed is None or tuple(_argv(args)[1:]) != allowed[2]:
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
