"""Session plan routes (claunch-7jzp8): the plan a session writes into its
plan pane, and the decisions the user takes there.

  GET  /api/plan?session=<name>          one session's plan ({"plan": null} without one)
  GET  /api/plan?session=&round=<run>/<n>  one sealed round of it (claunch-r3j1o.1; <run>/unknown too)
  GET  /api/plan/rounds?session=<name>   its sealed rounds, oldest first, and the current plan's round
  GET  /api/plan/rail                    per session with a plan: title, context, summary
  GET  /api/plan/joint?session=<name>    the joint tree holding it (plan/joint.py); no session: every tree
  GET  /api/plan/overview[?all=1&project=]  every running session's stage, run and plan progress (plan/overview.py)
  GET  /api/plan/aggregators             each project's aggregation session
  POST /api/plan/aggregators/{project}/start  start it {profile, model?, effort?, name?}         (the user)
  POST /api/plan                         set       {session, revision, title, goal?, issue?, steps?, closure?}
  POST /api/plan/node/{id}               patch     {session, title?, detail?, status?, options?, reopen?}
  POST /api/plan/clear                   clear     {session, revision}  (the plan is sealed first)
  POST /api/plan/round                   new_round {session, run, round, title?, goal?, issue?, leftover?}
  POST /api/plan/node/{id}/decide        decide    {session, rev, choice?|verdict?|action?, note?}  (the user)
  POST /api/plan/feedback                feedback  {session, text, node?}                       (the user)
  POST /api/plan/node/{id}/deliver       deliver the waiting notification now {session}        (the user)

Who is calling follows qa_api, with one difference: every plan route names
the plan's ``session``, so the caller is told by ``by`` -- the plan_* MCP
tools send ``by: $CLAUNCH_SESSION``; a body without ``by`` is the user (the
dashboard). The store refuses the user's hand on the plan and a session's
hand on a decision, and any session's hand on another session's plan.

Reading is open: any session may read any plan (``plan_get session=``), so a
leader reads its workers' plans and the joint views (claunch-7jzp8.1, .3)
need no second surface.

A decision or a comment the user saves is typed into the owning session as
a ``[claunch plan]`` block that carries what was decided and on what, so the
agent acts on it without a read. Delivery is tracked the way QA tracks it
(qa_api.Outbox, one latest notification per node).

A project's aggregation session (claunch-7jzp8.3) is started from the plan
page the way an operator v2's session is started from its view: one session
per project, bound in ``plan_aggregators.json`` and kept alive. It reads the
project's sessions (plan_overview, plan_get) and writes the project plan as
its own plan, so the page shows it and the user's decisions on it are typed
back into it like any session's.

The plan's ``context`` (project, cwd, spawn parent) is written by the daemon
from the session's record on every set, not by the session: the aggregated
views group plans by it, and a session cannot misplace itself.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Optional

from aiohttp import web

from ..plan import joint as plan_joint
from ..plan import overview as plan_overview
from ..plan import store as plan_store
from . import paths
from .qa_api import Outbox

log = logging.getLogger(__name__)

PREFIX = "[claunch plan]"


def _quote(text: str) -> str:
    return "\n".join("  > " + line for line in (text or "").splitlines()) or "  > (empty)"


def _what(node: dict) -> list[str]:
    lines = [f"  {node['kind']} {node['id']} (rev {node['rev']}): {node['title']}"]
    if node.get("detail"):
        lines.append(_quote(node["detail"]))
    return lines


def decided_text(session: str, node: dict, previous: Optional[dict]) -> str:
    res = node.get("result") or {}
    kind = node["kind"]
    if kind == "decision":
        opts = {o["label"]: o for o in node.get("options") or []}
        opt = opts.get(res.get("choice"), {})
        was = (previous or {}).get("choice")
        changed = bool(was) and was != res.get("choice")
        if changed:
            whose = "your default" if previous.get("by") == "agent" else "their earlier choice"
            head = (f"{PREFIX} The user CHANGED your plan's decision {node['id']}: \"{was}\" -> "
                    f"\"{res.get('choice')}\" (replacing {whose}):")
            old = opts.get(was, {})
            body = _what(node) + [
                f"  before: \"{was}\"",
                f"    expected effect: {old.get('effect', '(the option is no longer listed)')}",
                f"    risk: {old.get('risk', '')}",
                f"  after: \"{res.get('choice')}\"",
                f"    expected effect: {opt.get('effect', '')}",
                f"    risk: {opt.get('risk', '')}",
            ]
            then = (f"Steer to the new branch now. Re-check what you did on \"{was}\": steps you finished or "
                    f"started for it, code and commits it shaped, and the nodes under \"{was}\" (they no "
                    f"longer count). Undo or redo what the new choice changes, and rewrite the plan with "
                    f"plan_set so the nodes under \"{res.get('choice')}\" show the path you now take. If "
                    f"switching costs more than the note suggests the user expects, say so in a thread "
                    f"before you undo work.")
        else:
            head = f"{PREFIX} The user chose \"{res.get('choice')}\" on your plan's decision {node['id']}:"
            body = _what(node) + [f"  expected effect: {opt.get('effect', '')}", f"  risk: {opt.get('risk', '')}"]
            then = ("Follow that branch: rewrite the steps under it with plan_set (or patch them with plan_node) "
                    "so the plan shows the path you now take.")
            if was:
                then = ("They confirmed " + ("your default" if previous.get("by") == "agent" else
                                             "their earlier choice") + "; nothing changes. " + then)
    elif kind == "verify":
        head = f"{PREFIX} The user checked your plan's verify item {node['id']}: {res.get('verdict')}."
        body = _what(node)
        then = {
            "passed": "Nothing to do but keep going. Rewording the item later keeps this result; if what was "
                      "checked changes, ask again with plan_node reopen=<what changed>.",
            "deferred": "It cannot be checked yet (보류). It no longer waits on the user or keeps the session "
                        "open: hand it over -- file it as a beads issue (claunch beads create ... --parent "
                        "<your issue>) that says what to check and how -- and name the issue id in the item's "
                        "detail with plan_node (the result stays).",
        }.get(res.get("verdict"),
              "It failed by hand. Fix what the note says, then reword the item with plan_node so the user "
              "checks it again (a changed item asks again).")
    elif kind == "note":
        # claunch-i0whx: an optional pick on a note. It gates nothing either
        # way, so there is no status to close -- the record of what was done
        # goes into the note's detail (rewording keeps the user's result).
        action = res.get("action")
        head = f"{PREFIX} The user picked up your plan's note {node['id']}: {action}."
        body = _what(node)
        then = {
            "here": "Take it into this round: add its steps to the plan (plan_set, keep the ids) and do them. "
                    "The note itself waits on nobody; write what became of it into its detail with plan_node.",
            "issue": "Hand it over: file it as a beads issue (claunch beads create ... --parent <your issue>) "
                     "and name the issue id in the note's detail with plan_node (the result stays).",
        }.get(action, "")
    else:
        action = res.get("action")
        head = f"{PREFIX} The user disposed of your plan's followup {node['id']}: {action}."
        body = _what(node)
        then = {
            "here": "Do it in this session: add its steps to the plan, and mark the followup done "
                    "(plan_node status=done) when it is.",
            "issue": "Hand it over: file it as a beads issue (claunch beads create ... --parent <your issue>), "
                     "then mark the followup done (plan_node status=done) naming the issue id in its detail.",
            "drop": "Drop it; nothing more to do on it.",
        }.get(action, "")
    lines = [head, *body]
    if res.get("note"):
        lines += ["their note:", _quote(res["note"])]
    if previous and kind != "decision":
        before = previous.get("verdict") or previous.get("action") or previous
        lines.append(f"(this replaces an earlier result: {before})")
    lines.append(then)
    return "\n".join(lines)


def feedback_text(session: str, node: Optional[dict], text: str) -> str:
    on = f"on step {node['id']} ({node['title']})" if node else "on your plan"
    return "\n".join([
        f"{PREFIX} The user commented {on}:",
        _quote(text),
        "Answer by changing the plan where it calls for that (plan_set / plan_node); if it asks a question "
        "back, reply in a thread (thread_open) rather than in the terminal.",
    ])


def aggregator_handle(project: str) -> str:
    return f"plan-{project}"


def aggregator_task(project: str, mesh: Optional[str] = None) -> str:
    """The aggregator's opening task. With no ``mesh`` the session is on no
    mesh, so the task does not tell it to relay by mesh send."""
    relay = (f"그 결정이 닿는 세션들에 메시 '{mesh}'의 send로 전달한다(네 handle은 "
             f"{aggregator_handle(project)}). " if mesh else
             "너는 어느 메시에도 등록되지 않았으므로 메시로 전달하지 않는다: 그 결정이 닿는 세션들의 "
             "이름을 네 계획의 해당 결정 아래에 적어, 사용자가 각 세션의 plan 패널에서 전달하게 한다. ")
    return (f"너는 프로젝트 '{project}'의 plan 집계 세션이다. 이 프로젝트의 세션들이 각자 쓴 계획을 모아 "
            "프로젝트 계획 하나로 정리하는 것이 일이다. 다른 세션의 작업을 직접 하지 않는다. "
            f"plan_overview(project='{project}')로 세션마다 단계·런·계획 진행을 읽고, "
            "plan_get(joint=true)와 plan_get(session=...)으로 연관 세션 묶음과 각 계획을 읽는다. "
            "결과는 plan_set으로 네 계획에 쓴다: 단계는 연관 세션 묶음마다 한 줄(진행, 막힘, 내 결정 대기 수), "
            "결정은 여러 세션에 걸친 선택(착지 순서, 겹치는 작업 정리, 우선순위)마다 선택지별 기대효과·리스크와 "
            "함께 둔다. 계획에 쓰는 글은 한국어로 쓰고, 명령·경로·식별자는 원문 그대로 둔다. 개별 세션의 결정은 사용자가 그 세션의 plan 패널에서 하므로 옮겨 적지 않는다. "
            "사용자가 고른 분기는 [claunch plan] 블록으로 오고, " + relay +
            "10분마다(wait/timer) 다시 읽어 계획을 갱신한다.")


class Aggregators:
    """``{project: {session, profile, started_at}}`` in one JSON file."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {k: v for k, v in data.items() if isinstance(v, dict) and v.get("session")} \
            if isinstance(data, dict) else {}

    def bind(self, project: str, session: str, profile: str) -> dict:
        data = self.load()
        data[project] = {"session": session, "profile": profile, "started_at": int(time.time())}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)
        return data[project]


