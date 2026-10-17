"""One commit, read whole: what the dashboard's commit page draws.

Every surface that lists commits (the rail card's chips, the Details panel's
Commits block, the round report's hashes) names a commit by its sha and stops
there. This module answers the next question -- "what is in it" -- with the
commit's metadata, the files it changed and their diff against the first
parent, already split into hunks and numbered lines so the page only draws.

It is a reader like :mod:`claude_launcher.session_commits`: nothing is
written, the repository is the record, and every failure is an answer
(:class:`CommitError` with a reason) rather than a traceback, because the
caller is a web page describing a commit, not a gate deciding on one.

**Bounded.** A commit can carry a vendored tree or a generated file of
millions of lines, and the page is drawn in a browser. So the patch is read
up to :data:`MAX_PATCH_BYTES` (the git process is killed past it), a file's
diff is cut at :data:`MAX_FILE_LINES` lines, and at most :data:`MAX_FILES`
files carry hunks. Every cut is reported (``truncated`` on the file, on the
whole answer), never silent: a page that drops the end of a diff without
saying so reads as a commit that did not change it.

A merge commit is diffed against its first parent -- the change the merge
brought onto the branch it landed on, which is what ``git show
--first-parent`` and a PR's merge view show. A root commit is diffed against
the empty tree.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

#: Seconds before a git call is abandoned.
TIMEOUT = 15

#: Bytes of patch text read before the rest is dropped.
MAX_PATCH_BYTES = 2 * 1024 * 1024

#: Diff lines (context, added, removed) one file carries before it is cut.
MAX_FILE_LINES = 4000

#: Files that carry hunks; the rest are listed with their counts only.
MAX_FILES = 300

#: A sha the page may ask for: abbreviated (4+) or whole, SHA-1 or SHA-256.
#: Nothing else reaches git -- no ref names, no revision syntax (``HEAD~2``,
#: ``@{-1}``, ``--output=...``), so the argument cannot be an option.
SHA_RE = re.compile(r"^[0-9a-fA-F]{4,64}$")

_FS = "\x1f"
_META_FORMAT = _FS.join(
    ["%H", "%h", "%P", "%an", "%ae", "%aI", "%cn", "%ce", "%cI", "%s", "%B"]
)

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")

#: Name-status letters, spelled the way the page labels them.
STATUS_NAMES = {
    "A": "added",
    "D": "deleted",
    "M": "modified",
    "R": "renamed",
    "C": "copied",
    "T": "type-changed",
}


class CommitError(Exception):
    """The commit could not be read; ``status`` is the HTTP answer."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _run(cwd: str, *args: str, cap: Optional[int] = None) -> Tuple[int, str, bool]:
    """``(returncode, stdout, cut)``. With ``cap``, stdout is read up to that
    many bytes and the process is killed past it (``cut`` is then True)."""
    cmd = ["git", "-C", cwd, "-c", "core.quotePath=false", *args]
    if cap is None:
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=TIMEOUT,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise CommitError(f"git failed: {exc}", status=500) from None
        return proc.returncode, proc.stdout, False
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise CommitError(f"git failed: {exc}", status=500) from None
    chunks: List[bytes] = []
    size = 0
    cut = False
    try:
        assert proc.stdout is not None
        while True:
            block = proc.stdout.read(65536)
            if not block:
                break
            chunks.append(block)
            size += len(block)
            if size > cap:
                cut = True
                break
    finally:
        if cut:
            proc.kill()
        try:
            proc.wait(timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    data = b"".join(chunks)
    if cut:
        data = data[:cap]
        # Drop a partial last line so no line is drawn half-read.
        nl = data.rfind(b"\n")
        data = data[: nl + 1] if nl >= 0 else b""
    return (0 if cut else proc.returncode), data.decode("utf-8", "replace"), cut


def _resolve(cwd: str, sha: str) -> str:
    code, out, _ = _run(cwd, "rev-parse", "--verify", "--quiet", "--end-of-options",
                        f"{sha}^{{commit}}")
    full = out.strip()
    if code != 0 or not full:
        raise CommitError(f"no commit {sha} in this repository", status=404)
    return full


def _meta(cwd: str, full: str) -> Dict[str, object]:
    code, out, _ = _run(cwd, "show", "-s", "--no-notes", f"--format={_META_FORMAT}",
                        full)
    if code != 0:
        raise CommitError(f"cannot read commit {full}", status=500)
    parts = out.split(_FS, 10)
    if len(parts) < 11:
        raise CommitError(f"cannot read commit {full}", status=500)
    (sha, short, parents, an, ae, ad, cn, ce, cd, subject, message) = parts
    message = message.rstrip("\n")
    body = message.split("\n", 1)[1].lstrip("\n") if "\n" in message else ""
    return {
        "sha": sha,
        "short": short,
        "parents": parents.split(),
        "author": {"name": an, "email": ae, "date": ad},
        "committer": {"name": cn, "email": ce, "date": cd},
        "subject": subject,
        "body": body,
        "trailers": _trailers(cwd, message),
    }


def _trailers(cwd: str, message: str) -> List[Dict[str, str]]:
    """The message's trailer block as ``[{key, value}]``, as git parses it."""
    try:
        proc = subprocess.run(
            ["git", "-C", cwd, "interpret-trailers", "--parse", "--no-divider"],
            input=message + "\n", capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    out = []
    for line in proc.stdout.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip():
            out.append({"key": key.strip(), "value": value.strip()})
    return out


def _empty_tree(cwd: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", cwd, "hash-object", "-t", "tree", "--stdin"],
            input="", capture_output=True, text=True, timeout=TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CommitError(f"git failed: {exc}", status=500) from None
    return proc.stdout.strip()


def _name_status(cwd: str, base: str, full: str) -> List[Dict[str, object]]:
    code, out, _ = _run(cwd, "diff", "--no-ext-diff", "-M", "--name-status", "-z",
                        base, full)
    if code != 0:
        raise CommitError(f"cannot diff commit {full}", status=500)
    fields = iter(out.split("\0"))
    files: List[Dict[str, object]] = []
    for status in fields:
        if not status:
            continue
        letter = status[0]
        entry: Dict[str, object] = {
            "status": STATUS_NAMES.get(letter, letter),
            "similarity": int(status[1:]) if status[1:].isdigit() else None,
        }
        if letter in "RC":
            entry["old_path"] = next(fields, "")
            entry["path"] = next(fields, "")
        else:
            entry["path"] = next(fields, "")
            entry["old_path"] = None
        files.append(entry)
    return files


def _numstat(cwd: str, base: str, full: str) -> List[Tuple[Optional[int], Optional[int]]]:
    code, out, _ = _run(cwd, "diff", "--no-ext-diff", "-M", "--numstat", "-z",
                        base, full)
    if code != 0:
        return []
    fields = iter(out.split("\0"))
    counts: List[Tuple[Optional[int], Optional[int]]] = []
    for record in fields:
        if not record:
            continue
        add, _, rest = record.partition("\t")
        delete, _, path = rest.partition("\t")
        if not path:
            # A rename: the two paths follow as their own fields.
            next(fields, None)
            next(fields, None)
        counts.append((
            int(add) if add.isdigit() else None,
            int(delete) if delete.isdigit() else None,
        ))
    return counts


def parse_patch(text: str) -> List[Dict[str, object]]:
    """Split ``git diff`` output into one record per file.

    Each record has ``header`` (the extended header lines git prints before
    the hunks), ``binary`` and ``hunks``: ``[{header, old_start, new_start,
    lines: [[kind, old_no, new_no, text]]}]`` where kind is ``" "``, ``"+"``,
    ``"-"`` or ``"\\"`` (git's "No newline at end of file" note). A file is
    recognised by its ``diff --git`` line, which cannot occur inside a hunk:
    every hunk line starts with one of the four kinds.
    """
    files: List[Dict[str, object]] = []
    cur: Optional[Dict[str, object]] = None
    hunk: Optional[Dict[str, object]] = None
    old_no = new_no = 0
    for line in text.split("\n"):
        if line.startswith("diff --git "):
            cur = {"header": [line], "binary": False, "hunks": [], "lines": 0,
                   "truncated": False}
            files.append(cur)
            hunk = None
            continue
        if cur is None:
            continue
        m = _HUNK_RE.match(line)
        if m:
            old_no, new_no = int(m.group(1)), int(m.group(3))
            hunk = {"header": line, "old_start": old_no, "new_start": new_no,
                    "context": m.group(5).strip(), "lines": []}
            if cur["lines"] >= MAX_FILE_LINES:
                cur["truncated"] = True
            else:
                cur["hunks"].append(hunk)
            continue
        if hunk is None:
            if line.startswith("Binary files ") or line == "GIT binary patch":
                cur["binary"] = True
            if line:
                cur["header"].append(line)
            continue
        if not line:
            # The split's trailing empty string; a context line of an empty
            # line is " ", never "".
            continue
        kind = line[0]
        if kind not in " +-\\":
            continue
        if cur["lines"] >= MAX_FILE_LINES:
            cur["truncated"] = True
            continue
        if kind == " ":
            row = [kind, old_no, new_no, line[1:]]
            old_no += 1
            new_no += 1
        elif kind == "-":
            row = [kind, old_no, None, line[1:]]
            old_no += 1
        elif kind == "+":
            row = [kind, None, new_no, line[1:]]
            new_no += 1
        else:
            row = [kind, None, None, line[1:].strip()]
        hunk["lines"].append(row)
        cur["lines"] += 1
    return files


def read(cwd: str | Path, sha: str) -> Dict[str, object]:
    """The commit ``sha`` in the repository at ``cwd``, with its diff.

    Raises :class:`CommitError` (``status`` 400 for a malformed sha or a
    directory that is not a repository, 404 for a sha the repository does not
    have, 500 for git failing).
    """
    sha = (sha or "").strip()
    if not SHA_RE.match(sha):
        raise CommitError("sha must be 4 to 64 hex digits")
    path = Path(cwd) if cwd else None
    if path is None or not path.is_dir():
        raise CommitError("no repository directory to read")
    cwd = str(path)
    code, _, _ = _run(cwd, "rev-parse", "--git-dir")
    if code != 0:
        raise CommitError(f"{cwd} is not a git repository")
    full = _resolve(cwd, sha)
    meta = _meta(cwd, full)
    parents = meta["parents"]
    base = parents[0] if parents else _empty_tree(cwd)
    files = _name_status(cwd, base, full)
    counts = _numstat(cwd, base, full)
    code, patch, cut = _run(
        cwd, "diff", "--no-ext-diff", "--no-color", "-M", "--patch", base, full,
        cap=MAX_PATCH_BYTES,
    )
    patches = parse_patch(patch) if code == 0 else []
    total_add = total_del = 0
    for i, f in enumerate(files):
        add, delete = counts[i] if i < len(counts) else (None, None)
        f["additions"] = add
        f["deletions"] = delete
        total_add += add or 0
        total_del += delete or 0
        p = patches[i] if i < len(patches) and i < MAX_FILES else None
        if p is None:
            f["binary"] = add is None and delete is None and i < len(counts)
            f["hunks"] = []
            f["header"] = []
            # Its patch was past the byte cap or the file cap: listed, not drawn.
            f["truncated"] = True
            f["omitted"] = True
            continue
        f["binary"] = bool(p["binary"]) or (add is None and delete is None)
        f["hunks"] = p["hunks"]
        f["header"] = p["header"]
        f["omitted"] = False
        # The last file read is the one the byte cap cut into.
        f["truncated"] = bool(p["truncated"]) or (cut and i == len(patches) - 1)
    meta.update({
        "base": base if parents else None,
        "merge": len(parents) > 1,
        "files": files,
        "stats": {"files": len(files), "additions": total_add, "deletions": total_del},
        "truncated": cut or any(f["truncated"] for f in files),
        "limits": {"patch_bytes": MAX_PATCH_BYTES, "file_lines": MAX_FILE_LINES,
                   "files": MAX_FILES},
    })
    return meta
