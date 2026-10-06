"""Work spec: one YAML document that sets up a whole job — mesh, agents, issues.

A *work spec* describes a complex job the way gds6's issue-gen hands it over
(a frame epic, work tasks with per-task contracts, a dependency graph, open
decisions, the human's share), plus the part issue-gen does not produce: who
does it. The agent tree — a leader, optional sub-leads (nested workers running
a ``stack`` sub run), workers, reviewers, and workers carrying a subrole
prompt — the reference materials, and the cross-task contracts.

Importing a spec is meant to build all of it in one go. This module is the
front half of that importer and has no side effects:

* :func:`load` reads YAML text into a plain dict;
* :func:`validate` returns every problem it finds (empty list = valid);
* :func:`plan` resolves the import-time choice of leader (``own`` — spawn a
  new leader session for this spec's mesh — or ``shared:<session>`` — join an
  existing leader into it, so that session leads several meshes at once) and
  returns the ordered list of actions an executor would perform.

The format, the mapping onto claunch's mesh/spawn/beads/cflow, and the
conflicts a shared leader accepts are documented in ``docs/work-spec.md``.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

import yaml

from .daemon import mesh_roles

SPEC_VERSION = 1

#: agent kind -> (mesh role, default workflow). ``sub-lead`` is the "2nd
#: leader": a mesh allows one exclusive leader, so the second command line is
#: a worker that drives a ``stack`` sub run and collects its children.
KINDS: Dict[str, tuple] = {
    "sub-lead": ("worker", "improv-worker"),
    "worker": ("worker", "improv-worker"),
    "reviewer": ("reviewer", ""),
}
LEADER_ID = "leader"
LEADER_MODES = ("ask", "own", "shared")
MESH_MODES = ("new", "attach")
ISSUE_TYPES = ("epic", "task", "bug", "doc", "chore", "feature")
REF_KINDS = ("file", "url", "text", "issue")
CONTRACT_KINDS = ("seam", "event", "store", "interface", "policy")
#: gds6 ``WorkContract`` keys (issue-gen/src/impact.ts) — carried verbatim.
WORK_CONTRACT_KEYS = (
    "accept", "stores", "seams", "events", "gates", "allowlist", "done_when",
)
#: spawn.max_depth's default — the leader is depth 0.
MAX_DEPTH = 3
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")

_TOP = {
    "spec", "name", "title", "source", "workspace", "mesh", "leader",
    "roles", "subroles", "agents", "references", "contracts", "issues",
    "decisions", "human_steps",
}
_AGENT = {
    "id", "kind", "parent", "handle", "role", "subroles", "workflow",
    "profile", "model", "harness", "worktree", "task", "connect",
}
_ISSUE = {
    "key", "type", "title", "priority", "parent", "goal", "scope",
    "done_when", "blocked_by", "assignee", "review", "labels", "refs",
    "contracts", "contract", "rule_ids", "changes", "size",
}


class SpecError(Exception):
    """The document is not YAML, or not a mapping."""


def load(text: str) -> dict:
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SpecError(f"not valid YAML: {exc}") from None
    if not isinstance(doc, dict):
        raise SpecError("a work spec must be a mapping")
    return doc


def _list(doc: dict, key: str) -> list:
    value = doc.get(key)
    return value if isinstance(value, list) else []


def _unknown(errs: List[str], where: str, body: dict, allowed: set) -> None:
    extra = sorted(set(body) - allowed)
    if extra:
        errs.append(f"{where}: unknown key(s) {', '.join(extra)}")


def _cycle(edges: Dict[str, List[str]]) -> Optional[List[str]]:
    """A cycle in ``edges`` (node -> successors), or None."""
    state: Dict[str, int] = {}
    stack: List[str] = []

    def visit(node: str) -> Optional[List[str]]:
        state[node] = 1
        stack.append(node)
        for nxt in edges.get(node, ()):
            if state.get(nxt) == 1:
                return stack[stack.index(nxt):] + [nxt]
            if nxt not in state:
                found = visit(nxt)
                if found:
                    return found
        stack.pop()
        state[node] = 2
        return None

    for node in sorted(edges):
        if node not in state:
            found = visit(node)
            if found:
                return found
    return None


def _ids(errs: List[str], items: list, key: str, what: str, pattern) -> set:
    seen: set = set()
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            errs.append(f"{what}[{i}]: must be a mapping")
            continue
        value = str(item.get(key) or "")
        if not pattern.match(value):
            errs.append(f"{what}[{i}]: bad or missing {key} {value!r}")
        elif value in seen:
            errs.append(f"{what}: duplicate {key} {value!r}")
        seen.add(value)
    return seen


#: Fields whose value must be a mapping / a list of strings. Checked before
#: anything else reads them, so a generator's malformed output is reported
#: as an error line instead of crashing the validator (or, for a string where
#: a list belongs, being iterated letter by letter).
_TOP_MAPS = ("source", "mesh", "leader", "roles", "subroles")
_TOP_LISTS = ("agents", "references", "contracts", "issues", "decisions",
              "human_steps")
_STR_LISTS = {
    "agents": ("subroles", "connect"),
    "contracts": ("parties", "refs"),
    "issues": ("blocked_by", "refs", "contracts", "labels", "rule_ids"),
    "decisions": ("gates",),
}
#: Scalar fields the validator compares, hashes or formats. A list or mapping
#: there (``kind: [worker]``) is unhashable in a set lookup and crashed with
#: TypeError; it is reported here instead. ``priority`` alone is an integer
#: (bool excluded -- ``True`` is an int to Python, not a priority).
_STR_TOP = {"name": "name", "title": "title", "workspace": "workspace"}
_STR_NESTED = {
    "mesh": ("mode", "name"),
    "leader": ("mode",),
    "leader.own": ("name", "workflow"),
    "leader.shared": ("handle",),
}
_STR_ITEMS = {
    "agents": ("id", "kind", "parent", "role", "handle", "workflow"),
    "references": ("id", "kind", "file", "url", "text", "issue"),
    "contracts": ("id", "kind", "title", "text"),
    "issues": ("key", "type", "title", "parent", "assignee", "review",
               "goal", "size"),
    "decisions": ("key", "question", "owner", "recommended"),
}


def _scalars(doc: dict) -> List[str]:
    errs: List[str] = []
    ver = doc.get("spec")
    if ver is not None and (isinstance(ver, bool) or not isinstance(ver, int)):
        errs.append("spec: must be an integer")
    for key in _STR_TOP:
        if doc.get(key) is not None and not isinstance(doc[key], str):
            errs.append(f"{key}: must be a string")
    for path, fields in _STR_NESTED.items():
        node = doc
        for part in path.split("."):
            node = node.get(part) if isinstance(node, dict) else None
        if not isinstance(node, dict):
            continue
        for f in fields:
            if node.get(f) is not None and not isinstance(node[f], str):
                errs.append(f"{path}.{f}: must be a string")
    subroles = doc.get("subroles")
    for name, body in (subroles.items() if isinstance(subroles, dict) else ()):
        if isinstance(body, dict) and body.get("prompt") is not None \
                and not isinstance(body["prompt"], str):
            errs.append(f"subroles.{name}.prompt: must be a string")
    for block, fields in _STR_ITEMS.items():
        items = doc.get(block)
        if not isinstance(items, list):
            continue
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            for f in fields:
                if item.get(f) is not None and not isinstance(item[f], str):
                    errs.append(f"{block}[{i}].{f}: must be a string")
            if block == "issues":
                prio = item.get("priority")
                if prio is not None and (isinstance(prio, bool)
                                         or not isinstance(prio, int)):
                    errs.append(f"issues[{i}].priority: must be an integer")
    return errs


def _shape(doc: dict) -> List[str]:
    errs: List[str] = []
    for key in _TOP_MAPS:
        if doc.get(key) is not None and not isinstance(doc[key], dict):
            errs.append(f"{key}: must be a mapping")
    for key in _TOP_LISTS:
        if doc.get(key) is not None and not isinstance(doc[key], list):
            errs.append(f"{key}: must be a list")
    leader = doc.get("leader")
    if isinstance(leader, dict):
        for key in ("own", "shared"):
            if leader.get(key) is not None and not isinstance(leader[key], dict):
                errs.append(f"leader.{key}: must be a mapping")
        shared = leader.get("shared")
        if isinstance(shared, dict):
            cands = shared.get("candidates")
            if cands is not None and not (
                isinstance(cands, list) and all(isinstance(c, str) for c in cands)
            ):
                errs.append("leader.shared.candidates: must be a list of strings")
    for block, fields in _STR_LISTS.items():
        items = doc.get(block)
        if not isinstance(items, list):
            continue
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                continue  # reported by _ids with the block's own wording
            for f in fields:
                v = item.get(f)
                if v is not None and not (
                    isinstance(v, list) and all(isinstance(x, str) for x in v)
                ):
                    errs.append(f"{block}[{i}].{f}: must be a list of strings")
    for i, it in enumerate(doc.get("issues") if isinstance(doc.get("issues"), list) else []):
        if isinstance(it, dict) and it.get("contract") is not None \
                and not isinstance(it["contract"], dict):
            errs.append(f"issues[{i}].contract: must be a mapping")
    errs.extend(_scalars(doc))
    return errs


def validate(doc: dict) -> List[str]:
    """Every problem in ``doc``; an empty list means it can be planned."""
    if not isinstance(doc, dict):
        return ["spec: must be a mapping"]
    errs: List[str] = _shape(doc)
    if errs:
        return errs
    _unknown(errs, "spec", doc, _TOP)
    if doc.get("spec") != SPEC_VERSION:
        errs.append(f"spec: version must be {SPEC_VERSION}")
    if not _SLUG.match(str(doc.get("name") or "")):
        errs.append("name: lowercase slug [a-z0-9-] required")

    mesh = doc.get("mesh") or {}
    if mesh.get("mode", "new") not in MESH_MODES:
        errs.append(f"mesh.mode: one of {', '.join(MESH_MODES)}")
    if mesh.get("mode") == "attach" and not mesh.get("name"):
        errs.append("mesh.name: required when mode is attach")

    leader = doc.get("leader") or {}
    mode = leader.get("mode", "ask")
    if mode not in LEADER_MODES:
        errs.append(f"leader.mode: one of {', '.join(LEADER_MODES)}")
    if mesh.get("mode") == "attach" and mode == "own":
        errs.append(
            "leader.mode: own is impossible with mesh.mode attach — the "
            "existing mesh already has its leader"
        )

    # Roles: the spec's `roles:` is a mesh role-set upload, validated by the
    # same parser the daemon uses, so a spec cannot carry a vocabulary the
    # mesh would refuse.
    roleset = None
    try:
        roleset = mesh_roles.resolve(doc.get("roles") or None)
    except mesh_roles.RoleError as exc:
        errs.append(f"roles: {exc}")

    subroles = doc.get("subroles") or {}
    if not isinstance(subroles, dict):
        errs.append("subroles: must be a mapping name -> {prompt}")
        subroles = {}
    for name, body in subroles.items():
        if not isinstance(body, dict) or not str(body.get("prompt") or "").strip():
            errs.append(f"subroles.{name}: a prompt is required")
        # The import registers each subrole as a mesh role of that name (a
        # subrole must be a role the vocabulary knows), so it must not shadow
        # one that already means something else.
        if roleset is not None and roleset.canonical(str(name)) is not None:
            errs.append(f"subroles.{name}: collides with the mesh role "
                        f"{roleset.canonical(str(name))!r}")

    agents = _list(doc, "agents")
    agent_ids = _ids(errs, agents, "id", "agents", _SLUG)
    if LEADER_ID in agent_ids:
        errs.append(f"agents: {LEADER_ID!r} is reserved for the leader block")
    known_agents = agent_ids | {LEADER_ID}
    parents: Dict[str, str] = {}
    kind_of: Dict[str, str] = {LEADER_ID: "leader"}
    for a in agents:
        if not isinstance(a, dict):
            continue
        aid = str(a.get("id") or "")
        where = f"agents.{aid}"
        _unknown(errs, where, a, _AGENT)
        kind = a.get("kind", "worker")
        if kind not in KINDS:
            errs.append(f"{where}.kind: one of {', '.join(KINDS)}")
            continue
        kind_of[aid] = kind
        parent = str(a.get("parent") or LEADER_ID)
        if parent not in known_agents:
            errs.append(f"{where}.parent: unknown agent {parent!r}")
        parents[aid] = parent
        role = a.get("role") or KINDS[kind][0]
        if roleset is not None:
            try:
                canon = roleset.resolve(a.get("handle") or aid, role)
            except mesh_roles.RoleError as exc:
                errs.append(f"{where}.role: {exc}")
            else:
                if roleset.get(canon).exclusive:
                    errs.append(
                        f"{where}.role: {canon!r} is exclusive — the mesh's "
                        "one holder is the leader block; a second command "
                        "line is kind: sub-lead"
                    )
        for sr in a.get("subroles") or []:
            if sr not in subroles:
                errs.append(f"{where}.subroles: undefined subrole {sr!r}")
        for peer in a.get("connect") or []:
            if peer not in known_agents:
                errs.append(f"{where}.connect: unknown agent {peer!r}")
    for aid, parent in parents.items():
        if parent in kind_of and kind_of[parent] not in ("leader", "sub-lead"):
            errs.append(
                f"agents.{aid}.parent: {parent!r} is a {kind_of[parent]}; "
                "only the leader or a sub-lead holds children"
            )
    loop = _cycle({k: [v] for k, v in parents.items()})
    if loop:
        errs.append(f"agents: parent cycle {' -> '.join(loop)}")
    else:
        for aid in parents:
            depth, node = 0, aid
            while node != LEADER_ID and node in parents:
                node, depth = parents[node], depth + 1
            if depth > MAX_DEPTH:
                errs.append(
                    f"agents.{aid}: depth {depth} exceeds spawn.max_depth "
                    f"{MAX_DEPTH}"
                )

    refs = _list(doc, "references")
    ref_ids = _ids(errs, refs, "id", "references", _KEY)
    for r in refs:
        if isinstance(r, dict):
            kind = r.get("kind")
            if kind not in REF_KINDS:
                errs.append(f"references.{r.get('id')}.kind: one of "
                            f"{', '.join(REF_KINDS)}")
            elif not r.get(kind):
                errs.append(f"references.{r.get('id')}: {kind}: is required")

    contracts = _list(doc, "contracts")
    contract_ids = _ids(errs, contracts, "id", "contracts", _KEY)
    for c in contracts:
        if not isinstance(c, dict):
            continue
        where = f"contracts.{c.get('id')}"
        if c.get("kind") not in CONTRACT_KINDS:
            errs.append(f"{where}.kind: one of {', '.join(CONTRACT_KINDS)}")
        if not str(c.get("text") or "").strip():
            errs.append(f"{where}.text: required")
        for p in c.get("parties") or []:
            if p not in known_agents:
                errs.append(f"{where}.parties: unknown agent {p!r}")
        for r in c.get("refs") or []:
            if r not in ref_ids:
                errs.append(f"{where}.refs: unknown reference {r!r}")

    issues = _list(doc, "issues")
    keys = _ids(errs, issues, "key", "issues", _KEY)
    types: Dict[str, str] = {}
    blocks: Dict[str, List[str]] = {}
    for it in issues:
        if not isinstance(it, dict):
            continue
        key = str(it.get("key") or "")
        where = f"issues.{key}"
        _unknown(errs, where, it, _ISSUE)
        typ = it.get("type", "task")
        types[key] = typ
        if typ not in ISSUE_TYPES:
            errs.append(f"{where}.type: one of {', '.join(ISSUE_TYPES)}")
        if not str(it.get("title") or "").strip():
            errs.append(f"{where}.title: required")
        prio = it.get("priority", 2)
        if not isinstance(prio, int) or not 0 <= prio <= 4:
            errs.append(f"{where}.priority: integer 0-4")
        blocks[key] = [str(b) for b in it.get("blocked_by") or []]
        for b in blocks[key]:
            if b not in keys:
                errs.append(f"{where}.blocked_by: unknown issue {b!r}")
        assignee = it.get("assignee")
        if assignee not in (None, "", "human") and assignee not in known_agents:
            errs.append(f"{where}.assignee: unknown agent {assignee!r}")
        if typ == "doc" and assignee not in (None, ""):
            errs.append(f"{where}.assignee: a doc is not assigned")
        if (typ == "epic" and assignee not in (None, "")
                and kind_of.get(assignee) != "sub-lead"):
            errs.append(f"{where}.assignee: an epic is held only by a "
                        "sub-lead (its stack collects the children)")
        review = it.get("review")
        if review is not None:
            if review not in agent_ids:
                errs.append(f"{where}.review: unknown agent {review!r}")
            elif kind_of.get(review) != "reviewer":
                errs.append(f"{where}.review: {review!r} is not a reviewer")
        for r in it.get("refs") or []:
            if r not in ref_ids:
                errs.append(f"{where}.refs: unknown reference {r!r}")
        for c in it.get("contracts") or []:
            if c not in contract_ids:
                errs.append(f"{where}.contracts: unknown contract {c!r}")
        wc = it.get("contract")
        if wc is not None:
            _unknown(errs, f"{where}.contract", wc, set(WORK_CONTRACT_KEYS))
    for it in issues:
        if isinstance(it, dict) and it.get("parent") is not None:
            parent = str(it["parent"])
            if types.get(parent) != "epic":
                errs.append(f"issues.{it.get('key')}.parent: {parent!r} is "
                            "not an epic in this spec")
    loop = _cycle(blocks)
    if loop:
        errs.append(f"issues: blocked_by cycle {' -> '.join(loop)}")

    for i, d in enumerate(_list(doc, "decisions")):
        if not isinstance(d, dict) or not d.get("key") or not d.get("question"):
            errs.append(f"decisions[{i}]: key and question are required")
            continue
        for g in d.get("gates") or []:
            if g not in keys:
                errs.append(f"decisions.{d['key']}.gates: unknown issue {g!r}")
    return errs


def resolve_leader(doc: dict, choice: str = "") -> dict:
    """The import-time leader decision: ``own`` or ``shared:<session>``.

    On ``mesh.mode: attach`` the answer is always ``existing``: the mesh's
    current leader, optionally asserted by ``shared:<session>``.

    ``choice`` is what the person importing said; empty falls back to the
    spec's ``leader.mode``. ``ask`` with no choice refuses — the decision is
    the importer's to make, and guessing it would either start a second
    integrator on the same master or put one more mesh on someone's leader.
    """
    leader = doc.get("leader") or {}
    raw = (choice or leader.get("mode") or "ask").strip()
    if (doc.get("mesh") or {}).get("mode") == "attach":
        # An existing mesh already has its leader, and joining anyone else
        # as leader is refused by the mesh's exclusive check — so on attach
        # the leader is not a choice. A named session is an assertion the
        # executor checks against the mesh's live leader before anything is
        # spawned; no name adopts whoever leads it.
        if raw == "own":
            raise SpecError("own is impossible when attaching to a mesh")
        if raw not in ("ask", "shared") and not raw.startswith("shared:"):
            raise SpecError(f"unknown leader choice {raw!r}")
        return {"mode": "existing",
                "session": raw.partition(":")[2].strip() or None}
    if raw == "ask":
        raise SpecError(
            "this spec leaves the leader to the import: pass own or "
            "shared:<session>"
        )
    if raw == "own":
        if (doc.get("mesh") or {}).get("mode") == "attach":
            raise SpecError("own is impossible when attaching to a mesh")
        return {"mode": "own", **(leader.get("own") or {})}
    if raw == "shared" or raw.startswith("shared:"):
        shared = leader.get("shared") or {}
        session = raw.partition(":")[2].strip()
        candidates = [str(c) for c in shared.get("candidates") or []]
        if not session:
            if len(candidates) != 1:
                raise SpecError(
                    "shared needs a session: shared:<session> "
                    f"(candidates: {', '.join(candidates) or 'none listed'})"
                )
            session = candidates[0]
        if candidates and session not in candidates:
            raise SpecError(
                f"{session!r} is not among leader.shared.candidates "
                f"({', '.join(candidates)})"
            )
        return {"mode": "shared", "session": session,
                "handle": shared.get("handle") or "leader"}
    raise SpecError(f"unknown leader choice {raw!r}")


def mesh_name(doc: dict) -> str:
    return (doc.get("mesh") or {}).get("name") or f"spec-{doc['name']}"


def _agent_order(agents: List[dict]) -> List[dict]:
    """Agents parents-first, so every spawn names a parent that exists."""
    placed = {LEADER_ID}
    out: List[dict] = []
    pending = list(agents)
    while pending:
        rest = []
        for a in pending:
            if (a.get("parent") or LEADER_ID) in placed:
                out.append(a)
                placed.add(a["id"])
            else:
                rest.append(a)
        if len(rest) == len(pending):  # validate() reports the cycle
            break
        pending = rest
    return out


def plan(doc: dict, leader_choice: str = "") -> List[dict]:
    """The ordered actions an import performs. Raises if ``doc`` is invalid.

    Order matters: the board first (issues exist before anyone is pointed at
    them, so a spawn's ``--issue`` takes instead of minting), then the mesh
    and its vocabulary, then the leader, then agents parents-first.
    """
    errs = validate(doc)
    if errs:
        raise SpecError("invalid spec:\n- " + "\n- ".join(errs))
    lead = resolve_leader(doc, leader_choice)
    name = doc["name"]
    mesh = mesh_name(doc)
    tag = f"spec:{name}"
    acts: List[dict] = []
    if lead["mode"] == "existing":
        # First of all: a failed check must leave the board untouched.
        acts.append({"do": "leader-check", "agent": LEADER_ID, "mesh": mesh,
                     "session": lead["session"]})

    for r in _list(doc, "references"):
        acts.append({"do": "reference", "id": r["id"], "kind": r["kind"]})
    for c in _list(doc, "contracts"):
        acts.append({"do": "issue", "key": f"contract:{c['id']}", "type": "doc",
                     "external_ref": f"{tag}:contract:{c['id']}"})
    issues = _list(doc, "issues")
    epics_first = sorted(issues, key=lambda i: i.get("type") != "epic")
    for it in epics_first:
        acts.append({
            "do": "issue", "key": it["key"], "type": it.get("type", "task"),
            "priority": it.get("priority", 2), "parent": it.get("parent"),
            "external_ref": f"{tag}:{it['key']}",
            "labels": [tag, *(it.get("labels") or []),
                       *(["human"] if it.get("assignee") == "human" else [])],
        })
    for it in issues:
        for b in it.get("blocked_by") or []:
            acts.append({"do": "dep", "issue": it["key"], "blocked_by": b})
    for d in _list(doc, "decisions"):
        for g in d.get("gates") or []:
            acts.append({"do": "comment", "issue": g,
                         "text": f"BLOCKED: decision {d['key']}"})

    if (doc.get("mesh") or {}).get("mode", "new") == "new":
        acts.append({"do": "mesh-create", "mesh": mesh})
    if doc.get("roles") or doc.get("subroles"):
        acts.append({"do": "mesh-roles", "mesh": mesh,
                     "subroles": sorted(doc.get("subroles") or {})})

    if lead["mode"] == "own":
        acts.append({"do": "spawn", "agent": LEADER_ID, "role": "leader",
                     "mesh": mesh, "workflow": lead.get("workflow") or
                     "improv-leader", "parent": None})
    elif lead["mode"] == "shared":
        acts.append({"do": "mesh-join", "agent": LEADER_ID,
                     "session": lead["session"], "mesh": mesh,
                     "handle": lead["handle"], "role": "leader"})

    owned: Dict[str, List[dict]] = {}
    for it in issues:
        if it.get("assignee") not in (None, "", "human"):
            owned.setdefault(it["assignee"], []).append(it)
    for a in _agent_order(_list(doc, "agents")):
        kind = a.get("kind", "worker")
        queue = sorted(owned.get(a["id"], []),
                       key=lambda i: i.get("priority", 2))
        acts.append({
            "do": "spawn", "agent": a["id"], "kind": kind, "mesh": mesh,
            "parent": a.get("parent") or LEADER_ID,
            "role": a.get("role") or KINDS[kind][0],
            "subroles": list(a.get("subroles") or []),
            "workflow": a.get("workflow") or KINDS[kind][1],
            "issue": queue[0]["key"] if queue else None,
        })
        for it in queue[1:]:
            acts.append({"do": "assign", "issue": it["key"], "agent": a["id"]})
        if kind == "sub-lead":
            acts.append({"do": "stack", "agent": a["id"]})
    for a in _list(doc, "agents"):
        for peer in a.get("connect") or []:
            acts.append({"do": "connect", "mesh": mesh, "a": a["id"],
                         "b": peer})
    return acts
