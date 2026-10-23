"""Reclaim a session's pytest basetemps once the session has ended.

The gate tools root every run's ``--basetemp`` under one directory
(:mod:`claude_launcher.pytest_basetemp`, ``C:/t`` on Windows) and name it
after the session that ran it:

* ``<session>c<MMDDHHMMSS>`` -- ``tools/changed_tests.py`` (one per run),
* ``<session>w`` -- ``tools/sweep.py``,
* ``<session>-<anything>`` -- a basetemp a session pinned by hand,
* ``<session>`` (the bare name) -- the session's general test directory
  (``$CLAUNCH_TEST_DIR``), non-pytest artifacts included.

``changed_tests.prune_basetemps`` keeps the newest eight of a session's own
while the session is still running. Nothing reclaimed them after it ended,
and a fleet of sessions each leaving eight 200MB trees behind filled C: to
264MB free (claunch-4zc51). This module is that reclaim: the manager's exit
funnel calls :meth:`BasetempReaper.session_exited`, and the boot calls
:meth:`BasetempReaper.reap_ended` once for the sessions that ended while no
daemon was there to see it.

Before a directory goes, one line describing it is appended to
``basetemp-history.jsonl`` in the daemon directory: which session, when the
run started and ended (from file mtimes), how many xdist workers it used
(``popen-gw*``), its size, and -- when the run left one -- the
``claunch-run.json`` summary ``changed_tests`` writes (tree, exit code,
counts). Lines older than :data:`HISTORY_RETENTION_DAYS` are dropped.

Ownership is decided by name, and names nest: ``s99`` is a prefix of
``s995``, and ``s995-*`` also matches ``s995-rev2c1011...``. A directory
therefore belongs to the *longest* known session name that matches it, and a
session reaps only what belongs to it -- never what belongs to a session
that is still running.

A daemon shutdown is not an ending: the manager does not run exit hooks for
it, and :meth:`reap_ended` skips every session that is still running or held,
so a restart deletes nothing a live session is using.

Names are only unique within one daemon instance -- each named instance
(``<base home>/daemons/<name>``) hands out its own ``sN`` -- while the
Windows root is one ``C:/t`` for the whole machine. So before anything goes,
:func:`foreign_live` reads every *other* instance's registry, and a name that
is running or held there is skipped: an ``s1`` that ended here must not take
the trees of the ``s1`` still running under ``local2``.

Boot and the exit funnel only see a session *end*, so a leader that swept
several times between two handoffs kept every generation until the next one
(claunch-ssroc: four trees, ~375k inodes and 2.0GB on 2026-10-22). The
periodic pass, :meth:`BasetempReaper.reap_live` on
:class:`BasetempClock`, closes that while sessions are still running:

* a live session's sweep generations (``<session>w``, ``<session>w<number>``,
  and the ``<session>-<label>w`` a run from a differently-named worktree
  leaves) -- the newest one is kept, and of the rest only those whose
  directory was last written ``basetemp_sweep_hours`` ago (default 6);
* a name no session of this instance owns (``sweepw``, ``dfv2-*``,
  ``s1162tree*``) -- once it is ``basetemp_orphan_days`` old (default 7).

Both branches read their keys from the config on every pass, and 0 turns the
branch off. A session holding a measurement-window grant is skipped whole:
its run is writing into its basetemp right now and the grant does not say
which directory that is. So a leader that sweeps for an hour holds the
grant for an hour and nothing of its own is reaped meanwhile -- the next
pass after the release takes it (329 trees under s29 on 2026-10-22). If a
person ever asks why a live root is not shrinking, this is the reason to
check first: ``claunch window status``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import sqlite3
import stat
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Tuple

from .. import atomic, pytest_basetemp
from . import paths, pools

log = logging.getLogger(__name__)

HISTORY_NAME = "basetemp-history.jsonl"
HISTORY_RETENTION_DAYS = 30
HISTORY_RETENTION = HISTORY_RETENTION_DAYS * 86400.0

#: The summary ``tools/changed_tests.py`` leaves in a basetemp it ran in.
RUN_SUMMARY = "claunch-run.json"

#: Defaults for the periodic pass; live values are ``basetemp_sweep_hours``
#: and ``basetemp_orphan_days`` in ``store.DAEMON_DEFAULTS``. 0 turns the
#: branch off.
SWEEP_HOURS = 6.0
ORPHAN_DAYS = 7.0

#: Seconds between periodic passes. A generation is hours old before it goes
#: and an orphan days, so an hour costs nothing and keeps the root scan off a
#: busy machine's back.
TICK = 3600.0

_STAMP = re.compile(r"c\d{10}")
#: ``tools/sweep.py``'s basetemp under the root: ``<session>w``, the numbered
#: generations a fixed name leaves behind when a later run cannot reuse it
#: (``<session>w2``, ``<session>w15``), and the ``-``-labelled form a run
#: from a differently-named worktree leaves (``<session>-<label>w``).
_SWEEP = re.compile(r"(?:-.+)?w\d*")
#: The same, stamped: ``<session>-<label>t<MMDDHHMMSS>``. Seen on this host as
#: ``s29-egr6yt1014051949`` -- a sweep run whose basetemp carries a run stamp
#: instead of the bare ``w``.
_STAMPED_SWEEP = re.compile(r"-.+t\d{10}")
#: ``popen-gw<N>``: the directory xdist puts under a basetemp for each worker.
_XDIST_WORKER = "popen-gw"


def default_root() -> Path:
    """The directory reaped when none is given: the gate tools' root."""
    return pytest_basetemp.root()


