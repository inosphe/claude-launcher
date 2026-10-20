"""HTTP surface of mega threads (claunch-v5nhp).

A mega thread is created from the dashboard, members join as
``(session, thread)`` pairs -- a session with no open thread gets one opened
for it, so the pair always exists -- and the user posts one message that the
router (``daemon/megathread_route.py``) either delivers to the single member
that cleared the threshold or offers to the user as a short list.

Delivery is the threads path and nothing else: the message becomes a normal
user message in the chosen thread (``app["threads_post_user"]``), which types
it into that session and any guests exactly as the threads pane does. The
members that were not chosen are not written to at all; there is no shared
log for them to read later, which is what "does not know about this
conversation" has to mean.

Endpoints (``/api/megathreads``):
  GET    ?closed=          list clusters
  POST   {title, members?} create; members join in the same call
  GET    /settings         the router's policy and the rolling budget in use
  PUT    /settings         partial update of that policy
  GET    /{id}             one cluster with its members
  POST   /{id}/members     {session, thread?} join (thread opened when absent)
  DELETE /{id}/members/{thread}
  POST   /{id}/route       {text} judge, or {text, choose: [thread, ...]} deliver
  GET    /{id}/routes      the routing log, newest first
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Optional

from aiohttp import web

from .. import store as store_mod
from ..megathread import store as mega_store
from ..threads import store as thread_store
from . import jev, megathread_route as route_mod, paths

log = logging.getLogger(__name__)


def _cfg() -> dict:
    return store_mod.daemon_config()


async def _body(request: web.Request) -> dict:
    try:
        body = await request.json()
    except (ValueError, UnicodeError):
        return {}
    return body if isinstance(body, dict) else {}


def install(app, store: Optional[mega_store.MegaThreadStore] = None):
    manager = app["manager"]
    threads = app["threads"]
    megas = store or mega_store.MegaThreadStore(paths.megathread_db())
    app["megathreads"] = megas

    def fail(exc: Exception) -> web.Response:
        status = getattr(exc, "status", 400)
        return web.json_response({"error": str(exc)}, status=status)

    # -- membership --------------------------------------------------------
    def known(name: str) -> bool:
        try:
            manager.get(name)
            return True
        except Exception:  # noqa: BLE001 -- no such session
            return False

    def open_thread(session: str) -> str:
        """The thread a session joins with: its most recent open one, or a
        new one named after the session."""
        listing = threads.list(session, closed=False)
        if listing["threads"]:
            return listing["threads"][0]["id"]
        res = threads.open(thread_store.USER, session, session)
        return res["thread"]["id"]

    def join(mid: str, session: str, tid: Optional[str] = None) -> dict:
        if not known(session):
            raise mega_store.NotFound(f"no session {session!r} on this daemon")
        if tid:
            thread = threads.get(tid)
            if thread["session"] != session:
                raise mega_store.Conflict(
                    f"thread {tid} belongs to {thread['session']}, not {session}")
        else:
            tid = open_thread(session)
        return megas.add_member(mid, session, tid)

    def member_view(member: dict) -> dict:
        """A member with what the dashboard shows beside it: whether the
        session is up, and the thread's title and unread count."""
        name, tid = member["session"], member["thread"]
        out = dict(member)
        try:
            out["status"] = (manager.get(name).info() or {}).get("status") or "unknown"
        except Exception:  # noqa: BLE001
            out["status"] = "gone"
        try:
            thread = threads.get(tid)
            out.update({
                "thread_title": thread["title"],
                "thread_status": thread["status"],
                "unread": thread.get("agent_unread", 0),
            })
        except Exception:  # noqa: BLE001 -- the thread was dropped outside
            out.update({"thread_title": "", "thread_status": "gone", "unread": 0})
        return out

    def cluster_view(cluster: dict) -> dict:
        return {**cluster, "members": [member_view(m) for m in cluster["members"]]}

    # -- handlers ----------------------------------------------------------
    async def h_list(request: web.Request) -> web.Response:
        closed = (request.query.get("closed") or "") not in ("0", "false", "no")
        res = await asyncio.to_thread(megas.list, closed=closed)
        return web.json_response({**res, "megathreads": [cluster_view(m) for m in res["megathreads"]]})

    async def h_create(request: web.Request) -> web.Response:
        body = await _body(request)
        try:
            cluster = await asyncio.to_thread(megas.create, body.get("title"), actor="user")
        except mega_store.MegaError as exc:
            return fail(exc)
        joined = []
        for entry in body.get("members") or []:
            if not isinstance(entry, dict) or not entry.get("session"):
                continue
            try:
                res = await asyncio.to_thread(
                    join, cluster["id"], str(entry["session"]),
                    str(entry["thread"]) if entry.get("thread") else None)
                joined.append({"session": entry["session"],
                               "thread": entry.get("thread"), "added": res["added"]})
            except mega_store.MegaError as exc:
                joined.append({"session": entry.get("session"), "error": str(exc)})
        cluster = await asyncio.to_thread(megas.get, cluster["id"])
        return web.json_response({"mega": cluster_view(cluster), "joined": joined})

    async def h_get(request: web.Request) -> web.Response:
        try:
            cluster = await asyncio.to_thread(megas.get, request.match_info["id"])
        except mega_store.MegaError as exc:
            return fail(exc)
        return web.json_response({"mega": cluster_view(cluster)})

    async def h_add_member(request: web.Request) -> web.Response:
        body = await _body(request)
        session = str(body.get("session") or "")
        if not session:
            return web.json_response({"error": "session is required"}, status=400)
        tid = str(body["thread"]) if body.get("thread") else None
        try:
            res = await asyncio.to_thread(join, request.match_info["id"], session, tid)
        except mega_store.MegaError as exc:
            return fail(exc)
        return web.json_response({"mega": cluster_view(res["mega"]), "added": res["added"]})

    async def h_remove_member(request: web.Request) -> web.Response:
        try:
            res = await asyncio.to_thread(
                megas.remove_member, request.match_info["id"], request.match_info["thread"])
        except mega_store.MegaError as exc:
            return fail(exc)
        return web.json_response({"mega": cluster_view(res["mega"]), "removed": res["removed"]})

    async def h_routes(request: web.Request) -> web.Response:
        try:
            res = await asyncio.to_thread(
                megas.routes, request.match_info["id"], limit=request.query.get("limit") or 50)
        except mega_store.MegaError as exc:
            return fail(exc)
        return web.json_response(res)

    async def h_settings(request: web.Request) -> web.Response:
        cfg = await asyncio.to_thread(_cfg)
        pol = route_mod.policy(cfg)
        used = await asyncio.to_thread(megas.usage_tokens)
        key, source = jev.resolve_key(cfg)
        return web.json_response({"settings": {
            **pol,
            "model_pin": str(cfg.get("jev_model_megathread") or ""),
            "budget_used_tokens": used,
            "budget_remaining_tokens": max(0, pol["budget_tokens"] - used) if pol["budget_tokens"] else None,
            "key_set": bool(key),
            "key_source": source,
        }})

    async def h_put_settings(request: web.Request) -> web.Response:
        body = await _body(request)
        numbers = {
            "threshold_ratio": ("megathread_threshold_ratio", 1.0, None),
            "budget_tokens": ("megathread_jev_budget_tokens", 0.0, None),
            "max_chars": ("megathread_max_chars", 200.0, 60000.0),
            "concurrency": ("megathread_concurrency", 1.0, 32.0),
        }
        writes = []
        for field, (key, low, high) in numbers.items():
            if field not in body:
                continue
            try:
                value = float(body[field])
            except (TypeError, ValueError):
                return web.json_response({"error": f"'{field}' must be a number"}, status=400)
            # NaN compares false against every bound, so it would slip past the
            # span check and then sit in the config: a budget that is NaN
            # raises on every routing, and a NaN threshold would make no
            # member clear it.
            if not math.isfinite(value):
                return web.json_response({"error": f"'{field}' must be a finite number"}, status=400)
            if value < low or (high is not None and value > high):
                span = f"{low:g}..{high:g}" if high is not None else f">= {low:g}"
                return web.json_response({"error": f"'{field}' must be {span}"}, status=400)
            writes.append((key, int(value) if field != "threshold_ratio" else value))
        if "model" in body:
            model = body["model"]
            if model is not None and not isinstance(model, str):
                return web.json_response({"error": "'model' must be a string"}, status=400)
            # An empty model clears the pin: the feature follows jev_model.
            writes.append(("jev_model_megathread", (model or "").strip() or None))
        try:
            for key, value in writes:
                await asyncio.to_thread(store_mod.set_daemon_field, key, value)
        except store_mod.StoreError as exc:
            return web.json_response({"error": str(exc)}, status=500)
        return await h_settings(request)

    # -- routing -----------------------------------------------------------
    #: One lock per cluster, for the budget check -> Jev calls -> log window.
    locks: dict = {}

    def cluster_lock(mid: str) -> asyncio.Lock:
        """One routing at a time per cluster.

        The budget is read, the calls are made, and the spend is written in
        that order. Two routings of the same cluster at once would each read
        the same usage and each pass a ceiling that only one of them fits
        under. The lock is per cluster, so a ten-member routing does not hold
        up a one-member routing somewhere else. The window also covers
        delivery, because the log row is what the next routing checks.
        """
        lock = locks.get(mid)
        if lock is None:
            lock = locks[mid] = asyncio.Lock()
        return lock

    async def ledger(mid: str, text: str, **fields):
        """Write one routing to the log; return ``(row, error)``.

        A failure here must not turn a message the user already received into
        a 500: the answer still says what was delivered, and ``ledger_error``
        says the accounting for it did not land.
        """
        try:
            return await asyncio.to_thread(megas.record_route, mid, text, **fields), None
        except Exception as exc:  # noqa: BLE001 -- the delivery already happened
            log.exception("megathread: could not log the routing of %s", mid)
            return None, str(exc)

    def deliver(chosen: list[dict], text: str) -> list[dict]:
        """Post the message into each chosen thread; nothing else is written."""
        post = app.get("threads_post_user")
        out = []
        for member in chosen:
            tid = member["thread"]
            try:
                if post is None:
                    raise RuntimeError("threads_api is not installed")
                res = post(tid, text)
                out.append({
                    "thread": tid,
                    "session": member.get("session") or res["thread"]["session"],
                    "delivered": bool(res.get("delivered")),
                    "hold": res.get("hold"),
                })
            except Exception as exc:  # noqa: BLE001 -- one thread, one report
                log.info("megathread: could not deliver to %s: %s", tid, exc)
                out.append({"thread": tid, "session": member.get("session"), "delivered": False,
                            "error": str(exc)})
        return out

    async def h_route(request: web.Request) -> web.Response:
        mid = request.match_info["id"]
        body = await _body(request)
        text = str(body.get("text") or "").strip()
        if not text:
            return web.json_response({"error": "text is required"}, status=400)
        try:
            cluster = await asyncio.to_thread(megas.get, mid)
        except mega_store.MegaError as exc:
            return fail(exc)
        if cluster["status"] != "open":
            return web.json_response(
                {"error": f"mega thread {mid} is {cluster['status']}; only an open one routes"},
                status=409)
        members = cluster["members"]
        if not members:
            return web.json_response({"error": "this mega thread has no members"}, status=400)

        cfg = await asyncio.to_thread(_cfg)
        pol = route_mod.policy(cfg)

        # The user already chose (the answer to an earlier "choose" or
        # "none"): deliver without asking Jev again.
        choose = body.get("choose")
        if choose is not None:
            if not isinstance(choose, list) or not choose:
                return web.json_response({"error": "'choose' is a list of thread ids"}, status=400)
            by_thread = {m["thread"]: m for m in members}
            # A repeated id is the same thread, and posting twice would put the
            # message in it twice.
            picked_ids: list[str] = []
            for entry in choose:
                tid = str(entry)
                if tid not in picked_ids:
                    picked_ids.append(tid)
            unknown = [tid for tid in picked_ids if tid not in by_thread]
            if unknown:
                return web.json_response(
                    {"error": f"not members of {mid}: {', '.join(unknown)}"}, status=400)
            picked = [by_thread[tid] for tid in picked_ids]
            sent = await asyncio.to_thread(deliver, picked, text)
            row, ledger_error = await ledger(
                mid, text, verdict="manual", threshold=pol["threshold"],
                candidates=[], chosen=picked_ids, tokens=0,
                model=pol["model"], delivered=any(s.get("delivered") for s in sent))
            return web.json_response({
                "verdict": "manual", "delivered": sent, "route": row,
                "ledger_error": ledger_error,
                "budget_used_tokens": await asyncio.to_thread(megas.usage_tokens),
            })

        async with cluster_lock(mid):
            # Budget: refuse before spending, not after. The estimate is rough
            # on purpose (a state is assembled per member), and over-counting
            # only means the ceiling bites slightly early.
            if pol["budget_tokens"]:
                used = await asyncio.to_thread(megas.usage_tokens)
                estimate = len(members) * (pol["max_chars"] // route_mod.CHARS_PER_TOKEN + 200) \
                    + len(text) // route_mod.CHARS_PER_TOKEN
                if used + estimate > pol["budget_tokens"]:
                    exc = route_mod.BudgetExceeded(used, pol["budget_tokens"], estimate)
                    return web.json_response(
                        {"error": str(exc), "used_tokens": used,
                         "limit_tokens": pol["budget_tokens"],
                         "estimated_tokens": estimate}, status=429)

            judgment = await route_mod.evaluate(manager, threads, members, text, cfg=cfg)
            delivered = []
            if judgment["verdict"] == "auto":
                delivered = await asyncio.to_thread(deliver, judgment["options"], text)
            row, ledger_error = await ledger(
                mid, text,
                verdict=judgment["verdict"], threshold=judgment["threshold"],
                candidates=[{k: r.get(k) for k in ("session", "thread", "p_mine", "p_not_mine",
                                                   "logit", "choice", "confidence", "error")}
                            for r in judgment["candidates"]],
                chosen=[m["thread"] for m in judgment["options"]]
                if judgment["verdict"] == "auto" else [],
                tokens=judgment.get("tokens") or 0, model=judgment.get("model") or pol["model"],
                # The list itself only says a delivery was attempted. The flag
                # is whether any thread actually accepted the message; an
                # exited session leaves a row but types nothing.
                delivered=any(d.get("delivered") for d in delivered),
                error="; ".join(f"{e['thread']}: {e['error']}"
                                for e in judgment.get("errors") or [])[:500])
            used = await asyncio.to_thread(megas.usage_tokens)
        return web.json_response({
            **judgment,
            "delivered": delivered,
            "route": row,
            "ledger_error": ledger_error,
            "budget_used_tokens": used,
            "budget_remaining_tokens": (max(0, pol["budget_tokens"] - used)
                                        if pol["budget_tokens"] else None),
        })

    # -- wiring ------------------------------------------------------------
    # No exit hook: a session that ends keeps its seat. Membership is the
    # user's arrangement, not a liveness fact, and a resumed session with the
    # same name and thread would otherwise find itself outside the cluster it
    # was put in. A member whose session is down shows as ``gone`` instead
    # (``member_view``), and delivery to its thread still goes through the
    # threads path.
    r = app.router
    r.add_get("/api/megathreads", h_list)
    r.add_post("/api/megathreads", h_create)
    r.add_get("/api/megathreads/settings", h_settings)
    r.add_put("/api/megathreads/settings", h_put_settings)
    r.add_get("/api/megathreads/{id}", h_get)
    r.add_get("/api/megathreads/{id}/routes", h_routes)
    r.add_post("/api/megathreads/{id}/members", h_add_member)
    r.add_delete("/api/megathreads/{id}/members/{thread}", h_remove_member)
    r.add_post("/api/megathreads/{id}/route", h_route)
    return megas
