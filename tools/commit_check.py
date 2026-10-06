"""Does this commit exist on the remote, at the ref it was named with?

In a remote formation (leader and worker on different machines) the two
sessions share no object store. A message that says "landed 1a7b9dd" or "rebase
onto master" assumes the reader has that commit and that "master" means the
same thing on both sides; neither holds across machines. The protocol
(``docs/remote-commit-protocol.md``, claunch-yq80p.7) therefore names every
commit that crosses a machine as a pair -- the full 40-hex SHA and the remote
ref that holds it, written ``<sha> @ <remote>/<ref>`` -- and both ends check
the pair before acting on it. This script is that check, in one command.

Receiving side (default): fetch ``<ref>`` from ``<remote>``, then

* ``git cat-file -e <sha>^{commit}`` must succeed, and
* ``--exact``: ``<remote>/<ref>`` must equal ``<sha>``;
  ``--contains``: ``<sha>`` must be an ancestor of (or equal to) ``<remote>/<ref>``;
* ``--parent-of <tip>`` (optional): ``<sha>`` must list ``<tip>`` among its
  parents -- the merge-notice check, where ``<sha>`` is the leader's merge
  commit and ``<tip>`` the worker's own branch tip.

Sending side (``--pushed``): ``git ls-remote <remote> refs/heads/<ref>`` must
print ``<sha>``. Run after the push and before the message goes out; nothing is
fetched, because the question is what the remote holds, not what this clone
holds.

Exit codes (the interface the workflow probes read):

* 0 -- confirmed. stdout: ``OK <sha> @ <remote>/<ref> (<mode>)``; that line is
  the acknowledgement, and it repeats the SHA the checker saw.
* 1 -- the SHA exists here, but the ref points elsewhere (or, with
  ``--parent-of``, the commit does not have that parent).
* 2 -- the SHA is not present after the fetch (or, with ``--pushed``, the ref
  is absent on the remote).
* 3 -- the fetch / ls-remote failed, so nothing could be checked.
* 4 -- the arguments are not a valid cross-machine reference (a short hash,
  ``HEAD``, a missing mode). Kept apart from 2 so that a typo is never
  reported as "the commit is missing".

On 1 and 2 stdout carries the reply line the protocol prescribes,
``NOT FOUND <sha> @ <remote>/<ref> (fetched <current sha or ->)``, followed by
one line saying which check failed. The script never substitutes a nearby
commit for the one named.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

# Every gate script under tools/ puts this checkout first; this one imports
# nothing from the package, but keeps the rule so a later import cannot
# silently resolve against an installed copy (tests/test_gates_run_this_checkout.py).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

CONFIRMED = 0
REF_ELSEWHERE = 1
SHA_MISSING = 2
FETCH_FAILED = 3
BAD_ARGS = 4

_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # argparse would exit 2 = "missing"
        self.print_usage(sys.stderr)
        sys.stderr.write(f"{self.prog}: error: {message}\n")
        sys.exit(BAD_ARGS)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    # git writes UTF-8; the locale codec would kill the reader thread
    # (claunch-gds6-subprocess-decode-cp949-ja5ih).
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )


def normalize_ref(ref: str) -> Optional[str]:
    """``refs/heads/x`` or ``x`` -> ``x``; a relative or remote-qualified name -> None.

    ``HEAD``, ``@{upstream}``, ``x~1`` and the like mean different commits on
    different machines, so they are refused rather than resolved.
    """
    name = ref.strip()
    if name.startswith("refs/heads/"):
        name = name[len("refs/heads/"):]
    elif name.startswith("refs/"):
        return None
    if (not name or name in ("HEAD", "@") or "@{" in name or name.endswith("/")
            or any(c in name for c in "~^:* ")):
        return None
    return name


def _not_found(sha: str, remote: str, ref: str, current: Optional[str], why: str) -> List[str]:
    return [f"NOT FOUND {sha} @ {remote}/{ref} (fetched {current or '-'})", why]


def check_pushed(repo: Path, remote: str, ref: str, sha: str) -> tuple:
    proc = _git(repo, "ls-remote", remote, f"refs/heads/{ref}")
    if proc.returncode != 0:
        return FETCH_FAILED, [f"ls-remote {remote} refs/heads/{ref} failed: {proc.stderr.strip()}"]
    current = None
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == f"refs/heads/{ref}":
            current = parts[0]
    if current is None:
        return SHA_MISSING, _not_found(sha, remote, ref, None, f"{remote} has no refs/heads/{ref}; do not send")
    if current != sha:
        return REF_ELSEWHERE, _not_found(
            sha, remote, ref, current, f"{remote}/{ref} is {current}; push did not land {sha}; do not send")
    return CONFIRMED, [f"OK {sha} @ {remote}/{ref} (pushed)"]


def check_received(repo: Path, remote: str, ref: str, sha: str, mode: str,
                   parent_of: Optional[str]) -> tuple:
    tracking = f"refs/remotes/{remote}/{ref}"
    # An explicit refspec: the remote-tracking ref is updated even when the
    # remote's configured fetch refspec is narrower (a single-branch clone).
    proc = _git(repo, "fetch", "--no-tags", remote, f"+refs/heads/{ref}:{tracking}")
    if proc.returncode != 0:
        return FETCH_FAILED, [f"fetch {remote} refs/heads/{ref} failed: {proc.stderr.strip()}"]
    head = _git(repo, "rev-parse", "--verify", "--quiet", f"{tracking}^{{commit}}")
    current = head.stdout.strip() if head.returncode == 0 else None
    if _git(repo, "cat-file", "-e", f"{sha}^{{commit}}").returncode != 0:
        return SHA_MISSING, _not_found(sha, remote, ref, current, f"{sha} is not in this clone after the fetch")
    if current is None:
        return FETCH_FAILED, [f"fetch succeeded but {tracking} does not resolve"]
    if mode == "exact":
        if current != sha:
            return REF_ELSEWHERE, _not_found(sha, remote, ref, current, f"{remote}/{ref} is {current}, not {sha}")
    else:
        if _git(repo, "merge-base", "--is-ancestor", sha, current).returncode != 0:
            return REF_ELSEWHERE, _not_found(
                sha, remote, ref, current, f"{sha} is not an ancestor of {remote}/{ref} ({current})")
    if parent_of is not None:
        parents = _git(repo, "rev-list", "--parents", "-n", "1", sha).stdout.split()[1:]
        if parent_of not in parents:
            return REF_ELSEWHERE, _not_found(
                sha, remote, ref, current,
                f"{sha} does not have {parent_of} as a parent (parents: {' '.join(parents) or '-'})")
        mode = f"{mode}, parent {parent_of}"
    return CONFIRMED, [f"OK {sha} @ {remote}/{ref} ({mode})"]


def main(argv: Optional[List[str]] = None) -> int:
    parser = _Parser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--remote", required=True)
    parser.add_argument("--ref", required=True, help="branch on the remote (name or refs/heads/<name>)")
    parser.add_argument("--sha", required=True, help="full 40-hex commit SHA")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--exact", action="store_true", help="receiver: the ref must equal the SHA")
    mode.add_argument("--contains", action="store_true", help="receiver: the ref must contain the SHA")
    mode.add_argument("--pushed", action="store_true", help="sender: ls-remote must show the SHA at the ref")
    parser.add_argument("--parent-of", metavar="TIP",
                        help="receiver: the SHA must list this full SHA among its parents")
    parser.add_argument("--repo", default=".", help="the clone to check in (default: cwd)")
    args = parser.parse_args(argv)

    sha = args.sha.strip().lower()
    if not _FULL_SHA.match(sha):
        parser.error(f"--sha must be a full 40-hex SHA, got {args.sha!r}")
    parent_of = None
    if args.parent_of is not None:
        if args.pushed:
            parser.error("--parent-of is a receiving-side check; it cannot be combined with --pushed")
        parent_of = args.parent_of.strip().lower()
        if not _FULL_SHA.match(parent_of):
            parser.error(f"--parent-of must be a full 40-hex SHA, got {args.parent_of!r}")
    ref = normalize_ref(args.ref)
    if ref is None:
        parser.error(f"--ref must name a branch on the remote, got {args.ref!r}")
    remote = args.remote.strip()
    if not remote or remote.startswith("-"):
        parser.error(f"--remote must name a remote, got {args.remote!r}")
    repo = Path(args.repo).resolve()

    if args.pushed:
        code, lines = check_pushed(repo, remote, ref, sha)
    else:
        code, lines = check_received(repo, remote, ref, sha, "exact" if args.exact else "contains", parent_of)
    stream = sys.stdout if code in (CONFIRMED, REF_ELSEWHERE, SHA_MISSING) else sys.stderr
    for line in lines:
        print(line, file=stream)
    return code


if __name__ == "__main__":
    sys.exit(main())
