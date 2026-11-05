"""Fleet presets: a reusable shape of agent tree, expanded into a work spec.

A *fleet preset* names a tree of agent groups -- a root (usually an
``improv-coordinator-v2`` coordinator) and, under it, groups that repeat
``count`` times, each instance carrying its own copy of the groups below it.
The pattern it was made for::

    coordinator (improv-coordinator-v2)
      lead x n   (improv-worker-v2, relation independent)  -- a sub-lead each
        worker x m (improv-worker-v2, relation stacked)    -- under every lead

A preset holds no session, workspace or task. The dashboard's Fleet page
(``#/fleet``) asks for those at launch -- project, workspace, mesh, the
coordinator's opening task, the counts and each member's profile -- and
:func:`expand` turns preset + launch into a **work spec** document
(:mod:`claude_launcher.workspec`). Launching is that spec's import, so a fleet
is checked, previewed and planned by the code that runs ``claunch spec``, and
the same document can be opened on the Spec page (``#/spec``) to edit by hand.

Board alignment (``issue`` of a group): the root gets an epic from the
opening task. ``lane`` gives each instance an issue under its parent's issue
(an epic for a group that holds children), so the board's parent-child tree
mirrors the fleet from the first minute. ``pending`` starts the member with
no issue: its parent creates the real job under its own issue and names it
(``existing-pending``, claunch-5p2j4). ``none`` starts it with no issue at all.

Presets live as YAML files in ``<launcher home>/fleet-presets/<name>.yaml``;
:data:`BUILTIN` ships the one above. A user file of the same name replaces
the built-in. The format is ``docs/fleet.md``.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import yaml

from . import workspec

FLEET_VERSION = 1
#: Agent kinds a group may have: the work spec's.
KINDS = tuple(workspec.KINDS)
#: How a non-root member starts on the board (see the module docstring).
ISSUE_MODES = ("lane", "pending", "none")
#: Upper bounds that keep one launch from flooding the daemon by a typo.
MAX_COUNT = 12
MAX_AGENTS = 40
#: spawn.max_depth's default -- the root is depth 0.
MAX_DEPTH = workspec.MAX_DEPTH

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
_PARAM = re.compile(r"^[a-z][a-z0-9_]{0,23}$")
_TOP = {"fleet", "name", "title", "description", "params", "root"}
_NODE = {"id", "kind", "label", "count", "handle", "workflow", "inputs",
         "issue", "profile", "tier", "model", "task", "children"}
_PARAM_KEYS = {"label", "default", "min", "max"}
_HANDLE_VARS = re.compile(r"\{(\w*)\}")


class FleetError(Exception):
    """A preset that cannot be read or saved, or a launch that cannot expand."""


DEFAULT_NAME = "coordinator-leads-workers"

#: The built-in preset: the coordinator over independent leads, each with
#: stacked workers. ``tests/test_fleet.py`` holds it valid.
BUILTIN: Dict[str, str] = {DEFAULT_NAME: """\
fleet: 1
name: coordinator-leads-workers
title: 코디네이터 → 독립 리드 n → 스택 워커 n×m
description: |
  improv-coordinator-v2가 사용자와 요구를 조율하고, 리드(improv-worker-v2,
  relation independent)마다 lane epic을 맡긴다. 리드는 자기 아래 워커
  (improv-worker-v2, relation stacked)의 결과를 <리드>-stack으로 모아
  리더의 착지 큐에 올린다. 워커는 리드가 일감 이슈를 줄 때까지 대기한다.
params:
  leads: {label: 리드 수, default: 2, min: 1, max: 6}
  workers: {label: 리드당 워커 수, default: 2, min: 0, max: 6}
root:
  id: coord
  kind: coordinator
  label: 코디네이터
  handle: coord
  workflow: improv-coordinator-v2
  children:
    - id: lead
      kind: sub-lead
      label: 리드
      count: leads
      handle: "lead{n}"
      workflow: improv-worker-v2
      inputs: {relation: independent}
      issue: lane
      children:
        - id: w
          kind: worker
          label: 워커
          count: workers
          handle: "{parent}-w{n}"
          workflow: improv-worker-v2
          inputs: {relation: stacked}
          issue: pending
