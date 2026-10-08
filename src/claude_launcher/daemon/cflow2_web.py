"""The daemon's reading of cflow v2 for its HTTP surfaces (``docs/cflow-v2.md`` 9-10).

Kept apart from ``api.py`` so the handlers there stay one call each, and so
the onboarding path (``onboard.preflight``) and the API decide the same way
which runtime a requested workflow belongs to:

- :func:`startable` -- the v2 half of ``GET /api/cflow/workflows``.
- :func:`choose` -- v1 or v2 for a spawn/start request, and the compiled
  chart when it is v2.
- :func:`chart_view`, :func:`run_detail`, :func:`run_summary`,
  :func:`definition_view` -- what the dashboard draws.

Name collisions: while v1 and v2 run side by side a name may be declared in
both formats. v1 wins unless the request says ``workflow_version: 2`` (or
spells the name ``v2:<name>``); a name declared only in v2 needs neither.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..cflow import state as cflow_state
from ..cflow2 import ir, layers
from ..cflow2 import store as store2

#: Carried by every v1 entry of the workflow list and shown beside v1 names.
DEPRECATED_NOTE = (
    "cflow v1 format is deprecated; v2 runs beside it (docs/cflow-v2.md)"
)

#: Where a session may write a v2 workflow for a child (doc section 1).
GENERATED_V2 = Path(store2.BASE_DIR) / "generated"

#: ``v2:<name>`` -- the spelling a picker uses for a v2 entry whose name a v1
#: workflow also declares; the daemon reads it as ``workflow_version: 2``.
V2_PREFIX = "v2:"

JOURNAL_TAIL = 200

_FORMAT_RE = re.compile(r"^cflow\s*:\s*['\"]?2['\"]?\s*(#.*)?$", re.MULTILINE)

#: cwd -> (stamp, entries)
_LISTS: Dict[str, Tuple[tuple, List[dict]]] = {}
#: cwd -> (stamp, rows) for :func:`definitions`
_DEFS: Dict[str, Tuple[tuple, List[dict]]] = {}

log = logging.getLogger(__name__)


class ChoiceError(ValueError):
    """A request naming a v2 workflow that cannot be honoured."""


class InputsInvalid(ChoiceError):
    """``cflow_inputs`` that do not satisfy the chart's ``inputs`` schema;
    ``errors`` maps each input (or unknown key) to why -- the 400 body
    carries it so a form can mark the field."""

    def __init__(self, message: str, errors: Dict[str, str]) -> None:
        super().__init__(message)
        self.errors = dict(errors)


# --------------------------------------------------------------------------- #
# listing
# --------------------------------------------------------------------------- #
def v1_marks(entry: dict) -> dict:
    """The version fields a v1 entry of the workflow list carries."""
    entry["version"] = 1
    entry["deprecated"] = True
    entry["format_deprecated"] = True
    entry["deprecated_note"] = DEPRECATED_NOTE
    return entry


def _stamp(cwd: str) -> tuple:
    out = []
    for _layer, base in layers.search_dirs(cwd):
        try:
            names = sorted(p for p in os.listdir(base) if p.endswith((".yaml", ".yml")))
        except OSError:
            out.append((str(base), None))
            continue
        for name in names:
            path = os.path.join(str(base), name)
            try:
                st = os.stat(path)
            except OSError:
                out.append((path, None))
            else:
                out.append((path, st.st_mtime_ns, st.st_size))
    return tuple(out)


def _entry(item: "layers.Located2", cwd: str) -> Optional[dict]:
    entry: Dict[str, Any] = {
        "name": item.name,
        "path": str(item.path),
        "origin": item.layer,
        "shadowed": [str(p) for p in item.shadows],
        "version": 2,
        "deprecated": False,
    }
    if item.error:
        entry["error"] = item.error
        return entry
    if item.kind in (ir.KIND_PROCEDURE, ir.KIND_LIBRARY):
        return None  # called or included by other definitions, never started on its own
    try:
        chart = layers.load_file(item.path, cwd)
    except Exception as exc:  # noqa: BLE001 - a listing must survive any file
        entry["error"] = str(exc) or type(exc).__name__
        return entry
    meta = chart.meta or {}
    entry.update(
        description=chart.description,
        kind=chart.kind,
        steps=len(chart.tasks()),
        delegations=len(chart.delegation_points()),
        default_role=meta.get("default_role"),
        default_child_cflow=meta.get("default_child_cflow"),
        priority=meta.get("priority") or 0,
        filter_roles=meta.get("filter_roles"),
        recur=False,
        # the start form draws its fields from this (docs/cflow-v2.md 2.6)
        inputs=_inputs_view(chart),
        input_constraints=_constraints_view(chart),
    )
    return entry


def startable(cwd: str) -> List[dict]:
    """The v2 workflows startable in ``cwd`` (procedures left out), cached
    against every file in the three v2 layer directories -- an ``include`` is
    resolved by name from the same directories, so that set covers it."""
    stamp = _stamp(cwd)
    hit = _LISTS.get(cwd)
    if hit is not None and hit[0] == stamp:
        return hit[1]
    out = []
    for item in layers.list_definitions(cwd):
        entry = _entry(item, cwd)
        if entry is not None:
            out.append(entry)
    _LISTS[cwd] = (stamp, out)
    return out


def v2_names(cwd: str) -> List[str]:
    return [e["name"] for e in startable(cwd) if not e.get("error")]


def v1_names(cwd: str) -> List[str]:
    return [name for name, _ in cflow_state.list_workflows(cwd or None)]


# --------------------------------------------------------------------------- #
# which runtime a request means
# --------------------------------------------------------------------------- #
def declares_v2(path: Path) -> bool:
    """Whether a file's text carries the ``cflow: 2`` format key."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return bool(_FORMAT_RE.search(text))


