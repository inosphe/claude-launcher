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
from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional, Tuple

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
        )


def normalize(sdef: SessionDef) -> SessionDef:
    """Fill defaults (cwd) and validate the definition against config."""
    cwd = sdef.cwd or os.getcwd()
    sdef = replace(sdef, cwd=cwd)
    if sdef.harness == CLAUDE_HARNESS:
        if not sdef.profile:
            raise HarnessError(
                "the claude harness needs a profile (pass --profile NAME)"
            )
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

    ``restoring`` relaunches a previously running claude session with
    ``--continue`` so it picks the most recent conversation for that cwd within
    the profile's config dir — cwd and ``CLAUDE_CONFIG_DIR`` are both pinned by
    the definition, so this is deterministic.
    """
    if sdef.harness == CLAUDE_HARNESS:
        prof = profile_mod.require(sdef.profile)
        base = {
            k: v for k, v in os.environ.items() if k not in _NESTED_SESSION_MARKERS
        }
        env = runner.child_env(prof, with_token=True, base_env=base)
        argv = [launcher_config.claude_bin()]
        if restoring and "--continue" not in sdef.args and "--resume" not in sdef.args:
            argv.append("--continue")
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
    env.update(sdef.env)
    if sys.platform != "win32":
        env.setdefault("TERM", "xterm-256color")
    return argv, env, sdef.cwd
