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

The run session. When a simulation's ``interpreter_profile`` names a pi
profile and the Node runtime is installed (:mod:`claude_launcher.sim.runtime`),
starting it launches a pi session (kind ``pi-durable``) whose extension
(``sim/pi/runner.mjs``) does the interpreting: it formalizes the sources, has
the judge profile check every quoted rule, runs rounds and interprets the gap
batches, each question in its own durable conversation. The run session acts
with the owner's authority while attached; stopping the campaign kills it,
its exit ends the campaign, and either way the owner is told. Without a
profile or runtime the owner session does the interpreting itself (the S1
flow), and the start response says why.

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
import json
import logging
import re
from typing import Optional

from aiohttp import web

from .. import store as config_store
from ..sim import store as sim_store
from . import paths

log = logging.getLogger(__name__)

PREFIX = "[claunch simulation]"


#: ``<simulation id>-run<n>``: the name launch_runner gives a run session.
RUN_SESSION_RE = re.compile(r"^(sim-[0-9a-f]{6,12})-run[0-9]+$")


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


def pi_models(names) -> dict:
    """Model ids each profile offers pi, for profiles whose provider pi can
    be handed (an OAuth-only profile runs pi on its own login and cannot be
    registered as a second provider, so it cannot judge)."""
    from .. import harnesses, pi_provider, profile as profile_mod
    harness = harnesses.registry(config_store.load()).get("pi")
    out = {}
    for name in names:
        try:
            proj = pi_provider.resolve(profile_mod.require_selector(name), harness)
        except Exception:  # noqa: BLE001 -- not a choice
            continue
        if proj is not None:
            out[name] = list(proj.models)
    return out


def _guide() -> str:
    from .. import sim_mcp
    return sim_mcp.GUIDE


#: Settings the machine gives a new simulation (Settings page), each read
#: from the daemon config key ``simulation_<name>``.
DEFAULT_KEYS = ("interpreter_profile", "interpreter_model", "judge_profile", "judge_model")


def _defaults() -> dict:
    cfg = config_store.daemon_config()
    return {k: str(cfg.get("simulation_" + k) or "") for k in DEFAULT_KEYS}


def check_defaults(body: dict, profiles: list, models: dict) -> dict:
    """The defaults a PUT asks for, checked against what this machine can
    run: a profile must launch pi, a model must be one its profile offers
    (the judge's when it has no profile of its own: the interpreter's).
    Empty means "choose in the simulation" / "the profile's default"."""
    out = {}
    for k in DEFAULT_KEYS:
        v = body.get(k, "")
        if not isinstance(v, str):
            raise ValueError(f"{k} must be a string")
        out[k] = v.strip()
    for role in ("interpreter", "judge"):
        prof = out[role + "_profile"]
        if prof and prof not in profiles:
            raise ValueError(f"profile {prof} cannot launch the pi harness here")
        owner = sim_store._profile_of(role, out)
        model = out[role + "_model"]
        if model and model not in (models.get(owner) or []):
            raise ValueError(f"model {model} is not one profile {owner or '(none)'} offers"
                             if owner else f"{role}_model needs a profile to belong to")
    return out


def judge_projection(profile_name: str, session: str) -> dict:
    """The judge profile's provider as the run session registers it next to
    its own: the ``CLAUNCH_PI_*`` values of :func:`pi_provider.apply_env`
    renamed to ``CLAUNCH_SIMJ_*``, and the key it authenticates with. The
    key goes over the daemon's authenticated API to the process that uses
    it and is never written to a session definition."""
    from .. import harnesses, pi_provider, profile as profile_mod, runner
    doc = config_store.load()
    harness = harnesses.registry(doc).get("pi")
    if harness is None:
        raise sim_store.SimError("the pi harness is not declared")
    prof = profile_mod.require_selector(profile_name)
    env: dict = {}
    try:
        pi_provider.apply_env(prof, harness, env)
    except pi_provider.PiProviderError as exc:
        raise sim_store.SimError(f"judge profile {profile_name}: {exc}")
    if not env:
        raise sim_store.SimError(f"judge profile {profile_name} has no provider the pi harness can use")
    pi_provider.apply_session_header(env, session)
    out = {k.replace("CLAUNCH_PI_", "CLAUNCH_SIMJ_", 1): v for k, v in env.items()}
    out["CLAUNCH_SIMJ_PROVIDER"] = "claunch-judge"
    out["CLAUNCH_SIMJ_TOKEN_ENV"] = "CLAUNCH_SIMJ_API_KEY"
    key = runner._api_key_for(prof) if harness.auth == "api-key" else None
    return {"env": out, "key": key or "", "models": json.loads(env.get("CLAUNCH_PI_MODELS") or "[]")}


