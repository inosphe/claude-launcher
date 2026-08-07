"""Harness abstraction: what a session runs and with which environment.

A *harness* is a CLI agent program. ``claude`` is built in and reuses the whole
profile machinery (config dir, provider env, OAuth token) via
:func:`claude_launcher.runner.child_env`; other harnesses (codex, pi, ...) are
declared in the ``harnesses:`` section of ``~/.claunch.yaml``::

    harnesses:
      codex:
        command: codex          # string or argv list
        args: ["--foo"]         # optional, before the session's own args
        env: {KEY: VALUE}       # optional overrides

The session definition itself is also modelled here since the harness turns it
into a concrete (argv, env, cwd) triple.
"""

from __future__ import annotations

import os
import sys
import uuid
from dataclasses import dataclass, field, replace
from typing import Dict, Iterable, List, Optional, Tuple

from .. import profile as profile_mod, runner, store
from .. import config as launcher_config

CLAUDE_HARNESS = "claude"

#: Nested-session markers a parent Claude Code process leaves in the
#: environment. The daemon is often started from inside a claude session (the
#: user runs ``claunch new-session`` there), and a child claude that inherits
#: these thinks it is a nested/child session — which among other things turns
#: off transcript persistence and would break ``--continue`` restore.
_NESTED_SESSION_MARKERS = (
    "CLAUDECODE",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_SSE_PORT",
)


class HarnessError(Exception):
    """Raised for unknown harnesses or invalid session definitions."""


@dataclass(frozen=True)
class SessionDef:
    """The persistent *definition* of a session (what to run, where, how)."""

    name: str
    harness: str = CLAUDE_HARNESS
    profile: Optional[str] = None  # claude harness only
    cwd: str = ""
    args: Tuple[str, ...] = ()
    env: Dict[str, str] = field(default_factory=dict)
    restore: bool = True
    cols: int = 120
    rows: int = 30
    #: The claude conversation UUID pinned at creation (``--session-id``), so a
    #: restore resumes *this session's own* conversation (``--resume <id>``) —
    #: never ``--continue``, which grabs whatever conversation in the same
    #: cwd+profile happens to be the most recent and can hijack another one.
    conversation_id: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "harness": self.harness,
            "profile": self.profile,
            "cwd": self.cwd,
            "args": list(self.args),
            "env": dict(self.env),
            "restore": self.restore,
            "cols": self.cols,
            "rows": self.rows,
            "conversation_id": self.conversation_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SessionDef":
        return cls(
            name=str(data["name"]),
            harness=str(data.get("harness") or CLAUDE_HARNESS),
            profile=data.get("profile") or None,
            cwd=str(data.get("cwd") or ""),
            args=tuple(str(a) for a in data.get("args") or ()),
            env={str(k): str(v) for k, v in (data.get("env") or {}).items()},
            restore=bool(data.get("restore", True)),
            cols=int(data.get("cols") or 120),
            rows=int(data.get("rows") or 30),
            conversation_id=data.get("conversation_id") or None,
        )


#: Args that already steer which conversation claude opens; when the caller
#: passes any of these, the daemon must not pin or restore an id of its own.
_CONVERSATION_FLAGS = ("--continue", "-c", "--resume", "-r", "--session-id")


def _steers_conversation(args: Iterable[str]) -> bool:
    return any(
        a in _CONVERSATION_FLAGS or a.startswith(("--resume=", "--session-id="))
        for a in args
    )


def normalize(sdef: SessionDef, *, restoring: bool = False) -> SessionDef:
    """Fill defaults (cwd, pinned conversation) and validate against config."""
    cwd = sdef.cwd or os.getcwd()
    sdef = replace(sdef, cwd=cwd)
    if sdef.harness == CLAUDE_HARNESS:
        if not sdef.profile:
            raise HarnessError(
                "the claude harness needs a profile (pass --profile NAME)"
            )
        # Pin a fresh conversation id at creation only — an id invented while
        # *restoring* an old (id-less) definition would resume nothing.
        if (
            not restoring
            and not sdef.conversation_id
            and not _steers_conversation(sdef.args)
        ):
            sdef = replace(sdef, conversation_id=str(uuid.uuid4()))
    elif sdef.harness not in store.harnesses():
        known = ", ".join(sorted([CLAUDE_HARNESS, *store.harnesses()]))
        raise HarnessError(
            f"unknown harness {sdef.harness!r} (known: {known}); "
            f"define it under 'harnesses:' in {store.path()}"
        )
    return sdef


def build_command(
    sdef: SessionDef, *, restoring: bool = False
) -> Tuple[List[str], Dict[str, str], str]:
    """Resolve a definition to the ``(argv, env, cwd)`` to spawn.

    A fresh claude session is started with ``--session-id <uuid>`` (the id
    pinned in the definition); ``restoring`` relaunches it with ``--resume``
    of that same id, so a restore always reopens *this session's own*
    conversation. Definitions predating the pin fall back to ``--continue``
    (most recent conversation for that cwd + profile).
    """
    if sdef.harness == CLAUDE_HARNESS:
        prof = profile_mod.require(sdef.profile)
        base = {
            k: v for k, v in os.environ.items() if k not in _NESTED_SESSION_MARKERS
        }
        env = runner.child_env(prof, with_token=True, base_env=base)
        argv = [launcher_config.claude_bin()]
        if not _steers_conversation(sdef.args):
            if restoring:
                if sdef.conversation_id:
                    argv.extend(["--resume", sdef.conversation_id])
                else:
                    argv.append("--continue")
            elif sdef.conversation_id:
                argv.extend(["--session-id", sdef.conversation_id])
        argv.extend(sdef.args)
    else:
        entry = store.harnesses().get(sdef.harness) or {}
        command = entry.get("command") or sdef.harness
        argv = list(command) if isinstance(command, list) else [str(command)]
        argv.extend(str(a) for a in entry.get("args") or ())
        argv.extend(sdef.args)
        env = os.environ.copy()
        env.update(
            {str(k): str(v) for k, v in (entry.get("env") or {}).items()}
        )
    # The session's identity, tmux's ``$TMUX`` equivalent. Children (claude,
    # its MCP servers, `!` shells) inherit it — cflow keys its run state by
    # it, mapping each session 1:1 to its own workflow run.
    env["CLAUNCH_SESSION"] = sdef.name
    env.update(sdef.env)
    if sys.platform != "win32":
        env.setdefault("TERM", "xterm-256color")
    return argv, env, sdef.cwd
