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

**The document also carries the wiring** (``auto_link``): which pairs a join
should connect, stated as rules over role and tier rather than as edges. It
lives here rather than in a document of its own because the rules *name
roles*, and one document is the only place a rule pointing at a role somebody
just deleted can be caught — at parse time, before it silently matches
nothing. See :class:`AutoLink` for the semantics.
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
#: Auto-link rules are evaluated once per (joiner, existing member) pair, so
#: the cost is rules x members; the cap keeps a pathological document from
#: making a join quadratic in something the author did not think about.
MAX_RULES = 64
#: How deep a spawn chain may be before ``tier`` stops counting. Beyond this a
#: member is simply "deep" — no rule can name a tier this large anyway, and a
#: bound is what makes the walk safe against a hand-edited cycle.
MAX_TIER = 64

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
class LinkFacts:
    """What a rule may ask about one member, fixed at the moment of a join.

    ``tier`` is the member's depth in the mesh's spawn forest (0 for a root)
    and ``root`` the handle at the top of its tree — its own, for a root. Both
    are derived from the lineage the mesh already carries, and derived *once*:
    a rule is evaluated at join and the answer stored as an edge, so a parent
    that later exits (which makes its child a root) cannot silently re-tier a
    member that has already been wired.
    """

    role: str
    tier: int
    root: str


@dataclass(frozen=True)
class LinkPattern:
    """One end of a rule. An omitted field matches anything."""

    role: Optional[str] = None
    tier: Optional[int] = None

    def matches(self, facts: LinkFacts) -> bool:
        if self.role is not None and facts.role != self.role:
            return False
        if self.tier is not None and facts.tier != self.tier:
            return False
        return True

    def to_dict(self) -> dict:
        out: dict = {}
        if self.role is not None:
            out["role"] = self.role
        if self.tier is not None:
            out["tier"] = "root" if self.tier == 0 else self.tier
        return out


@dataclass(frozen=True)
class LinkRule:
    """An unordered pair pattern: which two kinds of member to connect.

    Unordered on purpose. A rule is a *predicate over a pair*, not an
    instruction to the joiner, so it gives the same answer whichever of the
    two members joined first — and that is what makes the wiring independent
    of the order a fleet happens to come up in.
    """

    a: LinkPattern
    b: LinkPattern
    #: ``"tree"`` restricts the rule to two members of the same spawn tree;
    #: ``"any"`` (the default) lets it reach across the whole mesh.
    within: str = "any"

    def matches(self, x: LinkFacts, y: LinkFacts) -> bool:
        if self.within == "tree" and (not x.root or x.root != y.root):
            return False
        return (self.a.matches(x) and self.b.matches(y)) or (
            self.a.matches(y) and self.b.matches(x)
        )

    def to_dict(self) -> dict:
        out: dict = {"between": [self.a.to_dict(), self.b.to_dict()]}
        if self.within != "any":
            out["within"] = self.within
        return out


@dataclass(frozen=True)
class AutoLink:
    """The wiring a join performs — the rules, and nothing else.

    Three things this is deliberately NOT:

    * **Not an ACL.** It decides the edges a join *creates*; it never vetoes
      one. An agent or a human may open or cut anything afterwards through
      the member graph, and that decision is stored as an edge, so it outlives
      any later edit of these rules.
    * **Not retroactive.** Rules run at join and the result is recorded. Change
      them and the members already wired keep the wiring they were given —
      exactly as a role, resolved once and stored, survives a new vocabulary.
    * **Not the parent edge.** A child is connected to its parent by the join
      itself, outside these rules. A child that cannot reach its parent cannot
      report, and the command its briefing hands it fails; that is not a
      property a YAML typo gets to remove.
    """

    rules: List[LinkRule] = field(default_factory=list)

    def decide(self, x: LinkFacts, y: LinkFacts) -> bool:
        """Should a join connect these two? Any matching rule is enough."""
        return any(rule.matches(x, y) for rule in self.rules)

    def to_dict(self) -> dict:
        return {"rules": [r.to_dict() for r in self.rules]}


@dataclass(frozen=True)
class RoleSet:
    """A resolved vocabulary: the roles, the default, and the alias index."""

    roles: Dict[str, Role]
    default: str
    #: The wiring rules that ride this vocabulary (see :class:`AutoLink`).
    auto_link: AutoLink = field(default_factory=AutoLink)
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
            "auto_link": self.auto_link.to_dict(),
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

# What a join wires up. A child is ALWAYS connected to its parent — that is
# the join, not a rule — so everything here is about the edges beyond it.
#
# The packaged set has exactly one rule, and it is the one that keeps a mesh
# usable: sessions a human started (tier 0, nobody spawned them) all reach
# each other, the way every member always has. Everything a session spawns
# hangs off its parent and goes no further, so a fleet is a tree until
# somebody says otherwise — an agent wiring its own workers together, or a
# rule added here.
auto_link:
  rules:
    - between: [{tier: root}, {tier: root}]

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
      deferring. Reply 1:1 to whoever asked, and report the real outcome of
      every assignment when it is done. Escalate what you cannot decide
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


