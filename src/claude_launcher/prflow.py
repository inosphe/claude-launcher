"""A branch and a pull request from a session's directory, without a checkout.

The session detail panel has a button for this, and the button opens a
wizard: pick the remote, the base, a branch name and a title, and the daemon
pushes what the session's directory holds and opens the pull request with
``gh``. Nothing here touches the session's checkout -- no ``git switch``, no
commit on the branch the session is working on, no index write the session
would see. That matters because the session is usually an agent mid-task:
a checkout moved under it is a task silently derailed.

Three git facts make that possible, and the whole module is built on them:

* ``git push <remote> <sha>:refs/heads/<name>`` publishes any commit under
  any branch name on the remote. The local repository needs no branch of
  that name at all -- and so none is created.
* A commit can be built without the working index: ``GIT_INDEX_FILE`` points
  ``git add -A`` / ``git write-tree`` at a scratch index, and ``git
  commit-tree`` turns that tree into a commit object that hangs off HEAD as
  its parent but sits on no ref. That is how uncommitted changes ride along
  when the wizard asks for them (:func:`snapshot`).
* ``gh pr create --head <name>`` only needs the branch to exist on the
  remote, which the push just made true.

``improv-worker-remote``'s ``pr-open`` step spells the same push and the same
``gh`` calls in prose for the worker to type; this is the same recipe as code,
for the button. The two must not drift: the push refspec, the ``gh pr view``
probe and the ``gh pr create`` flags are the ones that step names.

Everything the daemon route needs is one call, :func:`run`, which never
raises for a step that failed -- the wizard shows a step list, and a push
that was refused is a row in it, not a 500.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import ghcli

#: Seconds one git/gh call may take. A push talks to the host and a ``gh pr
#: create`` round-trips its API; ``ghcli.TIMEOUT`` (8s) is sized for status
#: probes and would cut a slow push off mid-transfer.
TIMEOUT = 120.0

#: The step ids :func:`run` reports, in the order they happen.
STEPS = ("inspect", "snapshot", "push", "pr")

Runner = Callable[..., Tuple[int, str, str]]


class PrError(Exception):
    """One step could not be done. ``step`` names it for the step list."""

    def __init__(self, step: str, message: str):
        super().__init__(message)
        self.step = step
        self.message = message


# --------------------------------------------------------------------------- #
# processes
# --------------------------------------------------------------------------- #
def _run(
    argv: Sequence[str],
    cwd: Optional[str] = None,
    env: Optional[dict] = None,
    timeout: float = TIMEOUT,
) -> Tuple[int, str, str]:
    """Run one command; a missing binary or a hang is a failure, not a crash."""
    try:
        proc = subprocess.run(
            list(argv), cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, env=env,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        return 127, "", str(exc)
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout:g}s: {' '.join(argv)}"
    except OSError as exc:
        return 126, "", str(exc)
    return proc.returncode, proc.stdout, proc.stderr


def _git(cwd: str, *args: str, env: Optional[dict] = None) -> Tuple[int, str, str]:
    code, out, err = _run(["git", "-C", cwd, *args], env=env)
    return code, out.strip(), err.strip()


def _gh_env() -> dict:
    env = dict(os.environ)
    env.setdefault("GH_NO_UPDATE_NOTIFIER", "1")
    env.setdefault("NO_COLOR", "1")
    env.setdefault("GH_PROMPT_DISABLED", "1")
    return env


def _fail(step: str, what: str, code: int, out: str, err: str) -> PrError:
    tail = (err or out or "").strip().splitlines()
    detail = " / ".join(ln.strip() for ln in tail[-3:]) if tail else f"exit {code}"
    return PrError(step, f"{what}: {detail}")


# --------------------------------------------------------------------------- #
# names
# --------------------------------------------------------------------------- #
def default_branch(session: str, now: Optional[datetime] = None) -> str:
    """``<session>-pr-<stamp>`` -- the session first, as every branch a
    session makes is named here, so a rail full of them still says whose."""
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M")
    head = re.sub(r"[^A-Za-z0-9._-]+", "-", (session or "").strip()).strip("-")
    return f"{head or 'session'}-pr-{stamp}"


def validate_branch(cwd: str, name: str) -> str:
    """``name`` if git accepts it as a branch, else raise for the ``push`` step.

    Asked of git itself (``check-ref-format --branch``) rather than of a
    regex here: the rules have a dozen clauses and the only one that matters
    is git's own reading of them, which is what the push would hit.
    """
    name = (name or "").strip()
    if not name:
        raise PrError("push", "branch name is empty")
    code, _, err = _git(cwd, "check-ref-format", "--branch", name)
    if code != 0:
        raise PrError("push", f"invalid branch name {name!r}: {err or 'refused by git'}")
    return name


# --------------------------------------------------------------------------- #
# what the directory is
# --------------------------------------------------------------------------- #
def _dirty(cwd: str) -> Dict[str, int]:
    """Tracked and untracked counts, separately: the wizard's checkbox is
    about both, the note beside it says which."""
    code, out, _ = _git(cwd, "status", "--porcelain", "--untracked-files=all")
    if code != 0:
        return {"tracked": 0, "untracked": 0}
    tracked = untracked = 0
    for line in out.splitlines():
        if line.startswith("??"):
            untracked += 1
        elif line.strip():
            tracked += 1
    return {"tracked": tracked, "untracked": untracked}


def _remotes(cwd: str) -> List[dict]:
    code, listing, _ = _git(cwd, "remote")
    if code != 0:
        return []
    _, chosen, _ = _git(cwd, "config", "--get", ghcli.REMOTE_KEY)
    rows: List[dict] = []
    for remote in (r.strip() for r in listing.splitlines() if r.strip()):
        _, url, _ = _git(cwd, "remote", "get-url", remote)
        host, slug = ghcli.parse_remote_url(url)
        rows.append({
            "remote": remote, "url": url, "host": host, "slug": slug,
            "configured": bool(chosen) and remote == chosen,
        })
    return rows


def _pick_remote(rows: List[dict]) -> str:
    """The configured one, else ``origin``, else the first with a host."""
    for r in rows:
        if r["configured"]:
            return r["remote"]
    for r in rows:
        if r["remote"] == "origin":
            return "origin"
    for r in rows:
        if r["host"]:
            return r["remote"]
    return rows[0]["remote"] if rows else ""


def _worktree_name(cwd: str) -> str:
    """The checkout's name when it is a linked worktree, '' in the main one --
    the same distinction the commit-stamp skill draws for its trailer."""
    code, top, _ = _git(cwd, "rev-parse", "--show-toplevel")
    if code != 0 or not top:
        return ""
    code, common, _ = _git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if code != 0 or not common:
        return ""
    root = Path(common).parent
    top_p = Path(top)
    try:
        same = top_p.resolve() == root.resolve()
    except OSError:
        same = top_p == root
    return "" if same else top_p.name


def preview(
    cwd: str,
    *,
    session: str = "",
    which=shutil.which,
    auth=ghcli.auth_status,
    now: Optional[datetime] = None,
) -> dict:
    """Everything the wizard shows before it asks anything.

    One call, off the loop: it is four or five git processes plus a ``gh auth
    status`` per host, and the form opens once. ``blockers`` is what would
    stop :func:`run` before it pushes -- not a repository, no remote with a
    host, no ``gh``, not signed in -- so the wizard can say so with its
    button disabled instead of failing on the press.
    """
    doc: dict = {
        "cwd": cwd, "repo": False, "root": "", "worktree": "", "branch": "",
        "head": "", "head_short": "", "head_subject": "",
        "dirty": {"tracked": 0, "untracked": 0},
        "remotes": [], "remote": "", "base": "master",
        "branch_default": default_branch(session, now),
        "gh": ghcli.client(which), "auth": {},
        "blockers": [],
    }
    code, top, _ = _git(cwd, "rev-parse", "--show-toplevel") if cwd and os.path.isdir(cwd) else (1, "", "")
    if code != 0:
        doc["blockers"].append("the session's directory is not inside a git repository")
        return doc
    doc["repo"] = True
    doc["root"] = top
    doc["worktree"] = _worktree_name(cwd)
    _, branch, _ = _git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    doc["branch"] = branch
    code, head, _ = _git(cwd, "rev-parse", "HEAD")
    if code != 0:
        doc["blockers"].append("HEAD has no commit yet -- nothing to push")
    else:
        doc["head"] = head
        doc["head_short"] = head[:8]
        _, subject, _ = _git(cwd, "log", "-1", "--format=%s", "HEAD")
        doc["head_subject"] = subject
    doc["dirty"] = _dirty(cwd)
    rows = _remotes(cwd)
    doc["remotes"] = rows
    doc["remote"] = _pick_remote(rows)
    _, base, _ = _git(cwd, "config", "--get", ghcli.BASE_KEY)
    doc["base"] = base or "master"
    if not rows:
        doc["blockers"].append("the repository has no remote to push to")
    elif not any(r["host"] for r in rows):
        doc["blockers"].append("no remote points at a GitHub host (local-path remotes cannot take a pull request)")
    gh = doc["gh"]
    if not gh.get("installed"):
        doc["blockers"].append(f"gh is not installed on the daemon's PATH ({ghcli.install_command()})")
    else:
        for host in sorted({r["host"] for r in rows if r["host"]}):
            st = auth(gh["path"], host)
            doc["auth"][host] = st
            if not st.get("authenticated"):
                doc["blockers"].append(f"gh is not signed in to {host} (gh auth login --hostname {host})")
    return doc


# --------------------------------------------------------------------------- #
# the commit to publish
# --------------------------------------------------------------------------- #
def snapshot(
    cwd: str,
    *,
    include_uncommitted: bool,
    session: str = "",
    message: str = "",
) -> dict:
    """The commit that will be pushed: HEAD, or a fresh commit of the working
    tree on top of it when there are uncommitted changes and they were asked
    for. Either way the checkout is left exactly as found.

    The fresh commit is built through a scratch index (``GIT_INDEX_FILE``):
    ``read-tree HEAD`` seeds it, ``add -A`` lays the working tree over that
    (untracked files included -- the wizard's checkbox says "what the
    directory holds", and a new file is the commonest thing it holds), and
    ``commit-tree`` makes a commit whose parent is HEAD and whose ref is
    nobody. The session's own index and branch are never opened for writing.
    The commit message carries the commit-stamp trailers so a reader of the
    PR can tell it was cut by the wizard and from which checkout.
    """
    code, head, err = _git(cwd, "rev-parse", "--verify", "HEAD")
    if code != 0:
        raise PrError("snapshot", f"HEAD has no commit: {err or 'unborn branch'}")
    doc = {"sha": head, "parent": head, "created": False, "files": 0}
    if not include_uncommitted:
        return doc
    dirty = _dirty(cwd)
    if not (dirty["tracked"] or dirty["untracked"]):
        return doc
    tmp = tempfile.mkdtemp(prefix="claunch-pr-")
    try:
        index = os.path.join(tmp, "index")
        env = dict(os.environ)
        env["GIT_INDEX_FILE"] = index
        code, _, err = _git(cwd, "read-tree", "HEAD", env=env)
        if code != 0:
            raise PrError("snapshot", f"read-tree: {err}")
        code, _, err = _git(cwd, "add", "-A", env=env)
        if code != 0:
            raise PrError("snapshot", f"add -A into the scratch index: {err}")
        code, tree, err = _git(cwd, "write-tree", env=env)
        if code != 0:
            raise PrError("snapshot", f"write-tree: {err}")
        _, head_tree, _ = _git(cwd, "rev-parse", "HEAD^{tree}")
        if tree == head_tree:
            # Dirty by status, identical by content (a touched-but-unchanged
            # file, or a mode flip git ignores here): nothing to add.
            return doc
        text = message.strip() or f"snapshot: uncommitted changes from session {session or 'unknown'}"
        trailers = []
        if session:
            trailers.append(f"Claunch-Session: {session}")
        wt = _worktree_name(cwd)
        if wt:
            trailers.append(f"Claunch-Worktree: {wt}")
        if trailers:
            text = text.rstrip() + "\n\n" + "\n".join(trailers) + "\n"
        msg = os.path.join(tmp, "message")
        with open(msg, "w", encoding="utf-8") as fh:
            fh.write(text)
        code, sha, err = _git(cwd, "commit-tree", tree, "-p", head, "-F", msg)
        if code != 0:
            raise PrError("snapshot", f"commit-tree: {err}")
        doc.update({"sha": sha, "created": True,
                    "files": dirty["tracked"] + dirty["untracked"]})
        return doc
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
# publishing
# --------------------------------------------------------------------------- #
def push(cwd: str, remote: str, sha: str, branch: str, *, force: bool = False) -> dict:
    """``git push <remote> <sha>:refs/heads/<branch>`` -- the refspec the
    remote overlay's pr-open step spells, with ``--force-with-lease`` for a
    branch that already exists there and is meant to be moved. Never a bare
    ``git push``: that reads the checkout's upstream, which is the session's
    own business."""
    branch = validate_branch(cwd, branch)
    if not remote:
        raise PrError("push", "no remote chosen")
    argv = ["push"]
    if force:
        argv.append(f"--force-with-lease=refs/heads/{branch}")
    argv += [remote, f"{sha}:refs/heads/{branch}"]
    code, out, err = _git(cwd, *argv)
    if code != 0:
        raise _fail("push", f"git push {remote} {branch}", code, out, err)
    return {"remote": remote, "branch": branch, "sha": sha, "forced": bool(force)}


_PR_FIELDS = "number,url,state,headRefOid,isDraft,title"
_URL_LINE = re.compile(r"https?://\S+/pull/\d+")


def _pr_json(text: str) -> Optional[dict]:
    try:
        doc = json.loads(text or "")
    except ValueError:
        return None
    return doc if isinstance(doc, dict) else None


def open_pr(
    cwd: str,
    *,
    repo: str,
    base: str,
    branch: str,
    title: str,
    body: str,
    draft: bool = False,
    gh: str = ghcli.BINARY,
    run: Runner = _run,
) -> dict:
    """``gh pr view`` first, ``gh pr create`` only when no OPEN pull request
    already stands for the branch -- a re-run of the wizard after a re-push
    finds the PR it made last time and leaves it (the push moved its head).
    A CLOSED or MERGED one is not revived; a new one is created beside it.
    """
    env = _gh_env()
    view = [gh, "pr", "view", branch, "-R", repo, "--json", _PR_FIELDS]
    code, out, err = run(view, cwd=cwd, env=env)
    if code == 0:
        found = _pr_json(out)
        if found and str(found.get("state", "")).upper() == "OPEN":
            found["existed"] = True
            return found
    tmp = tempfile.mkdtemp(prefix="claunch-pr-")
    try:
        body_file = os.path.join(tmp, "body.md")
        with open(body_file, "w", encoding="utf-8") as fh:
            fh.write(body or "")
        create = [gh, "pr", "create", "-R", repo, "--base", base, "--head", branch,
                  "--title", title, "--body-file", body_file]
        if draft:
            create.append("--draft")
        code, out, err = run(create, cwd=cwd, env=env)
        if code != 0:
            raise _fail("pr", "gh pr create", code, out, err)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    m = _URL_LINE.search(out or "") or _URL_LINE.search(err or "")
    url = m.group(0) if m else ""
    code, out, err = run([gh, "pr", "view", url or branch, "-R", repo, "--json", _PR_FIELDS],
                         cwd=cwd, env=env)
    found = _pr_json(out) if code == 0 else None
    if not found:
        if not url:
            raise PrError("pr", "gh pr create printed no pull request URL")
        found = {"url": url, "number": None, "state": "OPEN", "headRefOid": None,
                 "isDraft": bool(draft), "title": title}
    found["existed"] = False
    return found


# --------------------------------------------------------------------------- #
# the whole thing
# --------------------------------------------------------------------------- #
def _bool(value, fallback: bool) -> bool:
    if value is None:
        return fallback
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def run(
    cwd: str,
    request: dict,
    *,
    session: str = "",
    gh_run: Optional[Runner] = None,
    which=shutil.which,
    now: Optional[datetime] = None,
) -> dict:
    """Inspect, snapshot, push, open -- and report each as a step.

    ``request`` is the wizard's form: ``remote``, ``base``, ``branch``,
    ``title``, ``body``, ``draft``, ``include_uncommitted``, ``force``.
    Missing fields take the preview's defaults. The answer always carries
    ``ok`` and ``steps``; on a failure ``failed`` names the step and
    ``error`` says why, and the steps before it stand (a branch pushed
    before ``gh`` refused is still on the remote, and the answer says so).
    ``gh_run`` is the tests' seam: the same signature as :func:`_run`, fed
    only the ``gh`` commands.
    """
    steps: List[dict] = []
    result: dict = {"ok": False, "steps": steps, "session": session, "cwd": cwd}

    def done(step: str, detail: str, **extra) -> None:
        steps.append({"id": step, "ok": True, "detail": detail, **extra})

    def failed(exc: PrError) -> dict:
        steps.append({"id": exc.step, "ok": False, "detail": exc.message})
        result.update({"failed": exc.step, "error": exc.message})
        return result

    try:
        # ── inspect ──
        pv = preview(cwd, session=session, which=which, now=now)
        remotes = {r["remote"]: r for r in pv["remotes"]}
        remote = str(request.get("remote") or pv["remote"] or "").strip()
        if not pv["repo"]:
            raise PrError("inspect", pv["blockers"][0])
        if not remote or remote not in remotes:
            raise PrError("inspect", f"remote {remote!r} is not one of this repository's remotes")
        row = remotes[remote]
        if not row["host"] or not row["slug"]:
            raise PrError("inspect", f"remote {remote!r} does not point at a GitHub host ({row['url']})")
        repo = f"{row['host']}/{row['slug']}"
        base = str(request.get("base") or pv["base"] or "master").strip()
        branch = str(request.get("branch") or pv["branch_default"]).strip()
        include = _bool(request.get("include_uncommitted"), True)
        force = _bool(request.get("force"), False)
        draft = _bool(request.get("draft"), False)
        title = str(request.get("title") or "").strip() or (
            f"{session}: {pv['head_subject']}" if session else pv["head_subject"]
        ) or branch
        body = str(request.get("body") or "").strip() or (
            f"Opened by the claunch PR wizard from session `{session or '?'}`\n\n"
            f"- directory: `{cwd}`\n- checkout branch: `{pv['branch']}`\n"
        )
        if gh_run is None and not pv["gh"].get("installed"):
            raise PrError("inspect", "gh is not installed on the daemon's PATH")
        gh_path = pv["gh"].get("path") or ghcli.BINARY
        result.update({"remote": remote, "repo": repo, "base": base, "branch": branch,
                       "checkout_branch": pv["branch"], "head": pv["head"]})
        done("inspect", f"{repo} · base {base} · checkout on {pv['branch'] or '?'} @ {pv['head_short']}")

        # ── snapshot ──
        snap = snapshot(cwd, include_uncommitted=include, session=session)
        result["snapshot"] = snap
        result["tip"] = snap["sha"]
        done("snapshot",
             (f"commit {snap['sha'][:8]} built from {snap['files']} uncommitted path(s) on top of "
              f"{snap['parent'][:8]}") if snap["created"] else f"HEAD {snap['sha'][:8]} as it stands")

        # ── push ──
        pushed = push(cwd, remote, snap["sha"], branch, force=force)
        result["push"] = pushed
        done("push", f"{remote}/{branch} @ {snap['sha'][:8]}" + (" (force-with-lease)" if force else ""))

        # ── pr ──
        pr = open_pr(cwd, repo=repo, base=base, branch=branch, title=title, body=body,
                     draft=draft, gh=gh_path, run=gh_run or _run)
        result["pr"] = pr
        done("pr", (f"reused open #{pr.get('number')} " if pr.get("existed") else "opened ")
             + str(pr.get("url") or ""))
        result["ok"] = True
        return result
    except PrError as exc:
        return failed(exc)


# --------------------------------------------------------------------------- #
# what the session is told
# --------------------------------------------------------------------------- #
def report_block(result: dict) -> str:
    """The block typed into the session when the wizard's first checkbox is
    on: one screen of facts, in the fenced machine-generated shape every
    other automated delivery uses, ending with the one line the agent most
    needs -- that its checkout was not touched."""
    lines = ["---",
             "# claunch pr: a branch and pull request were submitted from this session's "
             "directory -- machine-generated, not typed by the user"]
    lines.append(f"directory: {result.get('cwd', '')}")
    if result.get("branch"):
        lines.append(f"branch: {result.get('remote', '')}/{result['branch']}")
    snap = result.get("snapshot") or {}
    if snap:
        if snap.get("created"):
            lines.append(f"tip: {snap['sha']} (a snapshot commit of {snap.get('files', 0)} "
                         f"uncommitted path(s), parent {snap.get('parent', '')[:8]}; it is on no local branch)")
        else:
            lines.append(f"tip: {snap['sha']} (HEAD as it stood)")
    pr = result.get("pr") or {}
    if result.get("ok"):
        num = f"#{pr['number']} " if pr.get("number") else ""
        state = "reused (already open)" if pr.get("existed") else "opened"
        draft = ", draft" if pr.get("isDraft") else ""
        lines.append(f"pr: {pr.get('url', '')} ({num}{state}{draft})")
        lines.append("status: ok")
    else:
        lines.append(f"status: failed at step '{result.get('failed', '?')}' -- {result.get('error', '')}")
        did = [s["id"] for s in result.get("steps", []) if s.get("ok")]
        if did:
            lines.append(f"done before that: {', '.join(did)}")
    lines.append("note: your checkout was not changed -- no branch was switched, nothing was "
                 "committed on it, and your index is as you left it. fyi only; no reply is expected.")
    lines.append("---")
    return "\n".join(lines)
