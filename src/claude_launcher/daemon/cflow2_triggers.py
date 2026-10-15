"""Daemon side of cflow v2 ``triggers:`` (docs/cflow-v2.md 2.8, 4.5, 10).

The engine records a trigger in ``state.triggers`` when a node instance is
entered or left and moves on; :class:`.cflow2_clock.Cflow2Clock` claims the
records (:func:`..cflow2.runtime.claim_triggers`, at least once: a claim
left by an earlier boot is performed again) and hands each one to
:func:`perform`, then writes ``trigger_done``/``trigger_skipped``
(:func:`..cflow2.runtime.complete_trigger`). A push carries ``_tid`` per
record (:func:`trigger_tid`), so a second run of the same trigger changes
nothing in a v2 collection. Nothing here can hold a run: every "could not"
is a reason string.

* ``briefing`` / ``checks``: the v1 implementation
  (:func:`.cflow_clock.recompose_briefing`, :func:`.cflow_clock.request_checks`)
  aimed at the run's driver session; skipped when that session is not live.
* ``push`` / ``enqueue-landing``: facts measured now in the run's checkout
  (:func:`measure_facts`), ``each``/``item`` evaluated with the v2 expression
  language over ``{facts, each}``, then the record goes to the one run of the
  parent session that holds the collection -- a v2 run
  (:func:`..cflow2.runtime.holders`, pushed with principal ``child``). None,
  more than one, or one of each is a skip. A ``landing`` push is routed
  instead by :mod:`.landing_route` (:func:`push_landing`): ``parent:`` walks
  the direct ancestors, then the mesh leader beside the worker or one of
  them; ``leader:`` is the mesh
  leader only; a v1 run declaring ``landing_queue:`` counts as a holder
  (:func:`..cflow.engine.enqueue_landing`), and of several holders one is
  picked (a main run first, then the first in order).

The v1 side of the same bridge (a v1 worker whose parent holds a v2
``landing`` collection) reuses :func:`v2_holders` and :func:`push_v2` from
:meth:`.cflow_clock.TriggerClock._enqueue_landing`.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from ..cflow import engine as cflow_engine, landing as cflow_landing, state as cflow_state
from ..cflow2 import expr as cflow2_expr, runtime

log = logging.getLogger("claunch.daemon.cflow2.triggers")

DO_BRIEFING = "briefing"
DO_CHECKS = "checks"
DO_PUSH = "push"
DO_ENQUEUE_LANDING = "enqueue-landing"

#: The collection name that also reaches a v1 run's ``landing_queue:``.
LANDING = "landing"
#: The principal a trigger push writes as (docs 2.7 「주체」).
PRINCIPAL_CHILD = "child"

#: ``enqueue-landing`` is this push (docs 2.8).
ENQUEUE_LANDING_PUSH: Dict[str, Any] = {
    "to": f"parent:{LANDING}",
    "each": "$facts.in_review",
    "item": {
        "issue": "$each",
        "tip": "$facts.head",
        "branch": "$facts.branch",
        "requested_by": "$facts.session",
    },
}

#: Upper bound of one action (a briefing waits on the user's endpoint).
ACTION_TIMEOUT = 120.0

Result = Tuple[bool, str]


class PushError(Exception):
    """A push that cannot be performed: the message is the skip reason."""


# -- sessions and directories -------------------------------------------------


def _resolve(cwd: str) -> str:
    try:
        return str(Path(cwd).resolve())
    except OSError:
        return str(cwd)


def live_session(manager: Any, name: Optional[str], *, include_held: bool = False) -> Any:
    """The live session called ``name``, or None."""
    if not name:
        return None
    try:
        session = manager.get(name)
    except Exception:
        return None
    if getattr(session, "exited", True) and not (include_held and getattr(session, "held_at", None)):
        return None
    return session


def parent_of(manager: Any, session: Optional[str]) -> Optional[str]:
    """The session that created ``session`` (``sdef.parent``), or None. Read
    whether or not ``session`` is still alive: a request stands either way."""
    if not session:
        return None
    try:
        parent = manager.get(session).sdef.parent
    except Exception:
        return None
    return str(parent) if parent else None


def session_cwds(manager: Any, *extra: Optional[str]) -> List[str]:
    """Directories of the live sessions (where v2 runs are looked for), plus
    the directory of each named session in ``extra`` (live or not)."""
    out = set()
    try:
        sessions = list(manager.list())
    except Exception:
        sessions = []
    for s in sessions:
        if getattr(s, "exited", True):
            continue
        cwd = getattr(getattr(s, "sdef", None), "cwd", "") or ""
        if cwd:
            out.add(_resolve(cwd))
    for name in extra:
        if not name:
            continue
        try:
            cwd = manager.get(name).sdef.cwd or ""
        except Exception:
            cwd = ""
        if cwd:
            out.add(_resolve(cwd))
    return sorted(out)


# -- holders ------------------------------------------------------------------


def v2_holders(name: str, parent: str, cwds: Sequence[str],
               index: Optional["runtime.HolderIndex"] = None) -> List[Tuple[str, str]]:
    """Live v2 runs driven by ``parent`` that declare collection ``name``.
    ``index`` (a :class:`..cflow2.runtime.HolderIndex` for ``name``) is the
    reading shared by a caller that asks for several sessions in one pass."""
    try:
        if index is not None:
            found = index.holders(parent, list(cwds))
        else:
            found = runtime.holders(name, parent, list(cwds))
    except Exception as exc:  # noqa: BLE001 - an unreadable slot is not a holder
        log.debug("cflow2 holders(%s, %s) failed: %s", name, parent, exc)
        return []
    out = []
    for h in found or []:
        if isinstance(h, dict):
            out.append((str(h.get("cwd")), str(h.get("scope"))))
        else:
            cwd, scope = h[0], h[1]
            out.append((str(cwd), str(scope)))
    return out


def v1_landing_holders(parent: str) -> List[Tuple[str, Optional[str]]]:
    """``(cwd, run)`` of each v1 run of ``parent`` that holds a
    ``landing_queue:`` (``run`` None for the main run). The way
    :meth:`.cflow_clock.TriggerClock.v1_holders` finds the parent's runs
    (the v1 registry), over every directory it drives."""
    out: List[Tuple[str, Optional[str]]] = []
    try:
        dirs = sorted({c for c, s in cflow_state.known_runs() if s == parent})
    except Exception:
        return out
    for cwd in dirs:
        try:
            holder = cflow_engine.landing_queue_run(cwd=cwd, scope=parent)
        except Exception:
            holder = None
        if holder:
            out.append((cwd, None if holder == cflow_state.MAIN_RUN else holder))
    return out


def both_warning(parent: str, name: str, role: str = "parent") -> str:
    return (f"{role} {parent!r} holds {name!r} twice: a v1 run declaring "
            f"'landing_queue:' and a v2 run declaring the '{name}' collection; "
            "the target is ambiguous, nothing was pushed")


# -- facts and expressions -----------------------------------------------------


def measure_facts(cwd: str, session: str, work: Optional[str] = None) -> Dict[str, Any]:
    """``$facts`` (docs 2.8), measured now. Blocking (git, the issue board).
    ``in_review`` is None when the board could not be read.

    ``head`` and ``branch`` come from ``work`` -- the checkout the session
    actually works in -- when it is given, else from the run's checkout.
    The two differ for a session whose run was started at the main checkout
    and which then moved into a worktree: measuring the run's checkout there
    recorded the integration branch and its tip as the landing request
    (claunch-4zc51.1.2). ``checkout`` says which directory was measured."""
    where = work or cwd
    return {
        "head": cflow_landing.head(where),
        "branch": cflow_landing.branch(where),
        "in_review": cflow_landing.in_review_of(cwd, session),
        "session": session,
        "checkout": where,
    }


def _on_feature_branch(where: str) -> bool:
    branch = cflow_landing.branch(where)
    return bool(branch) and branch not in TRUNKS


def session_facts(manager: Any) -> Callable[[str, str], Dict[str, Any]]:
    """The default ``facts_fn``: :func:`measure_facts` in the checkout the
    session works in (:func:`.work_checkout.of_session`), which falls back
    to the run's checkout when nothing better is known.

    The work checkout is the transcript's newest ``cwd``, which is wherever
    the session last ``cd``-ed. A session that works in a worktree and ends
    a turn at the main checkout (on the trunk) or in a detached scratch
    worktree would then be measured there and refused. When the measured
    checkout is not on a feature branch and the run's checkout is, the run's
    checkout is the better answer and is measured instead."""
    from . import work_checkout

    def facts(cwd: str, session: str) -> Dict[str, Any]:
        work = work_checkout.of_session(manager, session)
        if work and not _on_feature_branch(work) and _on_feature_branch(cwd):
            work = None
        return measure_facts(cwd, session, work)

    return facts


#: Branches a landing request is never for: the integration branch itself.
#: A request measured on one of these was measured in the wrong checkout,
#: and its tip is already in the target -- the queue would mark it landed at
#: once (claunch-4zc51.1.2). Refused rather than pushed.
TRUNKS = ("master", "main")


#: What a refusal measured in the wrong checkout tells the session to do.
RETRY_HINT = "cd into the feature checkout and request the landing again"


def landing_refusal(facts: Dict[str, Any]) -> Optional[str]:
    """Why a measured landing request must not be pushed, or None.

    Besides the branch name, the tip itself: a tip that is already an
    ancestor of a trunk has nothing left to merge, and the queue would mark
    the request landed at once -- the claunch-4zc51.1.2 outcome reached
    through a branch whose name is not a trunk (a branch with no commit of
    its own, a request raised before anything was committed). The trunks are
    the target that can be read from the worker's checkout alone; the
    holder's checkout is not read, because its HEAD moves while it merges
    and a stacked request's target (the parent's stack branch) is not what
    that checkout has checked out. A trunk git cannot answer for (no such
    branch here) is skipped."""
    where = facts.get("checkout") or "the run's checkout"
    branch = facts.get("branch")
    if not branch:
        return (f"HEAD is detached in {where}: a landing request names a branch, "
                f"nothing was pushed ({RETRY_HINT})")
    if branch in TRUNKS:
        return (f"{where} is on {branch}, the integration branch itself: this is "
                f"not the session's feature checkout, nothing was pushed ({RETRY_HINT})")
    head = facts.get("head")
    if head and facts.get("checkout"):
        for trunk in TRUNKS:
            if cflow_landing.landed(str(facts["checkout"]), str(head), trunk):
                return (f"{branch} @ {str(head)[:12]} is already contained in {trunk}: "
                        "there is nothing to land, nothing was pushed")
    return None


def _value(spec: Any, env: Dict[str, Any]) -> Any:
    if isinstance(spec, str):
        return cflow2_expr.evaluate(spec, env)
    return spec


def expand_push(push: Dict[str, Any], facts: Dict[str, Any],
                data: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """The records a push writes: one per ``each`` element (one record when
    ``each`` is absent). ``data`` is the run data the trigger record carries
    (the ``$data`` keys its expressions read, as they were when it was
    queued). Raises :class:`PushError` with the skip reason."""
    env: Dict[str, Any] = {"facts": facts, "data": data or {}}
    each_spec = push.get("each")
    try:
        if each_spec is None:
            elements: List[Any] = [None]
        else:
            each = _value(each_spec, env)
            if each is None:
                raise PushError(f"'each' ({each_spec}) is null")
            if not isinstance(each, list):
                raise PushError(f"'each' ({each_spec}) is not a list")
            if not each:
                raise PushError(f"'each' ({each_spec}) is an empty list")
            elements = list(each)
        items = []
        for element in elements:
            scope_env = dict(env, each=element)
            items.append({str(k): _value(v, scope_env)
                          for k, v in (push.get("item") or {}).items()})
    except cflow2_expr.ExprError as exc:
        raise PushError(f"expression error: {exc}") from exc
    return items


def split_addr(to: Any) -> Tuple[str, str]:
    """``[target:]name`` -> (target, name); the target defaults to self."""
    text = str(to or "").strip()
    target, sep, name = text.rpartition(":")
    if not sep:
        return "self", text
    return target, name


def is_landing(trig: Dict[str, Any]) -> bool:
    """The trigger files a landing request: ``enqueue-landing``, or a push to
    ``parent:landing`` / ``leader:landing`` (routed by :func:`push_landing`)."""
    do = str(trig.get("do") or "")
    if do == DO_ENQUEUE_LANDING:
        return True
    if do != DO_PUSH:
        return False
    target, name = split_addr((trig.get("push") or {}).get("to"))
    return name == LANDING and target in ("parent", "leader")


# -- pushing -------------------------------------------------------------------


def push_v2(holder: Tuple[str, str], name: str, items: Iterable[Dict[str, Any]],
            session: str, tid: Optional[str] = None) -> Result:
    """Push ``items`` into a v2 run's collection as ``child``. Every record
    is tried; one refusal makes the trigger a skip whose reason counts what
    went in (those records stay: each push is its own operation). ``tid``
    names the trigger: record ``i`` carries ``<tid>:<i>`` so a second run of
    the same trigger (at least once, doc 2.8) is recognised."""
    hcwd, hscope = holder
    items, dropped = _fit_fields(holder, name, list(items))
    pushed: List[str] = []
    replayed: List[str] = []
    refused: List[str] = []
    for i, item in enumerate(items):
        try:
            out = runtime.collection_op("push", name, cwd=hcwd, scope=hscope,
                                        principal=PRINCIPAL_CHILD, session=session, item=item,
                                        tid=f"{tid}:{i}" if tid else None)
        except runtime.FlowError as exc:
            refused.append(f"{_label(item)}: {exc}")
            continue
        if (out.get("applied") or {}).get("replay"):
            replayed.append(_label(item))
        else:
            pushed.append(_label(item))
    if refused:
        return False, _partial(f"{hscope}'s {name!r}", len(items), pushed + replayed, refused)
    parts = [f"pushed {', '.join(pushed)}"] * bool(pushed) + \
        [f"replay: already there {', '.join(replayed)}"] * bool(replayed) + \
        [f"fields the target does not declare were left out: {', '.join(dropped)}"] * bool(dropped)
    return True, f"{'; '.join(parts)} to {hscope}'s {name!r} (v2)"


def _fit_fields(holder: Tuple[str, str], name: str, items: List[Dict[str, Any]]
                ) -> Tuple[List[Dict[str, Any]], List[str]]:
    """``items`` without the fields the target collection does not declare,
    and the names of the non-null ones left out. A child's push may carry
    more than an older parent declares (improv-worker-v2's PR fields,
    claunch-yq80p.5); refusing the whole record for that would lose the
    landing request, so the extra fields go and the detail says so. A
    target whose declaration cannot be read gets the items unchanged (the
    push then reports its own error)."""
    from ..cflow2 import store as cstore

    try:
        chart = cstore.RunStore(*holder).load_chart()
        declared = set(((chart.meta or {}).get("collections") or {})[name]["item"])
    except Exception:  # noqa: BLE001 - the push reports what is wrong
        return items, []
    dropped: List[str] = []
    out = []
    for item in items:
        extra = [k for k in item if k not in declared]
        dropped += [k for k in extra if item[k] is not None and k not in dropped]
        out.append({k: v for k, v in item.items() if k in declared})
    return out, dropped


def _partial(where: str, total: int, pushed: List[str], refused: List[str]) -> str:
    """The skip reason of a push with refusals: counts, then each refusal."""
    head = f"{where} refused {len(refused)} of {total} record(s)"
    if pushed:
        head += f" ({len(pushed)} pushed and kept: {', '.join(pushed)})"
    return f"{head}: {'; '.join(refused)}"


def _label(item: Dict[str, Any]) -> str:
    for k in ("issue", "name", "key", "id"):
        if item.get(k) not in (None, ""):
            return str(item[k])
    return "1 record"


def push_v1(holder: Tuple[str, Optional[str]], parent: str,
            items: Sequence[Dict[str, Any]], session: str) -> Result:
    """Put ``items`` (``issue``, ``branch``, ``tip``, ``requested_by``) on a
    v1 run's landing queue: one :func:`..cflow.engine.enqueue_landing` call
    per (branch, tip, requested_by)."""
    hcwd, run = holder
    groups: Dict[Tuple[str, str, str], List[str]] = {}
    for item in items:
        issue = str(item.get("issue") or "").strip()
        tip = str(item.get("tip") or "").strip()
        if not issue or not tip:
            return False, ("a v1 landing queue takes records with 'issue' and 'tip'; "
                           f"got {sorted(k for k, v in item.items() if v not in (None, ''))}")
        key = (str(item.get("branch") or ""), tip, str(item.get("requested_by") or session))
        groups.setdefault(key, []).append(issue)
    added: List[str] = []
    renewed: List[str] = []
    refused: List[str] = []
    for (branch, tip, by), issues in groups.items():
        try:
            result = cflow_engine.enqueue_landing(issues, branch or None, tip, by=by,
                                                  cwd=hcwd, scope=parent, run=run)
        except cflow_engine.CflowError as exc:
            refused += [f"{i}: {exc}" for i in issues]
            continue
        added += result.get("added") or []
        renewed += result.get("renewed") or []
    if refused:
        return False, _partial(f"{parent}'s landing queue (v1)", len(items),
                               added + renewed, refused)
    if not added and not renewed:
        return True, f"already on {parent}'s landing queue (v1)"
    return True, (f"on {parent}'s landing queue (v1): "
                  + ", ".join([f"added {', '.join(added)}"] * bool(added)
                              + [f"renewed {', '.join(renewed)}"] * bool(renewed)))


def push_to_parent(manager: Any, *, session: str, name: str,
                   items: Sequence[Dict[str, Any]], cwds: Sequence[str],
                   tid: Optional[str] = None) -> Result:
    """Push into the one run of ``session``'s parent that holds ``name``.
    A ``landing`` push does not come here (:func:`push_landing`)."""
    parent = parent_of(manager, session)
    if not parent:
        return False, f"{session or 'this run'} has no parent session"
    v2 = v2_holders(name, parent, list(cwds) + session_cwds(manager, parent))
    v1 = v1_landing_holders(parent) if name == LANDING else []
    if not v2 and not v1:
        return False, f"parent {parent!r} has no live run that declares {name!r}"
    return _push_one(parent, "parent", v2, v1, session=session, name=name, items=items, tid=tid)


#: How many ancestors a ``leader:`` push looks through.
LEADER_HOPS = 8


def push_to_leader(manager: Any, *, session: str, name: str,
                   items: Sequence[Dict[str, Any]], cwds: Sequence[str],
                   tid: Optional[str] = None) -> Result:
    """Push into the nearest ancestor of ``session`` (its parent first)
    whose *main* run holds ``name`` (docs 2.8 ``leader:``). A side run or a
    nested run of an ancestor does not count: an independent child of a
    worker skips the worker's stack run and reaches the session above it.
    A ``landing`` push does not come here: it goes to the mesh leader
    (:func:`push_landing`)."""
    from ..cflow2 import store as cstore

    seen = {session}
    cur = parent_of(manager, session)
    if not cur:
        return False, f"{session or 'this run'} has no parent session"
    passed: List[str] = []
    index = runtime.HolderIndex(name)  # one reading of the slots for the whole walk
    while cur and cur not in seen and len(passed) < LEADER_HOPS:
        seen.add(cur)
        v2 = [h for h in v2_holders(name, cur, list(cwds) + session_cwds(manager, cur), index)
              if h[1] == cstore.main_scope(h[1])]
        v1 = [h for h in v1_landing_holders(cur) if h[1] is None] if name == LANDING else []
        if v2 or v1:
            return _push_one(cur, "leader", v2, v1, session=session, name=name,
                             items=items, tid=tid)
        passed.append(cur)
        cur = parent_of(manager, cur)
    return False, (f"no ancestor of {session!r} ({', '.join(passed)}) has a live main run "
                   f"that declares {name!r}")


def _push_one(owner: str, role: str, v2: List[Tuple[str, str]],
              v1: List[Tuple[str, Optional[str]]], *, session: str, name: str,
              items: Sequence[Dict[str, Any]], tid: Optional[str]) -> Result:
    """Push into the one holder of ``owner`` (``role`` names it in reasons);
    two holders, or a v1 and a v2 one, is a skip."""
    v2 = sorted(set(v2))
    if v2 and v1:
        msg = both_warning(owner, name, role)
        log.warning("cflow2 trigger push from %s: %s", session, msg)
        return False, msg
    if len(v2) + len(v1) > 1:
        where = [f"{c} {s}" for c, s in v2] + [f"{c} {r or 'main'} (v1)" for c, r in v1]
        return False, (f"{role} {owner!r} has {len(where)} runs declaring {name!r} "
                       f"({'; '.join(where)}); the target is ambiguous")
    if v2:
        return push_v2(v2[0], name, items, session, tid=tid)
    return push_v1(v1[0], owner, items, session)


def landing_holders(manager: Any, cwds: Sequence[str]) -> Callable[[str, str], List[Any]]:
    """The ``holders`` a :class:`.landing_route.Router` reads for a v2 push:
    each live v2 run of a session that declares ``landing`` and each v1 run
    that declares ``landing_queue:``, in a checkout of the worker's
    repository, ordered by directory then run."""
    from ..cflow2 import store as cstore
    from .landing_route import Holder

    # One reading of the slots for every session this closure is asked about:
    # a Router walks ancestors and the merger grant asks for every live member
    # (claunch-cb65c). The closure lives for one route or one grant pass.
    index = runtime.HolderIndex(LANDING)

    def holders(session: str, repo: Optional[str]) -> List[Holder]:
        out: List[Holder] = []
        for c, sc in sorted(set(v2_holders(LANDING, session,
                                           list(cwds) + session_cwds(manager, session),
                                           index))):
            if repo is None or cflow_landing.common_dir(c) == repo:
                out.append(Holder(c, sc, sc == cstore.main_scope(sc), "v2"))
        for c, run in v1_landing_holders(session):
            if repo is None or cflow_landing.common_dir(c) == repo:
                out.append(Holder(c, run, run is None, "v1"))
        return sorted(out, key=lambda h: (h.cwd, h.run or ""))
    return holders


def push_landing(manager: Any, mesh: Any, *, cwd: str, session: str, target: str,
                 items: Sequence[Dict[str, Any]], cwds: Sequence[str],
                 tid: Optional[str] = None) -> Result:
    """A landing request (``parent:landing`` or ``leader:landing``) onto the
    queue :class:`.landing_route.Router` names: for ``parent`` the nearest
    direct ancestor that can merge, else the mesh leader beside the worker
    or one of its ancestors; for
    ``leader`` the mesh leader only. The detail says who was passed over."""
    from .landing_route import Router

    router = Router(manager, mesh, landing_holders(manager, cwds))
    found = router.independent(cwd, session) if target == "leader" else router.stacked(cwd, session)
    if found.holder is None:
        log.warning("cflow2 enqueue-landing from %s: no merge target: %s", session, found.why)
        return False, found.why
    h = found.holder
    if h.version == "v2":
        ok, detail = push_v2((h.cwd, str(h.run)), LANDING, items, session, tid=tid)
    else:
        ok, detail = push_v1((h.cwd, h.run), str(found.owner), items, session)
    return ok, found.note() + detail


# -- one trigger ----------------------------------------------------------------


def _driver(cwd: str, scope: str) -> str:
    try:
        from ..cflow2 import store as cstore

        return runtime.acting_session(cstore.RunStore(cwd, scope).load_state()) or ""
    except Exception:
        return ""


async def perform(manager: Any, cwd: str, scope: str, trig: Dict[str, Any], *,
                  cwds: Sequence[str] = (),
                  facts_fn: Optional[Callable[[str, str], Dict[str, Any]]] = None,
                  driver: Optional[str] = None, mesh: Any = None) -> Result:
    """Perform one claimed trigger of the v2 run in (cwd, scope).
    ``(performed, detail)``; never raises for an ordinary failure."""
    from . import cflow_clock

    do = str(trig.get("do") or "")
    if driver is None:
        driver = await asyncio.to_thread(_driver, cwd, scope)
    if do == DO_CHECKS and driver:
        # A held driver is asleep, not gone: the request wakes it and is
        # typed after the respawn (claunch-t1r9j).
        try:
            held = manager.get(driver)
        except Exception:
            held = None
        if held is not None and cflow_clock.is_held(held):
            return await cflow_clock.request_checks(
                held, cflow_clock.waking_deliver(manager, driver, by="cflow2-checks"))
    if do in (DO_BRIEFING, DO_CHECKS):
        session = live_session(manager, driver, include_held=do == DO_CHECKS)
        if session is None:
            return False, (f"the driver session {driver!r} is not live" if driver
                           else "the run has no driver session")
        if do == DO_CHECKS:
            async def deliver(block: str) -> bool:
                try:
                    return bool(session.queue_delivery(block))
                except Exception:
                    log.exception("cflow2 trigger delivery to %r failed", driver)
                    return False
            return await cflow_clock.request_checks(session, deliver)
        return await cflow_clock.recompose_briefing(session)
    if do in (DO_PUSH, DO_ENQUEUE_LANDING):
        if not driver:
            return False, "the run has no driver session"
        push = ENQUEUE_LANDING_PUSH if do == DO_ENQUEUE_LANDING else (trig.get("push") or {})
        return await asyncio.to_thread(
            _push, manager, cwd, driver, push, do == DO_ENQUEUE_LANDING, list(cwds),
            facts_fn or session_facts(manager), trigger_tid(cwd, scope, trig),
            trig.get("data"), mesh)
    return False, f"this daemon has no trigger action named {do!r}"


def trigger_tid(cwd: str, scope: str, trig: Dict[str, Any]) -> str:
    """A trigger's name across runs: its run id (or slot) and its id."""
    return f"{trig.get('run') or f'{cwd}|{scope}'}/{trig.get('id') or ''}"


