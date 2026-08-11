"""Per-mesh role sets: the vocabulary a mesh's handles resolve into.

A *role* is what a member IS on the mesh — its stance, and the handful of
behaviours the daemon keys off it. Until now the vocabulary was a six-entry
dict in :mod:`mesh` with no aliases, no meaning and no way to change it: a
handle like ``coder1`` fell through to ``"member"``, a value nothing acts on.

The vocabulary now ships as a **document**. The packaged default carries
interconnect's proven set — ``leader``/``operator``/``worker``/``reviewer``/
``specialist``, their aliases, and ``reviewer`` as the default so an
unlabelled member is never a rubber stamp — and a mesh may **override** it by
uploading its own YAML through the daemon.

Scope is the mesh, not the machine: a mesh is the unit a team actually is,
one machine hosts members of several meshes at once, and the authority owns
the document exactly as it owns ``policy`` (see :mod:`mesh_policy`), so every
daemon in a mesh resolves handles through the *same* vocabulary. That is also
why a role's stance text is **inline** rather than a path: the document
federates, and a file path means nothing on the machine it arrives at.

**Overriding is per role, not per field.** A role named in the upload
replaces that role's whole definition; roles left unmentioned keep the
packaged default; ``<name>: null`` deletes one; ``replace: true`` makes the
upload the entire vocabulary. Merging *within* a role was rejected on
purpose — a half-overridden stance (new aliases, old prose) reads as a bug,
and there is no sane way to "merge" two pieces of prose.

**Uploads are not retroactive.** A member's role is resolved once, at join,
and stored as a plain string; changing the vocabulary never rewrites it.
A member whose role no longer exists simply matches no rule — which needs no
code, and is the whole reason this stays simple.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import yaml

log = logging.getLogger("claunch.daemon.mesh")

#: Schema version of the role-set document. A document without one is read as
#: this version; an unknown one is refused rather than guessed at.
SCHEMA_VERSION = 1

#: Caps. A role set rides the federation sync, so it has to stay small enough
#: to cross a link without thought. The stance cap is generous — an injected
#: brief that needs more than this wants to be a skill, not a role.
MAX_STANCE = 8000
MAX_TASK_POLL = 500
MAX_ROLES = 32
MAX_DOC = 64_000

#: A role/alias name: lower-case word characters, dashes and dots. Deliberately
#: narrower than a handle — a role name appears in config, CLI output and the
#: web UI, and is matched case-insensitively against a handle's leading word.
_NAME_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789_-.")


class RoleError(Exception):
    """Raised for an invalid role-set document."""


@dataclass(frozen=True)
class Role:
    """One role: what a member of it is, and what the daemon does about it."""

    name: str
    #: Extra leading words that resolve to this role. The role's own name
    #: always resolves to it and is never listed here.
    aliases: List[str] = field(default_factory=list)
    #: Prose handed to a member of this role when it joins. Delivered as a
    #: file the briefing points at, never pasted whole — an injection costs
    #: the agent a turn (see ``MeshManager._brief``).
    stance: str = ""
    #: Body for this role's task-poll nudge. The mesh's own
    #: ``policy.task_poll.bodies`` still wins when it sets one.
    task_poll: str = ""
    #: Whether members of this role receive stall warnings about others.
    stall_watch: bool = False

    def to_dict(self) -> dict:
        """The document form, with empty/default fields omitted."""
        out: dict = {}
        if self.aliases:
            out["aliases"] = list(self.aliases)
        if self.stall_watch:
            out["stall_watch"] = True
        if self.task_poll:
            out["task_poll"] = self.task_poll
        if self.stance:
            out["stance"] = self.stance
        return out


@dataclass(frozen=True)
class RoleSet:
    """A resolved vocabulary: the roles, the default, and the alias index."""

    roles: Dict[str, Role]
    default: str
    #: alias/name -> role name. Built once at resolve time; ``infer`` is on the
    #: join path and must not rescan every role's aliases.
    index: Dict[str, str] = field(default_factory=dict)

    def get(self, name: str) -> Optional[Role]:
        return self.roles.get(str(name or "").strip().lower())

    def canonical(self, token: str) -> Optional[str]:
        """The role a bare word names, whether by role name or alias."""
        return self.index.get(str(token or "").strip().lower())

    def infer(self, handle: str) -> str:
        """The role a handle self-selects, falling back to the default.

        The handle's LEADING word picks the role — ``worker_1`` and ``coder2``
        are both workers — which is interconnect's convention and the one
        already documented for claunch handles.
        """
        head = _leading_word(handle)
        return self.canonical(head) or self.default

    def resolve(self, handle: str, role: str = "") -> str:
        """The role to store for a join: an explicit ``role`` wins, else infer.

        An explicit role is normalized through the aliases, so ``--role mod``
        stores ``leader``. An explicit role the vocabulary does not know is an
        ERROR, not a silent fallback: a typo must not quietly hand a member a
        stance nobody meant it to have.
        """
        given = str(role or "").strip().lower()
        if not given:
            return self.infer(handle)
        canon = self.canonical(given)
        if canon is None:
            known = ", ".join(sorted(self.roles))
            raise RoleError(f"unknown role {role!r} (known: {known})")
        return canon

    def stall_watchers(self) -> List[str]:
        """Role names whose members are told when someone looks stuck."""
        return sorted(n for n, r in self.roles.items() if r.stall_watch)

    def to_doc(self) -> dict:
        """The document this set would serialize to."""
        return {
            "version": SCHEMA_VERSION,
            "default": self.default,
            "roles": {n: self.roles[n].to_dict() for n in sorted(self.roles)},
        }


def _leading_word(handle: str) -> str:
    """A handle's leading word — ``worker_1``/``coder2`` -> ``worker``/``coder``.

    Splits on the separators and digits claunch handles use, matching the
    convention documented in ``docs/mesh-design.md``.
    """
    head = ""
    for ch in str(handle or "").lower():
        if ch in "._-" or ch.isdigit():
            break
        head += ch
    return head


# --------------------------------------------------------------------------- #
# the packaged default — interconnect's vocabulary, in claunch's terms
#
# Held as YAML rather than a dict literal so it reads exactly like the file a
# user uploads, and so the default is proven by the same parser every upload
# goes through. The stance prose is deliberately short: the mesh SKILL.md
# already teaches the mechanics (delivery is an injection, intents, batch
# sections), and a stance repeating them would only crowd them out.
# --------------------------------------------------------------------------- #
DEFAULT_YAML = """\
version: 1

