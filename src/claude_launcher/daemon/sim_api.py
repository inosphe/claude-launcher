"""Dashboard and session routes for world-model simulations (``#/sims``).

The registry is :class:`claude_launcher.sim.store.SimStore`; this module is
its HTTP face, the place that knows sessions -- who is running, which
project and checkout a session stands in -- and the one that types lines
into an owner's terminal:

* when a running campaign ends (nothing left open, the round limit, a stop):
  the owner is told the result is ready, reads it, and carries the
  discussion on with the user from there;
* when the user answers a question the owner raised;
* when the user creates a simulation for a session from the page, or
  presses Nudge.

Who is calling. A body with ``session`` acts as that session (the MCP tools
always send ``$CLAUNCH_SESSION``); a body without one acts as the user. This
is the daemon's usual trust model -- the dashboard and the tools share one
token -- so the owner rule is enforced for agents that go through the tools,
the way the rest of the daemon's gates are.

Every store call runs off the loop: rounds are CPU work and every call
touches files.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from aiohttp import web

from .. import store as config_store
from ..sim import store as sim_store
from . import paths

log = logging.getLogger(__name__)

PREFIX = "[claunch simulation]"


def _live(manager, name: str) -> bool:
    """Still a session that can own something. A held session has ended
    its program, not itself (Session.held_at): the exit hooks skip it and a
    delivery brings it back, so it keeps what it owns."""
    try:
        s = manager.get(name)
    except Exception:  # noqa: BLE001 -- no such session
        return False
    return not s.exited or bool(getattr(s, "held_at", None))


def _session_where(manager, name: str) -> tuple:
    """(root, project) of a running session: its checkout and project."""
    try:
        sdef = manager.get(name).sdef
    except Exception:  # noqa: BLE001
        return "", ""
    return sdef.cwd or "", sdef.project or ""


def pi_profiles() -> list:
    """Profiles that can launch the pi harness on this machine: the choices
    for a simulation's interpreter and judge."""
    from .. import harness_policy, harnesses, profile as profile_mod
    doc = config_store.load()
    entry = harnesses.registry(doc).get("pi")
    if entry is None or not entry.available():
        return []
    out = []
    for p in profile_mod.list_all():
        try:
            if harness_policy.evaluate(p, "pi", doc=doc).allowed:
                out.append(p.name)
        except Exception:  # noqa: BLE001 -- an unreadable profile is not a choice
            continue
    return out


def _defaults() -> dict:
    cfg = config_store.daemon_config()
    return {
        "interpreter_profile": str(cfg.get("simulation_interpreter_profile") or ""),
        "judge_profile": str(cfg.get("simulation_judge_profile") or ""),
    }


def ended_text(sim: dict) -> str:
    c = sim.get("counts") or {}
    last = sim.get("last_round") or {}
    return (
        f"{PREFIX} {sim['id']} \"{sim['title']}\" finished: {sim['state']} -- {sim.get('state_reason')}. "
        f"After round {last.get('round', '?')}: {c.get('open', 0)} open, {c.get('question', 0)} waiting on the user, "
        f"{c.get('answered', 0)} answered, {c.get('dismissed', 0)} dismissed, {c.get('resolved', 0)} resolved. "
        f"Read the result (sim_show {sim['id']}, sim_findings {sim['id']}) and continue the discussion "
        "with the user from it."
    )


def created_text(sim: dict) -> str:
    goal = (sim.get("goal") or "").strip()
    return (
        f"{PREFIX} The user asked you to build and run simulation {sim['id']} \"{sim['title']}\""
        + (f" -- goal: {goal}" if goal else "") + ". "
        f"You own it. Read it with sim_show {sim['id']}, build the world model from the sources with "
        "sim_patch (every rule quotes its source file word for word, or is marked provisional with a why), "
        f"then sim_start {sim['id']} and work the gaps (sim_gaps). You will be told when it finishes."
    )


def answered_text(sim: dict, finding: dict) -> str:
    q = (finding.get("question") or {}).get("text", "")
    a = (finding.get("answer") or {}).get("text", "")
    return (
        f"{PREFIX} The user answered your question on {sim['id']} (finding {finding['key']}). "
        f"Q: {q} A: {a} -- put it into the model with sim_patch (source ref user:{finding['key']}, quote the answer)."
    )


def nudge_text(sim: dict, text: str = "") -> str:
    if text.strip():
        return f"{PREFIX} On simulation {sim['id']} \"{sim['title']}\", the user says: {text.strip()}"
    return (
        f"{PREFIX} The user asks you to continue simulation {sim['id']} \"{sim['title']}\": "
        f"sim_show {sim['id']}, then work the open findings (sim_gaps)."
    )


