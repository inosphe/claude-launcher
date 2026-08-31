"""One session's "what is it doing right now" briefing, composed by an LLM.

The web UI wants 3-4 sentences per session: the goal, what is happening now,
and a coarse state. No single registry holds that, so this module gathers the
hybrid evidence that exists — the session's own record (cwd, opening task),
its cflow run position (read-only), and the tail of its harness transcript
(Claude's project jsonl or Codex's rollout jsonl) — and asks an
OpenAI-compatible ``chat/completions`` endpoint to compress it into a fixed
JSON shape.

Configuration is the ``llm:`` block of ``~/.claunch.yaml`` (``store.load()``
is authoritative). An empty ``api_key`` means the feature is off — the key is
typed in by the user and must never be committed or logged; it leaves this
module only as the ``Authorization`` header of the LLM call itself.

Results are cached per session, keyed by what would change the answer: the
transcript file's (mtime, size) and the cflow step. The cache is persisted in
the daemon instance directory so a daemon restart keeps the last briefing;
``refresh`` bypasses it.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Tuple

import aiohttp

from .. import atomic, harnesses as harness_registry
from .. import profile as profile_mod, store, transcripts
from ..cflow import engine as cflow_engine, state as cflow_state
from ..cflow.engine import CflowError
from ..cflow.model import WorkflowError
from ..cflow.state import LockBusy, StateError
from ..profile import ProfileError
from . import codex_sessions, paths

#: Defaults for the ``llm`` config block. ``max_tokens`` bounds the *whole*
#: completion, and on a reasoning model the reasoning tokens are billed to it
#: too — so the budget is not "how long is the answer" but "how long is the
#: thinking plus the answer". Measured once, against the endpoint configured
#: that day — ``accounts/fireworks/models/deepseek-v4-flash-0731`` — at 1024:
#: 8 of 10 calls came back ``finish_reason="length"`` with
#: ``completion_tokens=1024`` and an EMPTY ``content``; the reasoning had
#: eaten the lot. The model, the budget and the N are that run's conditions,
#: not standing facts about the feature.
#:
#: 4096 leaves room for both, because the answer itself is far smaller. Three
#: medians were taken, and they are three UNITS rather than three samples —
#: name the unit or they read as disagreement:
#:
#:   p50 273 (149-633)  fields as rendered, the 25 that parsed of 30 sessions
#:   p50 357 (233-717)  those SAME 25, serialized back to JSON
#:   p50 385 (244-686)  raw ``content``, the 43 that parsed of 48 sweep calls
#:
#: 273 to 385 is a gap of 112, of which 84 is the change of unit and 28 the
#: change of sample. Only the last two are counted the same way, so only
#: those two compare.
DEFAULT_MAX_TOKENS = 4096

#: How much transcript feeds the prompt. ``TAIL_BYTES`` bounds the file read
#: (the tail is read from the end, never the whole file); ``EVENT_TAIL`` the
#: number of user/assistant events kept; ``TEXT_LIMIT`` each event's text;
#: ``TOTAL_LIMIT`` the whole tail block, newest events kept when it overflows.
TAIL_BYTES = 512 * 1024
EVENT_TAIL = 80
TEXT_LIMIT = 700
TOTAL_LIMIT = 60_000

#: The timeout for the whole LLM round-trip. Generous because the endpoint is
#: the user's own choice and may be slow; the aiohttp handler awaiting this is
#: one request, not the daemon.
LLM_TIMEOUT = 60.0

_STATES = frozenset({"working", "blocked", "waiting", "idle", "done", "unknown"})


class BriefingError(Exception):
    """The LLM call failed (transport, HTTP status, or an unusable body)."""


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
def llm_config(doc: Optional[dict] = None) -> dict:
    """The effective ``llm`` settings (missing keys filled with defaults).

    Mirrors :func:`store.daemon_config`'s shape-tolerance: a missing or
    malformed block yields the disabled default rather than an error, because
    this file is hand-edited.
    """
    doc = store.load() if doc is None else doc
    block = doc.get("llm")
    if not isinstance(block, dict):
        block = {}
    params = block.get("params")
    try:
        max_tokens = int(block.get("max_tokens") or DEFAULT_MAX_TOKENS)
    except (TypeError, ValueError):
        max_tokens = DEFAULT_MAX_TOKENS
    return {
        "endpoint": str(block.get("endpoint") or "").strip(),
        "model": str(block.get("model") or "").strip(),
        "api_key": str(block.get("api_key") or "").strip(),
        "max_tokens": max_tokens,
        "params": dict(params) if isinstance(params, dict) else {},
    }


def llm_configured(cfg: dict) -> bool:
    """Whether the feature is on: endpoint, model and api_key all present."""
    return bool(cfg.get("endpoint") and cfg.get("model") and cfg.get("api_key"))


# --------------------------------------------------------------------------- #
# transcript tail
# --------------------------------------------------------------------------- #
def _config_dir(sdef) -> Optional[Path]:
    """Where this session's claude config lives (transcripts underneath).

    A profile names it exactly; without one, claude's own resolution applies
    (``CLAUDE_CONFIG_DIR`` env, else ``~/.claude``).
    """
    if getattr(sdef, "profile", None):
        try:
            return profile_mod.require_selector(sdef.profile).config_dir
        except ProfileError:
            return None
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env) if env else Path.home() / ".claude"


def locate_transcript(sdef) -> Optional[Path]:
    """The session's transcript file, or ``None`` (no conversation, no file)."""
    cid = getattr(sdef, "conversation_id", None)
    if not cid:
        return None
    if getattr(sdef, "harness", None) == "codex":
        try:
            profile = profile_mod.require_selector(
                str(getattr(sdef, "profile", "") or "")
            )
            harness = harness_registry.get("codex")
            if harness is None:
                return None
            return codex_sessions.find(
                harness.profile_home(profile.config_dir), str(cid)
            )
        except (harness_registry.HarnessConfigError, ProfileError, OSError, ValueError):
            return None
    cdir = _config_dir(sdef)
    if cdir is None:
        return None
    if sdef.cwd:
        p = transcripts.project_dir(cdir, sdef.cwd) / f"{cid}.jsonl"
        if p.is_file():
            return p
    return transcripts.find(cdir, cid)


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[:limit] + " …"