# The role a handle gets when its leading word names none of the roles below.
# reviewer on purpose: an unlabelled member should audit, never rubber-stamp.
default: reviewer

roles:

  leader:
    aliases: [lead, moderator, mod, chair]
    stall_watch: true
    stance: |
      You set direction and OWN the decisions: scope, priority, tradeoffs,
      tie-breaks. Break a circling impasse with an explicit, recorded call.
      Fan work out as ONE batch send so each member reads only its own slice;
      broadcast only what genuinely binds everyone. Audit a consensus before
      certifying it — a peer caving to end the thread is not agreement.
      Escalate to your user only what they alone hold (value, priority, scope,
      authorization), batched with options and a recommendation.
      Do not do the members' production for them.

  operator:
    aliases: [op, liaison, relay]
    stance: |
      You relay between your user and the leader; you are not a producer.
      Carry your user's requirements, answers and authorizations to the leader
      faithfully — what they actually said, not your reading of it — and carry
      the leader's questions and status back. Address the leader for
      everything; when your requirements collide with another user's, surface
      both and let the leader reconcile them.
      Do not code, design, review or assign work.

  worker:
    aliases: [coder, dev, developer, engineer, builder, maker, programmer,
              implementer]
    task_poll: >-
      you are idle and caught up (no unread mesh mail). If you have no task in
      flight, ask the leader to assign one -- or leave the mesh if your work
      here is done. Do NOT reply to this notice.
    stance: |
      You are a PRODUCER: do the real work and ground every claim in code,
      docs or tests — never prose — citing the files you stand on. Own your
      slice, and raise a problem you spot in someone else's rather than
      deferring. Reply 1:1 to whoever asked; ack an assignment briefly, then
      report the real outcome when it is done. Escalate what you cannot decide
      (scope, priority, a true tie-break) to the leader with options.
      Never invent a missing requirement to keep moving.

  reviewer:
    aliases: [review, peer, critic, auditor, checker, qa]
    stance: |
      You are the independent ADVERSARY — the default role, so an unlabelled
      member audits rather than agrees. Pressure-test claims against the
      actual code, docs and tests, not the prose describing them, and demand
      justification. Reply to the author of the claim you challenge. Never own
      production you would then have to review. Record a real impasse and let
      the leader settle it.
      Do not rubber-stamp to close fast, and do not stack reviews: one
      reviewer per artifact, split by domain if there are several of you.

  specialist:
    aliases: [service, gatekeeper]
    stance: |
      You solely and serially own ONE resource and operate it for everyone
      else. Perform other members' requests against it rather than letting
      them touch it — that exclusivity is what stops concurrent use from
      corrupting it. Work requests in order and report each outcome back to
      whoever asked. You produce rather than audit, so escalate scope and
      priority calls to the leader like any other producer.
