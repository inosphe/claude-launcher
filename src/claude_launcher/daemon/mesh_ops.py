"""Peer operations: what one mesh member may do to another member's checkout.

A member on another machine cannot read that machine's files or run git
there, so the mesh carries three read-side operations over the same
authenticated peer links its messages already ride (``/peer/*`` bridged by
the relay, one link token per edge — see ``MeshManager._check_link_token``):

- **file** — one file, read from inside the target session's working
  directory. The path is resolved and must stay under that directory once
  symlinks are followed; anything else is refused before a byte is read.
- **git** — a read-only git query (``status`` / ``diff`` / ``log`` /
  ``show`` / ``branch``) in that directory. Only the operations named here
  run, and every argument is a typed field, never a free option string, so
  a caller cannot smuggle ``--output`` or a second command through.
- **lease** — the coordination primitive. Two members that edit the same
  file on two machines have no shared filesystem lock, so the mesh's
  authority (``peers[0]``, the one daemon that already sequences every
  message) keeps a registry of ``key -> holder`` with a TTL. Acquire
  succeeds when the key is free or expired, is idempotent for its holder,
  and otherwise reports who holds it and until when. Keys are plain strings
  by convention (``path:<repo-relative>``, ``issue:<id>``); the registry
  does not interpret them.

This module is the pure part: no mesh, no transport. ``MeshManager`` resolves
handles to sessions and machines, checks the member graph, and routes a call
locally or across a link; the functions here take a directory and answer.
"""

from __future__ import annotations

import hashlib
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional

#: Default and hard cap on bytes handed back by :func:`read_file` — the reply
#: is one JSON document over a bridged stream, and the caller is an agent
#: whose context the bytes are typed into.
FILE_DEFAULT_MAX = 64 * 1024
FILE_HARD_MAX = 1024 * 1024
#: git output cap, same reasoning.
GIT_OUTPUT_MAX = 256 * 1024
GIT_TIMEOUT = 20.0

#: Lease TTL bounds (seconds). A lease with no renewal must expire on its
#: own: the holder may have been killed mid-edit, and a key held forever by
#: a dead session is the deadlock this primitive exists to prevent.
LEASE_DEFAULT_TTL = 15 * 60
LEASE_MAX_TTL = 4 * 60 * 60

GIT_OPS = ("status", "diff", "log", "show", "branch")
#: Which typed arguments each operation reads. Anything else in ``args`` is
#: refused rather than ignored, so a typo does not silently change the query.
GIT_ARGS: Dict[str, tuple] = {
    "status": ("paths",),
    "diff": ("base", "head", "paths", "stat", "cached"),
    "log": ("n", "range", "paths"),
    "show": ("ref", "stat"),
    "branch": (),
}
_REF_MAX = 200


class OpsError(Exception):
    """A refused or failed peer operation (bad path, unknown op, git error)."""


class LeaseHeld(OpsError):
    """Acquire refused: another holder has the key."""

    def __init__(self, lease: dict) -> None:
        super().__init__(
            f"lease {lease['key']!r} is held by {lease['holder']!r} "
            f"until {lease['expires_at']}"
        )
        self.lease = lease


# --------------------------------------------------------------------------- #
# file
# --------------------------------------------------------------------------- #
def _sandbox(cwd: str, rel: str) -> Path:
    """``rel`` resolved under ``cwd``, or :class:`OpsError`.

    Both sides are resolved (symlinks followed) before the containment check,
    so a link inside the checkout that points outside it is refused the same
    as ``../``. An absolute ``rel`` is accepted only when it already lies
    under ``cwd`` — a caller that copied a path from ``git status`` output on
    the other machine should not have to relativise it first.
    """
    if not cwd:
        raise OpsError("the target session has no working directory")
    if not rel or not str(rel).strip():
        raise OpsError("'path' is required")
    root = Path(cwd).resolve()
    candidate = Path(rel)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        target = candidate.resolve()
    except OSError as exc:
        raise OpsError(f"cannot resolve {rel!r}: {exc}") from None
    try:
        target.relative_to(root)
    except ValueError:
        raise OpsError(
            f"{rel!r} is outside the session's working directory"
        ) from None
    return target


def read_file(cwd: str, path: str, *, max_bytes: Optional[int] = None) -> dict:
    """Read one file from under ``cwd``.

    Returns ``{path, size, content, truncated, sha256, encoding}`` where
    ``path`` is the resolved path relative to ``cwd`` (forward slashes),
    ``sha256`` is over the WHOLE file even when the content was cut, so the
    reader can tell "same file" from "same prefix", and ``encoding`` is
    ``utf-8`` or ``base64`` (binary content is not typed into a terminal).
    """
    limit = FILE_DEFAULT_MAX if not max_bytes else min(int(max_bytes), FILE_HARD_MAX)
    if limit <= 0:
        raise OpsError("'max_bytes' must be positive")
    target = _sandbox(cwd, path)
    if target.is_dir():
        raise OpsError(f"{path!r} is a directory")
    if not target.is_file():
        raise OpsError(f"{path!r} does not exist")
    try:
        raw = target.read_bytes()
    except OSError as exc:
        raise OpsError(f"cannot read {path!r}: {exc}") from None
    digest = hashlib.sha256(raw).hexdigest()
    cut = raw[:limit]
    try:
        content = cut.decode("utf-8")
        encoding = "utf-8"
    except UnicodeDecodeError:
        import base64

        content = base64.b64encode(cut).decode("ascii")
        encoding = "base64"
    return {
        "path": target.relative_to(Path(cwd).resolve()).as_posix(),
        "size": len(raw),
        "content": content,
        "truncated": len(raw) > limit,
        "sha256": digest,
        "encoding": encoding,
    }


