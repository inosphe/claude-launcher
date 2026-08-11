"""Per-mesh nudge policies: heartbeat, task-poll and stall warnings.

Ports interconnect's proxy-TUI policy set into the daemon, adapted to the
injection transport (see ``docs/mesh-design.md``). The originals watched
socket/recv state; here the observable member state is (a) the session's
idle tracker and (b) mesh activity — when the daemon last *delivered* into a
member's terminal and when that member last *sent*:

* **heartbeat** — a member had messages delivered but has sent nothing since:
  after ``interval`` (doubling per repeat up to ``max_interval``) inject a
  reminder. The port of interconnect's recv-idle liveness ping.
* **task-poll** — a member that is caught up (nothing pending, nothing
  unanswered) and idle: a role-targeted poke to pull work or leave. Only
  ``roles`` (default: workers) are polled; the text comes from the mesh's
  role set unless overridden here (see :func:`task_poll_body`).
* **stall warning** — a member has held one state too long
  (idle+caught-up, or undeliverable-behind): a real mesh message to every
  member whose role is a ``stall_watch`` role in the mesh's vocabulary
  (the leader by default), so it also crosses machines over federation.

interconnect's fourth tier — tmux send-keys escalation — has no port: every
delivery here *is* an injection, so there is nothing to escalate to.

Policies default **off** per mesh: unlike a socket append, a nudge consumes
the recipient agent's turn, so enabling is a deliberate (web-editable) choice.

All timing state is in-memory (timers restart with the daemon); the *config*
persists in ``mesh.json`` under ``policy``.
"""

from __future__ import annotations

import copy
import logging
import time

from .session import STATUS_IDLE

log = logging.getLogger("claunch.daemon.mesh")

DEFAULT_HEARTBEAT_BODY = (
    "heartbeat: you have unanswered mesh messages. Act on them or reply NOW; "
    "if you already handled them, send a brief ack so the mesh knows. "
    "Do NOT reply to this notice itself."
)
#: Per-role task-poll text now belongs to the ROLE, in the mesh's role set
#: (``mesh_roles``), so a mesh that defines its own roles gets bodies to match
#: without a second edit here. ``task_poll.bodies`` stays as the per-mesh
#: OVERRIDE and therefore starts empty — see :func:`task_poll_body`.
TASK_POLL_FALLBACK_BODY = (
    "you are idle and caught up -- engage if there is work for a {role} "
    "here, otherwise stay parked. Do NOT reply to this notice."
)

#: The handle stall warnings are sent as (external sender, delivered like any
#: other mesh message — including to remote leaders over federation).
POLICY_SENDER = "policy"

_MIN_SECS, _MAX_SECS = 1.0, 86400.0


def default_policy() -> dict:
    return {
        "heartbeat": {
            "enabled": False,
            "interval": 180.0,
            "max_interval": 1800.0,
            "body": DEFAULT_HEARTBEAT_BODY,
        },
        "task_poll": {
            "enabled": False,
            "interval": 600.0,
            "max_interval": 3600.0,
            "roles": ["worker"],
            "bodies": {},
        },
        "stall_warn": {
            "enabled": False,
            "warn_secs": 600.0,
        },
    }


class PolicyError(Exception):
    pass


def _num(value, lo=_MIN_SECS, hi=_MAX_SECS) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        raise PolicyError(f"not a number: {value!r}") from None
    if not (lo <= f <= hi):
        raise PolicyError(f"{f} out of range [{lo}, {hi}]")
    return f


def merge_policy(base: dict, patch: dict) -> dict:
    """Validated deep-merge of a (partial) policy patch over ``base``.

    Unknown sections/keys are rejected rather than ignored — a typo in a
    config edit must fail loudly, not silently change nothing.
    """
    if not isinstance(patch, dict):
        raise PolicyError("policy patch must be an object")
    out = copy.deepcopy(base)
    for section, fields in patch.items():
        if section not in out:
            raise PolicyError(f"unknown policy section {section!r}")
        if not isinstance(fields, dict):
            raise PolicyError(f"policy section {section!r} must be an object")
        for key, value in fields.items():
            if key not in out[section]:
                raise PolicyError(f"unknown key {section}.{key}")
            if key == "enabled":
                out[section][key] = bool(value)
            elif key in ("interval", "max_interval"):
                out[section][key] = _num(value)
            elif key == "warn_secs":
                out[section][key] = _num(value, lo=0.0)  # 0 disables
            elif key == "roles":
                if not isinstance(value, list) or not all(
                    isinstance(r, str) and r for r in value
                ):
                    raise PolicyError("task_poll.roles must be a list of role names")
                out[section][key] = [r.strip().lower() for r in value]
            elif key == "body":
                out[section][key] = " ".join(str(value).split())[:500]
            elif key == "bodies":
                if not isinstance(value, dict):
                    raise PolicyError("task_poll.bodies must be {role: text}")
                out[section][key] = {
                    str(r).strip().lower(): " ".join(str(t).split())[:500]
                    for r, t in value.items()
                }
    return out


