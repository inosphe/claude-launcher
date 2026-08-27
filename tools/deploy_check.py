"""Is the live daemon serving the code of <branch>, or something else?

``improv-leader``'s ``reflect`` step ends a round by restarting the daemon so
the merge it just made is what users get. Until this check existed the step
was prose only: nothing stopped the leader from filing "restarted, serving"
while the daemon kept running the pre-merge code, and nothing told the leader
a restart had happened either -- the run just repeated its 300-second reminder
until somebody thought to poke ``claunch daemon status`` by hand.

The first version of this file asked the question with two timestamps::

    daemon.json's started_at  >  committer time of <branch>'s tip

That is a fact about *when*, and the step needs a fact about *what*. Two
states came out of it wrong, in opposite directions, and both were measured in
this repository on 2026-08-27:

* ``claunch-tig1`` -- exit 0, "the running daemon booted after the merge",
  while the checkout it serves differed from ``master`` in 19 tracked paths,
  seven of them under ``src/claude_launcher/`` and three of those read at boot.
  The served code was not ``master``'s code and not any other commit's either.
  The green said otherwise and the step's ``done_when`` cites the green as its
  evidence that the merge is being served.
* ``claunch-33id`` -- exit 1, "the live server is still serving pre-merge
  code", for a tip whose only changed path was ``.beads/issues.jsonl``. Not one
  line of code had moved. ``tools/sweep.py`` passed the same commit at the same
  moment, because it compares content. The red forced a daemon restart that
  had nothing new to serve, and a restart drops every attached session's drive.

So the axis moves from time to content, and the left-hand side is recorded at
the only moment it is knowable. Python loads modules once, at import, so what a
daemon serves is the content of its source directory *at boot*;
``daemon/runtime_state.py`` now writes that under ``code`` in ``daemon.json``
(the package directory in use, its repository, its HEAD, and its ``git
status``). This file is the reader.

The right-hand side is ``tools/sweep.py``'s ``code_tree`` -- the branch tip's
tree with ``NON_CODE_ENTRIES`` subtracted -- imported rather than reimplemented.
Two files computing "which differences are code" separately is two rules with
one name, and the one already in the repository is the one with the measurement
behind it (see ``NON_CODE_ENTRIES``: pytest never collects the board file, and
no test reads the repository's own copy). Subtracting also fails safe: a path
nobody classified still moves the digest and still costs a restart.

Exit codes -- four, because there are four different facts to report and a
reader who cannot tell them apart has to re-derive the answer by hand::

    0  the daemon is serving <branch>'s code
    1  it is serving older code, or nothing is serving      (restart it)
    2  cannot tell what it is serving                       (not a verdict)
    3  what it is serving is not any commit's code          (a dirty checkout)

1 and 3 are separate on purpose. "Did not restart" and "restarted onto a tree
that matches no commit" are different problems with different fixes -- the
first is one command, the second needs somebody's uncommitted work committed or
put aside -- and folding them into one code loses which of the two happened.
2 is not a verdict at all: a gate that reports a missing file as a failed
deploy teaches people to pass it with ``|| true``.

``--allow-dirty`` is the escape hatch, and it takes a value rather than being
a switch. A bare "ignore dirt" flag would reinstate exactly the state 3 exists
to catch, under a different name; naming the paths (or their digest) means the
exemption is checked -- if the checkout is dirty in some *other* way than the
one declared, the answer is still 3, and it prints the digest of what it
actually found so the declaration can be corrected rather than widened.

Not merged into ``claunch daemon status``: that command reports on whatever
daemon is running, while this asks a question about one repository's history.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

# A gate runs the tree it is checking. This checkout's ``src`` goes in front of
# every installed copy, so the ``claude_launcher`` imports below resolve HERE --
# whatever the worktree's .venv holds (``uv run --no-sync`` promises never to
# populate it) and whatever else on the path answers to the same name.
# Pinned by tests/test_gates_run_this_checkout.py.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# Same rule for the sibling gate this one borrows its content rule from:
# ``tools/`` is not a package, and an installed ``sweep`` would be somebody
# else's definition of which paths count as code.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import sweep  # noqa: E402  -- tools/sweep.py, from the two lines above

#: Serving the branch's code.
SERVING = 0
#: Serving older code, or nothing is serving. The fix is a restart.
NOT_RESTARTED = 1
#: The question could not be answered. Never a verdict.
CANNOT_TELL = 2
#: What is served is not any commit's content. The fix is somebody's checkout.
DIRTY = 3


def _say(message: str, code: int) -> int:
    """One place decides which stream a verdict goes to.

    Tying the stream to the exit code rather than to the call site is what
    keeps them from disagreeing: three of this file's answers were written as
    successes on stdout and later became failures, and a message that stays on
    stdout after that is a red gate whose reason lands where nothing looks.
    """
    print(message, file=sys.stdout if code == SERVING else sys.stderr)
    return code


def _git(repo: Path, *args: str, strip: bool = True) -> str:
    """``git -C repo args...``. Raises LookupError on failure.

    ``strip=False`` for output whose leading whitespace carries meaning.
    ``git status --porcelain`` is that case and it is not obvious: every line
    begins with a two-character status field, so a clean ``.strip()`` eats the
    space in front of the *first* entry only, shifts that one path by a
    character, and leaves the other nineteen correct. Measured here as
    ``.beads/issues.jsonl`` arriving as ``beads/issues.jsonl``, which then
    missed ``NON_CODE_ENTRIES`` and counted as a code change. One wrong path,
    no error, and a verdict on top of it.
    """
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise LookupError(
            f"git {' '.join(args)} failed in {repo}: "
            f"{proc.stderr.strip() or 'no output'}"
        )
    return proc.stdout.strip() if strip else proc.stdout


def _daemon_doc(explicit: Optional[Path]) -> tuple:
    if explicit is not None:
        path = explicit
    else:
        from claude_launcher.daemon import paths

        path = paths.daemon_json()
    if not path.is_file():
        raise LookupError(
            f"no daemon.json at {path} -- nothing is serving, so nothing "
            f"restarted onto this merge"
        )
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LookupError(f"{path} is unreadable: {exc}") from exc
    if not isinstance(doc, dict) or "started_at" not in doc:
        raise LookupError(f"{path} carries no started_at")
    return path, doc


def _alive(pid: object) -> Optional[bool]:
    """Whether the pid in daemon.json is a live process (None = cannot tell)."""
    if not isinstance(pid, int):
        return None
    try:
        from claude_launcher import daemon_client
    except ImportError:  # pragma: no cover - only outside the venv
        return None
    return daemon_client.process_alive(pid)


def code_paths(paths) -> list:
    """The paths from a ``git status`` listing that count as code.

    ``sweep.NON_CODE_ENTRIES`` holds top-level tree entries, so a path is
    excluded when its first component is one of them. Subtraction, not
    selection: an unrecognised path is code, which is the direction that
    fails towards a restart nobody needed rather than towards a green over a
    tree nobody judged.
    """
    out = []
    for raw in paths:
        first = str(raw).replace("\\", "/").split("/", 1)[0]
        if first not in sweep.NON_CODE_ENTRIES:
            out.append(str(raw).replace("\\", "/"))
    return sorted(set(out))


def dirty_digest(paths) -> str:
    """A stable name for a set of dirty paths, for ``--allow-dirty``.

    The list form is unreadable past a handful of entries -- this repository's
    own case is nineteen -- and a wrong one has to fail loudly rather than
    silently exempt the wrong file, so the short form has to be exact too.
    """
    joined = chr(10).join(code_paths(paths))
    return "sha1:" + hashlib.sha1(joined.encode("utf-8")).hexdigest()


def _porcelain(repo: Path) -> Optional[list]:
    """``git status`` in the served checkout now, or None if it cannot be read.

    The boot snapshot says what was imported; this says what a file the daemon
    reads at *runtime* (``harnesses.yaml``, the workflow YAMLs) would give it
    today. Both have to be clean for the served content to be a commit's.
    """
    try:
        out = _git(repo, "status", "--porcelain", "-z", strip=False)
    except LookupError:
        return None
    fields = [f for f in out.split("\x00") if f]
    paths, i = [], 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if len(entry) < 4:
            continue
        paths.append(entry[3:])
        if entry[0] in "RC" or entry[1] in "RC":
            if i < len(fields):
                paths.append(fields[i])
                i += 1
    return paths


def _short(sha: str) -> str:
    return str(sha)[:12]


def _listed(paths, limit: int = 6) -> str:
    shown = list(paths)[:limit]
    rest = len(paths) - len(shown)
    text = ", ".join(shown)
    return f"{text}, and {rest} more" if rest else text


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="deploy_check",
        description="fail unless the live daemon is serving <branch>'s code",
    )
    ap.add_argument("--repo", type=Path, default=Path("."))
    ap.add_argument("--branch", default="master")
    ap.add_argument(
        "--daemon-json",
        type=Path,
        default=None,
        help="override the daemon.json location (tests, second instances)",
    )
    ap.add_argument(
        "--allow-dirty",
        default=None,
        metavar="PATHS|sha1:HEX",
        help=(
            "declare the dirty paths that are knowingly not part of this "
            "deploy, comma-separated, or the sha1: digest this check prints "
            "for them. Anything else dirty still fails."
        ),
    )
    args = ap.parse_args(argv)

    try:
        path, doc = _daemon_doc(args.daemon_json)
        started = datetime.fromisoformat(str(doc["started_at"]))
        tip = _git(args.repo, "rev-parse", args.branch)
        tip_code = sweep.code_tree(args.repo, args.branch)
    except (LookupError, ValueError) as exc:
        return _say(f"cannot tell: {exc}", CANNOT_TELL)

    if _alive(doc.get("pid")) is False:
        return _say(
            f"not restarted: {path} names pid {doc.get('pid')}, which is gone -- "
            f"the file is a leftover of a daemon that is no longer serving",
            NOT_RESTARTED,
        )

    code = doc.get("code")
    if not isinstance(code, dict):
        return _say(
            f"cannot tell: {path} carries no code snapshot -- the daemon "
            f"running since {started.isoformat()} booted before this gate "
            f"recorded what it imports, so which code it serves is not on "
            f"disk. Restart it and run this again.",
            CANNOT_TELL,
        )

    served_root = code.get("root")
    served_repo = code.get("repo")
    if not served_repo:
        return _say(
            f"cannot tell: the daemon imports {served_root}, which git does "
            f"not report as a checkout -- an installed copy cannot be compared "
            f"with {args.branch}",
            CANNOT_TELL,
        )
    served_repo = Path(served_repo)
    if not served_repo.is_dir():
        return _say(
            f"cannot tell: the daemon booted from {served_repo}, which is no "
            f"longer on disk",
            CANNOT_TELL,
        )
    try:
        if sweep.repo_key(served_repo) != sweep.repo_key(args.repo):
            return _say(
                f"cannot tell: the daemon serves {served_repo}, a different "
                f"repository from {Path(args.repo).resolve()} -- this "
                f"{args.branch} is not the one it booted on",
                CANNOT_TELL,
            )
    except LookupError as exc:
        return _say(f"cannot tell: {exc}", CANNOT_TELL)

    head = code.get("head")
    if not head:
        return _say(
            f"cannot tell: the daemon recorded no HEAD when it booted at "
            f"{started.isoformat()}, so the commit it loaded is unknown",
            CANNOT_TELL,
        )
    try:
        head_code = sweep.code_tree(args.repo, head)
    except LookupError:
        return _say(
            f"cannot tell: the daemon booted on commit {_short(head)}, which "
            f"this repository does not have -- it cannot be compared with "
            f"{args.branch}",
            CANNOT_TELL,
        )

    boot_dirty = code.get("dirty")
    if boot_dirty is None:
        return _say(
            f"cannot tell: the daemon could not read its checkout's state when "
            f"it booted at {started.isoformat()}, so whether it loaded "
            f"{_short(head)} or something edited on top of it is unknown",
            CANNOT_TELL,
        )
    now_dirty = _porcelain(served_repo)
    if now_dirty is None:
        return _say(
            f"cannot tell: git cannot read the state of {served_repo} now, so "
            f"what the daemon reads from it at runtime is unknown",
            CANNOT_TELL,
        )

    dirty = code_paths(list(boot_dirty) + list(now_dirty))
    exempted = []
    if dirty:
        if args.allow_dirty is None:
            return _say(
                f"not a commit: what the daemon serves is {_short(head)} with "
                f"{len(dirty)} path(s) changed on top of it ({_listed(dirty)}) "
                f"-- that content is in no commit, so it is not "
                f"{args.branch}'s code however recently the daemon restarted. "
                f"Commit or set aside those changes, restart, and run this "
                f"again; or declare them with --allow-dirty "
                f"{dirty_digest(dirty)}",
                DIRTY,
            )
        declared = args.allow_dirty.strip()
        actual = dirty_digest(dirty)
        matched = (
            actual == declared
            if declared.startswith("sha1:")
            else code_paths(p for p in declared.split(",") if p.strip()) == dirty
        )
        if not matched:
            return _say(
                f"not a commit: --allow-dirty declared something other than "
                f"what is there. The daemon serves {_short(head)} with "
                f"{len(dirty)} path(s) changed on top of it ({_listed(dirty)}), "
                f"whose digest is {actual}; declared was {declared!r}",
                DIRTY,
            )
        exempted = dirty

    if head_code != tip_code:
        return _say(
            f"not restarted: the daemon booted at {started.isoformat()} on "
            f"{_short(head)}, whose code tree is {_short(head_code)}; "
            f"{args.branch} tip {_short(tip)} has code tree "
            f"{_short(tip_code)}. The live server is serving pre-merge code. "
            f"Restart it and run this again.",
            NOT_RESTARTED,
        )

    if exempted:
        return _say(
            f"serving {args.branch}'s code except {len(exempted)} declared "
            f"path(s): the daemon booted at {started.isoformat()} on "
            f"{_short(head)} (code tree {_short(head_code)}, the same as "
            f"{args.branch} tip {_short(tip)}); exempted by --allow-dirty: "
            f"{_listed(exempted)}",
            SERVING,
        )

    if head == tip:
        return _say(
            f"serving {args.branch} tip {_short(tip)}: the daemon booted at "
            f"{started.isoformat()} on that commit and its checkout matches it "
            f"outside {', '.join(sorted(sweep.NON_CODE_ENTRIES))}",
            SERVING,
        )

    return _say(
        f"serving {args.branch}'s code: the daemon booted at "
        f"{started.isoformat()} on {_short(head)}, whose code tree "
        f"{_short(head_code)} is {args.branch} tip {_short(tip)}'s -- the two "
        f"commits differ only in {', '.join(sorted(sweep.NON_CODE_ENTRIES))}, "
        f"so no restart is needed",
        SERVING,
    )


if __name__ == "__main__":
    raise SystemExit(main())
