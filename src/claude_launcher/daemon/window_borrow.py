"""Borrowing a waiting test run from another machine's window (claunch-ohsyx).

Two daemons that register clones of one repository (the same remote address,
``reposync.url_key``) may run each other's queued tests. The machine whose
window holds the waiter is the *lender* (A); the machine whose operator
pressed Borrow is the *borrower* (B). Only a person starts a borrow: the
Window page's button on B, never a session and never a timer -- automatic
borrowing is a later decision (the user's instruction on the issue).

A borrow is allowed only when B can rebuild A's tree by git alone, from what
the shared remote already has (:func:`recipe`):

* ``commit`` -- the waiter's HEAD is contained in a remote-tracking ref. A
  rebased PR's fast-forward merge is this case: its head was pushed.
* ``merge`` -- HEAD is a merge commit whose parents are each on the remote;
  B replays the merge and must arrive at the same tree hash.

Anything else (a dirty tree, an unpushed commit, a rebase that would have to
be replayed) is shown as not borrowable, with the reason. The tree hash, not
the commit id, is what B must reproduce: a replayed merge has another id.

The lender's half is :class:`~.window.WindowManager`'s borrow state and the
``/peer/window/*`` handlers in ``daemon/api.py``; the borrower's half is
:class:`BorrowRunner`: fetch, a detached worktree, the merge replay and tree
check, ``uv sync``, B's own window grant, pytest with a junit report and the
window result file, a heartbeat every ``window_borrow_heartbeat`` seconds,
the result posted back, and the checkout removed. A heartbeat answered
``revoked`` or ``unknown`` stops the run: A has given the waiter back to its
own queue (or the waiter is gone), and a result would be discarded.

The peer routes sit outside the Bearer middleware like ``/peer/repos``: any
backend on a relay this daemon is registered with may list the queue and
borrow. What a borrow returns -- a token -- is what every later beat, result
and release must carry, because the relay does not say who sent a request.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import platform
import posixpath
import secrets
import shutil
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Callable, Dict, List, Optional

from .. import atomic, pytest_basetemp, reposync, store, test_window, workspaces
from . import paths
from . import window as window_mod

log = logging.getLogger(__name__)

#: How long a lender's answer about one tree is reused. The page polls the
#: peers' queues; ``git for-each-ref --contains`` over every remote ref is
#: not free on a large clone.
RECIPE_TTL = 60.0
#: One peer call's ceiling (the relay's own is 30s).
PEER_TIMEOUT = 15.0
#: What of a borrowed run travels back. The output's tail carries pytest's
#: summary and short failure lines, which is what ``tools/sweep.py`` reads.
OUTPUT_TAIL = 256 * 1024
JUNIT_MAX = 4 * 1024 * 1024
#: The lender's request body ceiling for a result (output + junit + JSON).
RESULT_BODY_MAX = 8 * 1024 * 1024
GIT_TIMEOUT = 300.0
SETUP_TIMEOUT = 1800.0
#: Finished borrow records kept on the page.
KEEP_RUNS = 20

ACTIVE = ("starting", "fetching", "checkout", "setup", "waiting-window", "testing", "reporting")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def enabled() -> bool:
    return bool(store.daemon_config().get("window_borrow", True))


def heartbeat_interval() -> float:
    try:
        value = float(store.daemon_config().get("window_borrow_heartbeat",
                                                window_mod.BORROW_HEARTBEAT))
    except (TypeError, ValueError):
        return window_mod.BORROW_HEARTBEAT
    return value if value > 0 else window_mod.BORROW_HEARTBEAT


def heartbeat_timeout() -> float:
    try:
        value = float(store.daemon_config().get("window_borrow_timeout",
                                                window_mod.BORROW_TIMEOUT))
    except (TypeError, ValueError):
        return window_mod.BORROW_TIMEOUT
    return value if value > 0 else window_mod.BORROW_TIMEOUT


# --------------------------------------------------------------------------- #
# the lender: can this waiter's tree be rebuilt elsewhere?
# --------------------------------------------------------------------------- #
def _git(repo: str, *args: str, check: bool = True) -> str:
    return reposync.git(repo, *args, check=check, timeout=GIT_TIMEOUT)


def _remote_ref_of(repo: str, sha: str, keyed: Dict[str, str]) -> Optional[dict]:
    """A remote-tracking ref that contains ``sha``, as the address key and the
    branch name a peer fetches; None when no remote has it."""
    for key, remote in keyed.items():
        out = _git(repo, "for-each-ref", "--contains", sha,
                   "--format=%(refname)", f"refs/remotes/{remote}/", check=False)
        for ref in out.splitlines():
            branch = ref[len(f"refs/remotes/{remote}/"):]
            if branch and branch != "HEAD":
                return {"sha": sha, "key": key, "branch": branch}
    return None


_recipes: Dict[tuple, tuple] = {}

#: pytest options that change which configuration, conftest files or plugins
#: a run loads. The lender's arguments select tests; they may not reach past
#: the borrower's checkout or swap its plugins (claunch-onqp6).
_SCOPE_OPTIONS = ("--config-file", "--rootdir", "--confcutdir")
_SCOPE_SHORT = ("-p", "-c")


def _escapes(path: str, base: str = "") -> bool:
    """``path`` is absolute (on either OS) or leaves the checkout when read
    from ``base``, both relative to the checkout's top."""
    path = path.replace("\\", "/")
    if PurePosixPath(path).is_absolute() or PureWindowsPath(path).drive:
        return True
    joined = posixpath.normpath(posixpath.join(base, path)) if path else base
    return joined == ".." or joined.startswith("../")