def _entry_events(entry: dict, text_limit: int) -> List[str]:
    """One jsonl entry's contribution: user/assistant text, tool_use names.

    Everything else (tool results, thinking, progress records) is noise for a
    3-sentence summary and is dropped here rather than truncated later.
    """
    msg = entry.get("message")
    codex = entry.get("payload")
    if (
        entry.get("type") == "response_item"
        and isinstance(codex, dict)
        and codex.get("type") == "message"
    ):
        msg = codex
    if not isinstance(msg, dict):
        if (
            entry.get("type") == "response_item"
            and isinstance(codex, dict)
            and codex.get("type") in ("function_call", "custom_tool_call")
            and codex.get("name")
        ):
            return [f"assistant tool_use: {codex['name']}"]
        return []
    role = msg.get("role") or entry.get("type")
    if role not in ("user", "assistant"):
        return []
    content = msg.get("content")
    texts: List[str] = []
    tools: List[str] = []
    if isinstance(content, str):
        if content.strip():
            texts.append(content)
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if (
                kind in ("text", "input_text", "output_text")
                and str(block.get("text") or "").strip()
            ):
                texts.append(str(block["text"]))
            elif kind == "tool_use" and block.get("name"):
                tools.append(str(block["name"]))
    out = [f"{role}: {_clip(t, text_limit)}" for t in texts]
    if tools:
        out.append(f"{role} tool_use: {', '.join(tools)}")
    return out


