"""Turn-end nudges: a session that asked its user in plain text (claunch-165w6).

Sessions have a QA pane (``qa_ask``, daemon/qa_api.py) for putting a question
to the user, because a question typed into the terminal is buried by the next
reminder or mesh message. Agents still end turns with a plain-text question
often enough that the pane is empty while the user scrolls for it. This clock
watches for exactly that and types a nudge into the session.

Where the turn's end comes from: the daemon's own status (``busy`` ->
``idle``, daemon/session.py ``_compute_status``) and not a harness Stop hook.
The status already folds every harness's turn signal (claude's title
spinner, codex's braille title, pi's OSC heartbeat) over a screen-quiescence
base, so one rule covers them all; a hook would be one implementation per
harness, and only claude has one.

What decides: Jev (daemon/jev.py), asked one Choice over the registered
:data:`ACTIONS`. Code does everything code can do first (jev-1.13's own
advice: keep the model for the judgment, keep the rest in code), and only a
turn that survives those checks costs a call:

- the feature is on and a key is set;
- the session has the claunch MCP connected, so ``qa_ask`` exists for it;
- this clock saw the session busy since its last judgment (switching the
  feature on does not judge the idle fleet's old last turns);
- the turn did not already use the pane (a ``qa_ask``/``qa_update`` tool call
  in the transcript, or an agent write on the QA store since the turn began);
- the turn ended on text;
- the session has not been nudged since a person last typed into it, and not
  within ``qa_nudge_cooldown`` seconds.

What the model sees: only the turn's final assistant text, capped at
``qa_nudge_max_chars`` characters (head plus tail, the question is usually at
the end). Jev's window is 64k tokens per request and 32k for state plus the
longest question; the cap keeps the request far below that even counting one
token per CJK character, and leaving out tool output and earlier turns is
also what keeps the answer accurate (jev-1.13 jaggedness: irrelevant state
costs accuracy).

Actions are a registry. ``none`` and ``use_qa`` are the two today; another
one is one more :class:`NudgeAction` -- its criteria join the Choice and its
message is what gets typed when it wins. An action whose confidence is under
``qa_nudge_min_confidence`` is treated as ``none``.

Every judgment, including the skipped ones, is appended to
``<daemon dir>/qa_nudge.jsonl`` (action, probabilities, confidence, usage, or
the skip reason): the threshold is a setting, and that file is what tuning it
reads.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from aiohttp import web

from .. import store
from . import jev, paths, transcript_view

log = logging.getLogger(__name__)

POLL = 3.0
#: Seconds a session must have read idle before its turn is judged. The
#: status can flicker idle between two tool calls; a turn that is really over
#: stays over.
SETTLE = 5.0
#: Records read from the tail of the transcript. A turn longer than this
#: still ends inside it; only the turn's start may fall outside, and then the
#: page's first record stands in for it.
PAGE = 60
#: Head kept when the final message is clipped; the rest of the cap is tail.
HEAD_CHARS = 1500
JOURNAL_MAX = 2_000_000

STATUS_BUSY = "busy"
STATUS_IDLE = "idle"

#: Tool names that mean "this turn used the QA pane". Matched as substrings
#: because each harness spells an MCP tool differently
#: (``mcp__claunch__qa_ask``, ``claunch.qa_ask``, ...).
QA_TOOLS = ("qa_ask", "qa_update")


@dataclass(frozen=True)
class NudgeAction:
    """One thing the clock can do after a turn. ``criteria`` is the option's
    description in the Choice (English: Jev's best language). ``message`` is
    what is typed into the session when it wins; None types nothing."""

    key: str
    criteria: str
    message: Optional[str] = None


USE_QA_MESSAGE = (
    "[claunch qa-nudge] machine-generated, not typed by the user\n"
    "Your last reply asks the user something in plain text. A question in the "
    "terminal gets buried under the next message; put it to the user with the "
    "qa_ask tool (claunch MCP) so it waits in their QA pane, then stop until "
    "the answer arrives. If nothing in that reply needs the user's answer, "
    "carry on and ignore this."
)

#: Order matters: Jev leans toward the first option (jev-1.13 jaggedness #8),
#: so the option that does nothing comes first.
ACTIONS: Tuple[NudgeAction, ...] = (
    NudgeAction(
        "none",
        "The message does not need an answer from the user before the agent "
        "can go on: it reports results or progress, says what the agent will "
        "do next, or only offers optional further help such as 'let me know "
        "if you need anything else'.",
    ),
    NudgeAction(
        "use_qa",
        "The message asks the user to answer a question, choose between "
        "options, approve or confirm something, or supply missing information, "
        "and the agent is waiting for that answer before it continues.",
        USE_QA_MESSAGE,
    ),
)

QUESTION_ID = "action"
INSTRUCTIONS = (
    "`agent_final_message` is the last message an AI coding agent showed its "
    "user before it stopped and waited. Which follow-up does the agent need?"
)


# --------------------------------------------------------------------------- #
# request
# --------------------------------------------------------------------------- #
def clip(text: str, max_chars: int) -> str:
    text = text.strip()
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    head = min(HEAD_CHARS, max_chars // 4)
    marker = f"\n[... {len(text) - max_chars} characters omitted ...]\n"
    tail = max_chars - head
    return text[:head] + marker + text[len(text) - tail:]


def build_request(
    text: str, max_chars: int, actions: Sequence[NudgeAction] = ACTIONS,
) -> Tuple[Dict[str, Any], Dict[str, dict]]:
    """``(state, questions)`` for :func:`jev.system_one`."""
    state = {"agent_final_message": clip(text, max_chars)}
    questions = {
        QUESTION_ID: {
            "type": "choice",
            "instructions": INSTRUCTIONS,
            "criteria": {a.key: a.criteria for a in actions},
        }
    }
    return state, questions


def decide(
    response: Dict[str, Any], min_confidence: float,
    actions: Sequence[NudgeAction] = ACTIONS,
) -> Tuple[NudgeAction, Dict[str, Any]]:
    """The action to take and the answer it came from. An unknown option or
    a confidence under the threshold is the first action (``none``)."""
    answer = (response.get("answers") or {}).get(QUESTION_ID) or {}
    by_key = {a.key: a for a in actions}
    chosen = by_key.get(str(answer.get("choice") or ""))
    try:
        confidence = float(answer.get("confidence"))
    except (TypeError, ValueError):
        confidence = 0.0
    if chosen is None or confidence < min_confidence:
        chosen = actions[0]
    return chosen, answer


# --------------------------------------------------------------------------- #
# the turn, read from the transcript
# --------------------------------------------------------------------------- #
@dataclass
class Turn:
    text: str = ""
    used_qa: bool = False
    #: seq of the record the text came from: the turn's identity, so one
    #: turn is judged once however many polls see it idle.
    seq: Optional[int] = None
    reason: Optional[str] = None


def _texts(record: dict) -> List[str]:
    return [b.get("text") or "" for b in record.get("blocks") or ()
            if b.get("type") == "text" and (b.get("text") or "").strip()]


def _tools(record: dict) -> List[str]:
    return [str(b.get("name") or "") for b in record.get("blocks") or ()
            if b.get("type") == "tool_use"]


def read_turn(records: Sequence[dict]) -> Turn:
    """The last turn of a projected transcript page (oldest first).

    The turn starts after the last user record that carries text; a user
    record holding only tool results is the agent's own loop, not a turn
    boundary. Sidechain records (sub-agents) are not the session's voice.
    """
    main = [r for r in records if not r.get("sidechain")]
    start = 0
    for i in range(len(main) - 1, -1, -1):
        if main[i].get("role") == "user" and _texts(main[i]):
            start = i + 1
            break
    turn = Turn()
    last = None
    for rec in main[start:]:
        if rec.get("role") != "assistant":
            continue
        if any(t in name for name in _tools(rec) for t in QA_TOOLS):
            turn.used_qa = True
        last = rec
    if last is None:
        turn.reason = "no_assistant_reply"
        return turn
    texts = _texts(last)
    if not texts or _tools(last):
        # Ended on a tool call (or nothing readable): not a message to a user.
        turn.reason = "ended_without_text"
        return turn
    turn.text = "\n".join(texts)
    turn.seq = last.get("seq")
    return turn


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #
def policy(cfg: Dict[str, Any]) -> Dict[str, Any]:
    def num(key: str, default: float) -> float:
        try:
            return float(cfg.get(key))
        except (TypeError, ValueError):
            return default
    return {
        "enabled": bool(cfg.get("qa_nudge")),
        "min_confidence": num("qa_nudge_min_confidence", 0.6),
        "cooldown": num("qa_nudge_cooldown", 300.0),
        "max_chars": int(num("qa_nudge_max_chars", 8000)),
    }


def settings_view() -> Dict[str, Any]:
    """What the settings page shows: the policy, the Jev endpoint settings and
    where the key comes from -- never the key."""
    cfg = store.daemon_config()
    key, source = jev.resolve_key(cfg)
    return {
        **policy(cfg),
        "jev_model": jev.model_of(cfg),
        # Every feature that may pin a model of its own, resolved: an empty
        # per-feature setting shows the global model it falls back to.
        "jev_models": {feature: jev.model_for(cfg, feature) for feature in jev.FEATURES},
        # The pin itself ("" when the feature follows jev_model), so the
        # settings card can edit it without turning a fallback into a pin.
        "jev_model_pins": {
            feature: str(cfg.get("jev_model_" + feature) or "") for feature in jev.FEATURES
        },
        "jev_base_url": str(cfg.get("jev_base_url") or jev.DEFAULT_BASE_URL),
        "jev_api_key_env": str(cfg.get("jev_api_key_env") or jev.DEFAULT_KEY_ENV),
        "key_set": bool(key),
        "key_source": source,
        "actions": [a.key for a in ACTIONS],
    }


# --------------------------------------------------------------------------- #
# journal
# --------------------------------------------------------------------------- #
def journal_path():
    return paths.daemon_dir() / "qa_nudge.jsonl"


def journal(row: Dict[str, Any]) -> None:
    path = journal_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > JOURNAL_MAX:
            path.replace(path.with_name(path.name + ".1"))
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        log.debug("qa nudge journal write failed", exc_info=True)


# --------------------------------------------------------------------------- #
# the clock
# --------------------------------------------------------------------------- #
@dataclass
class _Watch:
    busy_seen: bool = False
    #: QA store agent_seq when the turn was first seen busy.
    qa_seq: Optional[int] = None
    judged: Optional[Tuple[Any, ...]] = None
    nudged_at: Optional[float] = None
    pending: bool = False
    #: busy seen again while a judgment was pending: that is the next turn,
    #: and it must survive the re-arm at the judgment's end.
    rebusy: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)


class QaNudgeClock:
    """Watches every live session's busy -> idle edge and judges the turn
    that just ended (module docstring). In memory only: a daemon restart
    starts every session unwatched, which is the same as switching it on."""

    def __init__(self, manager, qa=None, *, poll: float = POLL,
                 settle: float = SETTLE, actions: Sequence[NudgeAction] = ACTIONS):
        self.manager = manager
        self.qa = qa
        self.poll = poll
        self.settle = settle
        self.actions = tuple(actions)
        self._watch: Dict[str, _Watch] = {}
        self._task: Optional[asyncio.Task] = None
        self._jobs: set = set()

    # lifecycle ---------------------------------------------------------- #
    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def shutdown(self) -> None:
        task, self._task = self._task, None
        tasks = [t for t in (task, *self._jobs) if t is not None]
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.poll)
                for name in self.scan(time.monotonic()):
                    job = asyncio.get_running_loop().create_task(self.judge(name))
                    self._jobs.add(job)
                    job.add_done_callback(self._jobs.discard)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("qa nudge clock tick failed")

    # scan --------------------------------------------------------------- #
    def _qa_seq(self, name: str) -> Optional[int]:
        if self.qa is None:
            return None
        try:
            row = self.qa.rail().get(name) or {}
        except Exception:
            return None
        seq = row.get("agent_seq")
        return int(seq) if isinstance(seq, int) else 0

    def scan(self, now: float) -> List[str]:
        """Sessions whose turn is due for judgment. Cheap (status reads only);
        public for the tests."""
        try:
            cfg = store.daemon_config()
        except store.StoreError as exc:
            log.warning("qa nudge: config unreadable, skipping: %s", exc)
            return []
        if not policy(cfg)["enabled"]:
            self._watch.clear()
            return []
        due: List[str] = []
        live = set()
        for session in self.manager.list():
            name = session.sdef.name
            try:
                status = session.status()
            except Exception:
                continue
            if status not in (STATUS_BUSY, STATUS_IDLE):
                continue
            live.add(name)
            w = self._watch.setdefault(name, _Watch())
            if status == STATUS_BUSY:
                if w.pending:
                    w.rebusy = True
                elif not w.busy_seen:
                    w.busy_seen = True
                    w.qa_seq = self._qa_seq(name)
                continue
            if not w.busy_seen or w.pending:
                continue
            idle_for = session.idle_since()
            if idle_for is None or idle_for < self.settle:
                continue
            w.pending = True
            due.append(name)
        for name in list(self._watch):
            if name not in live:
                del self._watch[name]
        return due

    # judge -------------------------------------------------------------- #
    def _session(self, name: str):
        try:
            return self.manager.get(name)
        except Exception:
            return None

    def _skip(self, session, w: _Watch, pol: Dict[str, Any], now: float) -> Optional[str]:
        ready = getattr(session, "mcp_ready_event", None)
        if ready is None or not ready().is_set():
            return "no_qa_tools"
        if w.nudged_at is not None:
            if now - w.nudged_at < pol["cooldown"]:
                return "cooldown"
            if getattr(session, "_last_human_input", 0.0) <= w.nudged_at:
                return "awaiting_human"
        return None

    async def judge(self, name: str) -> Optional[str]:
        """Judge one session's ended turn; returns the action key taken (or
        None when skipped). Always re-arms the watch for the next turn."""
        w = self._watch.get(name)
        if w is None:
            return None
        try:
            return await self._judge(name, w)
        finally:
            w.busy_seen, w.rebusy = w.rebusy, False
            w.pending = False
            w.qa_seq = self._qa_seq(name) if w.busy_seen else None

    async def _judge(self, name: str, w: _Watch) -> Optional[str]:
        session = self._session(name)
        if session is None:
            return None
        cfg = await asyncio.to_thread(store.daemon_config)
        pol = policy(cfg)
        row: Dict[str, Any] = {"at": time.time(), "session": name}

        def skip(reason: str) -> None:
            journal({**row, "skip": reason})

        reason = self._skip(session, w, pol, time.monotonic())
        if reason:
            skip(reason)
            return None
        try:
            page = await asyncio.to_thread(
                transcript_view.page, name, session.sdef, limit=PAGE)
        except Exception:
            log.debug("qa nudge: transcript of %r unreadable", name, exc_info=True)
            skip("no_transcript")
            return None
        turn = read_turn(page.get("records") or [])
        if turn.reason:
            skip(turn.reason)
            return None
        seq_now = await asyncio.to_thread(self._qa_seq, name)
        if turn.used_qa or (w.qa_seq is not None and seq_now is not None and seq_now > w.qa_seq):
            skip("used_qa")
            return None
        key = (page.get("source"), turn.seq)
        if w.judged == key:
            skip("already_judged")
            return None
        w.judged = key
        api_key, _source = jev.resolve_key(cfg)
        if not api_key:
            skip("no_key")
            return None
        state, questions = build_request(turn.text, pol["max_chars"], self.actions)
        try:
            response = await jev.system_one(
                cfg, state, questions, key=api_key, model=jev.model_for(cfg, "qa_nudge")
            )
        except jev.JevError as exc:
            skip(f"jev_error: {exc}")
            return None
        action, answer = decide(response, pol["min_confidence"], self.actions)
        row.update({
            "action": action.key,
            "choice": answer.get("choice"),
            "confidence": answer.get("confidence"),
            "probabilities": answer.get("probabilities"),
            "model": response.get("model"),
            "usage": response.get("usage"),
            "chars": len(state["agent_final_message"]),
        })
        if not action.message:
            journal(row)
            return action.key
        # The session may have moved on (a person typed, a message arrived)
        # while Jev answered: a nudge about a turn that is no longer the last
        # one is noise.
        if session.status() != STATUS_IDLE:
            journal({**row, "delivered": False, "why": "no_longer_idle"})
            return action.key
        try:
            delivered = bool(await session.deliver(action.message))
        except Exception:
            log.exception("qa nudge delivery to %r failed", name)
            delivered = False
        if delivered:
            w.nudged_at = time.monotonic()
            log.info("qa nudge (%s) delivered to %r", action.key, name)
        journal({**row, "delivered": delivered})
        return action.key


# --------------------------------------------------------------------------- #
# HTTP: the settings page's card
# --------------------------------------------------------------------------- #
_NUMBERS = {
    "min_confidence": ("qa_nudge_min_confidence", 0.0, 1.0),
    "cooldown": ("qa_nudge_cooldown", 0.0, None),
    "max_chars": ("qa_nudge_max_chars", 200.0, 30000.0),
}
_TEXTS = {
    "jev_model": "jev_model",
    "jev_base_url": "jev_base_url",
    "jev_api_key_env": "jev_api_key_env",
    # Per-feature pins (claunch-v5nhp): empty means "use jev_model".
    "jev_model_qa_nudge": "jev_model_qa_nudge",
    "jev_model_megathread": "jev_model_megathread",
}


async def h_get(_request: web.Request) -> web.Response:
    try:
        return web.json_response({"settings": await asyncio.to_thread(settings_view)})
    except store.StoreError as exc:
        return web.json_response({"error": str(exc)}, status=500)


async def h_put(request: web.Request) -> web.Response:
    """Partial update: only the fields sent change. ``api_key`` writes the
    key file (an empty string removes it) and is never echoed back."""
    try:
        body = await request.json()
    except (ValueError, UnicodeError):
        body = None
    if not isinstance(body, dict):
        return web.json_response({"error": "body must be a JSON object"}, status=400)
    writes: List[Tuple[str, Any]] = []
    if "enabled" in body:
        writes.append(("qa_nudge", bool(body["enabled"])))
    for field_name, (key, low, high) in _NUMBERS.items():
        if field_name not in body:
            continue
        try:
            value = float(body[field_name])
        except (TypeError, ValueError):
            return web.json_response({"error": f"'{field_name}' must be a number"}, status=400)
        if value < low or (high is not None and value > high):
            span = f"{low:g}..{high:g}" if high is not None else f">= {low:g}"
            return web.json_response({"error": f"'{field_name}' must be {span}"}, status=400)
        writes.append((key, int(value) if field_name == "max_chars" else value))
    for field_name, key in _TEXTS.items():
        if field_name not in body:
            continue
        value = body[field_name]
        if value is not None and not isinstance(value, str):
            return web.json_response({"error": f"'{field_name}' must be a string"}, status=400)
        # Empty clears it back to the packaged default.
        writes.append((key, (value or "").strip() or None))
    api_key = body.get("api_key")
    if api_key is not None and not isinstance(api_key, str):
        return web.json_response({"error": "'api_key' must be a string"}, status=400)
    try:
        for key, value in writes:
            await asyncio.to_thread(store.set_daemon_field, key, value)
        if api_key is not None:
            await asyncio.to_thread(jev.write_key_file, api_key.strip())
        return web.json_response({"settings": await asyncio.to_thread(settings_view)})
    except (store.StoreError, OSError) as exc:
        return web.json_response({"error": str(exc)}, status=500)


def install(app) -> None:
    """Routes plus the clock, started and stopped with the app. Needs
    ``app["manager"]``; uses ``app["qa"]`` when qa_api installed it first."""
    app.router.add_get("/api/qa-nudge", h_get)
    app.router.add_put("/api/qa-nudge", h_put)

    clock = QaNudgeClock(app["manager"], app.get("qa"))
    app["qa_nudge_clock"] = clock

    async def _start(_app) -> None:
        clock.start()

    async def _stop(_app) -> None:
        await clock.shutdown()

    app.on_startup.append(_start)
    app.on_shutdown.append(_stop)