def matches(name: str, session: str) -> bool:
    """Whether directory ``name`` has one of ``session``'s shapes under the root.

    The bare ``<session>`` name is the session's general test directory
    (``$CLAUNCH_TEST_DIR``, pytest_basetemp.session_test_dir): non-pytest
    artifacts a session writes there are reaped at its exit exactly like
    its basetemps. It was nobody's shape before that directory existed,
    so claiming it breaks no existing attribution.
    """
    if not session or not name.startswith(session):
        return False
    rest = name[len(session):]
    return rest == "" or bool(_SWEEP.fullmatch(rest)) or bool(_STAMP.fullmatch(rest)) or (
        rest.startswith("-") and len(rest) > 1
    )


def stamped_run(name: str, session: str) -> bool:
    """Whether ``name`` is a ``<session>c<MMDDHHMMSS>`` run basetemp.

    The shape ``tools/changed_tests.py`` writes, one per run. Callers that
    label basetemps for a person (the Artifacts page) read the kind from
    here rather than matching the stamp themselves, so two lists of the same
    root cannot disagree about which directories are a run's.
    """
    if not session or not name.startswith(session):
        return False
    return bool(_STAMP.fullmatch(name[len(session):]))


def sweep_generation(name: str, session: str) -> bool:
    """Whether ``name`` is one of ``session``'s sweep basetemps.

    The shapes ``tools/sweep.py`` writes: the bare ``w``, the numbered
    ``w<digits>``, and the ``-``-labelled ``<label>w`` a run started from a
    differently-named worktree leaves, because the tool names the basetemp
    after ``$CLAUNCH_SESSION`` and that session is the worktree's. Those
    labels are generated (``s29-07cd9bfd7e2cbe0dfc551b74w``), and once the
    worktree session is gone from the registry ``owner()`` collapses the name
    to the base session -- which is alive, so neither this branch nor the
    orphan branch ever reaches them (claunch-9gpqk: 339 such trees under s29
    on 2026-10-22, ~49M inodes by sample).

    A ``-``-labelled name that does *not* end in ``w`` or a run stamp is a
    basetemp a session pinned by hand (``s1160-ctrlj-treeiso``) and is not a
    swept run; the bare ``<session>`` directory is the general test directory.
    Both are left to the exit funnel. ``matches`` keeps answering True for
    them -- only this predicate is the narrower one.

    The name alone cannot settle it: a hand-pinned basetemp may end in ``w``
    (``s677-narrow``, ``s90-ipo2q-preview`` on this host), and a sweep may
    not (``<session>-<label>t<MMDDHHMMSS>``). So the shape is only the gate;
    :func:`is_sweep_tree` reads the content that does settle it, and
    :meth:`BasetempReaper._reap_generations` requires both.
    """
    if not session or not name.startswith(session):
        return False
    rest = name[len(session):]
    return bool(_SWEEP.fullmatch(rest) or _STAMPED_SWEEP.fullmatch(rest))


def is_sweep_tree(path: Path) -> bool:
    """Whether ``path`` holds an xdist run's worker directories.

    ``popen-gw<N>`` is what xdist creates under the basetemp for each worker,
    and it is the one signal that separates a sweep tree from a basetemp a
    session pinned by hand (claunch-9gpqk). One ``scandir`` per candidate,
    stopping at the first match: a serial run leaves none and is left to the
    exit funnel.
    """
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                if entry.name.startswith(_XDIST_WORKER) and entry.is_dir():
                    return True
    except OSError:
        return False
    return False