def tail_events(
    path: Path,
    *,
    max_events: int = EVENT_TAIL,
    text_limit: int = TEXT_LIMIT,
    total_limit: int = TOTAL_LIMIT,
) -> List[str]:
    """The transcript's tail as prompt lines, oldest first.

    Reads at most ``TAIL_BYTES`` from the file's end (a long session's jsonl
    runs to tens of MB), drops the first line when the read started mid-file,
    and keeps the newest events that fit the caps.
    """
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - TAIL_BYTES))
            blob = fh.read()
    except OSError:
        return []
    lines = blob.decode("utf-8", errors="replace").splitlines()
    if size > TAIL_BYTES and lines:
        lines = lines[1:]
    events: List[str] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            events.extend(_entry_events(entry, text_limit))
    events = events[-max_events:]
    kept: List[str] = []
    total = 0
    for ev in reversed(events):
        total += len(ev) + 1
        if total > total_limit:
            break
        kept.append(ev)
    kept.reverse()
    return kept


# --------------------------------------------------------------------------- #
# structured signals
# --------------------------------------------------------------------------- #
def gather_cflow(cwd: str, scope: str) -> Optional[dict]:
    """The session's cflow run position, read-only — ``None`` when it has none.

    A run is keyed by (directory, scope) and the scope IS the session name
    (same exact link :func:`api.h_session_meta` relies on). Any failure to
    read is treated as "no run": the briefing degrades, never errors, on this
    input.
    """
    if not cwd:
        return None
    try:
        resolved = cflow_state.resolve_cwd(cwd)
        payload = cflow_engine.status(resolved, scope=scope)
    except (CflowError, WorkflowError, StateError, LockBusy, OSError):
        return None
    if payload.get("status") in (None, "idle") or not payload.get("step_id"):
        return None
    info = {
        "workflow": payload.get("workflow"),
        "status": payload.get("status"),
        "step": payload.get("step_id"),
        "title": payload.get("title"),
    }
    try:
        reports = [
            e
            for e in cflow_state.read_journal(resolved, scope, run_id=payload.get("run"))
            if e.get("event") == "step_report"
        ]
    except OSError:
        reports = []
    if reports:
        info["last_report"] = str(reports[-1].get("summary") or "")
    return info


def gather_live(session) -> Optional[dict]:
    """Read the daemon's live state for a briefing.

    Transcript files lag while a harness is starting and do not expose the
    daemon's PTY/turn state.  The manager already computes this state for the
    session rail, so include the same signal in the briefing prompt.  A
    failure is deliberately non-fatal: the transcript and cflow evidence
    remain useful on their own.
    """
    try:
        return {
            "status": str(session.status()),
            "running": not bool(getattr(session, "exited", False)),
            "last_input_at": getattr(session, "last_input_at", None),
            "last_output_at": getattr(session, "last_output_at", None),
        }
    except (AttributeError, OSError, RuntimeError):
        return None


def gather_faq() -> List[dict]:
    """Read enabled user FAQ entries for the summariser prompt."""
    try:
        return [row for row in store.briefing_faq() if row.get("enabled", True)]
    except store.StoreError:
        return []


