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

import json
import logging
import os
import re
import sys
import uuid
from dataclasses import dataclass, field, replace
from typing import Dict, Iterable, List, Optional, Tuple

from .. import borrowing, harnesses as harness_registry
from .. import lineage, metering, pi_provider, profile as profile_mod, routing, runner
from .. import store, transcripts
from .. import config as launcher_config
from . import mesh_roles, paths, pty_backend

log = logging.getLogger("claunch.daemon.harness")

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

#: Injected on every claude spawn (``--settings``): a SessionStart hook that
#: fires on ``compact`` and ``clear`` — the two moments the transcript-carried
#: half of a session's briefing is lost while the process lives on — and whose
#: stdout claude reads back into context. That makes re-briefing deterministic
#: instead of resting on the agent remembering, post-loss, that it should ask.
#: The hook payload on stdin also carries the session id claude is now on, and
#: ``clear`` mints a new one; ``claunch rebrief`` posts it back so the pinned
#: conversation follows the switch (see :func:`cli_sessions._report_hook_conversation`).
#: One static command with no arguments: the hook process inherits
#: ``CLAUNCH_SESSION`` (exported below), which is all ``claunch rebrief``
#: needs to find its session, and a command that never varies survives every
#: shell claude may run hooks under. The composition itself is the daemon's
#: (see :mod:`rebrief`); this is only the trigger.
REBRIEF_HOOK_SETTINGS = {
    "hooks": {
        "SessionStart": [
            {
                "matcher": "compact|clear",
                "hooks": [{"type": "command", "command": "claunch rebrief"}],
            }
        ]
    }
}


class HarnessError(Exception):
    """Raised for unknown harnesses or invalid session definitions."""


