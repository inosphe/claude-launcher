"""The environment variables that belong to one managed session, and their removal.

A managed session's shell carries who it is: ``CLAUNCH_SESSION`` and its
siblings (set by ``daemon.harness``), the markers its harness exports to its
own tool shell (``CLAUDECODE``, ``CODEX_THREAD_ID``, ...), the Herdr pane the
operator's terminal sits in, and the profile home its harness reads. Every one
of them is right *in that shell* and wrong in any process that outlives it or
acts for more than one session.

The daemon is such a process, and it is routinely started from a session's
shell: ``daemon_client.ensure_running`` spawns it from whatever CLI found it
absent, and each restart's successor inherits the previous daemon's
environment. Until claunch-2yiij only the process-owner tag was removed on the
way, so a daemon spawned from s609's shell kept ``CLAUNCH_SESSION=s609`` for
every later generation, and everything it did on its own account read that
name as its caller -- the driver of every v2 run it started, the actor of its
board comments, the ``CLAUDE_CONFIG_DIR`` a global install wrote into, the
pane token in every worktree name it generated (``w3-p1-*``) -- while the
sessions it spawned inherited the codex and Herdr identity of a session they
were not.

This module is the one list. It is applied where such an environment can
cross into the daemon (:func:`daemon_client.spawn_daemon`, the daemon's own
boot) and where the daemon hands its environment to a session
(``daemon.harness``); a session's own identity is then set explicitly from its
definition, never inherited.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Mapping, MutableMapping, Optional

#: The operator v2 a session belongs to (``daemon.operators_v2``), carried by
#: the session so the MCP server it spawns can tell whose surface it is
#: without asking the daemon: an operator's session has no session QA
#: (``qa_*``) — its questions are ``opv2_ask`` in the Operator view.
OPERATOR_ENV = "CLAUNCH_OPERATOR_V2"

#: claunch's own per-session variables. Each is set by the daemon for one
#: session (or by a tool for one session's run) and names that session, its
#: files or its grants.
CLAUNCH_SESSION_ENV = (
    "CLAUNCH_SESSION",
    "CLAUNCH_PROJECT",
    "CLAUNCH_SCRATCH",
    "CLAUNCH_PROC_OWNER",  # daemon.session_procs.OWNER_ENV
    "CLAUNCH_CFLOW_RUN",  # cflow.state.RUN_ENV
    "CLAUNCH_WINDOW",  # test_window.WINDOW_*_ENV: one session's test grant
    "CLAUNCH_WINDOW_GRANT_ID",
    "CLAUNCH_WINDOW_CLASS",
    "CLAUNCH_WINDOW_ADVISORY_N",
    "CLAUNCH_WINDOW_REPORT",
    "CLAUNCH_SIM_ID",  # a simulation's run session (daemon.sim_api)
    OPERATOR_ENV,  # an operator v2's bound session (daemon.operators_v2)
)

#: What a harness exports into its own tool shell about the conversation that
#: shell belongs to. Inherited by another process they claim that conversation
#: (a nested-session refusal, a messaging socket, a codex thread id).
HARNESS_SESSION_ENV = (
    "CLAUDECODE",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_SSE_PORT",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_PID",
    "CODEX_SESSION_ID",
    "CODEX_THREAD_ID",
    "CODEX_CI",
)

#: The Herdr pane of the terminal the variables were exported in. The daemon
#: and its sessions are not in that pane; ``herdr.in_herdr`` / ``pane_id``
#: would otherwise label and name things after it (``worktree.pane_token``).
HERDR_PANE_ENV = (
    "HERDR_ENV",
    "HERDR_PANE_ID",
    "HERDR_TAB_ID",
    "HERDR_WORKSPACE_ID",
)

#: Every name removed unconditionally.
SESSION_ENV = CLAUNCH_SESSION_ENV + HARNESS_SESSION_ENV + HERDR_PANE_ENV

#: Profile homes the harness sets per session (see :func:`profile_home_env`).
CLAUDE_CONFIG_DIR_ENV = "CLAUDE_CONFIG_DIR"


def profile_home_env() -> List[str]:
    """The variables a harness points at a profile's home: Claude's
    ``CLAUDE_CONFIG_DIR`` and each declared harness's ``home_env``
    (``CODEX_HOME``...). A registry that cannot be read leaves the builtin
    names."""
    out = [CLAUDE_CONFIG_DIR_ENV, "CODEX_HOME"]
    try:
        from . import harnesses

        for entry in harnesses.registry().values():
            name = str(getattr(entry, "home_env", "") or "").strip()
            if name and name not in out:
                out.append(name)
    except Exception:  # noqa: BLE001 - a broken registry must not stop a spawn
        pass
    return out


def _inside_profiles(value: str) -> bool:
    try:
        from . import config

        root = config.profiles_dir().resolve()
        path = Path(value).expanduser().resolve()
    except Exception:  # noqa: BLE001 - not a path: not a profile home
        return False
    return path == root or root in path.parents


def session_names(env: Mapping[str, str]) -> List[str]:
    """The names in ``env`` that belong to a managed session.

    The fixed lists always. A profile home only when it is a session's: the
    environment also names a session (``CLAUNCH_SESSION``), or the value is
    inside claunch's profiles directory, which only the harness points it at.
    An operator's own ``CLAUDE_CONFIG_DIR`` elsewhere is their setting and
    stays.
    """
    out = [name for name in SESSION_ENV if name in env]
    from_session = bool(str(env.get("CLAUNCH_SESSION") or "").strip())
    for name in profile_home_env():
        value = env.get(name)
        if value is None:
            continue
        if from_session or _inside_profiles(value):
            out.append(name)
    return out


def scrub(env: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """A copy of ``env`` (default: this process's) without any session's
    variables."""
    out = dict(os.environ if env is None else env)
    for name in session_names(out):
        out.pop(name, None)
    return out


def scrub_in_place(env: Optional[MutableMapping[str, str]] = None) -> List[str]:
    """Remove the session variables from ``env`` (default: ``os.environ``)
    and return the names removed."""
    target = os.environ if env is None else env
    removed = session_names(target)
    for name in removed:
        target.pop(name, None)
    return removed
