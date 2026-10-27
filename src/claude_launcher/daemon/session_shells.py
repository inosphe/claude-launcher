"""One shell per agent session — the session's ``cli`` split pane.

The dashboard's CLI tab keeps the user's own shell (``daemon/clipty.py``'s
:class:`~claude_launcher.daemon.clipty.ShellPty`) and nothing else: an agent
has no route to it. What an agent may do with a shell, it does with *its own
session's* shell, which is this registry's job:

* a session's first viewer (the ``cli`` pane, or any ``shell_*`` call) spawns
  that session's shell lazily, in the session's working directory;
* every route and tool addresses it by session name —
  ``/api/sessions/<name>/shell/*`` — so there is no way to name somebody
  else's shell;
* the shell is dropped when its session exits. A shell that outlived its
  session would be an orphan: its address answers 404 (the session record
  is gone) and nothing could ever restart or decide its proposals.

A session *restart* keeps the shell: the manager defers exit hooks across a
restart (``manager._restart_exit_hooks``), so only a real ending drops it.

Nothing here survives the daemon. A session's shell is spawned in the daemon
process (``pty_backend.spawn_local``), like the CLI tab's, so a daemon
restart takes every session shell with it and the pane comes up with a fresh
one. The PTY host (``daemon/ptyhost.py``, claunch-xnnct) is not involved:
adopting these shells across a restart would need a record a successor could
adopt them by, and this issue decided not to add one (claunch-jg8pt).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable, Dict, Optional

from . import clipty

log = logging.getLogger(__name__)


class SessionShells:
    """The daemon's per-session shells, by session name.

    Created by ``build_app`` and reached through ``app["session_shells"]``.
    One instance per app: the shells are its state, and the exit hook that
    drops them rides this app's manager.
    """

    def __init__(self, manager, *, factory: Optional[Callable[[str], clipty.SessionShell]] = None):
        self._manager = manager
        self._factory = factory or self._spawn
        self._shells: Dict[str, clipty.SessionShell] = {}

    def _spawn(self, name: str) -> clipty.SessionShell:
        """A shell for ``name``, in that session's working directory."""
        cwd = None
        try:
            cwd = getattr(self._manager.get(name).sdef, "cwd", None) or None
        except Exception:  # noqa: BLE001 -- no record, no cwd; the shell still works
            cwd = None
        return clipty.SessionShell(cwd=cwd)

    def get(self, name: str) -> Optional[clipty.SessionShell]:
        """The shell ``name`` already has, or ``None``."""
        return self._shells.get(name)

    def get_or_create(self, name: str) -> clipty.SessionShell:
        """``name``'s shell, spawned on first use. The caller checks that
        the session exists — this registry does not, so a test can hand in
        names the manager does not know."""
        shell = self._shells.get(name)
        if shell is None:
            shell = self._factory(name)
            self._shells[name] = shell
        return shell

    def drop(self, name: str) -> None:
        """Forget ``name``'s shell and shut its child down.

        Synchronous because ``manager.exit_hooks`` are: the teardown itself
        is scheduled on the loop, which is where ``ShellPty.shutdown``
        belongs anyway (it awaits the child's exit).
        """
        shell = self._shells.pop(name, None)
        if shell is None:
            return
        try:
            asyncio.get_running_loop().create_task(shell.shutdown())
        except RuntimeError:  # no loop (teardown): nothing to await it on
            pass

    def session_exited(self, session) -> None:
        """``manager.exit_hooks`` entry: a session ended, so its shell goes."""
        name = getattr(getattr(session, "sdef", None), "name", "") or ""
        if name:
            self.drop(name)

    async def shutdown(self) -> None:
        """Drop every shell — the daemon is going away."""
        shells = list(self._shells.values())
        self._shells.clear()
        await asyncio.gather(*(s.shutdown() for s in shells), return_exceptions=True)


async def _close_session_shells(app) -> None:
    try:
        await app["session_shells"].shutdown()
    except Exception:  # noqa: BLE001 -- never let shutdown fail on this
        pass


def install(app) -> SessionShells:
    """Put the registry on ``app`` and wire the two hooks it needs.

    Idempotent: called once from ``build_app``; a second call (a test that
    built its own) returns what is already there.
    """
    registry = app.get("session_shells")
    if isinstance(registry, SessionShells):
        return registry
    registry = SessionShells(app["manager"])
    app["session_shells"] = registry
    # A registry handed to a test may carry only what that test needed; with
    # no exit hooks nobody drops a shell at its session's end, which is the
    # test's business, not a reason to refuse the app.
    exit_hooks = getattr(app["manager"], "exit_hooks", None)
    if exit_hooks is not None:
        exit_hooks.append(registry.session_exited)
    app.on_shutdown.append(_close_session_shells)
    return registry
