"""Joint plan trees (claunch-7jzp8.1): the plans of related sessions grouped
into one tree, so a parent and its children, or the sessions on one issue
and its sub-issues, read as one plan.

Each session is placed under one display parent, the first of:

  spawn   its spawn parent (the session record's ``parent``)
  same    the first session (by name) holding the same issue
  issue   the session holding its issue's parent issue -- beads ids are
          hierarchical, ``claunch-x.2`` is a child of ``claunch-x``

A spawn parent without a plan of its own is still drawn (``has_plan``
false) so its children meet under it. A link that would close a cycle is
dropped. A group is a tree; a session with no related session is a group
of one.

Pure: the caller passes the rail (PlanStore.rail) and a ``parent_of``
lookup for live sessions; nothing here reads the daemon.
"""

from __future__ import annotations

from typing import Callable, Optional

ParentOf = Callable[[str], Optional[str]]


def issue_parent(issue: Optional[str]) -> Optional[str]:
    if not issue or "." not in issue:
        return None
    head, _, tail = issue.rpartition(".")
    return head if head and tail.isdigit() else None


def _links(rail: dict, parent_of: ParentOf) -> tuple[dict, dict]:
    """Every node (planned sessions and their plan-less spawn ancestors)
    and its display parent with the link's kind."""
    nodes: dict = {name: dict(entry, has_plan=True) for name, entry in rail.items()}

    def spawn_parent(name: str) -> Optional[str]:
        p = parent_of(name)
        if p is None:
            p = (nodes.get(name, {}).get("context") or {}).get("parent")
        return p or None

    # plan-less spawn ancestors, so siblings meet under them
    for name in list(nodes):
        seen = {name}
        p = spawn_parent(name)
        while p and p not in seen:
            seen.add(p)
            if p not in nodes:
                nodes[p] = {"has_plan": False, "title": "", "issue": None, "context": {}}
            p = spawn_parent(p)

    by_issue: dict = {}
    for name in sorted(nodes):
        issue = nodes[name].get("issue")
        if issue:
            by_issue.setdefault(issue, []).append(name)

    link: dict = {}
    for name in sorted(nodes):
        p = spawn_parent(name)
        if p and p in nodes and p != name:
            link[name] = (p, "spawn")
            continue
        issue = nodes[name].get("issue")
        peers = by_issue.get(issue) or []
        if peers and peers[0] != name:
            link[name] = (peers[0], "same")
            continue
        up = issue_parent(issue)
        while up and up not in by_issue:
            up = issue_parent(up)
        if up:
            link[name] = (by_issue[up][0], "issue")

    # drop a link that closes a cycle (two sessions naming each other)
    for name in sorted(link):
        seen, cur = {name}, link[name][0]
        while cur in link:
            if cur in seen:
                if cur == name:
                    del link[name]
                break
            seen.add(cur)
            cur = link[cur][0]
    return nodes, link


def groups(rail: dict, parent_of: ParentOf) -> list[dict]:
    """The joint trees, largest first. Each: ``root``, ``members`` in tree
    order (depth-first, siblings by name) carrying ``session``, ``parent``,
    ``via``, ``depth``, ``has_plan`` and the rail entry, and the group's
    ``waiting`` (summed), ``closable`` (every planned member) and
    ``sessions`` count."""
    nodes, link = _links(rail, parent_of)
    kids: dict = {}
    for name, (p, _via) in link.items():
        kids.setdefault(p, []).append(name)
    out = []
    for root in sorted(n for n in nodes if n not in link):
        members: list = []

        def visit(name: str, depth: int) -> None:
            p, via = link.get(name, (None, None))
            members.append({**nodes[name], "session": name, "parent": p, "via": via, "depth": depth})
            for k in sorted(kids.get(name, [])):
                visit(k, depth + 1)

        visit(root, 0)
        planned = [m for m in members if m["has_plan"]]
        if not planned:
            continue
        out.append({"root": root, "members": members, "sessions": len(planned),
                    "waiting": sum(int(m.get("waiting") or 0) for m in planned),
                    "closable": all(m.get("closable") for m in planned)})
    out.sort(key=lambda g: (-g["sessions"], g["root"]))
    return out


def group_of(rail: dict, parent_of: ParentOf, session: str) -> Optional[dict]:
    for g in groups(rail, parent_of):
        if any(m["session"] == session for m in g["members"]):
            return g
    return None
