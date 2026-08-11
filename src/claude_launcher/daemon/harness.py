"""Harness abstraction: what a session runs and with which environment.

A *harness* is a CLI agent program. Which ones exist is declared, not decided
here — see :mod:`claude_launcher.harnesses` for the packaged set (claude,
codex, pi) and how ``~/.claunch.yaml`` extends it. ``claude`` is the one
spawned through the profile machinery (config dir, provider env, OAuth token)
via :func:`claude_launcher.runner.child_env`; every other harness is a plain
command with optional args and env.

This module is the other half: turning a session definition plus its declared
harness into a concrete ``(argv, env, cwd)`` triple. The definition itself is
modelled here too.
"""

from __future__ import annotations

import os
import sys
import uuid
from dataclasses import dataclass, field, replace
from typing import Dict, Iterable, List, Optional, Tuple

from .. import harnesses as harness_registry
from .. import profile as profile_mod, runner, store
from .. import config as launcher_config
from . import mesh_roles

CLAUDE_HARNESS = harness_registry.CLAUDE_HARNESS

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
    #: The role this session runs as, from the packaged vocabulary (see
    #: :mod:`mesh_roles`). Its stance is injected into the system prompt at
    #: every spawn — claude harness only. ``None`` = no role, no injection.
    role: Optional[str] = None
    #: Which conversation to open instead of a fresh one. ``None`` = a new
    #: conversation; ``""`` = claude's interactive picker (bare ``--resume``);
    #: otherwise the conversation UUID (a session *name* is resolved to its
    #: conversation by :meth:`SessionManager.create`).
    resume: Optional[str] = None
    #: Resume into a *copy* (``--fork-session``): the resumed conversation is
    #: left untouched and this session gets its own from that point on.
    fork_session: bool = False

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
            "role": self.role,
            "resume": self.resume,
            "fork_session": self.fork_session,
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
            role=str(data.get("role") or "").strip() or None,
            resume=_resume_field(data.get("resume")),
            fork_session=bool(data.get("fork_session")),
        )