def _parse_pattern(body, where: str) -> dict:
    """One end of a rule -> its document form. ``{}`` matches every member."""
    if body is None:
        body = {}
    if not isinstance(body, dict):
        raise RoleError(f"{where} must be a mapping like {{role: worker}}")
    unknown = sorted(set(body) - {"role", "tier"})
    if unknown:
        raise RoleError(
            f"{where} has unknown key(s): {', '.join(unknown)} "
            "(allowed: role, tier)"
        )
    out: dict = {}
    if body.get("role") is not None:
        # Left as written; it is checked against the *resolved* vocabulary in
        # `resolve`, which is the only place that knows the merged role names.
        out["role"] = _check_name(body["role"], f"role in {where}")
    tier = body.get("tier")
    if tier is not None:
        # 'root' rather than 0 is the spelling to reach for — the rules are
        # about position in the spawn forest, and 'root' says that where a
        # bare 0 makes the reader count.
        if isinstance(tier, str):
            if tier.strip().lower() != "root":
                raise RoleError(
                    f"{where}: tier must be a number or 'root', got {tier!r}"
                )
            out["tier"] = 0
        elif isinstance(tier, bool) or not isinstance(tier, int):
            raise RoleError(
                f"{where}: tier must be a number or 'root', got {tier!r}"
            )
        elif not (0 <= tier <= MAX_TIER):
            raise RoleError(f"{where}: tier {tier} out of range [0, {MAX_TIER}]")
        else:
            out["tier"] = tier
    return out


def _parse_rule(body, index: int) -> dict:
    where = f"auto_link rule {index + 1}"
    if not isinstance(body, dict):
        raise RoleError(
            f"{where} must be a mapping with a 'between:' pair, got {body!r}"
        )
    unknown = sorted(set(body) - {"between", "within"})
    if unknown:
        raise RoleError(
            f"{where} has unknown key(s): {', '.join(unknown)} "
            "(allowed: between, within)"
        )
    pair = body.get("between")
    if not isinstance(pair, list) or len(pair) != 2:
        raise RoleError(
            f"{where}: 'between' must be a list of exactly two patterns, "
            "e.g. between: [{role: worker}, {role: reviewer}]"
        )
    within = str(body.get("within") or "any").strip().lower()
    if within not in ("any", "tree"):
        raise RoleError(
            f"{where}: within must be 'any' or 'tree', got {body.get('within')!r}"
        )
    out: dict = {
        "between": [
            _parse_pattern(pair[0], f"{where} end 1"),
            _parse_pattern(pair[1], f"{where} end 2"),
        ]
    }
    if within != "any":
        out["within"] = within
    return out


def _parse_auto_link(body) -> dict:
    if not isinstance(body, dict):
        raise RoleError("'auto_link' must be a mapping with a 'rules:' list")
    unknown = sorted(set(body) - {"rules"})
    if unknown:
        raise RoleError(
            f"auto_link has unknown key(s): {', '.join(unknown)} (allowed: rules)"
        )
    rules = body.get("rules")
    if rules is None:
        rules = []
    if not isinstance(rules, list):
        raise RoleError("auto_link.rules must be a list")
    if len(rules) > MAX_RULES:
        raise RoleError(f"{len(rules)} auto_link rules, over the {MAX_RULES} limit")
    return {"rules": [_parse_rule(r, i) for i, r in enumerate(rules)]}


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
    unknown = sorted(
        set(doc) - {"version", "default", "roles", "replace", "auto_link"}
    )
    if unknown:
        raise RoleError(
            f"unknown top-level key(s): {', '.join(unknown)} "
            "(allowed: version, default, roles, replace, auto_link)"
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
    if doc.get("auto_link") is not None:
        out["auto_link"] = _parse_auto_link(doc.get("auto_link"))
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
    return RoleSet(
        roles=roles,
        default=default,
        auto_link=_resolve_auto_link(doc.get("auto_link"), index, roles),
        index=index,
    )


def _resolve_auto_link(doc, index: Dict[str, str], roles: Dict[str, Role]) -> AutoLink:
    """Rules -> the resolved predicate, with every role reference checked.

    This is the payoff for keeping the wiring in the same document as the
    vocabulary: a rule naming a role the upload just deleted is caught here,
    at parse time, instead of quietly matching nothing for the rest of the
    mesh's life. Role names go through the alias index too, so a rule may say
    ``coder`` and mean ``worker`` exactly as a handle does.
    """
    if not doc:
        return AutoLink()
    out = []
    for i, rule in enumerate(doc.get("rules") or []):
        ends = []
        for end in rule.get("between") or []:
            token = end.get("role")
            canon = None
            if token is not None:
                canon = index.get(token)
                if canon is None:
                    raise RoleError(
                        f"auto_link rule {i + 1} names role {token!r}, which "
                        f"this vocabulary does not define (roles: "
                        f"{', '.join(sorted(roles))})"
                    )
            ends.append(LinkPattern(role=canon, tier=end.get("tier")))
        out.append(
            LinkRule(a=ends[0], b=ends[1], within=str(rule.get("within") or "any"))
        )
    return AutoLink(rules=out)


def _merge(base: dict, patch: dict) -> dict:
    """Apply a parsed override onto the parsed default set.

    ``auto_link`` replaces wholesale where roles merge per-entry, because a
    rule list has no key to merge on: two rules are not "the same rule with
    new fields", and a set that half-arrived would wire a fleet in a shape
    nobody wrote down. Stating ``auto_link: {rules: []}`` is therefore how a
    mesh turns the packaged rule off and becomes a pure spawn tree.
    """
    if patch.get("replace"):
        # `replace` is about the VOCABULARY — it drops the packaged roles. The
        # wiring is a separate axis and survives, so a mesh that brings its own
        # role names does not also, silently, stop connecting its roots.
        out = {"version": SCHEMA_VERSION, "roles": dict(patch.get("roles") or {})}
        if patch.get("default"):
            out["default"] = patch["default"]
    else:
        out = copy.deepcopy(base)
        for name, body in (patch.get("roles") or {}).items():
            if body is None:
                out["roles"].pop(name, None)  # tombstone
            else:
                out["roles"][name] = body     # whole-role replace
        if patch.get("default"):
            out["default"] = patch["default"]
    link = patch.get("auto_link")
    if link is None:
        link = base.get("auto_link")
    if link is not None:
        out["auto_link"] = copy.deepcopy(link)
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