@dataclass(frozen=True)
class SessionDef:
    """The persistent *definition* of a session (what to run, where, how)."""

    name: str
    harness: str = CLAUDE_HARNESS
    # Normalized definitions always persist an explicit ``:harness`` suffix.
    # This makes restores deterministic even if the YAML default changes.
    profile: Optional[str] = None
    cwd: str = ""
    args: Tuple[str, ...] = ()
    #: Model selected for this session.  It stays separate from free harness
    #: args so create/spawn forms can inherit and override it without parsing
    #: an arbitrary argv string.  Profile/provider env may still resolve the
    #: selected alias to a backend-specific model id.
    model: Optional[str] = None
    effort: Optional[str] = None
    #: Builtin tools enabled for this session, or ``None`` to take the
    #: profile's default (``harness_options.<harness>.tools``). An empty
    #: tuple is an explicit "none". Only harnesses that declare ``tools``
    #: accept it.
    tools: Optional[Tuple[str, ...]] = None
    env: Dict[str, str] = field(default_factory=dict)
    restore: bool = True
    cols: int = 120
    rows: int = 30
    #: The claude conversation UUID pinned at creation (``--session-id``), so a
    #: restore resumes *this session's own* conversation (``--resume <id>``) —
    #: never ``--continue``, which grabs whatever conversation in the same
    #: cwd+profile happens to be the most recent and can hijack another one.
    conversation_id: Optional[str] = None
    #: Compatibility record for sessions created before role became solely a
    #: mesh-membership property. New creation keeps this empty; role stance is
    #: delivered in the common opening/rebrief/session-reminder path.
    role: Optional[str] = None
    #: Which conversation to open instead of a fresh one. ``None`` = a new
    #: conversation; ``""`` = claude's interactive picker (bare ``--resume``);
    #: otherwise the conversation UUID (a session *name* is resolved to its
    #: conversation by :meth:`SessionManager.create`).
    resume: Optional[str] = None
    #: Resume into a *copy* (``--fork-session``): the resumed conversation is
    #: left untouched and this session gets its own from that point on.
    fork_session: bool = False
    #: The session that spawned this one, by name — the edge that makes the
    #: session list a tree (see :mod:`claude_launcher.spawn`). ``None`` for a
    #: session a human started, which is what a tree root *is* here.
    #:
    #: Stored as a name rather than resolved at creation because the parent
    #: outlives neither the daemon nor necessarily the child: a parent that
    #: exited leaves its children running and still recorded, and a name is
    #: the only reference that survives that. Consumers must treat a parent
    #: that no longer resolves as a root.
    parent: Optional[str] = None
    #: Who this session *is*, decided at creation and true for its whole life:
    #: its mesh handle, the run it drives. Appended to Claude's system prompt;
    #: role stance follows the harness-independent opening/reminder path.
    #:
    #: Only the unchanging half lives here. Which peers it can reach right now
    #: is deliberately absent: the member graph is rewired mid-session by
    #: connect/disconnect, and a frozen roster would have the agent addressing
    #: peers it cannot reach and reading the refusal as a bug. That half stays
    #: in the mesh briefing, which is re-derived every time it is sent.
    identity: Optional[str] = None
    #: Run with another profile's auth (``--borrow``): this session keeps its
    #: own profile's config dir, env and skills, but the token — and the
    #: backend it talks to — comes from the named profile. API-key harnesses
    #: borrow only the shared token through their declared ``token_env``.
    #: Applied on every spawn, restores included: the arrangement is the
    #: session's, not the first launch's.
    borrow: Optional[str] = None
    #: Launch with no OAuth token at all (``--null``): nothing is injected and
    #: any inherited ``CLAUDE_CODE_OAUTH_TOKEN`` is cleared, so claude starts
    #: unauthenticated (log in with /login). claude harness only.
    null_token: bool = False
    #: The opening task, kept as a *record*. The live copy went in exactly
    #: once, on the first spawn (:func:`build_command`'s ``opening``), and is
    #: never replayed — this field changes nothing about that. It exists so a
    #: re-briefing can restate what the session was asked to do after its
    #: context is compacted or cleared (see :mod:`rebrief`), which was the one
    #: piece of a session's setup that nothing could reconstruct.
    task: Optional[str] = None
    #: The beads issue this session is for, by id — the exact half of the
    #: session↔board link (see :mod:`daemon.beads`; the other half is who the
    #: board says is assigned). Set at creation, from an ``issue: <id>`` the
    #: request named or the issue the daemon minted from the task; carried on
    #: restores, restated by a re-briefing, and read by the exit sweep.
    issue: Optional[str] = None
    #: Who may end this session when its driving cflow run finishes. When set,
    #: the run-event clock still writes the durable ending record but skips
    #: the termination — a user asked for this session's context to stay
    #: (``claunch keep-alive <name>``), and an automatic kill would drop it
    #: for the sake of a slot. Read live, right beside the kill, so a flag
    #: set while the end-sequence waits still protects the session.
    keep_alive: bool = False
    #: A person's standing pause for the session-level reminder service.
    #: Role and Cflow keep independent source clocks, but neither repeating
    #: reminder is delivered into this session while this flag is set.
    #: Persisted with the definition so a daemon restart or respawn does not
    #: silently undo a pause made from the terminal header.
    reminder_paused: bool = False
    #: Whether the observer's "pinned only" scope covers this session.
    #: The scope is a cost switch: while it is on, the loop observes only the
    #: sessions this flag names, so the API bill tracks what somebody asked to
    #: watch rather than the size of the fleet. It lives on the definition and
    #: not in the observer's own settings because two views read it — the
    #: observer card and the session rail — and the rail already polls the
    #: session list, so one field reaches both without a second read. Being a
    #: definition field it also survives a daemon restart, which is the whole
    #: point of a standing selection. A respawn constructs a fresh definition
    #: and so clears it, like the other lifecycle markers here.
    observe_pin: bool = False
    #: Creation opt-in and the operator's feedback counts. Each input sent
    #: through the session line may carry one reward or penalty point; the
    #: two counts are independent, start at zero, and stop nothing — while
    #: the selection is recorded, the score source repeats.
    score_goal: bool = False
    user_reward: int = 0
    user_penalty: int = 0
    #: The session this one is a quick-fork of, by name (see
    #: :mod:`claude_launcher.daemon.handoff`). Written only by the quick-fork
    #: route, never by a plain spawn: it is what makes ``merge`` available on
    #: this session — the wrap-up goes back to that one — and the marker block
    #: at the top of the copied conversation names the same session. Kept as
    #: a name for the same reason ``parent`` is: the origin may exit first,
    #: and the record still has to say where this copy came from.
    quick_fork_of: Optional[str] = None
    #: A person's own free-text annotation on this session, written from the
    #: web UI and read back in the rail row, the session header and the detail
    #: panel. It belongs to the *user*, not to the session: nothing injects it
    #: into a prompt, an opening or a re-briefing, so it stays usable for
    #: "why am I keeping this terminal around" without the agent reading it as
    #: an instruction. Persisted with the definition, so it survives a daemon
    #: restart and a respawn with the rest of the session's record.
    note: Optional[str] = None
    #: The project this session is filed under (see :mod:`claude_launcher.
    #: projects`). ``None`` means the default project — the reading every
    #: record written before projects existed gets, so nothing on disk moves.
    #: Set at creation (``--project``, the form's project field, a spawn's
    #: ``project`` or, failing all of those, the parent's) and carried on
    #: restores like the rest of the definition.
    project: Optional[str] = None

    def to_dict(self) -> dict:
        out = {
            "name": self.name,
            "harness": self.harness,
            "profile": self.profile,
            "cwd": self.cwd,
            "args": list(self.args),
            "model": self.model,
            "effort": self.effort,
            "env": dict(self.env),
            "restore": self.restore,
            "cols": self.cols,
            "rows": self.rows,
            "conversation_id": self.conversation_id,
            "role": self.role,
            "resume": self.resume,
            "fork_session": self.fork_session,
            "parent": self.parent,
            "identity": self.identity,
            "borrow": self.borrow,
            "null_token": self.null_token,
            "task": self.task,
            "issue": self.issue,
            "keep_alive": self.keep_alive,
        }
        # Same rule as the note and the reminder pause below: a session that
        # was never pinned writes no key, so the common record on disk is
        # unchanged by this field.
        if self.observe_pin:
            out["observe_pin"] = True
        # Keep old session records stable in the common enabled case.  The
        # field exists on disk only when it carries information.
        if self.reminder_paused:
            out["reminder_paused"] = True
        if self.score_goal:
            out["score_goal"] = True
            out["user_reward"] = self.user_reward
            out["user_penalty"] = self.user_penalty
        if self.tools is not None:
            out["tools"] = list(self.tools)
        if self.quick_fork_of:
            out["quick_fork_of"] = self.quick_fork_of
        # Same rule as the two blocks above: a session with no note writes no
        # key, so the common record on disk is unchanged by this field.
        if self.note:
            out["note"] = self.note
        # Absent for the default project, so a record that never named one
        # is byte-for-byte what it was — and reads as the default either way.
        if self.project:
            out["project"] = self.project
        return out

    @classmethod
    def from_dict(cls, data: dict) -> "SessionDef":
        from . import score_goal

        return cls(
            name=str(data["name"]),
            harness=str(data.get("harness") or CLAUDE_HARNESS),
            profile=data.get("profile") or None,
            cwd=str(data.get("cwd") or ""),
            args=tuple(str(a) for a in data.get("args") or ()),
            model=str(data.get("model") or "").strip() or None,
            effort=str(data.get("effort") or "").strip() or None,
            tools=_tools_field(data.get("tools")) if "tools" in data else None,
            env={str(k): str(v) for k, v in (data.get("env") or {}).items()},
            restore=bool(data.get("restore", True)),
            cols=int(data.get("cols") or 120),
            rows=int(data.get("rows") or 30),
            conversation_id=data.get("conversation_id") or None,
            role=str(data.get("role") or "").strip() or None,
            resume=_resume_field(data.get("resume")),
            fork_session=bool(data.get("fork_session")),
            parent=str(data.get("parent") or "").strip() or None,
            identity=str(data.get("identity") or "").strip() or None,
            borrow=str(data.get("borrow") or "").strip() or None,
            null_token=bool(data.get("null_token")),
            task=str(data.get("task") or "").strip() or None,
            issue=str(data.get("issue") or "").strip() or None,
            keep_alive=bool(data.get("keep_alive")),
            reminder_paused=bool(data.get("reminder_paused")),
            observe_pin=bool(data.get("observe_pin")),
            score_goal=score_goal.enabled(data.get("score_goal", False)),
            # A record written before the split carries one ``user_score``;
            # it is discarded here, not carried into either count.
            user_reward=score_goal.count(data.get("user_reward", 0)),
            user_penalty=score_goal.count(data.get("user_penalty", 0)),
            quick_fork_of=str(data.get("quick_fork_of") or "").strip() or None,
            note=str(data.get("note") or "").strip() or None,
            project=str(data.get("project") or "").strip() or None,
        )