def vet_borrow(info: dict) -> Optional[str]:
    """Why the borrower must not run ``info``'s pytest, or None.

    ``rel_cwd`` must name a directory inside the checkout; every argument's
    path (a positional test id or an ``--opt=value`` value, before ``::``)
    must stay inside it from there; and the options that move pytest's
    configuration or plugins (``-p``, ``-c``, ``--config-file``,
    ``--rootdir``, ``--confcutdir``) are refused outright.
    """
    rel = str(info.get("rel_cwd") or "")
    if _escapes(rel):
        return f"rel_cwd {rel!r} is outside the checkout"
    base = posixpath.normpath(rel.replace("\\", "/")) if rel else ""
    base = "" if base == "." else base
    for raw in [str(a) for a in info.get("pytest_args") or []]:
        name, eq, value = raw.partition("=")
        if name in _SCOPE_OPTIONS or any(
                raw.startswith(short) and not raw.startswith("--") for short in _SCOPE_SHORT):
            return f"pytest option {raw!r} changes the run's configuration or plugins"
        target = value if (eq and raw.startswith("-")) else ("" if raw.startswith("-") else raw)
        if _escapes(target.split("::", 1)[0], base):
            return f"pytest argument {raw!r} names a path outside the checkout"
    return None


def recipe(info: dict) -> dict:
    """How another machine rebuilds the waiter's tree, or why it cannot.

    ``{"ok": True, "kind": "commit"|"merge", "head", "tree", "keys",
    "refs": [{"sha", "key", "branch"}...]}`` -- for a merge, one ref per
    parent in parent order -- or ``{"ok": False, "reason": ...}``.
    """
    top = str(info.get("toplevel") or "")
    head = str(info.get("head") or "")
    tree = str(info.get("tree") or "")
    if info.get("dirty"):
        return {"ok": False, "reason": "the waiter's tree has uncommitted changes"}
    if not top or not head or not tree:
        return {"ok": False, "reason": "the waiter sent no commit to reproduce"}
    if info.get("pytest_args") is None:
        return {"ok": False, "reason": "the run's arguments name paths outside its repository"}
    refused = vet_borrow(info)
    if refused:
        return {"ok": False, "reason": refused}
    memo_key = (top, head, tree)
    hit = _recipes.get(memo_key)
    if hit and time.monotonic() - hit[0] < RECIPE_TTL:
        return hit[1]
    try:
        answer = _recipe(top, head, tree)
    except reposync.SyncError as exc:
        answer = {"ok": False, "reason": str(exc)}
    _recipes[memo_key] = (time.monotonic(), answer)
    if len(_recipes) > 256:
        oldest = sorted(_recipes, key=lambda k: _recipes[k][0])[:128]
        for key in oldest:
            _recipes.pop(key, None)
    return answer