def ended_text(sim: dict) -> str:
    c = sim.get("counts") or {}
    last = sim.get("last_round") or {}
    return (
        f"{PREFIX} {sim['id']} \"{sim['title']}\" finished: {sim['state']} -- {sim.get('state_reason')}. "
        f"After round {last.get('round', '?')}: {c.get('open', 0)} open, {c.get('question', 0)} waiting on the user, "
        f"{c.get('answered', 0)} answered, {c.get('dismissed', 0)} dismissed, {c.get('resolved', 0)} resolved. "
        + (f"{waiting} edit proposal(s) to the sources wait for the user's decision (sim_proposals {sim['id']}). "
           if (waiting := (sim.get("proposals") or {}).get("proposed")) else "")
        + f"Read the result (sim_show {sim['id']}, sim_findings {sim['id']}) and continue the discussion "
        f"with the user from it, in {_language(sim)}."
    )


def _language(sim: dict) -> str:
    return (sim.get("settings") or {}).get("language") or sim_store.DEFAULT_SETTINGS["language"]


def created_text(sim: dict) -> str:
    goal = (sim.get("goal") or "").strip()
    return (
        f"{PREFIX} The user asked you to build and run simulation {sim['id']} \"{sim['title']}\""
        + (f" -- goal: {goal}" if goal else "") + ". "
        f"You own it. Read it with sim_show {sim['id']}. "
        + (f"It has an interpreter profile: sim_start {sim['id']} launches a run session that builds the model "
           "and works the gaps; "
           if (sim.get("settings") or {}).get("interpreter_profile") else
           "Build the world model from the sources with sim_patch (every rule quotes its source file word for "
           f"word, or is marked provisional with a why), then sim_start {sim['id']} and work the gaps (sim_gaps); ")
        + "you will be told when it finishes. "
        f"Questions you raise (sim_resolve ask) and reasons you give go to the user: write them in {_language(sim)}."
    )


def answered_text(sim: dict, finding: dict) -> str:
    q = (finding.get("question") or {}).get("text", "")
    a = (finding.get("answer") or {}).get("text", "")
    return (
        f"{PREFIX} The user answered your question on {sim['id']} (finding {finding['key']}). "
        f"Q: {q} A: {a} -- put it into the model with sim_patch (source ref user:{finding['key']}, quote the answer)."
    )


def accepted_text(sim: dict, p: dict) -> str:
    where = f"replace this text in {p['file']}:\n<<<\n{p['old']}\n>>>\nwith:\n<<<\n{p['new']}\n>>>" if p["old"] else \
        f"append to {p['file']}:\n<<<\n{p['new']}\n>>>"
    note = (p.get("decision") or {}).get("note") or ""
    return (
        f"{PREFIX} The user accepted edit proposal {p['id']} on simulation {sim['id']} \"{sim['title']}\" "
        f"({p['kind']}: {p['reason']}; settles {', '.join(p['findings'])})" + (f" -- note: {note}" if note else "") +
        f". Apply it to the source and commit as usual: {where}\n"
        "Then run the simulation again (sim_start) so the walk checks it."
    )