def _tools_field(value) -> Tuple[str, ...]:
    """``tools`` as sent by a form or the CLI: a list, or a comma string.

    ``None``/``""``/``[]`` all mean "no tools" here -- the *absence* of the
    key is what means "profile default" (see :meth:`SessionDef.from_dict`).
    """
    if value is None:
        return ()
    if isinstance(value, str):
        if value.strip().lower() in ("", "none", "off"):
            return ()
        items = [part.strip() for part in value.split(",")]
    elif isinstance(value, (list, tuple)):
        items = [str(item).strip() for item in value]
    else:
        raise ValueError("tools must be a list of tool names")
    out = []
    for item in items:
        if item and item not in out:
            out.append(item)
    return tuple(out)


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
CONVERSATION_FLAGS = ("--continue", "-c", "--resume", "-r", "--session-id")

#: pi's own conversation switches. Any of these in the extra args means the
#: caller chose the conversation, so the daemon must not pin one of its own.
PI_HARNESS = "pi"
PI_CONVERSATION_FLAGS = (
    "--continue", "-c", "--resume", "-r", "--session", "--fork",
    "--session-dir", "--no-session",
)


def steers_pi_conversation(args: Iterable[str]) -> bool:
    return any(
        a in PI_CONVERSATION_FLAGS
        or a.startswith(("--session=", "--fork=", "--session-dir="))
        for a in args
    )


def pi_session_file(pi_home: str, cwd: str, conversation_id: str) -> str:
    """Where a pinned pi conversation lives: pi's own per-cwd session
    directory (``<home>/sessions/<encoded cwd>/``, encoded the way pi's
    ``getDefaultSessionDir`` does it) and a file named by the pinned id.

    Given to ``pi --session <path>`` on every launch: pi starts a fresh
    conversation AT that path when the file is missing and reopens it when
    it exists, which is exactly claude's ``--session-id`` / ``--resume``
    pair in one flag. A restore therefore reopens this session's own
    conversation and never the cwd's newest one (``--continue``), which in
    a directory with several pi sessions belongs to somebody else.
    """
    safe = "--" + re.sub(r"[/\\:]", "-", re.sub(r"^[/\\]", "", cwd)) + "--"
    return os.path.join(pi_home, "sessions", safe, f"{conversation_id}.jsonl")


def steers_model(args: Iterable[str]) -> bool:
    """Whether free harness args already select a model.

    Creation surfaces own the explicit ``model`` field.  Refusing a second
    model in ``args`` keeps the saved definition and concrete command from
    carrying competing choices whose winner depends on harness parsing.
    """
    return any(
        value in ("--model", "-m") or value.startswith("--model=")
        for value in args
    )