def _recipe(top: str, head: str, tree: str) -> dict:
    if _git(top, "rev-parse", "--verify", "-q", f"{head}^{{tree}}", check=False) != tree:
        return {"ok": False, "reason": f"{head[:12]} is not the commit of tree {tree[:12]} here"}
    keyed = reposync.remote_keys(top)
    if not keyed:
        return {"ok": False, "reason": "the repository has no network remote"}
    base = {"ok": True, "head": head, "tree": tree, "keys": sorted(keyed)}
    found = _remote_ref_of(top, head, keyed)
    if found:
        return {**base, "kind": "commit", "refs": [found]}
    parents = _git(top, "rev-list", "--parents", "-n", "1", head).split()[1:]
    if len(parents) >= 2:
        refs = []
        for parent in parents:
            ref = _remote_ref_of(top, parent, keyed)
            if ref is None:
                return {"ok": False, "reason": (
                    f"merge {head[:12]}: parent {parent[:12]} is on no remote")}
            refs.append(ref)
        return {**base, "kind": "merge", "refs": refs}
    return {"ok": False, "reason": f"{head[:12]} is on no remote (push it, or run it here)"}


def queue_rows(window) -> List[dict]:
    """The lender's waiting requests as a peer sees them (blocking: git)."""
    rows = []
    for position, entry in enumerate(window.borrowable(), 1):
        info = entry.get("borrow_info") or {}
        row = {
            "grant_id": entry.get("grant_id"),
            "cls": entry.get("cls"),
            "label": entry.get("label") or "",
            "session": entry.get("session") or "",
            "requested_by": entry.get("requested_by") or "",
            "enqueued_at": entry.get("enqueued_at"),
            "priority": entry.get("priority") or 0,
            "position": position,
            "rel_cwd": info.get("rel_cwd") or "",
            "pytest_args": info.get("pytest_args"),
            "borrow": entry.get("borrow"),
            "borrows": entry.get("borrows") or [],
        }
        if entry.get("borrow"):
            row.update(borrowable=False,
                       reason=f"borrowed by {entry['borrow'].get('machine')}")
        else:
            made = recipe(info)
            row["borrowable"] = bool(made.get("ok"))
            row["reason"] = made.get("reason") or ""
            if made.get("ok"):
                row["recipe"] = made
        rows.append(row)
    return rows


# --------------------------------------------------------------------------- #
# the borrower
# --------------------------------------------------------------------------- #
def local_repos() -> List[dict]:
    """Every registered workspace's clone with its remote address keys."""
    out = []
    for ws in workspaces.list_all():
        try:
            top = reposync.toplevel(ws.path)
            keyed = reposync.remote_keys(top)
        except reposync.SyncError:
            continue
        if keyed:
            out.append({"workspace": ws.name, "path": top, "keys": keyed})
    return out


def match_repo(recipe_doc: dict, repos: List[dict]) -> Optional[dict]:
    """The local clone that shares the recipe's remote, preferring the one
    whose address the first ref names."""
    refs = recipe_doc.get("refs") or []
    wanted = [r.get("key") for r in refs]
    for repo in repos:
        if all(k in repo["keys"] for k in wanted):
            return repo
    return None