def _same(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path.absolute()


def generated_dirs(cwd: Optional[str], roots: Sequence[Optional[str]] = ()) -> List[Path]:
    out: List[Path] = []
    for base in (cwd, *roots):
        if not base:
            continue
        home = _same(Path(cflow_state.resolve_cwd(base)) / GENERATED_V2)
        if home not in out:
            out.append(home)
    return out


def _requested_version(version: Any) -> Optional[int]:
    if version in (None, ""):
        return None
    try:
        value = int(version)
    except (TypeError, ValueError):
        raise ChoiceError(f"workflow_version must be 1 or 2, not {version!r}") from None
    if value not in (1, 2):
        raise ChoiceError(f"workflow_version must be 1 or 2, not {version!r}")
    return value


def choose(
    workflow: str,
    version: Any = None,
    *,
    cwd: str,
    roots: Sequence[Optional[str]] = (),
) -> Optional[Tuple[str, ir.Chart]]:
    """``(ref, chart)`` when ``workflow`` is a v2 request, ``None`` for v1.

    ``ref`` is what the run records: the name, or the resolved absolute path
    of a generated file. A path is v2 when it sits in a ``.cflow2/generated``
    directory of ``cwd`` or of one of ``roots`` (the spawning session's), or
    when it sits in v1's ``.cflow/generated`` and its text says ``cflow: 2``;
    any other path asked for as v2 is refused, the containment rule v1
    applies to its own generated directory. A name is v2 when only the v2
    layers declare it, or when ``version`` is 2.
    """
    workflow = str(workflow or "").strip()
    want = _requested_version(version)
    if workflow.startswith(V2_PREFIX):
        workflow = workflow[len(V2_PREFIX):]
        if want == 1:
            raise ChoiceError(f"{V2_PREFIX}{workflow} names a v2 workflow; workflow_version is 1")
        want = 2
    if not workflow or want == 1:
        return None
    if workflow.endswith((".yaml", ".yml")):
        path = Path(workflow).expanduser()
        if not path.is_absolute():
            path = Path(cflow_state.resolve_cwd(cwd)) / path
        path = _same(path)
        homes = generated_dirs(cwd, roots)
        if path.parent in homes:
            if not path.is_file():
                raise ChoiceError(f"generated workflow file not found: {path}")
            return str(path), _compile_path(path, cwd)
        v1_home = False
        try:
            cflow_state.generated_workflow(str(path), cwd=cwd or None, roots=tuple(roots))
            v1_home = True
        except Exception:  # noqa: BLE001 - not v1's either: decided below
            v1_home = False
        if v1_home and declares_v2(path):
            return str(path), _compile_path(path, cwd)
        if want == 2 or (path.is_file() and declares_v2(path)):
            raise ChoiceError(
                f"{workflow!r} is outside the generated workflow directory -- a spawn may "
                f"name a v2 workflow file only from "
                f"{', '.join(str(h) for h in homes) or str(GENERATED_V2)}"
            )
        return None
    if want != 2:
        # Nothing in the request asked for v2: whatever goes wrong while the
        # v2 layers are consulted (an unreadable directory, a definition that
        # breaks the compiler) must not refuse a v1 spawn -- the request is
        # read as v1 and the v2 problem is logged.
        try:
            if workflow in v2_names(cwd) and workflow not in v1_names(cwd):
                return workflow, layers.load(workflow, cwd)
        except Exception as exc:  # noqa: BLE001 - see above
            log.warning("cflow v2: could not decide whether %r is a v2 workflow in %s; "
                        "treating it as v1: %s", workflow, cwd, exc)
        return None
    v2 = v2_names(cwd)
    if want == 2:
        if workflow not in v2:
            raise ChoiceError(
                f"no v2 workflow named {workflow!r} in {cwd or 'the daemon cwd'} -- "
                f"available: {', '.join(v2) or '(none)'}"
            )
        return workflow, layers.load(workflow, cwd)
    return None


def _compile_path(path: Path, cwd: str) -> ir.Chart:
    from ..cflow2 import frontend

    try:
        return layers.load_file(path, cwd)
    except (frontend.FrontendError, ir.IRError) as exc:
        raise ChoiceError(f"generated workflow {str(path)!r}: {exc}") from None


def load(ref: str, cwd: str) -> ir.Chart:
    """The chart a :func:`choose` ``ref`` names (compiled again at start)."""
    if ref.endswith((".yaml", ".yml")):
        return _compile_path(Path(ref), cwd)
    return layers.load(ref, cwd)


def check_params(chart: ir.Chart, params: Optional[dict]) -> dict:
    """The run's params as the runtime would take them, or :class:`ChoiceError`."""
    from ..cflow2 import runtime

    try:
        return runtime._validate_params(chart, params)  # noqa: SLF001 - same rule as start
    except runtime.FlowError as exc:
        raise ChoiceError(f"{chart.name}: {exc}") from None


def check_inputs(chart: ir.Chart, raw: Any) -> Optional[dict]:
    """The start inputs a request carries (``cflow_inputs``), normalised
    against the chart's ``inputs`` schema (``cflow2/inputs.py``); ``None``
    when the chart declares none and the request sent none. Raises
    :class:`InputsInvalid` with per-field errors."""
    from ..cflow2 import inputs as inputs_mod

    schema = (chart.meta or {}).get("inputs") or {}
    if raw is not None and not isinstance(raw, dict):
        raise InputsInvalid("cflow_inputs must be an object of name -> value",
                            {"_": "cflow_inputs must be an object"})
    if not schema and not raw:
        return None
    values, errors = inputs_mod.normalize(schema, raw or {},
                                          (chart.meta or {}).get("input_constraints"))
    if errors:
        raise InputsInvalid(
            f"{chart.name}: invalid inputs: " + "; ".join(f"{k}: {v}" for k, v in errors.items()),
            errors)
    return values


def start_run(chart: ir.Chart, *, inputs: Optional[dict] = None, **kwargs: Any) -> dict:
    """``runtime.start`` with the start inputs folded in. The runtime takes
    ``inputs=`` itself (it calls ``inputs.apply`` before the snapshot); a
    runtime without that keyword gets a chart already applied here."""
    import inspect

    from ..cflow2 import inputs as inputs_mod, runtime

    if inputs is not None:
        if "inputs" in inspect.signature(runtime.start).parameters:
            kwargs["inputs"] = inputs
        else:
            chart = inputs_mod.apply(chart, inputs)
    return runtime.start(chart, **kwargs)


def params_for(chart: ir.Chart, params: Any, context: Optional[str]) -> dict:
    """The params a request carries: its ``workflow_params`` mapping, plus
    ``context`` when the chart declares a param of that name."""
    out = dict(params) if isinstance(params, dict) else {}
    if context and "context" in (chart.params or {}) and "context" not in out:
        out["context"] = context
    return out


# --------------------------------------------------------------------------- #
# runs
# --------------------------------------------------------------------------- #
def live_run(cwd: str, scope: str) -> Optional[dict]:
    """The state of a live v2 run in (cwd, scope), or ``None``."""
    try:
        st = store2.RunStore(cwd, scope)
        if not st.exists():
            return None
        state = st.load_state()
    except Exception:  # noqa: BLE001 - an unreadable slot has no run to show
        return None
    from ..cflow2 import runtime

    return state if state.get("status") in runtime.LIVE_STATUSES else None


def has_run(cwd: str, scope: str) -> bool:
    try:
        return store2.RunStore(cwd, scope).exists()
    except Exception:  # noqa: BLE001
        return False


def paired_child(parent_cwd: str, parent: str) -> str:
    """``meta.default_child_cflow`` of the live v2 run ``parent`` drives."""
    if not parent_cwd or live_run(parent_cwd, parent) is None:
        return ""
    try:
        raw = json.loads(store2.RunStore(parent_cwd, parent).chart_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str(((raw or {}).get("meta") or {}).get("default_child_cflow") or "")


def node_view(node: ir.Node, prompts: bool = False) -> dict:
    out: Dict[str, Any] = {"id": node.id, "type": node.type}
    if node.parent is not None:
        out["parent"] = node.parent
    if node.label:
        out["label"] = node.label
    if node.get("title"):
        out["title"] = node.get("title")
    if node.executor:
        out["executor"] = list(node.executor)
    t = node.type
    if t == ir.T_LOOP:
        for key in ("mode", "max", "guard", "items", "parallel"):
            if node.get(key) is not None:
                out[key] = node.get(key)
    elif t == ir.T_CHOOSE:
        out["chooser"] = node.get("chooser") or ir.CHOOSER_AGENT
        out["options"] = [o.get("name") for o in node.get("options") or []]
    elif t in (ir.T_PAR, ir.T_GROUP):
        out["settle"] = node.get("settle")
        if t == ir.T_GROUP:
            out["limit"] = node.get("limit")
            out["name"] = node.get("name")
    elif t == ir.T_SCOPE:
        out["proc"] = node.get("proc")
    elif t == ir.T_CONTROL:
        out["op"] = node.get("op")
        if node.get("outcome"):
            out["outcome"] = node.get("outcome")
    elif t == ir.T_WAIT:
        out["describe"] = node.get("describe") or node.get("probe") or (
            "checks: " + ", ".join(c.get("id") or "" for c in node.get("checks"))
            if node.get("checks") else None) or (
            f"{node.get('seconds')}s" if node.get("seconds") else None)
    elif t == ir.T_TIMEOUT:
        out["seconds"] = node.get("seconds")
    if prompts and t == ir.T_TASK and node.get("prompt"):
        out["prompt"] = node.get("prompt")
    return out


def _child_roles(chart: ir.Chart, node: ir.Node) -> List[Tuple[str, str]]:
    """``(child id, role)`` in display order -- the CLI's tree walk."""
    t = node.type
    out: List[Tuple[str, str]] = []
    if t == ir.T_SEQ:
        out += [(c, "") for c in node.get("children") or []]
    elif t == ir.T_COND:
        out += [(b["child"], f"if {b['guard']}" if b.get("guard") else "else")
                for b in node.get("branches") or []]
    elif t == ir.T_CHOOSE:
        out += [(o["child"], f"option {o['name']}") for o in node.get("options") or []
                if o.get("child")]
    elif t == ir.T_PAR:
        out += [(c, f"branch {n}") for n, c in (node.get("branches") or {}).items()]
    elif t == ir.T_LOOP:
        out.append((node.get("child"), "do"))
        if node.get("between"):
            out.append((node.get("between"), "between"))
    elif t == ir.T_TIMEOUT:
        out.append((node.get("child"), "do"))
        if node.get("else_child"):
            out.append((node.get("else_child"), "else"))
    elif t == ir.T_TRY:
        out.append((node.get("child"), "try"))
        out += [(c, f"catch {oc}") for oc, c in (node.get("catch") or {}).items()]
        if node.get("finally_child"):
            out.append((node.get("finally_child"), "finally"))
    elif t == ir.T_GROUP:
        out.append((node.get("child"), "do"))
        out.append((node.get("member"), "member"))
    elif t == ir.T_SCOPE:
        out.append((node.get("child"), ""))
    out += [(c, f"on {oc}") for oc, c in (node.on or {}).items()]
    return [(c, r) for c, r in out if c]


def chart_view(chart: ir.Chart, prompts: bool = False) -> dict:
    """The IR as the dashboard draws it: every node's display fields, and
    each node's children in order with the role each plays (``do``,
    ``option x``, ``catch fail``...). Prompts only when asked for."""
    nodes = {}
    for nid, node in chart.nodes.items():
        view = node_view(node, prompts)
        view["children"] = [c for c, _ in _child_roles(chart, node)]
        view["roles"] = [r for _, r in _child_roles(chart, node)]
        nodes[nid] = view
    meta = chart.meta or {}
    return {
        "name": chart.name,
        "kind": chart.kind,
        "description": chart.description,
        "source": chart.source,
        "root": chart.root,
        "meta": dict(meta),
        "params": chart.params,
        "warnings": list(chart.warnings),
        "nodes": nodes,
    }


def fallback_text(ev: dict) -> str:
    """One journal ``fallback``/``delegate_failed`` event as a line."""
    why = ev.get("cause") or ev.get("reason") or ev.get("error") or ""
    route = ev.get("route")
    decision = ev.get("decision")
    text = (f"{decision}: " if decision else "") + str(why)
    if route:
        text += f" (route {route})"
    return text.strip() or str(ev.get("event"))


def _fallbacks(journal: List[dict]) -> Dict[str, List[str]]:
    """Delegation id, instance or node -> the fallback reasons the journal
    recorded (a fallback decided before the delegation existed is keyed by
    the instance it was decided for)."""
    out: Dict[str, List[str]] = {}
    for ev in journal:
        if ev.get("event") not in ("fallback", "delegate_failed"):
            continue
        key = str(ev.get("delegation") or ev.get("instance") or ev.get("node") or "")
        out.setdefault(key, []).append(fallback_text(ev))
    return out


def delegations(state: dict, journal: List[dict]) -> List[dict]:
    reasons = _fallbacks(journal)
    out = []
    for did, rec in (state.get("delegations") or {}).items():
        entry = {k: rec.get(k) for k in ("id", "node", "label", "executor", "status",
                                         "receipt", "started", "finished", "errors")}
        entry["id"] = entry.get("id") or did
        route = rec.get("route")
        entry["route"] = route.get("route") if isinstance(route, dict) else route
        entry["degraded"] = list((route or {}).get("degraded") or []) \
            if isinstance(route, dict) else []
        entry["fallbacks"] = reasons.get(did, []) + reasons.get(str(rec.get("instance") or ""), [])
        out.append(entry)
    return out


def run_detail(cwd: str, scope: str, prompts: bool = False) -> dict:
    """``GET /api/cflow2/run``: the run, its chart, the journal tail and the
    delegation rows."""
    from ..cflow2 import runtime

    st = store2.RunStore(cwd, scope)
    if not st.exists():
        return {"cwd": str(st.cwd), "scope": st.scope, "status": "idle", "run": None}
    payload = runtime.status(cwd=str(st.cwd), scope=st.scope, tick=False)
    state = st.load_state()
    chart = st.load_chart()
    journal = st.journal(JOURNAL_TAIL)
    inst = state.get("inst") or {}
    return {
        "cwd": str(st.cwd),
        "scope": st.scope,
        "status": state.get("status"),
        "run": payload,
        "chart": chart_view(chart, prompts),
        # Which nodes have a live instance (the active configuration and its
        # ancestors), and which of those wait on the outside.
        "instances": [
            {"instance": iid, "node": rec.get("node"), "await": rec.get("await")}
            for iid, rec in sorted(inst.items())
        ],
        "visits": state.get("visits") or {},
        "journal": journal,
        "delegations": delegations(state, journal),
    }


def run_summary(cwd: str, scope: str) -> Optional[dict]:
    """One line's worth of a v2 run in (cwd, scope), or ``None`` without one."""
    try:
        st = store2.RunStore(cwd, scope)
        if not st.exists():
            return None
        state = st.load_state()
        chart = st.load_chart()
    except Exception as exc:  # noqa: BLE001 - shown, not raised
        return {"cwd": cwd, "scope": scope, "status": "error", "error": str(exc), "version": 2}
    labels = []
    for iid in state.get("active") or []:
        rec = (state.get("inst") or {}).get(iid) or {}
        try:
            node = chart.node(rec.get("node"))
        except Exception:  # noqa: BLE001
            continue
        labels.append(node.get("title") or node.label or node.id)
    return {
        "version": 2,
        "cwd": str(st.cwd),
        "scope": st.scope,
        "run": state.get("run_id"),
        "workflow": state.get("workflow"),
        "status": state.get("status"),
        "reason": state.get("reason"),
        "started": state.get("started"),
        "updated": state.get("updated"),
        "current": labels,
        "delegations": sum(1 for d in (state.get("delegations") or {}).values()
                           if d.get("status") in ("pending", "queued", "running")),
    }


def list_runs(cwds: Sequence[str]) -> List[dict]:
    out = []
    seen = set()
    for cwd in cwds:
        if not cwd:
            continue
        try:
            resolved = str(store2.resolve_cwd(cwd))
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        for scope in store2.list_scopes(resolved):
            entry = run_summary(resolved, scope)
            if entry is not None:
                out.append(entry)
    return out


def rail_entry(cwd: str, scope: str) -> Optional[dict]:
    """The session rail's line for the v2 run a session drives: the run in
    the session's own directory under the session's own name, or ``None``.

    Kept apart from :func:`run_summary` (the Flows list) and from the v1
    badge: the rail asks a narrower question -- does this run want the
    person reading the rail -- and answers it with ``gated`` and ``owed``.
    ``owed`` names what only a person releases: the holds that put the run
    in ``waiting_approval`` (a loop or visit limit, a halt, an ask that fell
    to a human) and every open choice whose chooser is ``user``."""
    from ..cflow2 import runtime

    try:
        st = store2.RunStore(cwd, scope)
        if not st.exists():
            return None
        state = st.load_state()
        chart = st.load_chart()
    except Exception as exc:  # noqa: BLE001 - shown, not raised
        return {"cwd": cwd, "scope": scope, "status": "error", "error": str(exc),
                "workflow": None, "current": [], "owed": [], "gated": False}
    inst = state.get("inst") or {}
    current = []
    owed = []
    for iid in state.get("active") or []:
        rec = inst.get(iid) or {}
        try:
            node = chart.node(rec.get("node"))
        except Exception:  # noqa: BLE001
            continue
        current.append(node.get("title") or node.label or node.id)
    for iid, rec in sorted(inst.items()):
        if rec.get("await") != runtime.AW_CHOOSE:
            continue
        try:
            node = chart.node(rec.get("node"))
        except Exception:  # noqa: BLE001
            continue
        if (node.get("chooser") or ir.CHOOSER_AGENT) == ir.CHOOSER_USER:
            owed.append(f"choose: {node.get('title') or node.label or node.id}")
    status = state.get("status")
    if status == runtime.ST_WAITING:
        for a in state.get("approvals") or []:
            kind = a.get("kind")
            if kind == "ask" and a.get("resolution") != runtime.RES_HUMAN:
                continue
            if kind == runtime.HOLD_HALT:
                owed.append(f"halted: {a.get('monitor')}")
            else:
                owed.append(f"{kind}: {a.get('label') or a.get('node') or a.get('instance')}")
    return {
        "cwd": str(st.cwd),
        "scope": st.scope,
        "run": state.get("run_id"),
        "workflow": state.get("workflow"),
        "status": status,
        "reason": state.get("reason"),
        "round": state.get("round"),
        "current": current,
        "owed": owed,
        "gated": bool(owed),
    }


#: ``(cwd, scope) -> (stamp, entry)``. The rail polls every few seconds for
#: every live session, and a run's chart snapshot runs to hundreds of KB, so
#: an entry is recomputed only when its state or chart file changed.
_RAIL: Dict[Tuple[str, str], Tuple[Any, Optional[dict]]] = {}


def _rail_stamp(cwd: str, scope: str) -> Any:
    st = store2.RunStore(cwd, scope)
    out = []
    for path in (st.state_path, st.chart_path):
        try:
            info = path.stat()
        except OSError:
            out.append(None)
            continue
        out.append((info.st_mtime_ns, info.st_size))
    return tuple(out)


def rail_entries(sessions: Sequence[Tuple[str, str]]) -> Dict[str, dict]:
    """``GET /api/cflow2/rail``: ``{session: rail_entry}`` for each
    ``(session, cwd)`` that drives a v2 run; sessions without one are left
    out."""
    out: Dict[str, dict] = {}
    for name, cwd in sessions:
        if not name or not cwd:
            continue
        key = (cwd, name)
        try:
            stamp = _rail_stamp(cwd, name)
        except (OSError, ValueError):
            continue
        if stamp[0] is None:
            _RAIL.pop(key, None)
            continue
        hit = _RAIL.get(key)
        if hit is not None and hit[0] == stamp:
            entry = hit[1]
        else:
            entry = rail_entry(cwd, name)
            _RAIL[key] = (stamp, entry)
        if entry is not None:
            out[name] = dict(entry)
    return out


def definition_view(name: str, cwd: str) -> dict:
    """``GET /api/cflow2/definition``: the compiled tree, the static check's
    findings and the source text of one v2 definition (read-only)."""
    from ..cflow2 import check

    if not layers.is_name(name):
        raise ChoiceError(f"{name!r} is not a definition name; the definition view reads "
                          f"v2 definitions by name from the layers only")
    path = layers.find(name, cwd)
    if path is None:
        raise ChoiceError(f"no v2 definition named {name!r}")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ChoiceError(f"{path}: {exc}") from None
    out: Dict[str, Any] = {"name": name, "path": str(path), "text": text, "version": 2}
    try:
        chart = layers.load_file(path, cwd)
    except Exception as exc:  # noqa: BLE001 - the error is the answer
        out["error"] = str(exc) or type(exc).__name__
        return out
    out["chart"] = chart_view(chart, prompts=False)
    out["inputs"] = _inputs_view(chart)
    out["input_constraints"] = _constraints_view(chart)
    out["findings"] = [
        {"level": f.level, "code": f.code, "message": f.message, "node": f.node}
        for f in check.check(chart)
    ]
    out["delegations"] = [n.id for n in chart.delegation_points()]
    return out


def _constraints_view(chart: ir.Chart) -> List[dict]:
    """The cross-input asserts (``input_constraints``) a form shows beside
    the fields; the server evaluates them (:func:`inputs_check`)."""
    import copy as _copy

    return _copy.deepcopy((chart.meta or {}).get("input_constraints") or [])


def inputs_check(chart: ir.Chart, raw: Any) -> dict:
    """What the start form asks while the user types: ``{ok, values,
    errors}`` -- the same :func:`check_inputs` a start runs, answered
    instead of raised."""
    try:
        values = check_inputs(chart, raw)
    except InputsInvalid as exc:
        return {"ok": False, "values": None, "errors": exc.errors}
    return {"ok": True, "values": values, "errors": {}}


def _inputs_view(chart: ir.Chart) -> List[dict]:
    from ..cflow2 import inputs as inputs_mod

    return inputs_mod.view((chart.meta or {}).get("inputs") or {})


def definitions(cwd: str) -> List[dict]:
    """The v2 rows of the definition editor's picker (procedures included),
    cached against the layer directories like :func:`startable` -- listing
    compiles every file. Call it off the event loop."""
    stamp = _stamp(cwd)
    hit = _DEFS.get(cwd)
    if hit is not None and hit[0] == stamp:
        return [dict(row) for row in hit[1]]
    rows = [
        {"name": i.name, "layer": i.layer, "path": str(i.path), "version": 2,
         "kind": i.kind, "error": i.error, "active": True}
        for i in layers.list_definitions(cwd)
    ]
    _DEFS[cwd] = (stamp, rows)
    return [dict(row) for row in rows]