def _v2_rows(app, pairs: list) -> dict:
    """The v2 rail rows of ``pairs`` ((session, cwd)), the same read and
    cache the session rail uses (cflow2_web.rail_entries)."""
    from . import cflow2_web
    return cflow2_web.rail_entries(pairs, registry=app.get("gates"))


def install(app, store: Optional[plan_store.PlanStore] = None, runs_of=None, create=None,
            aggregators: Optional[Aggregators] = None):
    """``create(request, spec) -> (status, payload)`` makes a session the way
    the new-session route does (api._create_session); without it the
    aggregator start answers 503."""
    manager = app["manager"]
    plans = store or plan_store.PlanStore(paths.plan_db())
    app["plans"] = plans
    bound = aggregators or Aggregators(paths.plan_aggregators())
    app["plan_aggregators"] = bound
    outbox = Outbox(manager)
    app["plan_outbox"] = outbox

    def context_of(name: str) -> dict:
        try:
            sdef = manager.get(name).sdef
        except Exception:  # noqa: BLE001 -- no such session
            return {}
        out = {}
        for key in ("project", "cwd", "parent"):
            value = getattr(sdef, key, None)
            if value:
                out[key] = str(value)
        return out

    def parent_of(name: str) -> Optional[str]:
        """The live spawn parent ("" for none); None for a session this
        daemon does not hold, so joint.py falls back to the plan's context."""
        try:
            sdef = manager.get(name).sdef
        except Exception:  # noqa: BLE001
            return None
        return str(getattr(sdef, "parent", None) or "")

    def known(name: str) -> bool:
        try:
            manager.get(name)
            return True
        except Exception:  # noqa: BLE001
            return False

    def live(name: str):
        try:
            s = manager.get(name)
        except Exception:  # noqa: BLE001
            return None
        return None if s.exited else s

    def mesh_of(project: str, asked: str) -> tuple:
        """``(mesh, why)``: the mesh an aggregator of ``project`` joins --
        ``asked`` when it exists, else the one team mesh the project's
        running sessions are on -- and, when there is none, why."""
        mm = app.get("mesh")
        if mm is None:
            return None, "this daemon has no meshes"
        if asked:
            try:
                mm.get(asked)
            except Exception as exc:  # noqa: BLE001 -- MeshError, or an ambiguous name
                return None, str(exc)
            return asked, ""
        found = set()
        for s in manager.list():
            if s.exited or getattr(s.sdef, "project", None) != project:
                continue
            found.update(row["mesh"] for row in mm.meshes_for_session(s.sdef.name)
                         if not row.get("parent_channel"))
        if len(found) == 1:
            return found.pop(), ""
        return None, (f"the sessions of {project!r} are on several meshes ({', '.join(sorted(found))}); "
                      "name one" if found else f"no running session of {project!r} is on a mesh")

    async def free_handle(project: str, mesh: str, prior: Optional[str]) -> tuple:
        """``(mesh, why)`` once ``plan-<project>`` is free on ``mesh``: a
        membership outlives its session, so the handle the project's last
        aggregator (``prior``, now ended) held is released here; one any
        other session holds is not touched and the start goes without a
        mesh."""
        handle = aggregator_handle(project)
        mm = app["mesh"]
        member = mm.get(mesh).members.get(handle)
        if member is None:
            return mesh, ""
        holder = getattr(member, "session", None)
        if holder != prior or live(holder) is not None:
            return None, f"handle {handle!r} on mesh {mesh!r} is held by session {holder!r}"
        try:
            await mm.leave(mesh, handle)
        except Exception as exc:  # noqa: BLE001 -- MeshError: the start goes on without a mesh
            return None, f"handle {handle!r} on mesh {mesh!r} could not be released: {exc}"
        return mesh, ""

    def aggregator_rows() -> list:
        return [{"project": project, **rec, "running": live(rec["session"]) is not None}
                for project, rec in sorted(bound.load().items())]

    async def body_of(request) -> dict:
        try:
            body = await request.json()
        except (ValueError, UnicodeError):
            body = None
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text='{"error": "body must be a JSON object"}',
                                     content_type="application/json")
        return body

    def actor_of(body: dict) -> str:
        s = body.get("by")
        return s if isinstance(s, str) and s else plan_store.USER

    def session_of(body: dict) -> str:
        s = body.get("session")
        if not isinstance(s, str) or not s:
            raise web.HTTPBadRequest(text='{"error": "session (whose plan) is required"}',
                                     content_type="application/json")
        return s

    async def call(fn, *args, **kw):
        try:
            return await asyncio.to_thread(fn, *args, **kw), None
        except plan_store.PlanError as exc:
            doc = {"error": str(exc)}
            if exc.current is not None:
                doc["current"] = exc.current
            return None, web.json_response(doc, status=exc.status)

    def with_delivery(plan: Optional[dict]) -> Optional[dict]:
        if plan is None:
            return None
        for node in [*plan_store.walk(plan.get("steps") or []), *(plan.get("closure") or [])]:
            view = outbox.view(f"{plan['session']}:{node['id']}")
            if view:
                node["delivery"] = view
        return plan

    async def h_get(request):
        session = request.query.get("session") or ""
        if not session:
            return web.json_response({"error": "session is required"}, status=400)
        key = request.query.get("round") or ""
        if key:
            res, err = await call(plans.get_round, session, key)
            return err or web.json_response({"session": session, "round": key, "plan": res})
        res, err = await call(plans.get, session)
        return err or web.json_response({"session": session, "plan": with_delivery(res)})

    async def h_rounds(request):
        session = request.query.get("session") or ""
        if not session:
            return web.json_response({"error": "session is required"}, status=400)
        res, err = await call(plans.rounds, session)
        return err or web.json_response(res)

    async def h_rail(_request):
        res, err = await call(plans.rail)
        return err or web.json_response({"sessions": res})

    def step_tree(nodes: list) -> list:
        """A plan's steps as the plan page draws them (claunch-7jzp8.5):
        what each node says and how far it got, without revs or history.
        ``auto`` marks a decision the session answered with its default;
        ``under`` keeps a child's option branch (claunch-vmcg7)."""
        out = []
        for n in nodes:
            item = {k: n[k] for k in ("id", "kind", "title", "detail", "status", "options", "under") if n.get(k)}
            res = n.get("result") or {}
            if res.get("choice"):
                item["choice"] = res["choice"]
                if res.get("by") == "agent":
                    item["auto"] = True
            item["children"] = step_tree(n.get("children") or [])
            out.append(item)
        return out

    async def with_steps(groups: list) -> list:
        for g in groups:
            for m in g["members"]:
                if not m.get("has_plan"):
                    continue
                plan, err = await call(plans.get, m["session"])
                m["tree"] = step_tree((plan or {}).get("steps") or []) if not err else []
        return groups

    async def h_joint(request):
        """``steps=1`` adds each planned member's step tree as ``tree``, for
        the plan page's tree view (``steps`` there is the rail's count)."""
        session = request.query.get("session") or ""
        steps = request.query.get("steps") in ("1", "true", "yes")
        rail, err = await call(plans.rail)
        if err:
            return err
        if not session:
            groups = plan_joint.groups(rail, parent_of)
            return web.json_response({"groups": await with_steps(groups) if steps else groups})
        group = plan_joint.group_of(rail, parent_of, session)
        if group and steps:
            await with_steps([group])
        return web.json_response({"session": session, "group": group})

    async def h_overview(request):
        """Running sessions by default; ``all=1`` adds every unarchived
        ended one (held, paused, failed, killed)."""
        from . import session as session_mod
        from ..cflow import state as cflow_state
        everyone = request.query.get("all") in ("1", "true", "yes")
        only = request.query.get("project") or ""
        aggs = aggregator_rows()
        agg_of = {a["session"]: a["project"] for a in aggs}
        rows, pairs = [], []
        for s in manager.list():
            category = session_mod.session_category(s)
            if category == session_mod.CATEGORY_ARCHIVED or (s.exited and not everyone):
                continue
            sdef = s.sdef
            project = getattr(sdef, "project", None) or None
            if only and project != only:
                continue
            rows.append({"session": sdef.name, "category": category,
                         "status": "exited" if s.exited else s.status(),
                         "project": project,
                         "parent": getattr(sdef, "parent", None) or None,
                         "aggregator": agg_of.get(sdef.name)})
            if getattr(sdef, "cwd", None):
                pairs.append((sdef.name, cflow_state.resolve_cwd(sdef.cwd)))
        rail, err = await call(plans.rail)
        if err:
            return err
        try:
            runs = await asyncio.to_thread(runs_of or (lambda p: _v2_rows(app, p)), pairs)
        except Exception:  # noqa: BLE001 -- the plans still answer
            log.exception("plan overview: reading the v2 rail failed")
            runs = {}
        out = plan_overview.overview(rows, rail, runs)
        out["aggregators"] = [a for a in aggs if not only or a["project"] == only]
        return web.json_response(out)

    async def h_aggregators(_request):
        return web.json_response({"aggregators": aggregator_rows()})

    async def h_aggregator_start(request):
        from .. import projects
        from . import actors
        if (actors.from_request(request) or {}).get("kind") == actors.AGENT:
            return web.json_response({"error": "only the user starts an aggregation session"}, status=403)
        if create is None:
            return web.json_response({"error": "this daemon cannot create sessions here"}, status=503)
        project = request.match_info["project"]
        try:
            project = projects.require(project).name
        except projects.ProjectError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        rec = bound.load().get(project)
        if rec and live(rec["session"]) is not None:
            return web.json_response({"error": f"project {project!r} is already aggregated by {rec['session']}",
                                      "session": rec["session"]}, status=409)
        body = await body_of(request)
        profile = str(body.get("profile") or "").strip()
        if not profile:
            return web.json_response({"error": "profile is required"}, status=400)
        mesh, why = mesh_of(project, str(body.get("mesh") or "").strip())
        if why and body.get("mesh"):
            return web.json_response({"error": why}, status=400)
        if mesh:
            mesh, why = await free_handle(project, mesh, (rec or {}).get("session"))
        spec = {"profile": profile, "project": project, "beads": False,
                "context": json.dumps({"plan_aggregator": project}),
                "name": str(body.get("name") or "").strip(), "task": aggregator_task(project, mesh)}
        if mesh:
            spec["mesh"], spec["handle"] = mesh, aggregator_handle(project)
        for key in ("model", "effort"):
            value = str(body.get(key) or "").strip()
            if value:
                spec[key] = value
        status, payload = await create(request, spec)
        if status >= 300:
            return web.json_response(payload, status=status)
        await asyncio.to_thread(bound.bind, project, payload["name"], profile)
        try:
            manager.set_keep_alive(payload["name"], True)
            payload["keep_alive"] = True
        except Exception:  # noqa: BLE001 -- the session is up; it only may close after a run
            log.exception("plan aggregator keep-alive of %r failed", payload["name"])
        payload["project"] = project
        payload["mesh"] = mesh
        if why:
            payload["mesh_note"] = why
        return web.json_response(payload, status=201)

    async def h_set(request):
        body = await body_of(request)
        session = session_of(body)
        actor = actor_of(body)
        if actor != plan_store.USER and not known(actor):
            return web.json_response({"error": f"no session {actor!r} on this daemon"}, status=404)
        res, err = await call(plans.set, actor, session, body.get("revision"), title=body.get("title"),
                              goal=body.get("goal"), issue=body.get("issue"), steps=body.get("steps"),
                              closure=body.get("closure"), context=context_of(session))
        return err or web.json_response({"plan": res})

    async def h_patch(request):
        body = await body_of(request)
        fields = {k: body.get(k) for k in ("title", "detail", "status", "options")}
        res, err = await call(plans.patch, actor_of(body), session_of(body), request.match_info["id"],
                              reopen=body.get("reopen"), **fields)
        return err or web.json_response(res)

    async def h_clear(request):
        body = await body_of(request)
        res, err = await call(plans.clear, actor_of(body), session_of(body), body.get("revision"))
        return err or web.json_response(res)

    async def h_round(request):
        body = await body_of(request)
        session = session_of(body)
        res, err = await call(plans.new_round, actor_of(body), session, run=body.get("run"),
                              round=body.get("round"), title=body.get("title"), goal=body.get("goal"),
                              issue=body.get("issue"), context=context_of(session),
                              leftover=body.get("leftover"))
        return err or web.json_response(res)

    async def h_decide(request):
        body = await body_of(request)
        session = session_of(body)
        res, err = await call(plans.decide, actor_of(body), session, request.match_info["id"], body.get("rev"),
                              choice=body.get("choice"), verdict=body.get("verdict"), action=body.get("action"),
                              note=body.get("note"))
        if err:
            return err
        node = res["node"]
        text = decided_text(session, node, res["previous"])
        key = f"{session}:{node['id']}"
        outbox.send(key, session, "decide", lambda: text)
        res["delivery"] = outbox.view(key)
        res["delivered"] = res["delivery"]["state"] != "failed"
        return web.json_response(res)

    async def h_feedback(request):
        body = await body_of(request)
        session = session_of(body)
        res, err = await call(plans.feedback, actor_of(body), session, text=body.get("text"),
                              node=body.get("node") or None)
        if err:
            return err
        text = feedback_text(session, res["node"], res["text"])
        key = f"{session}:{(res['node'] or {}).get('id', '_feedback')}"
        outbox.send(key, session, "feedback", lambda: text)
        delivery = outbox.view(key)
        return web.json_response({"delivery": delivery, "delivered": delivery["state"] != "failed"})

    async def h_deliver(request):
        body = await body_of(request)
        key = f"{session_of(body)}:{request.match_info['id']}"
        rec = await outbox.force(key)
        if rec is None:
            return web.json_response({"error": "no notification for that node is waiting",
                                      "delivery": outbox.view(key)}, status=409)
        return web.json_response({"delivery": outbox.view(key)})

    app.router.add_get("/api/plan", h_get)
    app.router.add_get("/api/plan/rail", h_rail)
    app.router.add_get("/api/plan/rounds", h_rounds)
    app.router.add_get("/api/plan/joint", h_joint)
    app.router.add_get("/api/plan/overview", h_overview)
    app.router.add_get("/api/plan/aggregators", h_aggregators)
    app.router.add_post("/api/plan/aggregators/{project}/start", h_aggregator_start)
    app.router.add_post("/api/plan", h_set)
    app.router.add_post("/api/plan/clear", h_clear)
    app.router.add_post("/api/plan/round", h_round)
    app.router.add_post("/api/plan/feedback", h_feedback)
    app.router.add_post("/api/plan/node/{id}", h_patch)
    app.router.add_post("/api/plan/node/{id}/decide", h_decide)
    app.router.add_post("/api/plan/node/{id}/deliver", h_deliver)
    return plans