def settings() -> Tuple[float, float]:
    """``(sweep_hours, orphan_days)`` from the daemon config, read now.

    Read on every pass, like the auto-archive keys, so an edit takes effect
    without a restart. ``store`` is imported here rather than at module level:
    it pulls in ``yaml``, and the gate tools import this package under
    ``python -S`` where that import fails (see :mod:`..pytest_basetemp`).
    """
    try:
        from .. import store

        cfg = store.daemon_config()
    except Exception:  # noqa: BLE001 - no config is the defaults, not an error
        return SWEEP_HOURS, ORPHAN_DAYS

    def number(key: str, default: float) -> float:
        try:
            return max(float(cfg.get(key, default)), 0.0)
        except (TypeError, ValueError):
            return default

    return number("basetemp_sweep_hours", SWEEP_HOURS), number(
        "basetemp_orphan_days", ORPHAN_DAYS
    )


def owner(name: str, sessions: Iterable[str]) -> Optional[str]:
    """The longest of ``sessions`` whose shape ``name`` has, or ``None``."""
    best: Optional[str] = None
    for session in sessions:
        if matches(name, session) and (best is None or len(session) > len(best)):
            best = session
    return best


def live_name(entry: dict) -> Optional[str]:
    """The name of a registry record that is running or held, else ``None``.

    A held session's program has ended but the session has not
    (``Session.held_at``), so it reads as live: resuming it must find its
    basetemps where it left them.
    """
    sdef = entry.get("def") if isinstance(entry.get("def"), dict) else {}
    name = sdef.get("name")
    if name and (entry.get("was_running") or entry.get("held_at")):
        return str(name)
    return None


#: The old spelling, kept for this module's own call sites.
_live = live_name