def _push(manager: Any, cwd: str, session: str, push: Dict[str, Any], landing: bool,
          cwds: List[str], facts_fn: Callable[[str, str], Dict[str, Any]],
          tid: Optional[str] = None, data: Optional[Dict[str, Any]] = None,
          mesh: Any = None) -> Result:
    target, name = split_addr(push.get("to"))
    if target not in ("parent", "leader") or not name:
        return False, ("a trigger pushes to 'parent:<collection>' or 'leader:<collection>', "
                       f"not {push.get('to')!r}")
    facts = facts_fn(cwd, session)
    if landing:
        # The v1 trigger's reasons, word for word where they apply.
        if not facts.get("head"):
            return False, f"no commit readable in {cwd}"
        if facts.get("in_review") is None:
            return False, "the issue board could not be read"
        if not facts.get("in_review"):
            return False, f"{session} holds no issue in_review (no landing request on the board)"
        refused = landing_refusal(facts)
        if refused:
            log.warning("cflow2 enqueue-landing from %s refused: %s", session, refused)
            return False, refused
    try:
        items = expand_push(push, facts, data)
    except PushError as exc:
        return False, str(exc)
    if name == LANDING:
        return push_landing(manager, mesh, cwd=cwd, session=session, target=target,
                            items=items, cwds=cwds, tid=tid)
    to = push_to_leader if target == "leader" else push_to_parent
    return to(manager, session=session, name=name, items=items, cwds=cwds, tid=tid)


async def perform_bounded(manager: Any, cwd: str, scope: str, trig: Dict[str, Any],
                          timeout: float = ACTION_TIMEOUT, **kw: Any) -> Result:
    """:func:`perform` with an upper bound and every exception turned into a
    reason, for the clock."""
    try:
        return await asyncio.wait_for(perform(manager, cwd, scope, trig, **kw), timeout)
    except asyncio.TimeoutError:
        return False, f"the {trig.get('do')!r} action did not finish within {timeout:g}s"
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - a trigger never fails the clock
        log.exception("cflow2 trigger %s of %s/%s failed", trig.get("do"), cwd, scope)
        return False, f"error: {exc}"