def _resume_field(raw) -> Optional[str]:
    """Read the ``resume`` field, keeping ``""`` (the picker) distinct from
    ``None`` (no resume at all) — the difference a plain falsiness test loses.

    ``true`` is accepted as a friendlier spelling of the picker, so an API
    client can say ``{"resume": true}`` for "let me pick".
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        return "" if raw else None
    return str(raw).strip()


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
    cwd = os.path.abspath(sdef.cwd or os.getcwd())
    if not os.path.isdir(cwd):
        # Caught here rather than at spawn time, where a missing directory
        # surfaces as "could not spawn 'claude'" and reads as a broken
        # install. This is the mistake `claunch workspace add` exists to stop
        # the web UI from making at all.
        raise HarnessError(f"working directory does not exist: {cwd}")
    sdef = replace(sdef, cwd=cwd)
    if sdef.harness == CLAUDE_HARNESS:
        if not sdef.profile:
            raise HarnessError(
                "the claude harness needs a profile (pass --profile NAME)"
            )
        sdef = _normalize_role(sdef)
        sdef = _normalize_resume(sdef)
        # Pin a fresh conversation id at creation only — an id invented while
        # *restoring* an old (id-less) definition would resume nothing.
        if (
            not restoring
            and not sdef.conversation_id
            and not _steers_conversation(sdef.args)
        ):
            # A resume without a fork *continues* the conversation it opened,
            # so that id is this session's own: pin it and a later restore
            # reopens the same one. A fork mints a new conversation, which
            # claude will happily put at an id we choose (--session-id
            # alongside --fork-session), so the fork stays restorable too.
            # The picker (resume == "") is the one case we cannot pin: nobody
            # knows yet which conversation the user will choose.
            if sdef.resume == "":
                pass
            elif sdef.resume and not sdef.fork_session:
                sdef = replace(sdef, conversation_id=sdef.resume)
            else:
                sdef = replace(sdef, conversation_id=str(uuid.uuid4()))
    else:
        entry = harness_registry.get(sdef.harness)
        if entry is None:
            known = ", ".join(harness_registry.names())
            raise HarnessError(
                f"unknown harness {sdef.harness!r} (known: {known}); "
                f"declare it under 'harnesses:' in {store.path()}"
            )
        if not entry.available():
            # Declared but not installed — the state 'pi' ships in. Saying so
            # here is the difference between "install pi" and a PtyError that
            # reads as claunch being broken.
            raise HarnessError(
                f"harness {sdef.harness!r} is declared but its command "
                f"{entry.program()!r} was not found on PATH — install it, or "
                f"point 'harnesses.{sdef.harness}.command' at the executable"
            )
        # Role injection and conversation resumption are both spelled in
        # claude's own flags. Refusing beats accepting and silently dropping
        # them: a session asked to run as 'reviewer' that does not would be
        # discovered much later, and by its behaviour.
        extras = [
            what
            for what, given in (
                ("role", sdef.role),
                ("resume", sdef.resume is not None),
                ("fork_session", sdef.fork_session),
            )
            if given
        ]
        if extras:
            raise HarnessError(
                f"{', '.join(extras)} only applies to the claude harness, "
                f"not {sdef.harness!r}"
            )
    return sdef


def _normalize_role(sdef: SessionDef) -> SessionDef:
    """Resolve the role name through the packaged vocabulary, or refuse it.

    Aliases are accepted (``--role mod`` stores ``leader``); an unknown name
    is an error rather than a silent no-role, so a typo cannot hand a session
    a blank stance nobody notices.
    """
    if not sdef.role:
        return sdef
    roleset = mesh_roles.resolve()
    canon = roleset.canonical(sdef.role)
    if canon is None:
        known = ", ".join(sorted(roleset.roles))
        raise HarnessError(f"unknown role {sdef.role!r} (known: {known})")
    return replace(sdef, role=canon)


def _normalize_resume(sdef: SessionDef) -> SessionDef:
    """Validate the resume/fork pair against the raw args the caller passed."""
    if sdef.fork_session and sdef.resume is None:
        raise HarnessError(
            "--fork-session needs a conversation to fork: pick one to resume "
            "(claude's own flag is 'use with --resume or --continue')"
        )
    if sdef.resume is not None and _steers_conversation(sdef.args):
        raise HarnessError(
            "the extra args already steer the conversation "
            f"({' '.join(sdef.args)}) — drop them, or drop the resume choice"
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

    A definition that opens someone else's conversation (``resume``) does so
    on the *first* spawn only — from then on the conversation is this
    session's own (forked or not) and a restore reopens it by its pinned id,
    exactly like any other session.
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
            elif sdef.resume is not None:
                # Bare --resume opens claude's picker; with a target it opens
                # that conversation. --session-id rides along only for a fork,
                # to catch the copy claude mints at an id we can restore later
                # (without a fork the pinned id *is* the resumed one, and
                # passing it twice would be a conflict).
                argv.append("--resume")
                if sdef.resume:
                    argv.append(sdef.resume)
                if sdef.fork_session:
                    argv.append("--fork-session")
                    if sdef.conversation_id:
                        argv.extend(["--session-id", sdef.conversation_id])
            elif sdef.conversation_id:
                argv.extend(["--session-id", sdef.conversation_id])
        # Re-injected on every spawn, restores included: an appended system
        # prompt lives in the process, not the transcript, so a resumed
        # session would otherwise come back without its role.
        if sdef.role:
            role = mesh_roles.resolve().get(sdef.role)
            if role is not None:
                argv.extend(["--append-system-prompt", mesh_roles.system_prompt(role)])
        argv.extend(sdef.args)
    else:
        entry = harness_registry.get(sdef.harness)
        if entry is None:  # normalize() refuses these; belt and braces
            raise HarnessError(f"unknown harness {sdef.harness!r}")
        argv = [*entry.command, *entry.args, *sdef.args]
        env = os.environ.copy()
        env.update(entry.env)
    # The session's identity, tmux's ``$TMUX`` equivalent. Children (claude,
    # its MCP servers, `!` shells) inherit it — cflow keys its run state by
    # it, mapping each session 1:1 to its own workflow run.
    env["CLAUNCH_SESSION"] = sdef.name
    env.update(sdef.env)
    if sys.platform != "win32":
        env.setdefault("TERM", "xterm-256color")
    return argv, env, sdef.cwd