"""}


# --------------------------------------------------------------------------- #
# reading and validating
# --------------------------------------------------------------------------- #

def load(text: str) -> dict:
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise FleetError(f"not valid YAML: {exc}") from None
    if not isinstance(doc, dict):
        raise FleetError("a fleet preset must be a mapping")
    return doc


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def validate(doc: dict) -> List[str]:
    """Every problem in ``doc``; an empty list means it can expand."""
    if not isinstance(doc, dict):
        return ["preset: must be a mapping"]
    errs: List[str] = []
    extra = sorted(set(doc) - _TOP)
    if extra:
        errs.append(f"preset: unknown key(s) {', '.join(extra)}")
    if doc.get("fleet") != FLEET_VERSION:
        errs.append(f"fleet: version must be {FLEET_VERSION}")
    if not _SLUG.match(str(doc.get("name") or "")):
        errs.append("name: lowercase slug [a-z0-9-] required (32 chars at most)")
    for key in ("title", "description"):
        if doc.get(key) is not None and not isinstance(doc[key], str):
            errs.append(f"{key}: must be a string")

    params = doc.get("params") or {}
    if not isinstance(params, dict):
        errs.append("params: must be a mapping name -> {label, default, min, max}")
        params = {}
    for name, p in params.items():
        where = f"params.{name}"
        if not _PARAM.match(str(name)):
            errs.append(f"{where}: name must be [a-z][a-z0-9_]*")
        if not isinstance(p, dict):
            errs.append(f"{where}: must be a mapping")
            continue
        bad = sorted(set(p) - _PARAM_KEYS)
        if bad:
            errs.append(f"{where}: unknown key(s) {', '.join(bad)}")
        lo, hi = p.get("min", 0), p.get("max", MAX_COUNT)
        if not (_is_int(p.get("default")) and _is_int(lo) and _is_int(hi)):
            errs.append(f"{where}: default, min and max must be integers")
        elif not 0 <= lo <= p["default"] <= hi <= MAX_COUNT:
            errs.append(f"{where}: needs 0 <= min <= default <= max <= {MAX_COUNT}")

    root = doc.get("root")
    if not isinstance(root, dict):
        errs.append("root: the tree's root group is required")
        return errs
    ids: set = set()

    def walk(node, path: str, depth: int, parent_kind: str,
             parent_issue: bool) -> None:
        if not isinstance(node, dict):
            errs.append(f"{path}: must be a mapping")
            return
        nid = str(node.get("id") or "")
        where = f"{path}({nid})" if nid else path
        bad = sorted(set(node) - _NODE)
        if bad:
            errs.append(f"{where}: unknown key(s) {', '.join(bad)}")
        if not _SLUG.match(nid):
            errs.append(f"{where}.id: lowercase slug required")
        elif nid in ids:
            errs.append(f"{where}.id: duplicate group id {nid!r}")
        ids.add(nid)
        for key in ("label", "handle", "workflow", "profile", "tier", "model", "task"):
            if node.get(key) is not None and not isinstance(node[key], str):
                errs.append(f"{where}.{key}: must be a string")
        kind = node.get("kind", "worker")
        if kind not in KINDS:
            errs.append(f"{where}.kind: one of {', '.join(KINDS)}")
            kind = "worker"
        if depth == 0:
            if kind == "reviewer":
                errs.append(f"{where}.kind: the root cannot be a reviewer")
            if node.get("count") not in (None, 1):
                errs.append(f"{where}.count: the root is one session")
        else:
            if parent_kind not in workspec.HOLDERS:
                errs.append(f"{where}: a {parent_kind} holds no children "
                            f"(only {', '.join(k for k in workspec.HOLDERS if k != 'leader')})")
            count = node.get("count", 1)
            if isinstance(count, str):
                if count not in params:
                    errs.append(f"{where}.count: unknown param {count!r}")
            elif not (_is_int(count) and 0 <= count <= MAX_COUNT):
                errs.append(f"{where}.count: an integer 0-{MAX_COUNT} or a param name")
        if depth > MAX_DEPTH:
            errs.append(f"{where}: depth {depth} exceeds spawn.max_depth {MAX_DEPTH}")
        if node.get("inputs") is not None and not isinstance(node["inputs"], dict):
            errs.append(f"{where}.inputs: must be a mapping")
        mode = node.get("issue", "pending" if depth else "lane")
        if depth and mode not in ISSUE_MODES:
            errs.append(f"{where}.issue: one of {', '.join(ISSUE_MODES)}")
        if depth and mode == "lane" and not parent_issue:
            errs.append(f"{where}.issue: lane needs its parent to hold an issue "
                        "(the root, or a lane group)")
        if kind == "reviewer" and depth and mode != "none" and "issue" in node:
            errs.append(f"{where}.issue: a reviewer holds no issue (use none)")
        handle = node.get("handle")
        if isinstance(handle, str):
            for var in _HANDLE_VARS.findall(handle):
                if var not in ("n", "parent", "id"):
                    errs.append(f"{where}.handle: unknown placeholder {{{var}}} "
                                "(use {n}, {parent}, {id})")
        kids = node.get("children") or []
        if not isinstance(kids, list):
            errs.append(f"{where}.children: must be a list")
            return
        holds = depth == 0 or mode == "lane"
        for i, child in enumerate(kids):
            walk(child, f"{where}.children[{i}]", depth + 1, kind, holds)

    walk(root, "root", 0, "", True)
    if not errs:
        try:
            n = len(_instances(doc, {}))
        except FleetError as exc:
            errs.append(str(exc))
        else:
            if n > MAX_AGENTS:
                errs.append(f"root: the defaults start {n} sessions, more than "
                            f"{MAX_AGENTS}")
    return errs


def params_of(doc: dict, given: Optional[dict] = None) -> Dict[str, int]:
    """The launch's counts: each param's default, overridden by ``given``
    (clamped to the param's min..max; a non-integer is refused)."""
    out: Dict[str, int] = {}
    given = given or {}
    for name, p in (doc.get("params") or {}).items():
        value = given.get(name, p.get("default", 0))
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            value = int(value)
        if not _is_int(value):
            raise FleetError(f"param {name}: an integer is required")
        out[name] = max(p.get("min", 0), min(p.get("max", MAX_COUNT), value))
    unknown = sorted(set(given) - set(out))
    if unknown:
        raise FleetError(f"unknown param(s) {', '.join(unknown)}")
    return out


def _default_handle(depth: int) -> str:
    return "{id}" if depth == 0 else "{id}{n}" if depth == 1 else "{parent}-{id}{n}"


def _instances(doc: dict, given: dict) -> List[dict]:
    """The members a launch starts, parents first: each a concrete agent
    with its group, index and parent handle."""
    params = params_of(doc, given)
    out: List[dict] = []
    seen: set = set()

    def walk(node: dict, depth: int, parent: Optional[dict]) -> None:
        count = node.get("count", 1) if depth else 1
        if isinstance(count, str):
            count = params[count]
        for n in range(1, count + 1):
            template = node.get("handle") or _default_handle(depth)
            handle = template.format(n=n, id=node["id"],
                                     parent=parent["handle"] if parent else "")
            if not _SLUG.match(handle):
                raise FleetError(f"group {node['id']}: handle {handle!r} is not a "
                                 "lowercase slug -- fix the handle template")
            if handle in seen:
                raise FleetError(f"group {node['id']}: handle {handle!r} repeats -- "
                                 "put {n} (and {parent} below the first level) in "
                                 "the handle template")
            seen.add(handle)
            me = {"handle": handle, "group": node["id"], "n": n, "depth": depth,
                  "node": node, "parent": parent["handle"] if parent else None}
            out.append(me)
            for child in node.get("children") or []:
                walk(child, depth + 1, me)

    walk(doc["root"], 0, None)
    return out


def shape(doc: dict, given: Optional[dict] = None) -> List[dict]:
    """The members a valid preset starts for these counts, without a task or
    profiles: what the editor draws while a preset is being written."""
    return [{"handle": m["handle"], "group": m["group"], "n": m["n"],
             "depth": m["depth"], "parent": m["parent"],
             "kind": m["node"].get("kind", "worker"),
             "label": m["node"].get("label") or m["group"],
             "workflow": m["node"].get("workflow")
             or workspec.KINDS[m["node"].get("kind", "worker")][1],
             "relation": str((m["node"].get("inputs") or {}).get("relation") or ""),
             "issue": "lane" if m["depth"] == 0 else
             ("none" if m["node"].get("kind") == "reviewer"
              else m["node"].get("issue", "pending"))}
            for m in _instances(doc, given or {})]


def groups(doc: dict) -> List[dict]:
    """The preset's groups (one per node, depth-first): the rows a launch's
    per-level profile choice is made on."""
    out: List[dict] = []

    def walk(node: dict, depth: int, parent: str) -> None:
        out.append({"id": node["id"], "depth": depth, "parent": parent,
                    "kind": node.get("kind", "worker"),
                    "label": node.get("label") or node["id"],
                    "count": node.get("count", 1) if depth else 1,
                    "profile": node.get("profile") or "",
                    "tier": node.get("tier") or "", "model": node.get("model") or ""})
        for child in node.get("children") or []:
            walk(child, depth + 1, node["id"])

    walk(doc["root"], 0, "")
    return out


# --------------------------------------------------------------------------- #
# expansion into a work spec
# --------------------------------------------------------------------------- #

def _title_of(task: str, fallback: str) -> str:
    first = next((ln.strip() for ln in (task or "").splitlines() if ln.strip()), "")
    first = first or fallback
    return first if len(first) <= 80 else first[:79] + "…"


def run_name(preset: str, stamp: Optional[float] = None) -> str:
    """A spec name for one launch: ``fleet-<preset>-<MMDDHHMM>``."""
    when = time.strftime("%m%d%H%M", time.localtime(stamp))
    head = preset[: 48 - len("fleet--") - len(when)].rstrip("-")
    return f"fleet-{head}-{when}"


def handle_prefix(launch: dict, attach: bool) -> str:
    """The prefix every member's mesh handle gets. An import reuses the
    session that already holds a handle in the mesh (that is how a rerun
    continues), so in an existing mesh a bare ``coord`` or ``lead1`` could
    adopt somebody else's session. ``launch['prefix']`` names it; empty
    there means none in a new mesh and ``f<MMDDHHMM>`` in an existing one."""
    given = launch.get("prefix")
    if given is not None and str(given).strip():
        p = str(given).strip()
        if not _SLUG.match(p) or len(p) > 16:
            raise FleetError(f"prefix {p!r}: a short lowercase slug (16 chars at most)")
        return p
    return "f" + time.strftime("%m%d%H%M") if attach else ""


TierLookup = Callable[[str], Optional[Tuple[str, str]]]


def expand(doc: dict, launch: dict, *, tiers: Optional[TierLookup] = None) -> dict:
    """Preset ``doc`` + ``launch`` -> ``{"spec": <work spec>, "yaml": <its
    text>, "agents": [<one row per member>]}``.

    ``launch`` keys: ``task`` (the root's opening task, required),
    ``workspace``, ``project``, ``mesh`` (a name; empty: ``spec-<name>``),
    ``mesh_exists`` (attach instead of creating it), ``name`` (the spec's;
    empty: :func:`run_name`), ``params`` (counts), ``profiles`` (group id or
    member handle -> profile; a handle wins over its group), ``models``
    (same keys -> model) and ``default_profile`` (the root's when nothing
    else names one). ``tiers(tier_id)`` answers ``(profile, model)`` for a
    group's ``tier`` -- the daemon passes the ``model_tiers`` resolver.

    A child with no profile chosen inherits its parent's, the way any spawn
    does; only the root must end up with one.
    """
    errs = validate(doc)
    if errs:
        raise FleetError("invalid preset:\n- " + "\n- ".join(errs))
    task = str(launch.get("task") or "").strip()
    if not task:
        raise FleetError("the opening task of the root is required")
    members = _instances(doc, launch.get("params") or {})
    profiles = {str(k): str(v) for k, v in (launch.get("profiles") or {}).items() if v}
    models = {str(k): str(v) for k, v in (launch.get("models") or {}).items() if v}
    name = str(launch.get("name") or "").strip() or run_name(doc["name"])
    if not workspec._SLUG.match(name):
        raise FleetError(f"name {name!r}: lowercase slug [a-z0-9-] required")
    mesh = str(launch.get("mesh") or "").strip()
    attach = bool(mesh and launch.get("mesh_exists"))
    prefix = handle_prefix(launch, attach)
    title = _title_of(task, doc.get("title") or doc["name"])

    rows: List[dict] = []
    agents: List[dict] = []
    issues: List[dict] = []
    issue_of: Dict[str, str] = {}
    for m in members:
        node = m["node"]
        kind = node.get("kind", "worker")
        tier = node.get("tier") or ""
        tier_profile = tier_model = ""
        if tier and tiers:
            got = tiers(tier)
            if got:
                tier_profile, tier_model = got[0] or "", got[1] or ""
        profile = (profiles.get(m["handle"]) or profiles.get(m["group"])
                   or node.get("profile") or tier_profile
                   or (str(launch.get("default_profile") or "") if m["depth"] == 0 else ""))
        model = (models.get(m["handle"]) or models.get(m["group"])
                 or node.get("model") or tier_model)
        if m["depth"] == 0 and not profile:
            raise FleetError(f"{m['handle']}: the root is a new top-level session "
                             "and needs a profile")
        agent = {"id": m["handle"], "kind": kind,
                 "parent": workspec.TOP if m["depth"] == 0 else m["parent"]}
        if prefix:
            agent["handle"] = f"{prefix}-{m['handle']}"
        if node.get("workflow"):
            agent["workflow"] = node["workflow"]
        if profile:
            agent["profile"] = profile
        if model:
            agent["model"] = model
        if node.get("inputs"):
            agent["inputs"] = dict(node["inputs"])
        extra = str(node.get("task") or "").strip()
        if m["depth"] == 0:
            agent["task"] = task + (f"\n\n{extra}" if extra else "")
        elif extra:
            agent["task"] = extra
        mode = "lane" if m["depth"] == 0 else node.get("issue", "pending")
        if kind == "reviewer":
            mode = "none"
        if mode == "lane":
            key = "root" if m["depth"] == 0 else f"lane-{m['handle']}"
            # An epic is what a holder keeps (the spec allows a sub-lead or
            # a coordinator to hold one); anyone else's lane is a task.
            it = {"key": key,
                  "type": "epic" if kind in ("coordinator", "sub-lead") else "task",
                  "title": title if m["depth"] == 0 else
                  f"[{node.get('label') or m['group']} {m['n']}] {title}",
                  "assignee": m["handle"], "labels": ["fleet"]}
            if m["depth"] == 0:
                it["goal"] = task
            else:
                it["parent"] = issue_of[m["parent"]]
                it["goal"] = (f"{m['handle']}이 맡는 lane이다. 범위와 일감은 "
                              f"부모 {m['parent']}가 이 이슈에 적고 자식 이슈로 "
                              "나눈다. 상위 목표는 부모 이슈에 있다.")
            issues.append(it)
            issue_of[m["handle"]] = key
        elif mode == "pending":
            agent["start_issue"] = "pending"
        agents.append(agent)
        rows.append({"handle": agent.get("handle") or m["handle"], "id": m["handle"],
                     "group": m["group"], "n": m["n"],
                     "depth": m["depth"], "parent": m["parent"], "kind": kind,
                     "label": node.get("label") or m["group"],
                     "workflow": agent.get("workflow") or workspec.KINDS[kind][1],
                     "relation": str((node.get("inputs") or {}).get("relation") or ""),
                     "profile": profile, "model": model,
                     "inherits": not profile and m["depth"] > 0,
                     "tier": tier, "issue": mode,
                     "issue_key": issue_of.get(m["handle"], "")})
    spec: dict = {"spec": workspec.SPEC_VERSION, "name": name, "title": title,
                  "source": {"generator": "claunch fleet", "preset": doc["name"]}}
    if launch.get("workspace"):
        spec["workspace"] = str(launch["workspace"])
    if launch.get("project"):
        spec["project"] = str(launch["project"])
    spec["mesh"] = ({"mode": "attach", "name": mesh} if attach
                    else {"mode": "new", **({"name": mesh} if mesh else {})})
    spec["leader"] = {"mode": "none"}
    spec["agents"] = agents
    spec["issues"] = issues
    problems = workspec.validate(spec)
    if problems:  # a preset validate() let through, expanded into a bad spec
        raise FleetError("the expanded spec does not validate:\n- "
                         + "\n- ".join(problems))
    text = yaml.safe_dump(spec, allow_unicode=True, sort_keys=False, width=100)
    # name and prefix are echoed: both default from the clock, and a launch
    # that sends them back gets the very document it previewed.
    return {"spec": spec, "yaml": text, "agents": rows, "name": name,
            "prefix": prefix}


# --------------------------------------------------------------------------- #
# storage
# --------------------------------------------------------------------------- #

def presets_dir() -> Path:
    from . import config
    return config.launcher_home() / "fleet-presets"


def _summary(doc: dict, text: str, source: str) -> dict:
    return {"name": doc.get("name"), "title": doc.get("title") or "",
            "description": doc.get("description") or "", "source": source,
            "params": doc.get("params") or {}, "yaml": text,
            "errors": validate(doc)}


def list_presets() -> List[dict]:
    """Every preset, built-ins first; a user file named like a built-in
    replaces it (``source: user``, ``overrides: builtin``)."""
    found: Dict[str, dict] = {}
    for name, text in BUILTIN.items():
        found[name] = _summary(load(text), text, "builtin")
    folder = presets_dir()
    if folder.is_dir():
        for path in sorted(folder.glob("*.yaml")):
            try:
                text = path.read_text(encoding="utf-8")
                doc = load(text)
            except (OSError, FleetError) as exc:
                found[path.stem] = {"name": path.stem, "title": "", "description": "",
                                    "source": "user", "params": {}, "yaml": "",
                                    "errors": [str(exc)]}
                continue
            row = _summary(doc, text, "user")
            if path.stem in BUILTIN:
                row["overrides"] = "builtin"
            if doc.get("name") != path.stem:
                row["errors"] = [*row["errors"],
                                 f"name: {doc.get('name')!r} differs from the file name {path.stem!r}"]
            found[path.stem] = row
    return list(found.values())


def get_preset(name: str) -> Tuple[dict, str]:
    for row in list_presets():
        if row["name"] == name:
            if not row["yaml"]:
                raise FleetError("; ".join(row["errors"]) or f"preset {name!r} is unreadable")
            return load(row["yaml"]), row["yaml"]
    raise FleetError(f"no fleet preset named {name!r}")


def save_preset(text: str) -> dict:
    """Validate and write a user preset; answers its summary."""
    doc = load(text)
    errs = validate(doc)
    if errs:
        raise FleetError("invalid preset:\n- " + "\n- ".join(errs))
    folder = presets_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{doc['name']}.yaml"
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    tmp.replace(path)
    row = _summary(doc, path.read_text(encoding="utf-8"), "user")
    if doc["name"] in BUILTIN:
        row["overrides"] = "builtin"
    return row


def delete_preset(name: str) -> None:
    """Remove a user preset (a built-in comes back if it was overridden)."""
    if not _SLUG.match(name or ""):
        raise FleetError(f"bad preset name {name!r}")
    path = presets_dir() / f"{name}.yaml"
    if not path.is_file():
        raise FleetError(f"no user preset named {name!r}"
                         + (" (a built-in cannot be deleted)" if name in BUILTIN else ""))
    path.unlink()