def _python(checkout: Path) -> Path:
    return checkout / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def setup_command(checkout: Path) -> Optional[List[str]]:
    """``uv sync`` for the checkout, with the ``test`` extra when it has one;
    None when the checkout is not a uv project."""
    if not (checkout / "uv.lock").exists() or not (checkout / "pyproject.toml").exists():
        return None
    try:
        import tomllib
    except ImportError:  # Python < 3.11
        tomllib = None
    extras = {}
    if tomllib is not None:
        with contextlib.suppress(OSError, ValueError):
            doc = tomllib.loads((checkout / "pyproject.toml").read_text(encoding="utf-8"))
            extras = (doc.get("project") or {}).get("optional-dependencies") or {}
    return ["uv", "sync", "--extra", "test"] if "test" in extras else ["uv", "sync"]


#: Launcher variables the borrowed suite does not inherit (tools/sweep.py's
#: ``_SUITE_DROPPED_VARS`` plus the session's): it runs in a throwaway
#: launcher home and reaches no daemon.
SUITE_DROPPED_VARS = (
    "CLAUNCH_SESSION", "CLAUNCH_INSTANCE", "CLAUNCH_DAEMON", "CLAUNCH_INSTANCE_APPLIED",
    "CLAUNCH_INSTANCE_PREV", "CLAUNCH_DAEMON_PORT", "VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT",
    "PYTEST_ADDOPTS",
)


def suite_env(home: Path, *, grant_id: str, cls: str, width: int, result_file: Path) -> dict:
    env = dict(os.environ)
    for name in SUITE_DROPPED_VARS:
        env.pop(name, None)
    env["CLAUDE_LAUNCHER_HOME"] = str(home)
    env["CLAUNCH_INSTANCE_BASE"] = str(home)
    env["CLAUDE_LAUNCHER_SYNC_FILE"] = str(home / ".claunch.yaml")
    # The grant B's window gave this run: the suite's conftest treats it as
    # inherited, clamps -n to its width and writes its result to the file.
    env[test_window.WINDOW_GRANT_ENV] = grant_id
    env[test_window.WINDOW_CLASS_ENV] = cls
    env[test_window.WINDOW_WORKERS_ENV] = str(width)
    env[test_window.WINDOW_REPORT_ENV] = "1"
    env[test_window.WINDOW_RESULT_FILE_ENV] = str(result_file)
    return env


class BorrowStopped(Exception):
    """The run stops: revoked by the lender, stopped here, or unanswered."""