def registry(directory: Path) -> List[dict]:
    """The session records an instance directory holds, read-only.

    ``sessions.db`` when there is one (opened ``mode=ro`` so a reader never
    creates or migrates another daemon's database), else the legacy
    ``sessions.json`` an instance that predates the database still has.
    """
    db = directory / "sessions.db"
    if db.is_file():
        try:
            conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=5.0)
            try:
                rows = conn.execute("SELECT data FROM sessions").fetchall()
            finally:
                conn.close()
        except sqlite3.Error:
            return []
        out = []
        for (data,) in rows:
            try:
                entry = json.loads(data)
            except (ValueError, TypeError):
                continue
            if isinstance(entry, dict):
                out.append(entry)
        return out
    try:
        entries = json.loads((directory / "sessions.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []


def registry_dirs() -> List[Path]:
    """Every directory that may hold a session registry.

    The base is :func:`paths.base_home`, not the launcher home: a named
    instance may run under its own home while its registry stays in the
    base (claunch-llmi1, and the reason ``foreign_live`` reads it too).
    """
    base = paths.base_home()
    dirs = [base / "daemon"]
    try:
        dirs += [p for p in (base / "daemons").iterdir() if p.is_dir()]
    except OSError:
        pass
    return dirs


def foreign_live() -> set:
    """Names running or held in any daemon instance other than this one."""
    own = paths.daemon_dir().resolve()
    names: set = set()
    for directory in registry_dirs():
        try:
            if not directory.is_dir() or directory.resolve() == own:
                continue
        except OSError:
            continue
        names.update(n for n in map(_live, registry(directory)) if n)
    return names


def describe(path: Path) -> dict:
    """Start, end, worker count, file count and size of one basetemp."""
    times: List[float] = []
    files = size = 0
    workers = set()
    for root, dirs, names in os.walk(path):
        workers.update(d for d in dirs if d.startswith("popen-gw"))
        for name in names:
            try:
                st = os.stat(os.path.join(root, name))
            except OSError:
                continue
            times.append(st.st_mtime)
            files += 1
            size += st.st_size
    if not times:
        try:
            times = [path.stat().st_mtime]
        except OSError:
            times = []
    row: dict = {"workers": len(workers), "files": files, "bytes": size}
    if times:
        row["started_at"] = _iso(min(times))
        row["ended_at"] = _iso(max(times))
        row["seconds"] = round(max(times) - min(times), 1)
    try:
        summary = json.loads((path / RUN_SUMMARY).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        summary = None
    if isinstance(summary, dict):
        row["run"] = summary
    return row


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def _retry_writable(failed: List[str]) -> Callable:
    """An rmtree error handler: clear the read-only bit, try once more.

    git writes ``.git/objects`` files read-only, and on Windows
    ``os.unlink`` refuses a read-only file (WinError 5) -- every test that
    builds a repository leaves such files, so without the retry a basetemp
    full of them stayed behind as "locked" on every pass (claunch-4zc51.3).
    Whatever still fails after the retry is recorded in ``failed``. Fits
    both ``onexc`` and ``onerror``: the third argument is not read.

    Only the removals are retried. rmtree also reports failures of
    ``os.open``, ``os.scandir`` or ``os.lstat`` (POSIX fd-based walk), and
    calling those again with a path alone either raises ``TypeError`` --
    which would escape the reap -- or deletes nothing.

    The write bit is added to the mode, never assigned as the mode: a bare
    ``chmod(path, S_IWRITE)`` on a directory that is still not empty leaves
    it ``0200`` -- unlistable, so every later rmtree of its parent, pytest's
    own basetemp cleanup included, fails on it. A delayed sweep with a fixed
    basetemp then exited 3 on every retry for days (claunch-ersj2).
    """
    def handle(func, path, _exc) -> None:
        if func not in _REMOVALS:
            failed.append(path)
            return
        try:
            os.chmod(path, stat.S_IMODE(os.lstat(path).st_mode) | stat.S_IWRITE)
            func(path)
        except OSError:
            failed.append(path)
    return handle


_REMOVALS = (os.unlink, os.remove, os.rmdir)


def _restore_traversal(root: Path) -> bool:
    """Give the owner read and search back on every directory under ``root``.

    A basetemp left by an older build can be ``0200`` (claunch-ersj2): nobody
    can list it, so ``rmtree`` reports an ``os.scandir`` failure before it
    removes anything, and the tree is written down as "locked" and tried
    again on every boot -- 9,135 of 10,033 history rows on this machine
    (2026-10-22), and the inodes with them. The removal retry in
    :func:`_retry_writable` cannot reach that: it runs after the walk has
    already failed on the directory itself.

    Restoring the bits is what makes a second walk possible. Only directories
    are touched (a file's read-only bit is the removal retry's business),
    symlinks are not followed, and the mode is added to, never assigned.
    Returns whether anything changed.
    """
    changed = False
    stack = [root]
    while stack:
        path = stack.pop()
        try:
            st = os.lstat(path)
        except OSError:
            continue
        if not stat.S_ISDIR(st.st_mode):
            continue
        mode = stat.S_IMODE(st.st_mode)
        if not (mode & stat.S_IRUSR and mode & stat.S_IXUSR):
            try:
                os.chmod(path, mode | stat.S_IRWXU)
                changed = True
            except OSError:
                continue
        try:
            with os.scandir(path) as entries:
                stack.extend(
                    Path(e.path) for e in entries if e.is_dir(follow_symlinks=False)
                )
        except OSError:
            continue
    return changed


def _rmtree(path: Path, handler: Callable) -> None:
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=handler)
    else:
        shutil.rmtree(path, onerror=handler)


class BasetempReaper:
    """Deletes ended sessions' basetemps and keeps one line per deletion.

    ``sessions`` returns ``(name, ended)`` for every session the registry
    knows. ``background=False`` runs deletions inline (tests); the daemon
    runs them on a thread, because a 25,000-file tree takes seconds to
    remove and the exit funnel is called on the event loop.
    """

    def __init__(
        self,
        sessions: Callable[[], List[tuple]],
        *,
        root: Optional[Path] = None,
        history: Optional[Path] = None,
        background: bool = True,
        foreign: Callable[[], set] = foreign_live,
        grants: Optional[Callable[[], set]] = None,
    ) -> None:
        self._sessions = sessions
        self._foreign = foreign
        self._grants = grants if grants is not None else (lambda: set())
        self._root = root
        self._history = history
        self._background = background
        self._lock = threading.Lock()

    @property
    def root(self) -> Path:
        return self._root if self._root is not None else default_root()

    @property
    def history_path(self) -> Path:
        return self._history if self._history is not None else paths.daemon_dir() / HISTORY_NAME

    # ---- what belongs to whom -------------------------------------------- #

    def owned(self, session: str) -> List[Path]:
        """``session``'s basetemps under the root, attributed by longest name."""
        names = [name for name, _ in self._sessions()]
        if session not in names:
            names.append(session)
        try:
            entries = list(self.root.iterdir())
        except OSError:
            return []
        return sorted(
            p for p in entries
            if p.is_dir() and owner(p.name, names) == session
        )

    # ---- the two entry points -------------------------------------------- #

    def session_exited(self, session) -> None:
        """The manager's exit hook: reap that session's basetemps."""
        self._submit([session.sdef.name])

    def reap_ended(self) -> None:
        """At boot: reap every session the registry holds as ended."""
        self._submit([name for name, ended in self._sessions() if ended])

    def reap_live(self, *, now: Optional[float] = None) -> List[dict]:
        """The periodic pass: old sweep generations of live sessions, and
        names no session owns.

        Runs inline: the clock calls it through ``asyncio.to_thread`` so the
        walks never sit on the event loop. A session that is running or held
        in another instance (``foreign_live``) is skipped whole, and so is
        one holding a measurement-window grant -- its run is writing into
        that basetemp at this moment.
        """
        now = time.time() if now is None else now
        sweep_hours, orphan_days = settings()
        rows: List[dict] = []
        with self._lock:
            sessions = list(self._sessions())
            names = [name for name, _ in sessions]
            elsewhere = self._foreign()
            held = self._grants()
            try:
                entries = [p for p in self.root.iterdir() if p.is_dir()]
            except OSError:
                return []
            if sweep_hours > 0:
                rows += self._reap_generations(
                    entries, sessions, names, elsewhere, held,
                    now - sweep_hours * 3600.0,
                )
            if orphan_days > 0:
                rows += self._reap_orphans(
                    entries, names, elsewhere, now - orphan_days * 86400.0
                )
            if rows:
                self._append(rows)
        return rows

    def _reap_generations(
        self,
        entries: List[Path],
        sessions: List[tuple],
        names: List[str],
        elsewhere: set,
        held: set,
        cutoff: float,
    ) -> List[dict]:
        """Each live session's sweep generations but the newest, aged out.

        The newest is kept whatever its age: it is the one the next run
        would have reused, and keeping it bounds the pile-up without emptying
        the root under a session that is between sweeps.
        """
        rows: List[dict] = []
        for name, ended in sessions:
            # A session holding a window grant is running tests right now, and
            # which directory that run writes into is not in the grant -- so
            # the whole session is left alone until the grant is released.
            # The cost is visible and worth saying: a leader that sweeps for
            # an hour holds the grant for an hour, and the daemon reaps none
            # of its generations until the run ends. The next pass after the
            # release takes them (claunch-9gpqk measured 329 waiting).
            if ended or name in elsewhere or name in held:
                continue
            stamped: List[Tuple[float, Path]] = []
            for path in entries:
                if (
                    sweep_generation(path.name, name)
                    and owner(path.name, names) == name
                    and is_sweep_tree(path)
                ):
                    when = _mtime(path)
                    if when is not None:
                        stamped.append((when, path))
            stamped.sort(key=lambda pair: pair[0], reverse=True)
            for when, path in stamped[1:]:
                if when < cutoff:
                    row = self._reap_one(name, path)
                    row["reason"] = "sweep-generation"
                    rows.append(row)
        return rows

    def _reap_orphans(
        self,
        entries: List[Path],
        names: List[str],
        elsewhere: set,
        cutoff: float,
    ) -> List[dict]:
        """Directories whose name is no known session's, once they are old.

        ``sweepw`` (a sweep run with no ``CLAUNCH_SESSION``), ``dfv2-*`` and
        ``s1162tree*`` were left by sessions this registry never heard of and
        would otherwise stay forever. Age alone decides: a fresh one may be a
        sessionless sweep writing right now. A name some *other* instance's
        live session answers to is not an orphan, however old: that session's
        tree must not go because this registry never heard the name.
        """
        rows: List[dict] = []
        for path in entries:
            if owner(path.name, names) is not None:
                continue
            if any(matches(path.name, name) for name in elsewhere):
                continue
            when = _mtime(path)
            if when is None or when >= cutoff:
                continue
            row = self._reap_one(None, path)
            row["orphan"] = True
            rows.append(row)
        return rows

    def _submit(self, names: List[str]) -> None:
        if not names:
            return
        if not self._background:
            self.reap(names)
            return
        threading.Thread(
            target=self.reap, args=(names,), name="basetemp-reaper", daemon=True
        ).start()

    # ---- the work --------------------------------------------------------- #

    def reap(self, names: List[str]) -> List[dict]:
        """Delete each named session's basetemps; return the rows written."""
        rows: List[dict] = []
        with self._lock:
            elsewhere = self._foreign()
            for name in names:
                if name in elsewhere:
                    log.info("basetemp: %r is live in another daemon instance; kept", name)
                    continue
                for path in self.owned(name):
                    rows.append(self._reap_one(name, path))
            if rows:
                self._append(rows)
        return rows

    def _reap_one(self, session: Optional[str], path: Path) -> dict:
        row = {"session": session, "path": path.as_posix(), **describe(path)}
        row["reaped_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        failed: List[str] = []
        handler = _retry_writable(failed)
        _rmtree(path, handler)
        if path.exists() and _restore_traversal(path):
            # A directory nobody can list fails the walk before any removal
            # is attempted, so the removal retry never sees it. With the bits
            # back the second walk can do the whole job.
            failed = []
            handler = _retry_writable(failed)
            _rmtree(path, handler)
        if path.exists():
            # A file another process still holds open: Windows refuses to
            # delete it even once it is writable. Left for the next ending
            # or boot, and said so.
            row["outcome"] = "locked"
            row["locked"] = [Path(p).as_posix() for p in failed[:5]]
            log.warning("basetemp %s: %d entries could not be removed", path, len(failed))
        else:
            row["outcome"] = "removed"
        return row

    def _append(self, rows: List[dict]) -> None:
        cutoff = time.time() - HISTORY_RETENTION
        dest = self.history_path
        try:
            kept = [
                line for line in dest.read_text(encoding="utf-8").splitlines()
                if _fresh(line, cutoff)
            ]
        except OSError:
            kept = []
        kept += [json.dumps(r, ensure_ascii=False) for r in rows]
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".tmp")
            tmp.write_text("\n".join(kept) + "\n", encoding="utf-8")
            atomic.replace(tmp, dest)
        except OSError:
            log.exception("basetemp history: could not write %s", dest)


def _mtime(path: Path) -> Optional[float]:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _fresh(line: str, cutoff: float) -> bool:
    try:
        stamp = json.loads(line).get("reaped_at")
        return datetime.fromisoformat(stamp).timestamp() >= cutoff
    except (ValueError, TypeError, AttributeError):
        return False


def window_grants(window) -> Callable[[], set]:
    """The session names holding a measurement-window grant right now.

    A run that holds the window is running tests into its basetemp, so the
    periodic pass must leave that session's generations alone even when the
    directory is old. ``status`` is the documented read (``claunch window
    status``) and takes the state lock, so this is safe from the reap thread.
    ``requested_by`` is included because a delayed sweep drops
    ``CLAUNCH_SESSION`` and names the leader it works for.
    """
    def view() -> set:
        try:
            holders = window.status().get("holders") or []
        except Exception:  # noqa: BLE001 - no window is no exclusion
            return set()
        names: set = set()
        for holder in holders:
            if not isinstance(holder, dict):
                continue
            for key in ("session", "requested_by"):
                name = holder.get(key)
                if name:
                    names.add(str(name))
        return names

    return view


class BasetempClock:
    """Runs :meth:`BasetempReaper.reap_live` every :data:`TICK` seconds.

    Boot and the exit funnel only see a session end, so a leader that swept
    several times between handoffs kept every generation until the next one
    (claunch-ssroc). The first pass runs right after the clock starts, off
    the event loop, and then once per tick; the reaper's own lock serialises
    it with the exit funnel.
    """

    def __init__(self, reaper: BasetempReaper, *, poll: float = TICK) -> None:
        self.reaper = reaper
        self.poll = poll
        self._task: Optional[asyncio.Task] = None
        #: The last pass's rows, for a reader that asks (tests, a console).
        self.last: Optional[List[dict]] = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(
                self._run(), name="basetemp-clock"
            )

    async def shutdown(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _run(self) -> None:
        while True:
            try:
                self.last = await pools.background(self.reaper.reap_live)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad pass must not stop the clock
                log.exception("basetemp: periodic reap failed")
            await asyncio.sleep(self.poll)


def registry_view(manager) -> Callable[[], List[tuple]]:
    """``(name, ended)`` for every session in ``manager``.

    A held session's program has ended but the session has not
    (``Session.held_at``), so it reads as running.
    """
    def view() -> List[tuple]:
        out = []
        for session in manager.list():
            ended = bool(getattr(session, "exited", False)) and not getattr(
                session, "held_at", None
            ) and not getattr(session, "ended_by_os", False)
            out.append((session.sdef.name, ended))
        return out

    return view