def fill_text(sim: dict) -> str:
    """The deferred create: the user left the title or goal for the owner."""
    missing = [k for k in ("title", "goal") if not (sim.get(k) or "").strip()]
    given = [f"{k}: {sim[k].strip()}" for k in ("title", "goal") if (sim.get(k) or "").strip()]
    srcs = sim.get("sources") or []
    return (
        f"{PREFIX} The user asked you to set up and run simulation {sim['id']} and left its "
        f"{' and '.join(missing)} for you to fill" + (f" ({'; '.join(given)})" if given else "") + ". "
        "Decide from the work you are doing (your current task and issue) what feature to simulate and what to "
        f"check, then give it with sim_configure {sim['id']} (title, goal"
        + (")" if srcs else ", and the source files/globs to build from as sources)")
        + f" -- it refuses to start until both are set. You own it; sim_show {sim['id']} has the settings the "
        "user chose. Then "
        + ("sim_start launches a run session that builds the model and works the gaps; "
           if (sim.get("settings") or {}).get("interpreter_profile") else
           "build the world model from the sources with sim_patch (every rule quotes its source file word for "
           "word, or is marked provisional with a why), sim_start and work the gaps (sim_gaps); ")
        + "you will be told when it finishes. "
        f"Write the title, goal, questions and reasons in {_language(sim)}."
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

    def kill_runner(sim: dict) -> None:
        name = (sim.get("runner") or {}).get("session")
        if not name:
            return
        try:
            manager.kill(name)
        except Exception:  # noqa: BLE001 -- already gone
            log.info("sim: could not kill run session %s", name)

    def released(session) -> None:
        name = getattr(getattr(session, "sdef", None), "name", None)
        if not name:
            return
        try:
            for sid in sims.session_exited(name):
                # The owner left: nobody is told when its run ends, so stop it.
                kill_runner(sims.get(sid))
            for sim in sims.runner_exited(name):
                notify_end(sim, True)
        except Exception:  # noqa: BLE001 -- the exit funnel must not break
            log.exception("sim: releasing %s failed", name)

    async def launch_runner(sid: str) -> dict:
        """Launch the run session of a campaign that has just started.
        Returns ``{"session": name}`` or ``{"manual": why}``."""
        from . import harness as harness_mod
        from ..sim import runtime
        sim = await asyncio.to_thread(sims.get, sid)
        profile = sim["settings"].get("interpreter_profile") or ""
        if not profile:
            return {"manual": "no interpreter profile is set; the owner session interprets"}
        if profile not in await asyncio.to_thread(pi_profiles):
            return {"manual": f"profile {profile} cannot launch the pi harness here; the owner session interprets"}
        ext = await asyncio.to_thread(runtime.runner_path)
        if ext is None:
            return {"manual": "the simulation runtime is not installed (claunch sim runtime install); "
                              "the owner session interprets"}
        n = int(sim.get("runner_count") or 0) + 1
        name = f"{sid}-run{n}"
        while name in getattr(manager, "_sessions", {}) or paths.session_dir(name).is_dir():
            n += 1
            name = f"{sid}-run{n}"
        from .. import pi_provider
        sdef = harness_mod.SessionDef(
            name=name, profile=f"{profile}:pi", cwd=sim["root"] or "", kind="pi-durable",
            project=sim.get("project") or None, args=("--extension", str(ext)),
            env={"CLAUNCH_SIM_ID": sid, "CLAUNCH_SIM_STORE": str(sims.root / sid / "durable.sqlite"),
                 "CLAUNCH_SIM_DAEMON_DIR": str(paths.daemon_dir()),
                 "CLAUNCH_SIM_PROVIDER_JS": str(pi_provider.extension_path())},
            note=f"simulation {sid} run session",
        )
        await asyncio.to_thread(sims.attach_runner, sid, name)
        try:
            await manager.create_async(sdef)
        except Exception as exc:  # noqa: BLE001 -- the reason goes back to the caller
            await asyncio.to_thread(sims.runner_done, sid, "daemon", f"launch failed: {exc}", True)
            return {"failed": f"could not launch the run session: {exc}"}
        return {"session": name}

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
        # A deferred end is told at runner/done or the run session's exit.
        if not ended or (isinstance(ended, dict) and ended.get("deferred")) or not sim.get("owner"):
            return False
        return deliver(sim["owner"], ended_text(sim))

    # -- routes ------------------------------------------------------------

    async def h_list(request):
        q = request.query
        owner = q.get("owner")
        rows, err = await call(sims.list, project=q.get("project") or None, owner=owner,
                               compact=q.get("view") == "rail")
        if err:
            return err
        return web.json_response({"sims": rows})

    async def h_profiles(request):
        names = await asyncio.to_thread(pi_profiles)
        models = await asyncio.to_thread(pi_models, names)
        return web.json_response({"profiles": names, "judges": sorted(models), "models": models,
                                  "defaults": _defaults()})

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
            issue=str(body.get("issue") or ""), defaults=_defaults(), from_sim=str(body.get("from") or ""),
            deferred=body.get("fill") is True)
        if err:
            return err
        nudged = False
        if actor == sim_store.USER and owner and body.get("nudge", True):
            nudged = deliver(owner, fill_text(sim) if sim.get("fill_pending") else created_text(sim))
        return web.json_response({"sim": sim, "nudged": nudged}, status=201)

    async def h_defaults(request):
        body = await body_of(request)
        names = await asyncio.to_thread(pi_profiles)
        models = await asyncio.to_thread(pi_models, names)
        try:
            vals = check_defaults(body, names, models)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)

        def write(doc: dict) -> None:
            block = doc.get("daemon")
            if not isinstance(block, dict):
                block = doc["daemon"] = {}
            for k, v in vals.items():
                if v:
                    block["simulation_" + k] = v
                else:
                    block.pop("simulation_" + k, None)
        await asyncio.to_thread(config_store.update, write)
        return web.json_response({"defaults": _defaults()})

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
        # With a run session attached, it runs the rounds itself.
        auto = body.get("round", True) and not await asyncio.to_thread(sims.runner_of, sid)
        if res.get("ok") and not res.get("duplicate") and auto:
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
        actor = actor_of(body)
        sim, err = await call(sims.get, sid)
        if err:
            return err
        running = (sim.get("runner") or {}).get("session")
        if running and _live(manager, running):
            return web.json_response({"error": f"run session {running} is already running {sid}"}, status=409)
        if body.get("runner", True) and sim["settings"].get("interpreter_profile"):
            res, err = await call(sims.start, sid, actor, False)
            if err:
                return err
            launched = await launch_runner(sid)
            if "session" in launched:
                return web.json_response({"result": res, "sim": await asyncio.to_thread(sims.get, sid),
                                          "runner": launched["session"], "notified": False})
            if "failed" in launched:
                sim = await asyncio.to_thread(sims.get, sid)
                return web.json_response({"error": launched["failed"], "sim": sim,
                                          "notified": notify_end(sim, True)}, status=502)
            # No runner possible: the owner interprets, as without a profile.
            resp = await run_and_report(sid, sims.run_round, sid, actor)
            data = json.loads(resp.text)
            data["manual"] = launched["manual"]
            return web.json_response(data, status=resp.status)
        return await run_and_report(sid, sims.start, sid, actor)

    async def h_round(request):
        sid = request.match_info["id"]
        body = await body_of(request)
        return await run_and_report(sid, sims.run_round, sid, actor_of(body))

    async def h_stop(request):
        sid = request.match_info["id"]
        body = await body_of(request)
        sim, err = await call(sims.get, sid)
        if err:
            return err
        resp = await run_and_report(sid, sims.stop, sid, actor_of(body), str(body.get("reason") or ""))
        if resp.status == 200:
            kill_runner(sim)
        return resp

    def require_runner(sid: str, body_or_query) -> Optional[web.Response]:
        who = str(body_or_query.get("session") or "")
        if not who or who != sims.runner_of(sid):
            return web.json_response({"error": f"only the run session of {sid} may do this"}, status=403)
        return None

    async def h_runner(request):
        """The run session's configuration: settings and the judge provider."""
        sid = request.match_info["id"]
        sim, err = await call(sims.get, sid)
        if err:
            return err
        denied = require_runner(sid, request.query)
        if denied:
            return denied
        st = sim["settings"]
        judge = None
        if st.get("judge_profile") and st["judge_profile"] != st.get("interpreter_profile"):
            judge, err = await call(judge_projection, st["judge_profile"], request.query["session"])
            if err:
                return err
        return web.json_response({"sim": sim, "judge": judge, "guide": _guide()})

    async def h_work(request):
        sid = request.match_info["id"]
        try:
            gaps = int(request.query.get("gaps") or 4)
        except ValueError:
            gaps = 4
        res, err = await call(sims.work, sid, gaps)
        if err:
            return err
        return web.json_response(res)

    async def h_verdicts(request):
        sid = request.match_info["id"]
        body = await body_of(request)
        res, err = await call(sims.judge, sid, actor_of(body), body.get("items") or [], body.get("key"))
        if err:
            return err
        return web.json_response(res)

    async def h_proposals(request):
        sid = request.match_info["id"]
        res, err = await call(sims.proposals, sid)
        if err:
            return err
        rows = sorted(res.values(), key=lambda p: p["at"], reverse=True)
        return web.json_response({"proposals": rows})

    async def h_propose(request):
        sid = request.match_info["id"]
        body = await body_of(request)
        res, err = await call(sims.propose, sid, actor_of(body), body.get("items") or [], body.get("key"))
        if err:
            return err
        return web.json_response(res)

    async def h_decide(request):
        sid = request.match_info["id"]
        body = await body_of(request)
        if actor_of(body) != sim_store.USER:
            return web.json_response({"error": "edit proposals are accepted or rejected by the user"}, status=403)
        res, err = await call(sims.decide_proposal, sid, sim_store.USER, request.match_info["pid"],
                              bool(body.get("accept")), str(body.get("note") or ""))
        if err:
            return err
        res["nudged"] = False
        if res["proposal"]["status"] == "accepted":
            sim = await asyncio.to_thread(sims.get, sid)
            res["nudged"] = deliver(res["owner"], accepted_text(sim, res["proposal"]))
        return web.json_response(res)

    async def h_triage(request):
        sid = request.match_info["id"]
        body = await body_of(request)
        res, err = await call(sims.triage, sid, actor_of(body), body.get("items") or [], body.get("key"))
        if err:
            return err
        return web.json_response(res)

    async def h_runner_done(request):
        sid = request.match_info["id"]
        body = await body_of(request)
        denied = require_runner(sid, body)
        if denied:
            return denied
        res, err = await call(sims.runner_done, sid, body["session"], str(body.get("reason") or ""),
                              bool(body.get("failed")))
        if err:
            return err
        return web.json_response({"result": res, "notified": notify_end(res, res.get("ended"))})

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

    async def h_events(request):
        q = request.query
        try:
            since = int(q.get("since") or 0)
        except ValueError:
            return web.json_response({"error": "since must be an integer"}, status=400)
        res, err = await call(sims.durable_events, request.match_info["id"], q.get("session") or "", since)
        if err:
            return err
        return web.json_response(res)

    async def h_session_durable(request):
        """A pi-durable session's own events, found from the session: the
        chart pane of its terminal reads this. Two producers: a simulation's
        run session (env ``CLAUNCH_SIM_ID``, or the name ``<sid>-run<n>``)
        logs into the simulation store; any other runner that names its run
        directory in env ``SIM_RUN_DIR`` (gds6's sim2) keeps the log there."""
        name = request.match_info["name"]
        try:
            since = int(request.query.get("since") or 0)
        except ValueError:
            return web.json_response({"error": "since must be an integer"}, status=400)
        session = None
        try:
            session = manager.get(name)
        except Exception:  # noqa: BLE001 -- gone from the manager: the name still says which simulation
            pass
        env = (getattr(session.sdef, "env", None) or {}) if session is not None else {}
        sid = str(env.get("CLAUNCH_SIM_ID") or "")
        if not sid:
            m = RUN_SESSION_RE.match(name)
            sid = m.group(1) if m else ""
        run_dir = str(env.get("SIM_RUN_DIR") or "")
        if sid:
            res, err = await call(sims.durable_events, sid, name, since)
        elif run_dir:
            res, err = await call(sim_store.run_dir_events, run_dir, since)
        else:
            return web.json_response(
                {"error": f"{name} keeps no pi-durable event log "
                          "(neither CLAUNCH_SIM_ID nor SIM_RUN_DIR is set for it)"}, status=404)
        if err:
            return err
        live = session is not None and not session.exited
        res["live"] = live
        res["progress"] = manager.progress.get(name) if live and hasattr(manager, "progress") else None
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
        text = str(body.get("text") or "")
        # Asked again to continue one still waiting for its title or goal:
        # the request it needs is the fill request.
        msg = fill_text(sim) if sim.get("fill_pending") and not text.strip() else nudge_text(sim, text)
        return web.json_response({"nudged": deliver(to, msg), "to": to})

    r = app.router
    r.add_get("/api/sims", h_list)
    r.add_post("/api/sims", h_create)
    r.add_get("/api/sims/profiles", h_profiles)
    r.add_put("/api/sims/defaults", h_defaults)
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
    r.add_get("/api/sims/{id}/events", h_events)
    r.add_get("/api/sessions/{name}/durable", h_session_durable)
    r.add_post("/api/sims/{id}/nudge", h_nudge)
    r.add_get("/api/sims/{id}/runner", h_runner)
    r.add_post("/api/sims/{id}/runner/done", h_runner_done)
    r.add_get("/api/sims/{id}/work", h_work)
    r.add_post("/api/sims/{id}/verdicts", h_verdicts)
    r.add_post("/api/sims/{id}/triage", h_triage)
    r.add_get("/api/sims/{id}/proposals", h_proposals)
    r.add_post("/api/sims/{id}/proposals", h_propose)
    r.add_post("/api/sims/{id}/proposals/{pid}/decide", h_decide)
    return sims
