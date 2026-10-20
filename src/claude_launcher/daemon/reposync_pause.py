"""The merge pause a running repository sync holds (claunch-z6da6).

A sync agent (:mod:`daemon.reposync_agent`, workflow ``reposync-merge-push-v2``)
plans against a snapshot of the local branch. If the leader merges into that
branch while the plan is being prepared, checked, reviewed and approved, the
approval card fails with "original source changed; replan" and the whole
round is lost (s1116 against d73e6029, merged over by 7cd1e969).

So while such a run stands in ``prepare`` .. ``execute`` the repository is
paused for ordinary merges: ``tools/delayed_sweep.py admit`` asks this module
(through ``GET /api/reposync/pause``) and refuses every candidate except the
sync's own issue. ``inspect`` does not pause; a master that moved meanwhile
is replanned when ``prepare`` begins.

Nothing writes the pause: it is read off the live state every time, so it
ends by itself when the run reaches ``receipt``, finishes or is aborted, or
the session ends. Two bounds keep a stuck sync from holding the repository:

* after the agent filed an approval card -- the card's deadline plus
  :data:`CARD_GRACE` (user decision qa-6061787c); a newer card replaces the
  deadline;
* before any card -- the run's last progress plus the daemon setting
  ``reposync_pause_precard_s`` (default 1800 s). Every report the run takes
  renews it. The 30 minutes is the leader's assumption, not a measured value,
  which is why it is a setting.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Optional

from . import reposync_agent

#: The nodes of ``reposync-merge-push-v2`` that hold the pause.
SPAN = ("prepare", "checks", "review", "execute")

#: How long the pause outlives the approval card's deadline (seconds).
CARD_GRACE = 600.0

#: Default of the ``reposync_pause_precard_s`` daemon setting (seconds).
PRECARD_DEFAULT = 1800.0

#: The workflow whose runs pause the repository.
WORKFLOW = reposync_agent.WORKFLOWS["agent"]


def _parse(when: Any) -> Optional[datetime]:
    try:
        stamp = datetime.fromisoformat(str(when))
    except (TypeError, ValueError):
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def _iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).isoformat(timespec="seconds")


def node_of(instance: str) -> str:
    """``prepare`` for an active instance id such as ``prepare@3``."""
    return str(instance).split("@", 1)[0].rsplit("/", 1)[-1]


def span_node(state: dict) -> Optional[str]:
    """The paused node a live run state stands in, or None."""
    from ..cflow2 import runtime

    if state.get("status") not in runtime.LIVE_STATUSES or state.get("workflow") != WORKFLOW:
        return None
    nodes = [node_of(i) for i in state.get("active") or []]
    return next((n for n in nodes if n in SPAN), None)


def base_local(state: dict) -> str:
    """The local SHA the sync planned against (the assignment's ``local``)."""
    raw = ((state.get("data") or {}).get("inputs") or {}).get("assignment")
    try:
        card = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except ValueError:
        return ""
    return str(card.get("local") or "") if isinstance(card, dict) else ""


def latest_card(cards: Iterable[dict], session: str) -> Optional[dict]:
    """The newest approval card ``session`` filed (a newer card replaces an older one)."""
    mine = [c for c in cards if c.get("session") == session and c.get("deadline")]
    return max(mine, key=lambda c: c.get("submitted_at") or "", default=None)


def compute(state: dict, *, session: str, issue: Optional[str], cards: Iterable[dict],
            precard: float, now: Optional[datetime] = None) -> Optional[dict]:
    """The pause one sync session's run holds, or None when it holds none.

    The answer carries ``expired: True`` when a bound ran out: the run still
    stands in the span, but merges are no longer refused."""
    node = span_node(state)
    if node is None:
        return None
    now = now or datetime.now(timezone.utc)
    card = latest_card(cards, session)
    if card is not None:
        until = _parse(card["deadline"]) + timedelta(seconds=CARD_GRACE)
        bound = "card"
    else:
        progress = _parse(state.get("updated")) or _parse(state.get("started")) or now
        until = progress + timedelta(seconds=float(precard))
        bound = "progress"
    return {
        "session": session, "issue": issue or "", "node": node, "local": base_local(state),
        "run": state.get("run_id") or "", "bound": bound, "card": (card or {}).get("id"),
        "until": _iso(until), "expired": now >= until,
    }


def precard_seconds(cfg: Optional[dict] = None) -> float:
    if cfg is None:
        from .. import store
        cfg = store.daemon_config()
    try:
        return max(0.0, float(cfg.get("reposync_pause_precard_s", PRECARD_DEFAULT)))
    except (TypeError, ValueError):
        return PRECARD_DEFAULT


def sync_sessions(manager) -> list:
    """Sessions started as Workspaces sync agents that have not ended. A held
    session is asleep, not ended: its run still stands where it stopped."""
    return [s for s in manager.list()
            if (not s.exited or getattr(s, "held_at", None))
            and reposync_agent.MARKER_PREFIX in (s.sdef.task or "")]


def for_repo(manager, repo: str, *, cards: Iterable[dict],
             common_dir: Callable[[str], Optional[str]],
             load_state: Callable[[str, str], Optional[dict]],
             precard: Optional[float] = None, now: Optional[datetime] = None) -> dict:
    """``{"paused": bool, "pause": <the pause or None>, "expired": [...]}`` for
    the repository ``repo`` belongs to. ``common_dir`` and ``load_state`` are
    the git and run-store readers (injected so tests need no daemon)."""
    target = common_dir(repo)
    precard = precard_seconds() if precard is None else precard
    cards = list(cards)
    held, expired = [], []
    if target:
        for s in sync_sessions(manager):
            if common_dir(s.sdef.cwd or "") != target:
                continue
            state = load_state(s.sdef.cwd, s.sdef.name)
            pause = state and compute(state, session=s.sdef.name, issue=s.sdef.issue,
                                      cards=cards, precard=precard, now=now)
            if pause:
                (expired if pause["expired"] else held).append(pause)
    held.sort(key=lambda p: p["session"])
    return {"paused": bool(held), "pause": held[0] if held else None,
            "pauses": held, "expired": expired}


def refusal(pause: dict) -> str:
    """The admit refusal: who holds the pause, against which local SHA, until when."""
    return (f"repository paused by sync session {pause['session']} "
            f"(base local {pause['local'] or '?'}, {pause['node']}, until {pause['until']}); "
            f"only its issue {pause['issue'] or '(none)'} may merge -- defer this candidate")
