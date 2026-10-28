"""Who takes a worker's landing request: the rule both trigger clocks share.

A worker's ``enqueue-landing`` (v1 :class:`.cflow_clock.TriggerClock`, v2
:mod:`.cflow2_triggers`) files its request on the landing queue of a session
that can merge. Which session that is, is decided here once:

* **stacked** (v1 always; v2 ``parent:landing``) -- the parent, then each
  direct ancestor above it: the nearest one that can merge. With none, the
  mesh leader (below).
* **independent** (v2 ``leader:landing``) -- the mesh leader only.

The mesh leader is a ``leader``-role member of a mesh the worker is in, taken
only when it is one of the worker's direct ancestors or a sibling (the same
parent; two sessions with no parent count as siblings) of the worker or of one
of those ancestors -- so a leader beside a parent that cannot merge (a
free-role root with no workflow) takes the request (claunch-g0ttr). Never a
child, a cousin or a sibling's child. Not exactly one such leader that can
merge is a skip.

A session *can merge* when it is live, holds the ``merger`` role in a mesh
the worker is in (a ``leader`` is taken as holding it; roles are mesh
membership, so that is where they are read -- claunch-lhgfx), and drives, in a
checkout of the worker's own repository, a run that holds a landing queue. One
whose queues are all in other repositories is skipped with those repositories
named (claunch-u7nu3).
With no mesh manager at all (a daemon or test without one) the role is not
checked. When it drives
several, one is picked without asking: a main run first, whatever its order,
then the first in order (``holders`` hands them over sorted).

The clocks differ only in what a holder is (a v1 ``landing_queue:`` run, a v2
``landing`` collection, or either), so they pass ``holders``; everything
returned is a reason string or a holder, nothing here raises.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..cflow import landing as cflow_landing


@dataclass(frozen=True)
class Holder:
    """One run of a session that holds a landing queue."""

    cwd: str
    #: The run's name in its slot: a v1 sub run's name or a v2 scope; None
    #: for a v1 main run.
    run: Optional[str]
    main: bool
    version: str = "v1"


#: ``holders(session, repo)``: the runs ``session`` drives in a checkout of
#: ``repo`` that hold a landing queue, in order. ``repo`` is None when the
#: worker's checkout has no readable git (its request then fails on the tip
#: it cannot read, not here): no checkout is filtered out.
HoldersFn = Callable[[str, Optional[str]], List[Holder]]

#: The role a session must hold (in the worker's mesh) to take a landing
#: request, and the role that always counts as holding it.
MERGER = "merger"
LEADER = "leader"

#: How many ancestors a walk looks through (a cycle or a runaway chain ends).
MAX_HOPS = 8


def pick(holders: Sequence[Holder]) -> Optional[Holder]:
    """A main run first, whatever its order; then the first in order."""
    for h in holders:
        if h.main:
            return h
    return holders[0] if holders else None


@dataclass
class Target:
    """Where the request goes, or why nowhere."""

    owner: Optional[str] = None
    holder: Optional[Holder] = None
    why: str = ""
    #: Each session passed over on the way, and why (for the trigger detail).
    passed: Tuple[str, ...] = ()
    #: ``why`` is "this could not be read", not "there is nobody": the walk
    #: stopped on a fact it could not establish (a mesh membership, a run
    #: slot). A push reads it as one more skip reason, but a caller that
    #: *asks before* pushing -- the landing-target probe -- must not read it
    #: as the answer "no target", because acting on that would ask a person
    #: to re-route a request nobody had shown to be unroutable. Defaulted, so
    #: every existing caller keeps its meaning.
    undecidable: bool = False

    def note(self) -> str:
        """``"<passed>; fell back to 'X' at <cwd>: "`` when the request did
        not go to the parent, else empty."""
        if not self.passed or self.holder is None:
            return ""
        return f"{'; '.join(self.passed)}; fell back to {self.owner!r} at {self.holder.cwd}: "


class Router:
    def __init__(self, manager: Any, mesh: Any, holders: HoldersFn) -> None:
        self.manager = manager
        self.mesh = mesh
        self.holders = holders
        #: Parents stated by the caller for a session the manager cannot yet
        #: answer for (a child being onboarded, claunch-u7nu3).
        self.parents: Dict[str, str] = {}

    def _session(self, name: str) -> Any:
        try:
            return self.manager.get(name)
        except Exception:
            return None

    def parent_of(self, name: Optional[str]) -> Optional[str]:
        if not name:
            return None
        if name in self.parents:
            return self.parents[name] or None
        session = self._session(name)
        return getattr(getattr(session, "sdef", None), "parent", None) or None

    def ancestors(self, scope: str) -> List[str]:
        """The parent first, then upward, as far as sessions are known."""
        out: List[str] = []
        name = self.parent_of(scope)
        while name and name != scope and name not in out and len(out) < MAX_HOPS:
            out.append(name)
            name = self.parent_of(name)
        return out

    def merger_role(self, scope: str, name: str,
                    blind: Optional[List[str]] = None) -> str:
        """Why ``name`` holds no ``merger`` role where ``scope`` (the worker)
        can see it, or empty when it does. A ``leader`` counts as one.

        ``blind`` collects the reasons this could not be *read* rather than
        found absent; the caller turns a non-empty list into
        :attr:`Target.undecidable`. Collected through an argument rather
        than matched out of the returned sentence, so rewording a reason
        cannot silently reclassify a blind walk as a definite "nobody".
        """
        if self.mesh is None:
            return ""
        try:
            meshes = sorted({m["mesh"] for m in self.mesh.meshes_for_session(scope)})
        except Exception:
            if blind is not None:
                blind.append(f"the worker's mesh membership could not be read ({scope!r})")
            return "holds a role that could not be read (the worker's mesh membership)"
        for mname in meshes:
            try:
                mesh = self.mesh.get(mname)
            except Exception:
                # A mesh this daemon cannot read may be where the role lives,
                # so a walk that only failed here has not answered "nobody".
                if blind is not None:
                    blind.append(f"mesh {mname!r} could not be read for {name!r}")
                continue
            for m in mesh.members.values():
                # A handle on a mirrored mesh can carry the same session name
                # for a session on another machine; only this daemon's own
                # member speaks for ``name``.
                if m.session != name or not self.mesh.is_local_member(mesh, m):
                    continue
                if MERGER in m.roles or LEADER in m.roles:
                    return ""
        where = ", ".join(repr(n) for n in meshes) or "no mesh"
        return f"holds no {MERGER!r} role in the worker's mesh ({where})"

    def merger(self, name: str, repo: Optional[str], scope: str = "",
               blind: Optional[List[str]] = None) -> Tuple[Optional[Holder], str]:
        """(holder, why): the run of ``name`` the request goes on, or why
        ``name`` cannot merge. ``scope`` is the worker whose meshes the
        ``merger`` role is read in, and ``blind`` collects what could not be
        read (see :meth:`merger_role`)."""
        session = self._session(name)
        if session is None:
            return None, "is not a session of this daemon"
        if getattr(session, "exited", False):
            return None, "has exited"
        why = self.merger_role(scope, name, blind)
        if why:
            return None, why
        try:
            found = self.holders(name, repo)
        except Exception as exc:  # noqa: BLE001 - an unreadable slot is no holder
            if blind is not None:
                blind.append(f"the run slots of {name!r} could not be read ({exc})")
            return None, f"has runs that could not be read ({exc})"
        holder = pick(found)
        if holder is None:
            return None, self._elsewhere(name, repo) or (
                "has no run in this repository that declares a landing queue")
        return holder, ""

    def _elsewhere(self, name: str, repo: Optional[str]) -> str:
        """Why ``name`` is the landing queue of another repository, or empty.

        A leader that integrates one repository is no target for a worker of
        another (claunch-u7nu3: a gds6 worker routed to the claunch leader);
        naming the repositories it does hold tells the reader that apart from
        a leader that has no queue at all."""
        if repo is None:
            return ""
        try:
            found = self.holders(name, None)
        except Exception:  # noqa: BLE001 - the plain reason stands
            return ""
        others = sorted({cflow_landing.common_dir(h.cwd) or h.cwd for h in found} - {repo})
        if not others:
            return ""
        return (f"takes landings for another repository ({', '.join(others)}), "
                f"not the worker's ({repo})")

    def stacked(self, cwd: str, scope: str) -> Target:
        """The nearest direct ancestor that can merge, else the mesh leader."""
        repo = cflow_landing.common_dir(cwd)
        passed: List[str] = []
        blind: List[str] = []
        chain = self.ancestors(scope)
        if not chain:
            passed.append("this session has no parent session")
        for depth, name in enumerate(chain):
            holder, why = self.merger(name, repo, scope, blind)
            if holder is not None:
                return Target(name, holder, passed=tuple(passed))
            passed.append(f"{'parent' if depth == 0 else 'ancestor'} {name!r} {why}")
        owner, holder, why = self.leader(scope, chain, repo, tried=set(chain), blind=blind)
        if holder is not None:
            return Target(owner, holder, passed=tuple(passed))
        return Target(why="; ".join(passed + [f"mesh leader fallback: {why}"]),
                      passed=tuple(passed), undecidable=bool(blind))

    def independent(self, cwd: str, scope: str, parent: Optional[str] = None) -> Target:
        """The mesh leader only (a sibling or a direct ancestor). ``parent``
        states ``scope``'s parent when the session record may not carry it
        yet (the spawn that wires a child to its leader)."""
        if parent is not None:
            self.parents[scope] = parent
        repo = cflow_landing.common_dir(cwd)
        blind: List[str] = []
        owner, holder, why = self.leader(scope, self.ancestors(scope), repo, tried=set(),
                                         blind=blind)
        if holder is None:
            return Target(why=f"mesh leader: {why}", undecidable=bool(blind))
        return Target(owner, holder)

    def leader(self, scope: str, chain: Sequence[str], repo: Optional[str], *,
               tried: set, blind: Optional[List[str]] = None,
               ) -> Tuple[Optional[str], Optional[Holder], str]:
        """(leader, holder, why) of the one mesh leader that takes the request.

        Only a direct ancestor of ``scope`` (``chain``) or a sibling of
        ``scope`` or of one of those ancestors (a known session); one
        already asked (``tried``) is not asked again. ``blind`` collects what
        could not be read, as in :meth:`merger_role`.
        """
        if self.mesh is None:
            return None, None, "no mesh to look for a leader in"
        try:
            names = sorted({m["mesh"] for m in self.mesh.meshes_for_session(scope)})
        except Exception:
            if blind is not None:
                blind.append(f"the worker's mesh membership could not be read ({scope!r})")
            return None, None, "the worker's mesh membership could not be read"
        if not names:
            return None, None, "this session is in no mesh"
        leaders: Dict[str, tuple] = {}
        for mname in names:
            try:
                mesh = self.mesh.get(mname)
            except Exception:
                if blind is not None:
                    blind.append(f"mesh {mname!r} could not be read for its leaders")
                continue
            for _, m in sorted(mesh.members.items()):
                if LEADER in m.roles and m.session != scope:
                    leaders.setdefault(m.session, (mesh, m))
        if not leaders:
            return None, None, f"no leader in mesh {', '.join(repr(n) for n in names)}"
        # The parent each lead may share to count as a sibling: the worker's,
        # and each known ancestor's -- a leader beside a parent that cannot
        # merge (a free-role root, claunch-g0ttr) takes the request too.
        sibling_of = {scope: self.parent_of(scope)}
        for name in chain:
            if self._session(name) is not None:
                sibling_of[name] = self.parent_of(name)
        eligible: List[Tuple[str, Holder]] = []
        refused: List[str] = []
        for lead, (mesh, member) in sorted(leaders.items()):
            if lead in tried:
                refused.append(f"leader {lead!r} is a direct ancestor that cannot merge")
                continue
            if not self.mesh.is_local_member(mesh, member):
                refused.append(f"leader {lead!r} is on another machine")
                continue
            if self._session(lead) is None:
                refused.append(f"leader {lead!r} is not a session of this daemon")
                continue
            lead_parent = self.parent_of(lead)
            if lead not in chain and not any(
                lead != name and lead_parent == parent for name, parent in sibling_of.items()
            ):
                refused.append(
                    f"leader {lead!r} is neither a sibling nor a direct ancestor of {scope!r} "
                    "or of one of its ancestors"
                )
                continue
            holder, why = self.merger(lead, repo, scope, blind)
            if holder is None:
                refused.append(f"leader {lead!r} {why}")
                continue
            eligible.append((lead, holder))
        if len(eligible) == 1:
            return eligible[0][0], eligible[0][1], ""
        if len(eligible) > 1:
            return None, None, (
                f"several leaders can merge ({', '.join(l for l, _ in eligible)}); "
                "the target is ambiguous"
            )
        return None, None, "; ".join(refused)