# --------------------------------------------------------------------------- #
# git (read-only)
# --------------------------------------------------------------------------- #
def _ref(value, what: str) -> str:
    """A git revision argument that cannot be read as an option."""
    s = str(value or "").strip()
    if not s:
        raise OpsError(f"'{what}' is required")
    if s.startswith("-") or len(s) > _REF_MAX or any(c.isspace() for c in s):
        raise OpsError(f"'{what}' is not a valid revision: {s!r}")
    return s


def _paths(cwd: str, value) -> List[str]:
    if value in (None, "", []):
        return []
    items = value if isinstance(value, list) else [value]
    out = []
    for item in items:
        s = str(item or "").strip()
        if not s:
            continue
        if s.startswith("-"):
            raise OpsError(f"path {s!r} looks like an option")
        # Must stay inside the checkout, same rule as file reads.
        _sandbox(cwd, s)
        out.append(s)
    return out


def git_argv(cwd: str, op: str, args: Optional[dict] = None) -> List[str]:
    """The argv (after ``git``) for a whitelisted read-only query.

    Separated from running it so the tests can check exactly what would be
    executed, and so a refused argument fails before any process starts.
    """
    args = dict(args or {})
    if op not in GIT_OPS:
        raise OpsError(
            f"unknown git op {op!r} (allowed: {', '.join(GIT_OPS)})"
        )
    unknown = sorted(set(args) - set(GIT_ARGS[op]))
    if unknown:
        raise OpsError(
            f"git {op} does not take {', '.join(unknown)} "
            f"(allowed: {', '.join(GIT_ARGS[op]) or 'nothing'})"
        )
    if op == "status":
        argv = ["status", "--porcelain=v1", "--branch", "--untracked-files=all"]
        paths = _paths(cwd, args.get("paths"))
        return argv + (["--", *paths] if paths else [])
    if op == "diff":
        argv = ["diff", "--no-color", "--no-ext-diff"]
        if args.get("stat"):
            argv.append("--stat")
        if args.get("cached"):
            argv.append("--cached")
        base = args.get("base")
        head = args.get("head")
        if base:
            argv.append(_ref(base, "base"))
        if head:
            if not base:
                raise OpsError("'head' needs 'base'")
            argv.append(_ref(head, "head"))
        paths = _paths(cwd, args.get("paths"))
        return argv + (["--", *paths] if paths else [])
    if op == "log":
        n = args.get("n", 20)
        try:
            n = int(n)
        except (TypeError, ValueError):
            raise OpsError("'n' must be an integer") from None
        if not 1 <= n <= 500:
            raise OpsError("'n' must be between 1 and 500")
        argv = ["log", "--no-color", f"--max-count={n}",
                "--format=%h%x09%an%x09%aI%x09%s"]
        if args.get("range"):
            argv.append(_ref(args["range"], "range"))
        paths = _paths(cwd, args.get("paths"))
        return argv + (["--", *paths] if paths else [])
    if op == "show":
        argv = ["show", "--no-color", "--no-ext-diff", _ref(args.get("ref"), "ref")]
        if args.get("stat"):
            argv.append("--stat")
        return argv
    # branch
    return ["branch", "--no-color", "--list", "--format=%(HEAD)%09%(refname:short)%09%(objectname:short)"]