"""


def _check_name(name: str, what: str) -> str:
    canon = str(name or "").strip().lower()
    if not canon:
        raise RoleError(f"{what} must be a non-empty name")
    bad = sorted(set(canon) - _NAME_CHARS)
    if bad:
        raise RoleError(
            f"invalid {what} {name!r}: use lower-case letters, digits, "
            f"'.', '_' or '-' (offending: {' '.join(bad)})"
        )
    return canon


def _text(value, cap: int, what: str) -> str:
    if not isinstance(value, str):
        raise RoleError(f"{what} must be text, got {type(value).__name__}")
    text = value.strip()
    if len(text) > cap:
        raise RoleError(f"{what} is {len(text)} chars, over the {cap} limit")
    return text


def _parse_role(name: str, body) -> Role:
    if not isinstance(body, dict):
        raise RoleError(f"role {name!r} must be a mapping, got {body!r}")
    unknown = sorted(set(body) - {"aliases", "stance", "task_poll", "stall_watch"})
    if unknown:
        raise RoleError(
            f"role {name!r} has unknown key(s): {', '.join(unknown)} "
            "(allowed: aliases, stance, task_poll, stall_watch)"
        )
    raw_aliases = body.get("aliases") or []
    if not isinstance(raw_aliases, list):
        raise RoleError(f"role {name!r}: aliases must be a list")
    aliases = []
    for alias in raw_aliases:
        canon = _check_name(alias, f"alias in role {name!r}")
        # The role's own name always resolves to it; listing it as an alias is
        # harmless but would make the collision check below fire on itself.
        if canon != name and canon not in aliases:
            aliases.append(canon)
    return Role(
        name=name,
        aliases=aliases,
        stance=_text(body.get("stance") or "", MAX_STANCE, f"role {name!r} stance"),
        task_poll=" ".join(
            _text(
                body.get("task_poll") or "", MAX_TASK_POLL,
                f"role {name!r} task_poll",
            ).split()
        ),
        stall_watch=bool(body.get("stall_watch")),
    )


def parse(text) -> dict:
    """Validate a role-set document (YAML text or an already-parsed mapping).

    Returns the document as a plain dict, ready to persist and federate.
    Strict on purpose — this is the *upload* path, and a typo that silently
    changed nothing would be worse than a rejected upload.
    """
    if isinstance(text, str):
        if len(text) > MAX_DOC:
            raise RoleError(
                f"role set is {len(text)} bytes, over the {MAX_DOC} limit"
            )
        try:
            doc = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise RoleError(f"not valid YAML: {exc}") from None
    else:
        doc = text
    if doc is None:
        raise RoleError("role set is empty")
    if not isinstance(doc, dict):
        raise RoleError(f"role set must be a mapping, got {type(doc).__name__}")
    unknown = sorted(set(doc) - {"version", "default", "roles", "replace"})
    if unknown:
        raise RoleError(
            f"unknown top-level key(s): {', '.join(unknown)} "
            "(allowed: version, default, roles, replace)"
        )
    version = doc.get("version", SCHEMA_VERSION)
    try:
        version = int(version)
    except (TypeError, ValueError):
        raise RoleError(f"version must be a number, got {version!r}") from None
    if version != SCHEMA_VERSION:
        raise RoleError(
            f"unsupported role-set version {version} (this daemon speaks "
            f"version {SCHEMA_VERSION})"
        )
    roles = doc.get("roles")
    if roles is None:
        roles = {}
    if not isinstance(roles, dict):
        raise RoleError("'roles' must be a mapping of name -> definition")
    if len(roles) > MAX_ROLES:
        raise RoleError(f"{len(roles)} roles, over the {MAX_ROLES} limit")
    out_roles: dict = {}
    for raw_name, body in roles.items():
        name = _check_name(raw_name, "role name")
        if name in out_roles:
            raise RoleError(f"role {name!r} is defined twice")
        # A null body is the TOMBSTONE that deletes a packaged role. It is
        # only meaningful against the default set, so it is validated here but
        # resolved in `resolve`.
        out_roles[name] = None if body is None else _parse_role(name, body).to_dict()
    out: dict = {"version": SCHEMA_VERSION, "roles": out_roles}
    if doc.get("replace"):
        out["replace"] = True
    if doc.get("default") is not None:
        out["default"] = _check_name(doc.get("default"), "default role")
    return out


def resolve(override: Optional[dict] = None) -> RoleSet:
    """The packaged default with ``override`` applied — the vocabulary in force.

    Per-role replace (see the module docstring): a role in the override wins
    whole, ``None`` deletes, unmentioned roles survive. ``replace: true``
    drops the packaged set entirely.
    """
    base = parse(DEFAULT_YAML)
    # `override` may still be YAML text; parse before inspecting it, or the
    # replace-hint below would be read off a string.
    patch = parse(override) if override else None
    doc = base if patch is None else _merge(base, patch)
    roles: Dict[str, Role] = {}
    for name, body in (doc.get("roles") or {}).items():
        if body is None:
            continue
        roles[name] = _parse_role(name, body)
    if not roles:
        raise RoleError("a role set must define at least one role")
    # Aliases first: a collision is almost always the REAL mistake, and
    # checking the default before it would answer a confusing second-order
    # complaint ("default role '' is not defined") instead.
    index: Dict[str, str] = {}
    for name, role in sorted(roles.items()):
        for token in (name, *role.aliases):
            owner = index.get(token)
            if owner is not None and owner != name:
                extra = (
                    ""
                    if (patch or {}).get("replace")
                    # The common way in: a document meant as "here is my whole
                    # vocabulary" that merged onto the packaged one instead.
                    else " (an upload MERGES onto the packaged roles — add "
                         "'replace: true' if yours is meant to be the whole "
                         "vocabulary)"
                )
                raise RoleError(
                    f"{token!r} resolves to both {owner!r} and {name!r} — a "
                    f"role name or alias must name exactly one role{extra}"
                )
            index[token] = name
    default = doc.get("default") or ""
    if not default:
        raise RoleError(
            "a role set that replaces the packaged one must name its "
            f"'default:' role (roles: {', '.join(sorted(roles))})"
        )
    if default not in roles:
        raise RoleError(
            f"default role {default!r} is not defined (roles: "
            f"{', '.join(sorted(roles))})"
        )
    return RoleSet(roles=roles, default=default, index=index)


def _merge(base: dict, patch: dict) -> dict:
    """Apply a parsed override onto the parsed default set."""
    if patch.get("replace"):
        out = {"version": SCHEMA_VERSION, "roles": dict(patch.get("roles") or {})}
        if patch.get("default"):
            out["default"] = patch["default"]
        return out
    out = copy.deepcopy(base)
    for name, body in (patch.get("roles") or {}).items():
        if body is None:
            out["roles"].pop(name, None)  # tombstone
        else:
            out["roles"][name] = body     # whole-role replace
    if patch.get("default"):
        out["default"] = patch["default"]
    return out


def load_override(doc) -> Optional[dict]:
    """A persisted/synced override -> a usable one, or None.

    Lenient where :func:`parse` is strict: a document that arrives unreadable
    (an older daemon, a hand-edited ``mesh.json``) must not stop the mesh from
    loading — it falls back to the packaged vocabulary with a warning, exactly
    as :func:`mesh_policy.load_policy` does.
    """
    if not doc:
        return None
    try:
        parsed = parse(doc)
        resolve(parsed)  # prove it still resolves before we adopt it
        return parsed
    except RoleError as exc:
        log.warning("ignoring bad persisted mesh role set: %s", exc)
        return None


def to_yaml(doc: dict) -> str:
    """A role-set document as YAML the user can edit and upload back."""
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=88)


def system_prompt(role: Role) -> str:
    """The stance as a session's system-prompt injection.

    A *mesh* member reads its stance through a file the join briefing points
    at — it is already running, and an injection would cost it a turn. A
    session spawned WITH a role has no such turn to spend: it is told who it
    is before its first prompt, by appending this to claude's system prompt
    (``--append-system-prompt``, which adds to the built-in one rather than
    replacing it).
    """
    head = (
        f"Your role in this session is {role.name!r}. It was chosen when the "
        f"session was spawned and holds for the whole session."
    )
    return f"{head}\n\n{role.stance.rstrip()}" if role.stance else head