def alias_for_model_id(entry, model_id: str) -> Optional[str]:
    """Read a backend model id back into one of the harness's ``models``.

    The two directions are not symmetric. A launch hands the harness an alias
    (``opus``, ``luna``), but what comes back in a transcript is the concrete
    id the request was answered with (``claude-opus-5``), and one alias covers
    several ids over its life (``claude-opus-5``, ``claude-opus-4-7``). So the
    ``model_aliases`` table -- one id per alias -- answers this direction for
    the ids it happens to name and nothing else.

    Hence two steps, in order:

    1. the inverse of ``model_aliases``, which is exact and wins;
    2. ``<model_id_prefix>-<alias>`` as a prefix of the id, the alias then
       being followed either by nothing or by a ``-`` (so ``opus`` claims
       ``claude-opus-5`` but not a hypothetical ``claude-opusx-1``).

    Returns ``None`` when neither step answers. That is deliberate: the caller
    writes this into the session definition, and a guessed alias would relaunch
    the session on a model nobody chose. Not answering leaves the definition as
    it was, which is the behaviour that existed before this function.

    Longest alias first, so a harness declaring both ``opus`` and ``opus-mini``
    does not have the shorter one swallow the longer one's ids.
    """
    wanted = str(model_id or "").strip()
    if not wanted:
        return None
    models = list(getattr(entry, "models", None) or ())
    if not models:
        return None
    for alias, mapped in (getattr(entry, "model_aliases", None) or {}).items():
        if mapped == wanted and alias in models:
            return alias
    prefix = str(getattr(entry, "model_id_prefix", "") or "").strip()
    if not prefix:
        return None
    for alias in sorted(models, key=len, reverse=True):
        stem = f"{prefix}-{alias}"
        if wanted == stem or wanted.startswith(stem + "-"):
            return alias
    return None


def steers_conversation(args: Iterable[str]) -> bool:
    """Whether these args open an *existing* conversation rather than a new one.

    Public because two questions turn on it and they must not answer it from
    two different lists. The daemon's is "may I pin an id of my own"; the
    CLI's is "may this launch move to a fresh directory" -- claude keeps
    transcripts per working directory, so a conversation resumed in a checkout
    that has never been worked in resolves to nothing at all.
    """
    return any(
        a in CONVERSATION_FLAGS or a.startswith(("--resume=", "--session-id="))
        for a in args
    )