def install(app, store: Optional[sim_store.SimStore] = None):
    manager = app["manager"]
    sims = store or sim_store.SimStore(paths.simulations_dir(), live=lambda n: _live(manager, n))
    app["sims"] = sims

    def deliver(name: str, text: str) -> bool:
        if not name:
            return False
        try:
            return bool(manager.get(name).queue_delivery(text))
        except Exception:  # noqa: BLE001 -- gone or not deliverable
            log.info("sim: could not deliver to %s", name)
            return False

    def released(session) -> None:
        name = getattr(getattr(session, "sdef", None), "name", None)
        if name:
            try:
                sims.session_exited(name)
            except Exception:  # noqa: BLE001 -- the exit funnel must not break
                log.exception("sim: releasing %s failed", name)

    manager.exit_hooks.append(released)

    async def sweep(_app):
        try:
            await asyncio.to_thread(sims.sweep)
        except Exception:  # noqa: BLE001
            log.exception("sim: startup sweep failed")

    app.on_startup.append(sweep)

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
        s = body.get("session")
        return s if isinstance(s, str) and s else sim_store.USER

    async def call(fn, *args, **kw):
        try:
            return await asyncio.to_thread(fn, *args, **kw), None
        except sim_store.SimError as exc:
            return None, web.json_response({"error": str(exc)}, status=exc.status)

    def notify_end(sim: dict, ended) -> bool:
        if not ended or not sim.get("owner"):
            return False
        return deliver(sim["owner"], ended_text(sim))

    # -- routes ------------------------------------------------------------

    async def h_list(request):
        q = request.query
        owner = q.get("owner")
        rows, err = await call(sims.list, project=q.get("project") or None, owner=owner)
        if err:
            return err
        return web.json_response({"sims": rows})

    async def h_profiles(request):
        names = await asyncio.to_thread(pi_profiles)
        return web.json_response({"profiles": names, "defaults": _defaults()})

    async def h_create(request):
        body = await body_of(request)
        actor = actor_of(body)
        owner = actor if actor != sim_store.USER else str(body.get("owner") or "")
        root, project = _session_where(manager, owner) if owner else ("", "")
        root = str(body.get("root") or root)
        project = str(body.get("project") or project)
        sim, err = await call(
            sims.create, title=str(body.get("title") or ""), goal=str(body.get("goal") or ""),
            root=root, project=project, owner=owner, actor=actor,
            sources=body.get("sources") or [], settings=body.get("settings") or {},
            issue=str(body.get("issue") or ""), defaults=_defaults())
        if err:
            return err
        nudged = False
        if actor == sim_store.USER and owner and body.get("nudge", True):
            nudged = deliver(owner, created_text(sim))
        return web.json_response({"sim": sim, "nudged": nudged}, status=201)

    async def h_get(request):
        sid = request.match_info["id"]
        sim, err = await call(sims.get, sid)
        if err:
            return err
        rounds = await asyncio.to_thread(sims.rounds, sid, 100)
        journal = await asyncio.to_thread(sims.journal, sid, 100)
        return web.json_response({"sim": sim, "rounds": rounds, "journal": journal,
                                  "owner_live": bool(sim.get("owner")) and _live(manager, sim["owner"])})

    async def h_delete(request):
        sid = request.match_info["id"]
        _, err = await call(sims.delete, sid, sim_store.USER)
        if err:
            return err
        return web.json_response({"ok": True})

    async def h_model(request):
        m, err = await call(sims.model, request.match_info["id"])
        if err:
            return err
        return web.json_response({"model": m})

    async def h_patch(request):
        sid = request.match_info["id"]
        body = await body_of(request)
        actor = actor_of(body)
        res, err = await call(sims.patch_model, sid, actor, body.get("patch") or {}, body.get("key"))
        if err:
            return err
        if res.get("ok") and not res.get("duplicate"):
            sim = await asyncio.to_thread(sims.get, sid)
            if sim["state"] == "running":
                summary, err = await call(sims.run_round, sid, actor)
                if err:
                    return err
                res["round"] = summary
                if summary.get("ended"):
                    res["notified"] = notify_end(await asyncio.to_thread(sims.get, sid), True)
        # A rejected patch is an answer, not an error: the tools read the
        # problem list, which an error status would reduce to one line.
        return web.json_response(res)

    async def h_settings(request):
        sid = request.match_info["id"]
        body = await body_of(request)
        kw = {k: body[k] for k in ("settings", "title", "goal", "sources", "issue") if k in body}
        sim, err = await call(sims.configure, sid, actor_of(body), **kw)
        if err:
            return err
        return web.json_response({"sim": sim})

    async def run_and_report(sid: str, fn, *args):
        res, err = await call(fn, *args)
        if err:
            return err
        sim = await asyncio.to_thread(sims.get, sid)
        ended = res.get("ended")
        return web.json_response({"result": res, "sim": sim, "notified": notify_end(sim, ended)})

    async def h_start(request):
        sid = request.match_info["id"]
        body = await body_of(request)
        return await run_and_report(sid, sims.start, sid, actor_of(body))

    async def h_round(request):
        sid = request.match_info["id"]
        body = await body_of(request)
        return await run_and_report(sid, sims.run_round, sid, actor_of(body))

    async def h_stop(request):
        sid = request.match_info["id"]
        body = await body_of(request)
        return await run_and_report(sid, sims.stop, sid, actor_of(body), str(body.get("reason") or ""))

    async def h_transfer(request):
        sid = request.match_info["id"]
        body = await body_of(request)
        if actor_of(body) != sim_store.USER:
            return web.json_response({"error": "only the user transfers ownership"}, status=403)
        to = str(body.get("to") or "")
        sim, err = await call(sims.transfer, sid, sim_store.USER, to)
        if err:
            return err
        nudged = False
        if body.get("nudge", True):
            nudged = deliver(to, f"{PREFIX} The user made you the owner of simulation {sid} \"{sim['title']}\". "
                                 f"Read it with sim_show {sid}.")
        return web.json_response({"sim": sim, "nudged": nudged})

    async def h_release(request):
        sid = request.match_info["id"]
        body = await body_of(request)
        sim, err = await call(sims.release, sid, actor_of(body))
        if err:
            return err
        return web.json_response({"sim": sim})

    async def h_claim(request):
        sid = request.match_info["id"]
        body = await body_of(request)
        sim, err = await call(sims.claim, sid, str(body.get("session") or ""))
        if err:
            return err
        return web.json_response({"sim": sim})

    async def h_findings(request):
        q = request.query
        try:
            limit = int(q.get("limit") or 100)
        except ValueError:
            limit = 100
        res, err = await call(sims.list_findings, request.match_info["id"], q.get("status") or None,
                              q.get("kind") or None, limit)
        if err:
            return err
        return web.json_response(res)

    async def h_gaps(request):
        try:
            limit = int(request.query.get("limit") or 8)
        except ValueError:
            limit = 8
        res, err = await call(sims.gap_batches, request.match_info["id"], limit)
        if err:
            return err
        return web.json_response({"batches": res})

    async def h_resolve(request):
        sid = request.match_info["id"]
        body = await body_of(request)
        res, err = await call(sims.resolve, sid, actor_of(body), body.get("items") or [], body.get("key"))
        if err:
            return err
        return web.json_response(res)

    async def h_answer(request):
        sid = request.match_info["id"]
        body = await body_of(request)
        if actor_of(body) != sim_store.USER:
            return web.json_response({"error": "questions are answered by the user"}, status=403)
        res, err = await call(sims.answer, sid, sim_store.USER, str(body.get("key") or ""), str(body.get("text") or ""))
        if err:
            return err
        sim = await asyncio.to_thread(sims.get, sid)
        res["nudged"] = deliver(res.get("owner") or "", answered_text(sim, res["finding"]))
        return web.json_response(res)

    async def h_replay(request):
        res, err = await call(sims.replay, request.match_info["id"], request.query.get("key") or "")
        if err:
            return err
        return web.json_response(res)

    async def h_coverage(request):
        res, err = await call(sims.coverage, request.match_info["id"])
        if err:
            return err
        return web.json_response(res)

    async def h_nudge(request):
        sid = request.match_info["id"]
        body = await body_of(request)
        sim, err = await call(sims.get, sid)
        if err:
            return err
        to = str(body.get("to") or sim.get("owner") or "")
        if not to:
            return web.json_response({"error": "nobody owns this simulation; transfer it to a session first"},
                                     status=409)
        return web.json_response({"nudged": deliver(to, nudge_text(sim, str(body.get("text") or ""))), "to": to})

    r = app.router
    r.add_get("/api/sims", h_list)
    r.add_post("/api/sims", h_create)
    r.add_get("/api/sims/profiles", h_profiles)
    r.add_get("/api/sims/{id}", h_get)
    r.add_delete("/api/sims/{id}", h_delete)
    r.add_get("/api/sims/{id}/model", h_model)
    r.add_post("/api/sims/{id}/model", h_patch)
    r.add_post("/api/sims/{id}/settings", h_settings)
    r.add_post("/api/sims/{id}/start", h_start)
    r.add_post("/api/sims/{id}/round", h_round)
    r.add_post("/api/sims/{id}/stop", h_stop)
    r.add_post("/api/sims/{id}/transfer", h_transfer)
    r.add_post("/api/sims/{id}/release", h_release)
    r.add_post("/api/sims/{id}/claim", h_claim)
    r.add_get("/api/sims/{id}/findings", h_findings)
    r.add_get("/api/sims/{id}/gaps", h_gaps)
    r.add_post("/api/sims/{id}/resolve", h_resolve)
    r.add_post("/api/sims/{id}/answer", h_answer)
    r.add_get("/api/sims/{id}/replay", h_replay)
    r.add_get("/api/sims/{id}/coverage", h_coverage)
    r.add_post("/api/sims/{id}/nudge", h_nudge)
    return sims