def git_query(cwd: str, op: str, args: Optional[dict] = None) -> dict:
    """Run one whitelisted read-only git query in ``cwd``.

    Returns ``{op, argv, rc, output, truncated}``. A non-zero exit is not an
    exception: ``output`` then carries git's own message, which is what the
    caller wants to read (an unknown ref, not a repository, ...).
    """
    if not cwd:
        raise OpsError("the target session has no working directory")
    argv = git_argv(cwd, op, args)
    try:
        proc = subprocess.run(
            ["git", *argv],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        raise OpsError(f"git {op} did not finish within {GIT_TIMEOUT:.0f}s") from None
    except FileNotFoundError:
        raise OpsError("git is not on PATH on the target machine") from None
    except OSError as exc:
        raise OpsError(f"cannot run git: {exc}") from None
    text = proc.stdout if proc.returncode == 0 else (proc.stdout + proc.stderr)
    truncated = len(text) > GIT_OUTPUT_MAX
    return {
        "op": op,
        "argv": argv,
        "rc": proc.returncode,
        "output": text[:GIT_OUTPUT_MAX],
        "truncated": truncated,
    }


# --------------------------------------------------------------------------- #
# leases
# --------------------------------------------------------------------------- #
class LeaseRegistry:
    """``key -> lease`` with expiry; the mesh authority owns one per mesh.

    A lease is ``{key, holder, note, acquired_at, expires_at, renewals}``
    with times as epoch seconds. ``now`` is injectable so the tests do not
    sleep.
    """

    def __init__(self, leases: Optional[Dict[str, dict]] = None) -> None:
        self._leases: Dict[str, dict] = dict(leases or {})

    # -- persistence ------------------------------------------------------ #
    def to_dict(self) -> Dict[str, dict]:
        return {k: dict(v) for k, v in self._leases.items()}

    @classmethod
    def from_dict(cls, doc) -> "LeaseRegistry":
        if not isinstance(doc, dict):
            return cls()
        good = {}
        for key, entry in doc.items():
            if isinstance(entry, dict) and entry.get("holder"):
                good[str(key)] = dict(entry)
        return cls(good)

    # -- queries ---------------------------------------------------------- #
    @staticmethod
    def _expired(lease: dict, now: float) -> bool:
        return float(lease.get("expires_at") or 0) <= now

    def get(self, key: str, *, now: Optional[float] = None) -> Optional[dict]:
        """The live lease on ``key``, or None (missing or expired)."""
        now = time.time() if now is None else now
        lease = self._leases.get(key)
        if lease is None or self._expired(lease, now):
            return None
        return dict(lease)

    def list(self, *, now: Optional[float] = None, holder: str = "") -> List[dict]:
        """Live leases, oldest first; ``holder`` narrows to one member."""
        now = time.time() if now is None else now
        out = [
            dict(v) for v in self._leases.values()
            if not self._expired(v, now) and (not holder or v["holder"] == holder)
        ]
        out.sort(key=lambda v: (float(v["acquired_at"]), v["key"]))
        return out

    # -- writes ----------------------------------------------------------- #
    @staticmethod
    def _ttl(ttl) -> float:
        if ttl in (None, ""):
            return float(LEASE_DEFAULT_TTL)
        try:
            ttl = float(ttl)
        except (TypeError, ValueError):
            raise OpsError("'ttl' must be a number of seconds") from None
        if ttl <= 0:
            raise OpsError("'ttl' must be positive")
        return min(ttl, float(LEASE_MAX_TTL))

    def acquire(
        self, key: str, holder: str, *, ttl=None, note: str = "",
        now: Optional[float] = None,
    ) -> dict:
        """Take ``key`` for ``holder``.

        Free or expired: a fresh lease. Already ours: the deadline is pushed
        out (same as :meth:`renew`), so a holder that re-acquires by habit
        is not refused its own key. Someone else's and live: :class:`LeaseHeld`,
        carrying the lease so the caller can see who and until when.
        """
        key = str(key or "").strip()
        holder = str(holder or "").strip()
        if not key:
            raise OpsError("'key' is required")
        if not holder:
            raise OpsError("a lease needs a holder")
        now = time.time() if now is None else now
        ttl = self._ttl(ttl)
        current = self._leases.get(key)
        if current is not None and not self._expired(current, now):
            if current["holder"] != holder:
                raise LeaseHeld(dict(current))
            current["expires_at"] = now + ttl
            current["renewals"] = int(current.get("renewals") or 0) + 1
            if note:
                current["note"] = str(note)
            return dict(current)
        lease = {
            "key": key,
            "holder": holder,
            "note": str(note or ""),
            "acquired_at": now,
            "expires_at": now + ttl,
            "renewals": 0,
        }
        self._leases[key] = lease
        return dict(lease)

    def renew(self, key: str, holder: str, *, ttl=None, now: Optional[float] = None) -> dict:
        """Push the deadline of a lease ``holder`` already has."""
        now = time.time() if now is None else now
        current = self.get(key, now=now)
        if current is None:
            raise OpsError(f"no live lease on {key!r} to renew")
        if current["holder"] != holder:
            raise LeaseHeld(current)
        return self.acquire(key, holder, ttl=ttl, now=now)

    def release(self, key: str, holder: str, *, now: Optional[float] = None) -> dict:
        """Give ``key`` back. Only the holder may; an expired lease is gone
        already and releasing it is a no-op that says so."""
        now = time.time() if now is None else now
        key = str(key or "").strip()
        holder = str(holder or "").strip()
        current = self._leases.get(key)
        if current is None or self._expired(current, now):
            self._leases.pop(key, None)
            return {"key": key, "released": False, "reason": "not held"}
        if current["holder"] != holder:
            raise LeaseHeld(dict(current))
        self._leases.pop(key, None)
        return {"key": key, "released": True, "holder": holder}

    def release_all(self, holder: str) -> List[str]:
        """Drop every lease ``holder`` has (a member left, its session died)."""
        keys = [k for k, v in self._leases.items() if v.get("holder") == holder]
        for k in keys:
            self._leases.pop(k, None)
        return keys

    def prune(self, *, now: Optional[float] = None) -> int:
        """Forget expired entries; returns how many were dropped."""
        now = time.time() if now is None else now
        dead = [k for k, v in self._leases.items() if self._expired(v, now)]
        for k in dead:
            self._leases.pop(k, None)
        return len(dead)