class BorrowRunner:
    """The borrower's runs: started by the operator, one task each.

    ``transport(machine, path, body)`` is the mesh manager's relay POST;
    ``window`` is this daemon's own window, which arbitrates the borrowed
    runs against everything else on this machine (D10: several borrows may
    run at once, as many as this window grants).

    ``setup_command`` and ``python_of`` are the test environment's setup and
    its interpreter, as attributes so a test can stand in for ``uv sync``.
    """

    setup_command = staticmethod(setup_command)
    python_of = staticmethod(_python)

    def __init__(
        self,
        window,
        transport: Callable,
        machine: Callable[[], str],
        *,
        root: Optional[Path] = None,
        lister: Optional[Callable] = None,
    ) -> None:
        self._window = window
        self._transport = transport
        self._lister = lister
        self._machine = machine
        self._root = root or (paths.daemon_dir() / "window-borrow")
        self._record_path = self._root.parent / "window-borrows.json"
        self._runs: List[dict] = self._load()
        self._tasks: Dict[str, asyncio.Task] = {}
        self._stop_reason: Dict[str, str] = {}

    # ---- records --------------------------------------------------------- #

    def _load(self) -> List[dict]:
        try:
            doc = json.loads(self._record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [r for r in doc.get("runs") or [] if isinstance(r, dict)]

    def _save(self) -> None:
        finished = [r for r in self._runs if r.get("status") not in ACTIVE]
        active = [r for r in self._runs if r.get("status") in ACTIVE]
        self._runs = finished[-KEEP_RUNS:] + active
        try:
            self._record_path.parent.mkdir(parents=True, exist_ok=True)
            with atomic.scratch(self._record_path) as tmp:
                tmp.write_text(json.dumps({"runs": self._runs}, indent=1,
                                          ensure_ascii=False), encoding="utf-8")
                atomic.replace(tmp, self._record_path)
        except OSError as exc:
            log.warning("window borrow: records not saved: %s", exc)

    def runs(self) -> List[dict]:
        return [{k: v for k, v in r.items() if k != "token"} for r in reversed(self._runs)]

    def _set(self, run: dict, **fields) -> None:
        run.update(fields)
        self._save()

    # ---- the peers' queues ------------------------------------------------ #

    async def remote_queues(self) -> List[dict]:
        """Every relay peer's waiting requests, each row told whether THIS
        machine could run it (a clone sharing its remote)."""
        if self._lister is None or self._transport is None:
            return []
        try:
            names = await self._lister()
        except Exception as exc:  # noqa: BLE001 — PeerError
            return [{"machine": None, "error": f"cannot list relay peers ({exc})"}]
        me = self._machine()
        repos = await asyncio.to_thread(local_repos)
        on = enabled()

        async def one(machine: str) -> dict:
            try:
                payload = await asyncio.wait_for(
                    self._transport(machine, "/peer/window/queue", {}), PEER_TIMEOUT)
            except Exception as exc:  # noqa: BLE001 — timeout, an old peer's 404
                return {"machine": machine, "error": str(exc) or type(exc).__name__}
            rows = list(payload.get("queue") or [])
            for row in rows:
                if not row.get("borrowable"):
                    continue
                local = match_repo(row.get("recipe") or {}, repos)
                if not on:
                    row.update(borrowable=False, reason="window_borrow is off on this daemon")
                elif local is None:
                    row.update(borrowable=False,
                               reason="no workspace here shares this repository's remote")
                else:
                    row["local_workspace"] = local["workspace"]
            return {"machine": machine, "queue": rows,
                    "lending": bool(payload.get("lending", True))}

        return list(await asyncio.gather(*(one(m) for m in sorted(names) if m != me)))

    # ---- one borrow -------------------------------------------------------- #

    async def start(self, machine: str, grant_id: str) -> dict:
        """Borrow ``grant_id`` from ``machine`` and start running it."""
        if not enabled():
            raise ValueError("window_borrow is off on this daemon")
        if any(r.get("machine") == machine and r.get("grant_id") == grant_id
               and r.get("status") in ACTIVE for r in self._runs):
            raise ValueError(f"{machine}:{grant_id} is already running here")
        answer = await asyncio.wait_for(self._transport(
            machine, "/peer/window/borrow",
            {"grant_id": grant_id, "machine": self._machine()}), PEER_TIMEOUT)
        if not answer.get("ok"):
            raise ValueError(answer.get("error") or "the lender refused")
        made = answer.get("recipe") or {}
        repo = match_repo(made, await asyncio.to_thread(local_repos))
        entry = answer.get("entry") or {}
        run = {
            "id": secrets.token_hex(4),
            "machine": machine,
            "grant_id": grant_id,
            "token": answer["token"],
            "cls": entry.get("cls") or test_window.TARGETED,
            "label": entry.get("label") or "",
            "session": entry.get("session") or entry.get("requested_by") or "",
            "info": entry.get("borrow_info") or {},
            "recipe": made,
            "workspace": repo["workspace"] if repo else None,
            "repo": repo["path"] if repo else None,
            "status": "starting",
            "started_at": _utcnow(),
            "last_beat": None,
            "beat": None,
        }
        self._runs.append(run)
        self._save()
        refused = vet_borrow(run["info"])
        if refused:
            await self._give_back(run, f"the borrower refused: {refused}")
            self._set(run, status="failed", error=refused, finished_at=_utcnow())
            return self._view(run)
        if repo is None:
            await self._give_back(run, "no workspace here shares this repository's remote")
            self._set(run, status="failed", error="no matching local clone",
                      finished_at=_utcnow())
            return self._view(run)
        self._tasks[run["id"]] = asyncio.ensure_future(self._drive(run))
        return self._view(run)

    def stop(self, run_id: str) -> bool:
        """The borrower's operator stops a run; the lender gets it back."""
        task = self._tasks.get(run_id)
        if task is None or task.done():
            return False
        self._stop_reason[run_id] = "stopped by the operator here"
        task.cancel()
        return True

    @staticmethod
    def _view(run: dict) -> dict:
        return {k: v for k, v in run.items() if k != "token"}

    async def _post(self, run: dict, path: str, body: dict, timeout: float = PEER_TIMEOUT) -> dict:
        return await asyncio.wait_for(self._transport(
            run["machine"], path,
            {"grant_id": run["grant_id"], "token": run["token"], **body}), timeout)

    async def _give_back(self, run: dict, reason: str) -> None:
        with contextlib.suppress(Exception):
            await self._post(run, "/peer/window/release", {"reason": reason})

    async def _heartbeat(self, run: dict, main: asyncio.Task) -> None:
        interval = heartbeat_interval()
        timeout = heartbeat_timeout()
        last_ok = time.monotonic()
        while not main.done():
            await asyncio.sleep(interval)
            try:
                answer = await self._post(run, "/peer/window/beat",
                                          {"phase": run.get("status") or ""})
            except Exception as exc:  # noqa: BLE001 — relay down: keep trying (D6)
                run["beat"] = f"unanswered: {exc or type(exc).__name__}"
                if time.monotonic() - last_ok > timeout:
                    # The lender has revoked by now; a result would be discarded.
                    self._stop_reason[run["id"]] = (
                        f"heartbeat unanswered for {timeout:.0f}s")
                    main.cancel()
                    return
                continue
            verdict = answer.get("beat") or "unknown"
            last_ok = time.monotonic()
            run["last_beat"] = _utcnow()
            run["beat"] = verdict
            if verdict != "ok":
                self._stop_reason[run["id"]] = f"lender answered {verdict}"
                run["revoked"] = verdict
                main.cancel()
                return

    async def _drive(self, run: dict) -> None:
        workdir = self._root / run["id"]
        main = asyncio.current_task()
        beat = asyncio.ensure_future(self._heartbeat(run, main))
        state: dict = {}
        try:
            await self._run(run, workdir, state)
        except asyncio.CancelledError:
            reason = self._stop_reason.pop(run["id"], "the daemon is stopping")
            if not run.get("revoked"):
                await self._give_back(run, reason)
            self._set(run, status="aborted", error=reason, finished_at=_utcnow())
        except Exception as exc:  # noqa: BLE001 — any failure gives the waiter back
            log.warning("window borrow %s failed: %s", run["id"], exc)
            await self._give_back(run, str(exc)[:200])
            self._set(run, status="failed", error=str(exc)[:500], finished_at=_utcnow())
        finally:
            beat.cancel()
            proc = state.get("proc")
            if proc is not None and proc.returncode is None:
                _kill(proc)
            grant = state.get("grant")
            if grant:
                with contextlib.suppress(Exception):
                    self._window.release(grant)
            await asyncio.to_thread(self._cleanup, run, workdir)
            self._tasks.pop(run["id"], None)

    def _cleanup(self, run: dict, workdir: Path) -> None:
        checkout = workdir / "checkout"
        if run.get("repo") and checkout.exists():
            subprocess.run(["git", "-C", run["repo"], "worktree", "remove", "--force",
                            str(checkout)], capture_output=True)
        shutil.rmtree(workdir, ignore_errors=True)
        if run.get("repo"):
            subprocess.run(["git", "-C", run["repo"], "worktree", "prune"],
                           capture_output=True)

    async def _run(self, run: dict, workdir: Path, state: dict) -> None:
        made = run["recipe"]
        repo = run["repo"]
        keyed = reposync.remote_keys(repo)
        self._set(run, status="fetching")
        for ref in made["refs"]:
            await asyncio.to_thread(self._fetch, repo, keyed[ref["key"]], ref)
        checkout = workdir / "checkout"
        workdir.mkdir(parents=True, exist_ok=True)
        self._set(run, status="checkout")
        await asyncio.to_thread(self._checkout, repo, checkout, made)
        self._set(run, status="setup")
        command = self.setup_command(checkout)
        if command is None:
            raise RuntimeError("the checkout is not a uv project (no uv.lock); "
                               "this daemon cannot set up its test environment")
        env = dict(os.environ)
        for name in ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "CLAUNCH_SESSION"):
            env.pop(name, None)
        code, out = await _run_proc(command, cwd=checkout, env=env, state=state,
                                    timeout=SETUP_TIMEOUT)
        if code != 0:
            raise RuntimeError(f"{' '.join(command)} exit {code}: {out[-400:]}")
        python = self.python_of(checkout)
        if not python.exists():
            raise RuntimeError("the test environment has no Python interpreter")

        self._set(run, status="waiting-window")
        info = run["info"]
        args = [str(a) for a in info.get("pytest_args") or []]
        requested = test_window.requested_width_of(args)
        while True:
            answer = await self._window.acquire(
                run["cls"], session=None, pid=os.getpid(),
                label=f"borrowed {run['machine']}: {run['label']}"[:200],
                wait=window_mod.MAX_WAIT, workers=requested,
                requested_by=f"borrow:{run['machine']}")
            if answer.get("granted"):
                state["grant"] = answer["grant_id"]
                width = int(answer.get("advisory_n") or 1)
                break
            if answer.get("error"):
                raise RuntimeError(f"this machine's window refused: {answer['error']}")

        self._set(run, status="testing", window_grant=state["grant"])
        home = workdir / "home"
        home.mkdir(parents=True, exist_ok=True)
        result_file = workdir / "result.json"
        junit = workdir / "junit.xml"
        basetemp = pytest_basetemp.ensure(pytest_basetemp.root()) / f"borrow-{run['id']}w"
        argv = [str(python), "-m", "pytest", *args, f"--basetemp={basetemp}",
                f"--junit-xml={junit}", "-o", "junit_family=xunit1"]
        cwd = checkout / (info.get("rel_cwd") or "")
        try:
            cwd.resolve().relative_to(checkout.resolve())
        except ValueError:
            raise RuntimeError(f"rel_cwd {info.get('rel_cwd')!r} resolves outside the checkout")
        clock = time.monotonic()
        code, output = await _run_proc(
            argv, cwd=cwd, state=state, env=suite_env(
                home, grant_id=state["grant"], cls=run["cls"], width=width,
                result_file=result_file))
        seconds = time.monotonic() - clock
        try:
            window_result = json.loads(result_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            window_result = None
        if window_result is not None:
            self._window.report_result(state["grant"], window_result)
        junit_text = None
        with contextlib.suppress(OSError):
            if junit.stat().st_size <= JUNIT_MAX:
                junit_text = junit.read_text(encoding="utf-8", errors="replace")
        payload = {
            "exit_code": code,
            "output": output[-OUTPUT_TAIL:],
            "output_truncated": len(output) > OUTPUT_TAIL,
            "junit": junit_text,
            "window_result": window_result,
            "machine": self._machine(),
            "platform": platform.platform(),
            "python": platform.python_version(),
            "duration": round(seconds, 1),
            "recipe": made.get("kind"),
            "tree": made.get("tree"),
            "argv": ["python", "-m", "pytest", *args],
        }
        self._set(run, status="reporting", exit_code=code, duration=round(seconds, 1))
        verdict = await self._send_result(run, payload)
        if verdict == "ok":
            self._set(run, status="passed" if code == 0 else "failed-tests",
                      finished_at=_utcnow())
        else:
            self._set(run, status="discarded", error=f"lender answered {verdict}",
                      finished_at=_utcnow())

    async def _send_result(self, run: dict, payload: dict) -> str:
        """Post the result, retrying while the lender may still accept it."""
        deadline = time.monotonic() + heartbeat_timeout()
        last = "unanswered"
        while True:
            try:
                answer = await self._post(run, "/peer/window/result",
                                          {"result": payload}, timeout=30.0)
                return answer.get("result") or "unknown"
            except Exception as exc:  # noqa: BLE001
                last = f"unanswered: {exc or type(exc).__name__}"
            if time.monotonic() >= deadline:
                return last
            await asyncio.sleep(5.0)

    @staticmethod
    def _fetch(repo: str, remote: str, ref: dict) -> None:
        sha = ref["sha"]
        if reposync._ok(repo, "cat-file", "-e", f"{sha}^{{commit}}"):
            return
        reposync.git(repo, "fetch", "--no-tags", remote, ref["branch"],
                     check=False, timeout=GIT_TIMEOUT)
        if reposync._ok(repo, "cat-file", "-e", f"{sha}^{{commit}}"):
            return
        # The branch moved on and dropped it; a remote that serves reachable
        # commits by id still has it.
        reposync.git(repo, "fetch", "--no-tags", remote, sha, check=False,
                     timeout=GIT_TIMEOUT)
        if not reposync._ok(repo, "cat-file", "-e", f"{sha}^{{commit}}"):
            raise RuntimeError(f"{sha[:12]} could not be fetched from {remote} {ref['branch']}")

    @staticmethod
    def _checkout(repo: str, checkout: Path, made: dict) -> None:
        refs = made["refs"]
        reposync.git(repo, "worktree", "add", "--detach", str(checkout), refs[0]["sha"],
                     timeout=GIT_TIMEOUT)
        if made["kind"] == "merge":
            reposync.git(str(checkout), "-c", "user.name=claunch borrow",
                         "-c", "user.email=borrow@claunch.invalid",
                         "merge", "--no-ff", "--no-edit", "--no-verify",
                         *[r["sha"] for r in refs[1:]], timeout=GIT_TIMEOUT)
        tree = reposync.git(str(checkout), "rev-parse", "HEAD^{tree}")
        if tree != made["tree"]:
            raise RuntimeError(
                f"the rebuilt tree {tree[:12]} is not the lender's {made['tree'][:12]}")

    # ---- daemon lifecycle -------------------------------------------------- #

    async def recover(self) -> None:
        """After a restart: every run the last process left active is over.
        Give each back to its lender now (D12) instead of after the
        lender's heartbeat deadline, and remove its checkout."""
        stale = [r for r in self._runs if r.get("status") in ACTIVE]
        for run in stale:
            await self._give_back(run, "the borrower's daemon restarted")
            self._set(run, status="aborted", error="the borrower's daemon restarted",
                      finished_at=_utcnow())
            await asyncio.to_thread(self._cleanup, run, self._root / run["id"])

    async def close(self) -> None:
        for run_id, task in list(self._tasks.items()):
            self._stop_reason[run_id] = "the borrower's daemon is stopping"
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)


def _kill(proc) -> None:
    with contextlib.suppress(ProcessLookupError, OSError):
        if os.name != "nt":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()


async def _run_proc(argv, *, cwd, env, state: dict, timeout: Optional[float] = None):
    """Run ``argv`` with stdout and stderr together; the process is kept in
    ``state`` so a stop kills it (with its xdist workers, as a group)."""
    kwargs = {"start_new_session": True} if os.name != "nt" else {}
    proc = await asyncio.create_subprocess_exec(
        *[str(a) for a in argv], cwd=str(cwd), env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, **kwargs)
    state["proc"] = proc
    try:
        out = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        _kill(proc)
        raise RuntimeError(f"{argv[0]} did not finish in {timeout:.0f}s") from None
    except asyncio.CancelledError:
        _kill(proc)
        raise
    finally:
        if state.get("proc") is proc and proc.returncode is not None:
            state.pop("proc", None)
    return proc.returncode, (out[0] or b"").decode("utf-8", errors="replace")