def build_prompt(
    sdef,
    cflow_info: Optional[dict],
    events: List[str],
    live_info: Optional[dict] = None,
    faq: Optional[List[dict]] = None,
) -> str:
    """The single user message the LLM answers with the briefing JSON."""
    lines = [
        "당신은 개발 에이전트 세션의 상태 요약기다. 아래 자료만 근거로 이",
        "세션의 현재 상태를 요약하라. 반드시 JSON 오브젝트 하나만 출력한다",
        "(코드펜스·설명·다른 텍스트 금지):",
        '{"goal": "작업 목표 1~2문장", "now": "지금 하는 일 1~2문장",'
        ' "state": "working|blocked|waiting|idle|done|unknown",'
        ' "progress": "진행 정도 한 문장",'
        ' "one-line-job-description": "이 세션이 맡은 일을 한 줄로",'
        ' "faq": [{"question": "사용자 FAQ 질문", "answer": "현재 세션 자료에 근거한 답변"}]}',
        "opening task 안에 포함된 과거 요약 문구는 현재 상태의 근거로",
        "사용하지 않는다. live 상태와 최신 로그를 우선한다. 자료에 없는",
        "내용은 지어내지 않는다. 판단 근거가 없으면 state는",
        '"unknown"으로 둔다. state 열거값을 제외한 모든 필드 값은 자료의 언어와',
        '관계없이 자연스럽고 정확한 한국어로 작성한다. 코드·경로·식별자 등',
        '변경하면 안 되는 기술적 값은 원문을 유지한다.',
        "one-line-job-description은 세션이 맡은 작업 전체를 한 줄에 담는다"
        "(목표와 달라도 좋다 — 리더가 볼 한 줄짜리 설명).",
        "",
        "[세션]",
        f"이름: {sdef.name}",
        f"작업 디렉터리: {sdef.cwd or '(없음)'}",
    ]
    if getattr(sdef, "task", None):
        lines.append(f"개설 시 태스크: {_clip(str(sdef.task), TEXT_LIMIT)}")
    if live_info:
        lines += ["", "[데몬 실시간 상태]"]
        lines.append(
            f"상태: {live_info.get('status') or 'unknown'} / "
            f"실행 중: {live_info.get('running', 'unknown')} / "
            f"마지막 입력: {live_info.get('last_input_at') or '(없음)'} / "
            f"마지막 출력: {live_info.get('last_output_at') or '(없음)'}"
        )
    if faq:
        lines += ["", "[사용자 FAQ — 요약 시 참고]"]
        for row in faq[:50]:
            lines.append(f"질문: {_clip(str(row.get('question') or ''), 1000)}")
        lines.append("각 질문에 대해 현재 세션 자료에 근거한 답변을 faq 배열에 작성한다. 자료에 없으면 모른다고 명시한다.")
    if cflow_info:
        lines += ["", "[cflow 런]"]
        lines.append(
            f"워크플로: {cflow_info.get('workflow')} / 현재 스텝: "
            f"{cflow_info.get('step')} ({cflow_info.get('title')}) / "
            f"런 상태: {cflow_info.get('status')}"
        )
        if cflow_info.get("last_report"):
            lines.append(f"직전 스텝 보고: {_clip(cflow_info['last_report'], TEXT_LIMIT)}")
    if events:
        lines += ["", "[대화 로그 꼬리 — 오래된 것부터]"]
        lines.extend(events)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# LLM call and answer parsing
# --------------------------------------------------------------------------- #
class LlmAnswer(NamedTuple):
    """One completion: its text plus the two fields that say if it is whole.

    ``finish_reason`` is the endpoint's own verdict — ``"length"`` means the
    text was cut off at the budget, not that the model stopped talking — and
    ``completion_tokens`` is what it spent getting there (reasoning included).
    Both exist so the caller can tell a *truncated* answer from a *disobedient*
    one; only the first is worth failing the request over.
    """

    text: str
    finish_reason: Optional[str]
    completion_tokens: Optional[int]