def without_restore_subcommand(args: Iterable[str], subcommand: str) -> List[str]:
    """``args`` with a harness's conversation *subcommand* and its tail cut off.

    The positional counterpart of :func:`steers_conversation`. Claude steers a
    conversation with flags, which :data:`CONVERSATION_FLAGS` can recognise one
    token at a time; codex steers it with a subcommand instead --
    ``codex resume [SESSION_ID] [PROMPT]`` -- so a definition created to open an
    existing conversation carries a bare ``resume`` in its ``args``, which no
    flag list matches.

    The restore then appends its own ``resume <id>`` after it and the command
    line reads ``resume resume <id>``. Codex takes the first token as the
    subcommand and the literal string ``"resume"`` as the SESSION_ID (its help:
    "Session id (UUID) or session name. UUIDs take precedence if it parses"), so
    it hunts for a *session named* ``resume``, finds nothing, and spends the real
    id as the prompt. Measured on this machine 2026-09-11: four of six codex
    definitions built that argv on restore (sessions ``s507``, ``s513``,
    ``s514``, ``s516``), and ``s507`` is the case the user reported.

    Everything after the subcommand goes with it, because it *is* the
    subcommand's argument list: a SESSION_ID the first spawn named fills the
    same slot the restore is about to fill, and leaving it behind moves the
    duplication one token along instead of removing it. The restore owns which
    conversation reopens -- that is the whole point of pinning the id.
    """
    args = list(args)
    try:
        cut = args.index(subcommand)
    except ValueError:
        return args
    return args[:cut]


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
    # A profile is now the source of the harness. Keep the resolved value in
    # the session record as a useful snapshot/display field, but user-facing
    # creation never accepts it as an independent choice. Profile-less custom
    # definitions remain supported for the Python embedding API and old saved
    # records; the HTTP and CLI creation doors require a profile.
    prof = None
    if sdef.profile:
        prof = profile_mod.require_selector(sdef.profile)
        try:
            selected = lineage.effective_harness(prof)
        except lineage.LineageError as exc:
            raise HarnessError(str(exc)) from exc
        # Persist one canonical execution selector even when the caller used
        # a bare profile. The default is resolved and policy-checked above;
        # pinning that answer keeps restore deterministic if the profile's
        # default harness changes later. The base profile still owns storage.
        canonical_profile = f"{prof.name}:{selected}"
        sdef = replace(
            sdef, profile=canonical_profile, harness=selected
        )
    entry = harness_registry.get(sdef.harness)
    if entry is None:
        known = ", ".join(harness_registry.names())
        raise HarnessError(
            f"unknown harness {sdef.harness!r} (known: {known}); "
            f"declare it under 'harnesses:' in {store.path()}"
        )
    if sdef.model:
        if not entry.models:
            raise HarnessError(
                f"harness {sdef.harness!r} does not declare selectable models"
            )
        if sdef.model not in entry.models:
            known = ", ".join(entry.models)
            raise HarnessError(
                f"unknown model {sdef.model!r} for harness {sdef.harness!r} "
                f"(known: {known})"
            )
        if steers_model(sdef.args):
            raise HarnessError(
                "the extra args already select a model; drop their --model/-m "
                "flag, or drop the session model choice"
            )
    if sdef.effort:
        if not entry.efforts:
            raise HarnessError(f"harness {sdef.harness!r} does not declare selectable efforts")
        if sdef.effort not in entry.efforts:
            raise HarnessError(
                f"unknown effort {sdef.effort!r} for harness {sdef.harness!r} "
                f"(known: {', '.join(entry.efforts)})"
            )
    if sdef.tools is not None:
        if not entry.tools:
            raise HarnessError(
                f"harness {sdef.harness!r} has no builtin tools to choose from"
            )
        unknown = [t for t in sdef.tools if t not in entry.tools]
        if unknown:
            raise HarnessError(
                f"unknown tool {', '.join(repr(t) for t in unknown)} for harness "
                f"{sdef.harness!r} (known: {', '.join(entry.tools)})"
            )
    if sdef.null_token and sdef.borrow:
        # Both answer the same question (whose credential) and the pair is
        # invalid before lender lookup: a typo or deleted lender must not hide
        # the contradictory request behind a different error.
        raise HarnessError(
            "--null launches without any OAuth token; "
            f"it cannot be combined with --borrow {sdef.borrow}"
        )
    if sdef.borrow:
        if prof is None:
            raise HarnessError("--borrow needs a profile that selects the harness")
        try:
            lender, _report = borrowing.require_allowed(
                prof, sdef.borrow, entry=entry
            )
        except borrowing.BorrowError as exc:
            raise HarnessError(str(exc)) from exc
        # Persist only the base profile identity. A lender's harness setting is
        # irrelevant and a qualified selector was rejected above.
        sdef = replace(sdef, borrow=lender.name)
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
            and not steers_conversation(sdef.args)
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
        if (
            sdef.harness == PI_HARNESS
            and not restoring
            and not sdef.conversation_id
            and not steers_pi_conversation(sdef.args)
        ):
            # Same contract as claude's pin above: the id is minted once, at
            # creation, and every relaunch reopens it (see pi_session_file).
            # An id invented while restoring an old definition would open an
            # empty conversation under a name that suggests otherwise.
            sdef = replace(
                sdef, conversation_id=sdef.resume or str(uuid.uuid4())
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
        # Conversation resumption and null-token launch are spelled in
        # claude's own flags. Role is absent here: it belongs to mesh
        # onboarding and every harness receives it through the common opening.
        extras = [
            what
            for what, given in (
                ("resume", sdef.resume is not None),
                ("fork_session", sdef.fork_session),
                ("borrow", sdef.borrow and not entry.borrowable),
                ("null", sdef.null_token),
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
    is an error rather than a silent fall to the free-role default, so a typo
    cannot hand a session a role it never asked for.
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
    if sdef.resume is not None and steers_conversation(sdef.args):
        raise HarnessError(
            "the extra args already steer the conversation "
            f"({' '.join(sdef.args)}) — drop them, or drop the resume choice"
        )
    return sdef


def takes_opening_argv(harness: str) -> bool:
    """Whether this harness accepts an opening message on its command line.

    Declared per harness because both Claude and Codex accept a positional
    prompt, while arbitrary configured agents may not. A message handed over
    as argv is read before the process ever reads a key, so it cannot be
    caught between the harness going quiet and its input actually being live.
    Harnesses declaring ``pty`` are typed into by :func:`onboard.open_with`.

    Declaring it is not the same as getting it: the command line has a
    ceiling, and an opening that would push it past that is left off the
    argv and typed in instead. :func:`carries_opening` reads the argv that was
    actually spawned to tell which happened.
    """
    entry = harness_registry.get(harness)
    return entry is not None and entry.opening_transport == "argv"


#: ``CreateProcessW``'s command-line ceiling, in UTF-16 code units including
#: the terminating NUL. One over and the spawn fails with
#: ``ERROR_FILENAME_EXCED_RANGE`` ("파일 이름이나 확장명이 너무 깁니다") —
#: the whole line counts, executable and flags and ``--append-system-prompt``
#: included, so the opening's budget is what those leave. Measured on this
#: machine 2026-09-11: a bare claude session took a 32,000-character task on
#: argv and refused a 32,768-character one.
WIN_CMDLINE_LIMIT = 32767
#: Linux's ``MAX_ARG_STRLEN``: the ceiling on one argv element, in bytes.
#: ``ARG_MAX`` (the whole line plus the environment) is megabytes and not
#: something an opening reaches; this per-element cap is.
UNIX_ARG_LIMIT = 131072
#: Kept back from either ceiling. pywinpty builds the line that reaches
#: ``CreateProcessW`` itself (its own quoting, a resolved executable path), so
#: the measurement here is an estimate, and an estimate that errs toward
#: typing the block in costs a few seconds; one that errs the other way costs
#: the session.
CMDLINE_MARGIN = 512


def _argv_fits(argv: List[str]) -> bool:
    """Whether the spawn layer will take ``argv`` at all."""
    if sys.platform == "win32":
        import subprocess  # local: the daemon's spawn goes through pty_backend

        line = subprocess.list2cmdline(argv)
        units = len(line.encode("utf-16-le")) // 2 + 1
        return units + CMDLINE_MARGIN <= WIN_CMDLINE_LIMIT
    return all(
        len(arg.encode("utf-8")) + 1 + CMDLINE_MARGIN <= UNIX_ARG_LIMIT
        for arg in argv
    )


def _append_opening(argv: List[str], opening: str, *, harness: str) -> None:
    """Put ``opening`` on ``argv`` as the positional prompt — unless the line
    would then be too long to spawn, in which case ``argv`` is left as it was
    and the block is :func:`onboard.open_with`'s to type in.

    Behind ``--`` because an opening block routinely starts with a line of
    dashes (the mesh briefing's own fence does), and an option parser reads
    that as a flag and refuses to start. Dated with the same stamp
    :meth:`Session.deliver` prefixes to every typed-in message: the argv
    handoff is the one delivery that skips deliver(), and it must not be the
    one delivery a transcript cannot date.
    """
    from . import session as session_mod  # late: session imports us

    candidate = argv + ["--", f"{session_mod.delivery_stamp()}\n{opening}"]
    if _argv_fits(candidate):
        argv[:] = candidate
        return
    log.info(
        "opening block for %s (%d chars) does not fit the command line; "
        "it will be typed in instead",
        harness,
        len(opening),
    )


def carries_opening(argv: List[str], opening: str) -> bool:
    """Whether a spawned ``argv`` took ``opening`` as its positional prompt.

    The question :func:`onboard.open_with` has to answer after the fact, from
    the argv the session actually started with: a harness that declares the
    argv transport still gets its block typed in when the block did not fit
    (:func:`_append_opening`).
    """
    return (
        bool(opening)
        and len(argv) >= 2
        and argv[-2] == "--"
        and argv[-1].endswith("\n" + opening)
    )


def restores_blank(sdef: SessionDef) -> bool:
    """Whether restoring ``sdef`` opens an *empty* conversation.

    True for exactly one branch of :func:`build_command`'s restore: a pinned
    conversation id with no transcript behind it, which is relaunched on
    ``--session-id`` because ``--resume`` of a jsonl claude never wrote is
    fatal. The session comes back alive and with nothing in it.

    That branch is a deliberate trade — an empty terminal beats one that exits
    on startup — but it is invisible from the outside, and two things downstream
    are wrong without it. The opening task is not replayed on a restore
    (:func:`build_command`'s ``opening``), so the session no longer knows what
    it was for; and the resume nudge's standing text says the conversation
    above is intact, which for this branch is false. Both callers need the same
    answer, so the rule is stated once, here, rather than re-derived from the
    argv or asked of the filesystem a second time.

    Measured window: a spawned claude writes its first transcript line 7-28
    seconds after the daemon records the session (37 sessions, this machine,
    2026-08-27), and a restart inside that window lands on this branch.

    False for everything else, including definitions this process cannot
    resolve (an unknown profile, an unreadable config): a restore that is going
    to fail is not a blank restore, and it fails loudly on its own.
    """
    if sdef.harness != CLAUDE_HARNESS:
        return False
    if steers_conversation(sdef.args) or not sdef.conversation_id:
        return False
    try:
        prof = profile_mod.require_selector(sdef.profile) if sdef.profile else None
    except Exception:  # noqa: BLE001 — ProfileError and anything below it
        return False
    if prof is None:
        return False
    return not transcripts.exists(prof.config_dir, sdef.conversation_id, sdef.cwd)


def codex_restores_blank(sdef: SessionDef) -> bool:
    """Whether restoring this *codex* definition opens an empty conversation.

    The counterpart of :func:`restores_blank`, and it has to be asked at a
    different moment. Claude's answer is about the transcript the previous
    daemon left behind, so it is read *before* the relaunch. Codex's is about
    whether the launch managed to resolve a conversation at all -- the args it
    names, then the newest rollout in its cwd that no other session holds
    (:meth:`SessionManager.launch`) -- so it is only true *after* that has run.
    Asking it too early says "blank" about a session that went on to resume
    fine, and the blank briefing then tells it the scrollback above is not its
    own when it is.

    True means :func:`build_command` appended no conversation at all, because
    reaching that branch means every rollout in this directory belongs to
    somebody else. The session comes back alive and with nothing in it, so it
    needs the same re-briefing a blank claude restore does.
    """
    return sdef.harness == "codex" and not sdef.conversation_id


def build_command(
    sdef: SessionDef, *, restoring: bool = False, opening: str = ""
) -> Tuple[List[str], Dict[str, str], str]:
    """Resolve a definition to the ``(argv, env, cwd)`` to spawn.

    ``opening`` is a first user message to hand the harness directly, for the
    harnesses that take one (see :func:`takes_opening_argv`). It is deliberately
    *not* a :class:`SessionDef` field: it is true once, at the first spawn, and
    a restore that replayed it would send the session's opening instruction a
    second time into a conversation that already contains it.

    A fresh claude session is started with ``--session-id <uuid>`` (the id
    pinned in the definition); ``restoring`` relaunches it with ``--resume``
    of that same id, so a restore always reopens *this session's own*
    conversation. Definitions predating the pin fall back to ``--continue``
    (most recent conversation for that cwd + profile).

    A pinned id with no transcript behind it is the one exception. A session
    spawned in the last seconds before a restart has not written its
    conversation yet, and ``--resume`` of a jsonl that is not there kills
    claude on startup -- the session is restored into the record and then
    exits 1, looking healthy in ``sessions.json`` and being gone in fact. It
    is relaunched on ``--session-id`` instead: the same pinned id, a new
    conversation, which is all the first spawn would have given it anyway.

    A definition that opens someone else's conversation (``resume``) does so
    on the *first* spawn only — from then on the conversation is this
    session's own (forked or not) and a restore reopens it by its pinned id,
    exactly like any other session.
    """
    prof = profile_mod.require_selector(sdef.profile) if sdef.profile else None
    # The daemon's own environment, minus the two families of answer a
    # parent leaves behind that are wrong for a PTY child: the nested-session
    # markers above, and the "do not colour your output" ones the daemon
    # inherits when it is started from an agent's tool shell (see
    # pty_backend.strip_inherited_color_answers).
    base = pty_backend.strip_inherited_color_answers({
        k: v for k, v in os.environ.items() if k not in _NESTED_SESSION_MARKERS
    })
    entry = harness_registry.get(sdef.harness)
    borrow_prof = None
    if sdef.borrow:
        if prof is None or entry is None:
            raise HarnessError("--borrow needs a declared profile harness")
        try:
            borrow_prof, _report = borrowing.require_allowed(
                prof, sdef.borrow, entry=entry
            )
        except borrowing.BorrowError as exc:
            raise HarnessError(str(exc)) from exc
    if sdef.harness == CLAUDE_HARNESS:
        assert prof is not None
        # Resolved at spawn time like the profile itself, so a lender deleted
        # between restarts fails the restore loudly instead of silently
        # falling back to the session's own token.
        env = runner.child_env(
            prof, with_token=True, base_env=base,
            borrow=borrow_prof, null_token=sdef.null_token,
        )
        argv = [launcher_config.claude_bin()]
        # The rebrief hook, on every spawn and restore for the same reason the
        # system prompt is: it lives in the process, not the transcript. First
        # among the flags because a bare --resume must stay last (claude reads
        # a trailing bare flag as "open the picker"). Withheld when the caller
        # steers settings itself — two --settings on one command line leaves
        # claude to pick, and the caller's must win.
        if "--settings" not in sdef.args:
            argv.extend(["--settings", json.dumps(REBRIEF_HOOK_SETTINGS)])
        if not steers_conversation(sdef.args):
            if restoring:
                if not sdef.conversation_id:
                    argv.append("--continue")
                elif not restores_blank(sdef):
                    argv.extend(["--resume", sdef.conversation_id])
                else:
                    # Pinned, but claude never wrote the conversation: the
                    # session was born too close to the restart for its first
                    # turn to land. --resume of a jsonl that is not there is
                    # fatal ("No conversation found with session ID"), and a
                    # session that dies on restore is worse than one that
                    # comes back empty -- so come back empty, on the same
                    # pinned id, which is what a first spawn does anyway.
                    # What "empty" then costs is paid on the way up: the same
                    # predicate puts this session on the resume nudge's blank
                    # list, which re-states the task this restore does not
                    # replay (see resume.blank_block).
                    log.info(
                        "session %r has no transcript for %s yet; restoring it "
                        "as a fresh conversation on that id rather than "
                        "resuming one claude never wrote",
                        sdef.name, sdef.conversation_id,
                    )
                    argv.extend(["--session-id", sdef.conversation_id])
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
        # Identity still uses Claude's persistent channel. Role stance has
        # moved to the harness-independent opening/rebrief/reminder path.
        if sdef.identity:
            argv.extend(["--append-system-prompt", sdef.identity])
        if sdef.model:
            argv.append(f"--model={sdef.model}")
        argv.extend(sdef.args)
        if opening and not restoring:
            # The positional prompt — claude's first turn; see _append_opening
            # for the stamp, the ``--`` and the length ceiling.
            _append_opening(argv, opening, harness=sdef.harness)
    else:
        entry = harness_registry.get(sdef.harness)
        if entry is None:  # normalize() refuses these; belt and braces
            raise HarnessError(f"unknown harness {sdef.harness!r}")
        managed_groups = (
            entry.mode_conflict_args,
            entry.skip_permissions_args,
            entry.full_access_args,
            entry.full_access_off_args,
        )
        manages_mode = any(
            group and any(
                tuple(sdef.args[i:i + len(group)]) == tuple(group)
                for i in range(len(sdef.args) - len(group) + 1)
            )
            for group in managed_groups
        )
        base_args = [
            arg for arg in entry.args
            if not (manages_mode and arg in entry.mode_conflict_args)
        ]
        model_id = entry.model_aliases.get(sdef.model, sdef.model) or ""

        def selection_args(template, value):
            if not value:
                return []
            return [str(arg).replace("{model}", model_id)
                    .replace("{effort}", str(sdef.effort or "")) for arg in template]
        model_args = selection_args(entry.model_args, sdef.model)
        if not model_args and sdef.model:
            model_args = [f"--model={model_id}"]
        effort_args = selection_args(entry.effort_args, sdef.effort)
        session_args = list(sdef.args)
        if restoring and entry.restore_args:
            # The first spawn's own conversation subcommand, dropped so the
            # restore below is the only thing that names a conversation. See
            # :func:`without_restore_subcommand` for what the duplicate did.
            session_args = without_restore_subcommand(
                session_args, entry.restore_args[0]
            )
        runtime_args = [*base_args, *model_args, *effort_args, *session_args]
        if prof is not None:
            try:
                runtime_args = runner.harness_launch_args(
                    prof, entry, runtime_args, tools=sdef.tools
                )
            except runner.RunnerError as exc:
                raise HarnessError(str(exc)) from exc
        argv = [*entry.launch_command(), *runtime_args]
        if restoring:
            if sdef.harness == "codex" and sdef.conversation_id:
                argv.extend(["resume", sdef.conversation_id])
            elif sdef.harness == "codex":
                # Nothing pinned, and codex's declared restore args select a
                # conversation by *cwd* (``resume --last``; its own help says
                # ``--all`` "disables cwd filtering"). A cwd is not an identity:
                # six codex sessions stood in F:/works/gds6 on 2026-09-11 over a
                # single rollout. And by the time this branch is reached, the
                # manager has already tried every way this session could own a
                # conversation in that directory -- the id its args name, then
                # the newest rollout no other session holds
                # (:meth:`SessionManager.launch`). So there is nothing left for
                # ``--last`` to find that is this session's: it opens somebody
                # else's, two sessions then append to one transcript, it grows
                # into two divergent histories, and whichever the user opens
                # later reads as the session having lost work.
                #
                # So come back empty -- the same trade the claude branch above
                # makes ("a session that dies on restore is worse than one that
                # comes back empty"), and paid for the same way:
                # :func:`codex_restores_blank` puts this session on the resume
                # nudge's blank list, which re-states the task an empty restore
                # does not replay.
                log.info(
                    "session %r has no codex conversation of its own to reopen; "
                    "restoring it as a new conversation rather than opening "
                    "whichever one was written last in %s",
                    sdef.name, sdef.cwd,
                )
            else:
                argv.extend(entry.restore_args)
        if prof is None:
            # Legacy restored definitions may lack a profile. Preserve their
            # old plain-command environment until they are recreated.
            env = base
            env.update(entry.env)
        else:
            try:
                env = runner.harness_child_env(
                    prof, entry, base_env=base, borrow=borrow_prof
                )
            except runner.RunnerError as exc:
                raise HarnessError(str(exc)) from exc
        if (
            sdef.harness == PI_HARNESS
            and sdef.conversation_id
            and entry.home_env
            and env.get(entry.home_env)
            and not steers_pi_conversation(sdef.args)
        ):
            argv.extend([
                "--session",
                pi_session_file(
                    env[entry.home_env],
                    os.path.abspath(sdef.cwd or os.getcwd()),
                    sdef.conversation_id,
                ),
            ])
        if opening and not restoring and entry.opening_transport == "argv":
            # Declared positional-prompt transport, same handoff as Claude's
            # builtin path (and the same length ceiling).
            _append_opening(argv, opening, harness=sdef.harness)
    # The session's identity, tmux's ``$TMUX`` equivalent. Children (claude,
    # its MCP servers, `!` shells) inherit it — cflow keys its run state by
    # it, mapping each session 1:1 to its own workflow run.
    env["CLAUNCH_SESSION"] = sdef.name
    # Where this session writes intermediate files, so that two sessions
    # picking the same file name do not overwrite each other. ``/tmp`` is one
    # machine-wide directory here and cannot be moved per session (MSYS mounts
    # it fixed, so TMP/TEMP do not shift it), which is why the fix is a path
    # of our own rather than a redirect. Assembling the env is pure -- the
    # directory itself is created by ``Session.__init__``, next to the log.
    env["CLAUNCH_SCRATCH"] = str(paths.session_scratch_dir(sdef.name))
    if sdef.harness == CLAUDE_HARNESS and routing.is_shim_url(
        env.get("ANTHROPIC_BASE_URL")
    ):
        # The shim serving this provider is shared by every session on it;
        # the header is how a record gets this session's name (see
        # ``metering``). Only meaningful when the base URL is our shim, and
        # only claude reads ``ANTHROPIC_CUSTOM_HEADERS``.
        metering.apply_session_header(env, sdef.name)
    env.update(sdef.env)
    if prof is not None and entry is not None:
        try:
            runner.finalize_harness_env(
                prof, entry, env, borrow=borrow_prof, tools=sdef.tools
            )
        except runner.RunnerError as exc:
            raise HarnessError(str(exc)) from exc
    if routing.is_shim_url(env.get(pi_provider.ENV_BASE_URL)):
        # Pi's counterpart of the ``ANTHROPIC_CUSTOM_HEADERS`` line above. It
        # has to come after ``finalize_harness_env``, which rebuilds the
        # whole ``CLAUNCH_PI_*`` projection (and decides whether the base URL
        # is the shim at all).
        pi_provider.apply_session_header(env, sdef.name)
    # Storage isolation is launcher-owned and cannot be escaped through
    # ``--env``. Claude stays at the historical root; each other supported
    # harness gets its own child directory.
    if prof is not None:
        if sdef.harness == CLAUDE_HARNESS:
            env[launcher_config.CLAUDE_CONFIG_DIR_ENV] = str(prof.config_dir)
        elif entry is not None and entry.home_env:
            env[entry.home_env] = str(entry.profile_home(prof.config_dir))
    if sys.platform != "win32":
        env.setdefault("TERM", "xterm-256color")
    return argv, env, sdef.cwd