def load_policy(doc) -> dict:
    """Policy as persisted (possibly partial/old) -> full validated policy."""
    if not isinstance(doc, dict):
        return default_policy()
    try:
        return merge_policy(default_policy(), doc)
    except PolicyError as exc:
        log.warning("ignoring bad persisted mesh policy: %s", exc)
        return default_policy()


def format_nudge(mesh_name: str, kind: str, handle: str, body: str) -> str:
    """The injected block for an in-band nudge (heartbeat / task-poll)."""
    return (
        "---\n"
        "# claunch mesh policy: automated nudge -- machine-generated, not typed by the user\n"
        f"mesh: {mesh_name}\n"
        f"kind: {kind}\n"
        f"to: {handle}\n"
        f"note: {body}\n"
        "---"
    )


def task_poll_body(mesh, task_poll: dict, role: str) -> str:
    """The task-poll text for ``role``, most specific source first.

    The mesh's own ``task_poll.bodies`` wins — it is the per-mesh edit made
    right here in the policy editor. Below it sits the role set's ``task_poll``
    (a property of what the role *is*, and the only one that travels with a
    custom role), and below that the generic interpolated line.

    Only the fallback is ``format``-ed: an authored body is used verbatim, so
    a stray brace in one is text rather than a KeyError that would take the
    whole nudge down.
    """
    authored = (task_poll.get("bodies") or {}).get(role)
    if authored:
        return authored
    role_def = mesh.roleset.get(role)
    if role_def is not None and role_def.task_poll:
        return role_def.task_poll
    return TASK_POLL_FALLBACK_BODY.format(role=role)