async def call_llm(cfg: dict, prompt: str, *, timeout: float = LLM_TIMEOUT) -> LlmAnswer:
    """POST the prompt to the OpenAI-compatible endpoint, return the answer.

    Any transport or shape failure becomes :class:`BriefingError`; the message
    carries at most a snippet of the *response* body — never the request, so
    the api_key cannot leak through an error path.

    An empty ``content`` is one of those failures. It is what a reasoning model
    returns when ``max_tokens`` ran out mid-thought, and it arrives as a
    perfectly ordinary HTTP 200 — so if it were passed on, the caller would
    cache "" and the UI would render a blank card with nothing to say why.
    """
    body = {
        "model": cfg["model"],
        "max_tokens": cfg["max_tokens"],
        "messages": [{"role": "user", "content": prompt}],
    }
    body.update(cfg.get("params") or {})
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {cfg['api_key']}",
    }
    try:
        client_timeout = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(timeout=client_timeout) as http:
            async with http.post(cfg["endpoint"], json=body, headers=headers) as resp:
                if resp.status != 200:
                    snippet = (await resp.text())[:300]
                    raise BriefingError(
                        f"llm endpoint answered {resp.status}: {snippet}"
                    )
                data = await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise BriefingError(f"llm call failed: {exc}") from exc
    try:
        choice = data["choices"][0]
        content = choice["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise BriefingError("llm response has no choices[0].message.content") from None
    finish = choice.get("finish_reason") if isinstance(choice, dict) else None
    usage = data.get("usage") if isinstance(data, dict) else None
    spent = usage.get("completion_tokens") if isinstance(usage, dict) else None
    text = str(content or "")
    if not text.strip():
        # Two different things arrive here, and telling them apart is the
        # whole point of reading finish_reason: "length" is OUR budget being
        # too small for a reasoning model, and the operator can fix it.
        # Anything else — measured live as finish_reason="stop" with
        # completion_tokens=1 — is the endpoint returning a degenerate
        # completion, which no local setting changes. Blaming the budget for
        # that one would send whoever reads the log to the wrong knob.
        if finish == "length":
            why = (
                f"the budget ran out (max_tokens={cfg['max_tokens']}); a "
                "reasoning model spends it on its reasoning before it writes "
                "anything, so raise llm.max_tokens in ~/.claunch.yaml"
            )
        else:
            why = (
                "the endpoint produced nothing and did not say it was cut off "
                "— a provider-side empty completion, not a local setting"
            )
        raise BriefingError(
            f"llm returned empty content (finish_reason={finish!r}, "
            f"completion_tokens={spent}): {why}"
        )
    return LlmAnswer(text, str(finish) if finish is not None else None, spent)


_FENCE_OPEN = re.compile(r"^```[A-Za-z0-9_-]*\s*")
_FENCE_CLOSE = re.compile(r"\s*```\s*$")


def parse_briefing(text: str) -> Optional[dict]:
    """The model's answer as the fixed briefing dict — ``None`` if unusable.

    Lenient on purpose: code fences are stripped, and when the whole text is
    not JSON the outermost ``{...}`` span gets a second chance. An unusable
    answer is not an error — the caller serves the raw text instead.
    """
    if not text:
        return None
    t = _FENCE_CLOSE.sub("", _FENCE_OPEN.sub("", text.strip())).strip()
    candidates = [t]
    start, end = t.find("{"), t.rfind("}")
    if start != -1 and end > start:
        candidates.append(t[start : end + 1])
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        state = str(data.get("state") or "unknown").strip().lower()
        result = {
            "goal": str(data.get("goal") or "").strip(),
            "now": str(data.get("now") or "").strip(),
            "state": state if state in _STATES else "unknown",
            "progress": str(data.get("progress") or "").strip(),
            "one-line-job-description": str(
                data.get("one-line-job-description") or ""
            ).strip(),
        }
        # FAQ answers are optional for older providers, but preserve the
        # generated question/answer pairs when present so the web card can
        # display them.
        if isinstance(data.get("faq"), list):
            result["faq"] = [
                {
                    "question": str(item.get("question") or "").strip(),
                    "answer": str(item.get("answer") or "").strip(),
                }
                for item in data["faq"]
                if isinstance(item, dict) and str(item.get("question") or "").strip()
            ]
        return result
    return None


# --------------------------------------------------------------------------- #
# composition and cache
# --------------------------------------------------------------------------- #
#: session name -> (cache key, last result). The key is everything that would
#: change the answer; the dict is mirrored to the daemon instance directory.
_cache: Dict[str, Tuple[tuple, dict]] = {}
_loaded_cache_path: Optional[Path] = None


def _restore_cache() -> None:
    """Load durable briefings once for the active daemon instance."""
    global _loaded_cache_path
    path = paths.briefings_json()
    if _loaded_cache_path == path:
        return
    _loaded_cache_path = path
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, ValueError):
        return
    if not isinstance(data, dict):
        return
    for name, row in data.items():
        if not isinstance(row, dict) or not isinstance(row.get("key"), list):
            continue
        result = row.get("result")
        if isinstance(result, dict):
            _cache[str(name)] = (_tupleize(row["key"]), result)


def _tupleize(value):
    if isinstance(value, list):
        return tuple(_tupleize(item) for item in value)
    return value


def _persist_cache() -> None:
    path = paths.briefings_json()
    data = {
        name: {"key": _jsonable(key), "result": result}
        for name, (key, result) in _cache.items()
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with atomic.scratch(path) as tmp:
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            atomic.replace(tmp, path)
    except OSError:
        pass


def _jsonable(value):
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    return value


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def compose(session, cfg: dict, *, refresh: bool = False) -> dict:
    """The briefing payload for one session (the endpoint's 200 body).

    Shape (frozen contract with the web UI): ``session``, ``generated_at``
    (ISO, of the actual generation — a cache hit keeps it), ``cached``,
    ``source`` (which inputs existed), and ``briefing`` XOR ``raw`` — the
    parsed JSON when the model obeyed, its raw text when it did not.

    ``raw`` now means one thing only: the model wrote a WHOLE answer in the
    wrong shape. A cut-off one raises :class:`BriefingError` instead, because
    the two used to be indistinguishable here and both went out as a silent
    200. On the 30-session run behind this: 5 briefings did not parse, 4 of
    them empty and 1 cut off mid-string at 68 bytes. Only that last one
    reaches this function — :func:`call_llm` raises on an empty answer before
    :func:`parse_briefing` ever sees it — so the case this branch exists for
    was 1 of 30 there. Which of the two is commoner was not measured.
    """
    sdef = session.sdef
    name = sdef.name
    _restore_cache()
    live_info = gather_live(session)
    faq = gather_faq()
    cflow_info = gather_cflow(sdef.cwd or "", name)
    jsonl_path = locate_transcript(sdef)
    stat_key = None
    if jsonl_path is not None:
        try:
            st = jsonl_path.stat()
            stat_key = (st.st_mtime_ns, st.st_size)
        except OSError:
            jsonl_path = None
    cache_key = (
        name,
        stat_key,
        cflow_info.get("step") if cflow_info else None,
        live_info.get("status") if live_info else None,
        live_info.get("last_input_at") if live_info else None,
        live_info.get("last_output_at") if live_info else None,
        tuple(
            (row.get("id"), row.get("question"), row.get("answer"), row.get("enabled"))
            for row in faq
        ),
    )
    if not refresh:
        hit = _cache.get(name)
        if hit is not None and hit[0] == cache_key:
            return {**hit[1], "cached": True}
    events = tail_events(jsonl_path) if jsonl_path is not None else []
    prompt = build_prompt(sdef, cflow_info, events, live_info, faq)
    answer = await call_llm(cfg, prompt)
    parsed = parse_briefing(answer.text)
    if parsed is None and answer.finish_reason == "length":
        # Text arrived, but the endpoint says it was CUT at the budget — the
        # JSON is half-written, not badly written. Serving it as ``raw`` would
        # put a torn-off sentence on the card and cache it there, so this is
        # the same failure as the empty answer and gets the same 502.
        raise BriefingError(
            f"llm answer was truncated at max_tokens={cfg['max_tokens']} "
            f"(finish_reason='length', completion_tokens={answer.completion_tokens}, "
            f"{len(answer.text)} chars, not parseable) — raise llm.max_tokens "
            "in ~/.claunch.yaml"
        )
    result = {
        "session": name,
        "generated_at": _now_iso(),
        "cached": False,
        "source": {"jsonl": jsonl_path is not None, "cflow": cflow_info is not None},
        "briefing": parsed,
        "raw": None if parsed is not None else answer.text,
    }
    _cache[name] = (cache_key, result)
    _persist_cache()
    return result


def digest(name: str) -> Optional[dict]:
    """The cached one-liner (+ state) for the session list, never composed.

    Served on the ``/api/sessions`` poll so a rail row can show the briefing's
    one-line without opening the card — and without an LLM call. Reading the
    cache is what makes the one-line survive a browser refresh without
    regeneration: the browser loses its in-memory copy, the daemon does not,
    and the list poll pours the digest straight back. ``None`` when no
    briefing has been composed for this session (the row then falls back to
    the recorded opening task).
    """
    _restore_cache()
    hit = _cache.get(name)
    if hit is None:
        return None
    brief = (hit[1] or {}).get("briefing")
    if not isinstance(brief, dict):
        return None
    one = str(brief.get("one-line-job-description") or "").strip()
    if not one:
        return None
    return {"one_line": one, "state": str(brief.get("state") or "").strip()}