def stall_body(handle: str, role: str, *, secs: float, behind: int) -> str:
    what = (
        f"behind -- {behind} message(s) delivered-pending, its session never went idle"
        if behind
        else "idle and caught up with nothing to do"
    )
    minutes = int(secs // 60)
    return (
        f"stall: {handle} ({role}) has been {what} for {minutes} min. "
        f"If this is unexpected, check on {handle}."
    )


async def tick(mm, mesh) -> None:
    """One policy evaluation pass over ``mesh``'s members. Primary-only.

    Runs on the mesh's owner daemon exclusively (a mirror's tick is a
    guarded no-op — federation v2). Local members are observed through
    their sessions directly; remote (guest) members through the activity
    reports their daemons piggyback on sync acks. Nudges for remote
    members become fanout instructions, injected by the guest daemon
    (which re-checks idleness at fire time, as we do locally).
    """
    if getattr(mesh, "primary", ""):
        return  # the engine lives on the primary daemon only
    pol = mesh.policy
    hb, tp, sw = pol["heartbeat"], pol["task_poll"], pol["stall_warn"]
    if not (hb["enabled"] or tp["enabled"] or sw["enabled"]):
        return
    now = time.monotonic()
    # Who hears about a stalled member is the ROLE SET's call, not a constant
    # here: a mesh that renamed its lead role (or wants two roles watching)
    # says so in its vocabulary. Defaults to leader alone.
    watching = set(mesh.roleset.stall_watchers())
    watchers = sorted(
        h for h, m in mesh.members.items() if m.role in watching
    )
    for handle, member in list(mesh.members.items()):
        local = mm._is_local(mesh, member)
        session = None
        if local:
            st = mesh.activity.setdefault(handle, {"anchor": now})
            try:
                session = mm.manager.get(member.session)
            except Exception:  # ManagerError — session removed
                continue
            if session.exited:
                continue
            idle = session.status() == STATUS_IDLE
            last_sent = st.get("last_sent", 0.0)
            last_delivered = st.get("last_delivered", 0.0)
            # last_asked marks the newest *reply-expecting* delivery (fyi/ack
            # traffic never arms the heartbeat — expects_reply in mesh.py).
            last_asked = st.get("last_asked", 0.0)
            unanswered = last_asked > 0 and last_sent < last_asked
            pending = len(mesh.pending(handle))
            caught_up = not unanswered and pending == 0
            active_at = max(last_sent, last_delivered, st["anchor"])
            first_pending = mesh._first_pending.get(handle)
            hb_base = last_asked
        else:
            rep = mesh.remote_activity.get(handle)
            if not isinstance(rep, dict):
                continue  # the guest has not reported yet
            reported_at = float(rep.get("at") or 0.0)
            if now - reported_at > max(15.0, 3 * mm.report_interval):
                continue  # stale report; wait for a fresh sync ack
            st = mesh.activity.setdefault(handle, {"anchor": now})
            idle = bool(rep.get("idle"))
            unanswered = bool(rep.get("unanswered"))
            caught_up = bool(rep.get("caught_up"))
            pending = int(rep.get("pending") or 0)
            active_at = reported_at - float(rep.get("active_ago") or 0.0)
            fpa = rep.get("first_pending_ago")
            first_pending = (
                reported_at - float(fpa) if fpa is not None else None
            )
            hb_base = reported_at

        # -- heartbeat: asked something, no reply since --------------------- #
        if hb["enabled"] and unanswered and idle:
            due = st.get("hb_next", hb_base + hb["interval"])
            if now >= due:
                await _dispatch(
                    mm, mesh, member, session, "heartbeat", handle, hb["body"]
                )
                backoff = min(
                    st.get("hb_backoff", hb["interval"]) * 2, hb["max_interval"]
                )
                st["hb_backoff"] = backoff
                st["hb_next"] = now + backoff
        elif not unanswered:
            st.pop("hb_next", None)
            st.pop("hb_backoff", None)

        # -- task-poll: caught up, idle, polled role ------------------------ #
        if (
            tp["enabled"]
            and idle
            and caught_up
            and member.role in tp["roles"]
        ):
            due = st.get("tp_next", active_at + tp["interval"])
            if now >= due:
                body = task_poll_body(mesh, tp, member.role)
                await _dispatch(
                    mm, mesh, member, session, "task-poll", handle, body
                )
                backoff = min(
                    st.get("tp_backoff", tp["interval"]) * 2, tp["max_interval"]
                )
                st["tp_backoff"] = backoff
                st["tp_next"] = now + backoff
        elif not caught_up:
            st.pop("tp_next", None)
            st.pop("tp_backoff", None)

        # -- stall warning: one state held too long -> tell the watchers --- #
        if (
            sw["enabled"]
            and sw["warn_secs"] > 0
            and watchers
            and handle not in watchers
        ):
            stalled_idle = idle and caught_up and now - active_at >= sw["warn_secs"]
            # "behind": messages await this member but its session never goes
            # idle enough (or delivery keeps failing) — the injection analogue
            # of interconnect's undrained socket.
            stalled_behind = (
                pending > 0
                and first_pending is not None
                and now - first_pending >= sw["warn_secs"]
            )
            if stalled_idle or stalled_behind:
                due = st.get("warn_next", 0.0)
                if now >= due:
                    secs = now - (first_pending if stalled_behind else active_at)
                    body = stall_body(
                        handle, member.role, secs=secs,
                        behind=pending if stalled_behind else 0,
                    )
                    try:
                        # fyi: watchers are informed, not asked — the warning
                        # must not arm their own heartbeat or invite replies.
                        # An ordinary mesh message, so it federates to remote
                        # watchers through the normal fanout.
                        mm._send_core(
                            mesh, POLICY_SENDER, watchers, body,
                            external=True, type="fyi",
                        )
                        mm._flush_guests_soon(mesh)
                    except Exception as exc:  # noqa: BLE001
                        log.debug("mesh %r: stall warn failed: %s", mesh.name, exc)
                    backoff = min(
                        st.get("warn_backoff", sw["warn_secs"]) * 2, _MAX_SECS
                    )
                    st["warn_backoff"] = backoff
                    st["warn_next"] = now + backoff
            else:
                st.pop("warn_next", None)
                st.pop("warn_backoff", None)


async def _dispatch(mm, mesh, member, session, kind, handle, body) -> None:
    """Deliver a nudge decision: inject locally, or queue for the member's
    guest daemon to inject on its next sync."""
    if session is not None:
        await _inject(mm, session, mesh, kind, handle, body)
        return
    mesh.pending_nudges.setdefault(member.machine, []).append(
        {"handle": handle, "kind": kind, "body": body}
    )
    mm._flush_guests_soon(mesh)
    log.info("mesh %r: %s nudge queued for %r (guest %r)",
             mesh.name, kind, handle, member.machine)


async def _inject(mm, session, mesh, kind: str, handle: str, body: str) -> None:
    block = format_nudge(mesh.name, kind, handle, body)
    if not await session.deliver(block):
        return  # logged by deliver(); the next tick re-fires the nudge
    log.info("mesh %r: %s nudge -> %r", mesh.name, kind, handle)
