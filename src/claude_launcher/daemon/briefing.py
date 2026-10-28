"""One session's "what is it doing right now" briefing, composed by an LLM.

The web UI wants 3-4 sentences per session: the goal, what is happening now,
and a coarse state. No single registry holds that, so this module gathers the
hybrid evidence that exists — the session's own record (cwd, opening task),
its cflow run position (read-only), and the tail of its harness transcript
(Claude's project jsonl or Codex's rollout jsonl) — and asks an
OpenAI-compatible ``chat/completions`` endpoint to compress it into a fixed
JSON shape.

Configuration is the ``llm:`` block of ``~/.claude-launcher/.claunch.yaml`` (``store.load()``
is authoritative), editable from the web dashboard's Settings page
(``GET``/``PUT /api/briefing/llm``). The backend comes from one of two
places: the block's own fields, or — when ``preset:`` names one — a *saved
preset* from the block's ``presets:`` list. A preset is one whole backend
(the same four fields), so switching which model answers is a pointer move
rather than a retype, and the list holds as many as the operator wants.

Within whichever is in charge, the backend is said in one of two ways:

* ``profile: <name>`` — the call reuses a claunch profile's backend. That
  profile's provider chain states the OpenAI-compatible endpoint
  (``endpoints.openai``), the key that reaches it and the model ids it
  serves; see :func:`profile_backend`.
* ``endpoint:`` plus ``api_key:`` typed in directly, which is what the block
  held before a profile was an option and still works.

The two never sit in the block together: naming a profile from the Settings
card removes the typed pair, because a call under a profile reads neither and
the values left behind describe whichever backend was typed in before. A
block that holds both was hand-edited, and the profile wins.

``model:`` is the id sent to the backend in either form, and falls back to
the profile's ``models.default`` when a profile is named and the field is
empty. An empty ``api_key`` with no profile to supply one means the feature
is off — the key must never be committed or logged; it leaves this module
only as the ``Authorization`` header of the LLM call itself.

``max_tokens`` and ``params`` belong to the block, never to a preset:
switching which backend answers must not quietly change what the call is
allowed to spend. A ``preset:`` that names nothing in ``presets:`` (deleted,
or renamed by hand) reads as no preset at all and falls back to the block's
own fields, which is what makes the card and the call agree about what is in
use.

Results are cached per session, keyed by what would change the answer: the
transcript file's (mtime, size) and the cflow step. The cache is persisted in
the daemon instance directory so a daemon restart keeps the last briefing;
``refresh`` bypasses it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, NamedTuple, Optional, Tuple

import aiohttp

try:
    import ssl as _ssl
    import truststore as _truststore
except ImportError:  # Python < 3.10, or truststore not installed
    _truststore = None

log = logging.getLogger("claunch.daemon.briefing")

from .. import atomic, harnesses as harness_registry
from .. import lineage, profile as profile_mod, provider_spec, providers
from .. import routing, store, transcripts
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

#: A saved preset's name. Lowercase alphanumerics and hyphens, short enough
#: for a select — the same shape ``model_tiers`` uses for its ids.
PRESET_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")

#: Most presets one block may hold. A picker, not a collection: the card draws
#: every preset as an editable row.
MAX_PRESETS = 20

#: The fields a preset carries. ``max_tokens``, ``params`` and the preset list
#: itself stay on the block: switching a preset changes which backend answers,
#: and must not quietly change how much the call is allowed to spend.
PRESET_FIELDS = ("profile", "model", "endpoint", "api_key")

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

#: The timeout of the Settings card's "Test connection" probe. Shorter than a
#: briefing's: the probe spends one token, and a card that waits a minute to
#: say "the host did not answer" reads as a broken page rather than a broken
#: endpoint.
CONNECTION_TIMEOUT = 20.0

#: What the probe asks for. One token, no instructions to follow — the probe
#: reports whether the endpoint answers, not what it answers, so anything
#: longer would be spent for a text this feature never reads.
CONNECTION_PROMPT = "ping"

#: An SSL context that verifies against the OS's own trust store instead of
#: OpenSSL's own chain validation, the same one :mod:`.rag` and
#: :mod:`.observer` already use for their endpoints. A corporate TLS-inspection
#: root that the OS trusts can still fail OpenSSL's stricter X.509 checks:
#: measured here as ``SSLCertVerificationError: ... Basic Constraints of CA
#: cert not marked critical`` against ``api.deepseek.com``, with the same
#: request returning 200 once this context carried it. Without it the whole
#: feature is dark on such a machine, and the only sign is a 502 on the card.
#: ``None`` when ``truststore`` is unavailable, and the caller then falls back
#: to aiohttp's default context rather than failing.
_OS_TRUST_CONTEXT = _truststore.SSLContext(_ssl.PROTOCOL_TLS_CLIENT) if _truststore else None

_STATES = frozenset({"working", "blocked", "waiting", "idle", "done", "unknown"})


class BriefingError(Exception):
    """The LLM call failed (transport, HTTP status, or an unusable body)."""


class FaqError(Exception):
    """The daemon-local briefing FAQ could not be read or written."""


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
class Backend(NamedTuple):
    """One profile's OpenAI-compatible call values."""

    #: The full ``chat/completions`` URL, ``/chat/completions`` appended when
    #: the provider records the base alone.
    endpoint: str
    #: The key that reaches it: the launcher-managed token first, the
    #: provider's own ``api_key`` second, ``""`` when neither is set.
    api_key: str
    #: The model the profile leads with (its ``default`` role).
    model: str
    #: Every model id the profile names, ``default`` first — what the
    #: Settings page offers beside the model field.
    models: Tuple[str, ...]
    #: The provider name behind the profile. The call needs it to apply the
    #: request-header rules that provider declares (:mod:`claude_launcher.
    #: routing`), which the harness paths get from the shim and this one
    #: otherwise would not.
    provider: str = ""


def profile_backend(name: str, doc: Optional[dict] = None) -> Backend:
    """Resolve profile ``name`` into the values :func:`call_llm` needs.

    The same resolution the Observer performs
    (:func:`daemon.observer.configuration`), and for the same reason: a
    profile already records where its backend is, which key opens it and
    which models it serves, so naming one is shorter than retyping all three
    into the ``llm:`` block and cannot drift from what the sessions on that
    profile call.

    Only the ``openai`` protocol is read, because that is the protocol
    :func:`call_llm` speaks. A profile whose provider declares an Anthropic
    endpoint alone raises :class:`BriefingError` here rather than producing a
    call that fails at the endpoint later; the Settings page shows that
    message beside the profile it belongs to.

    Model ids are returned without Claude's context-window tag
    (``deepseek-flash[1m]`` -> ``deepseek-flash``), which is a decoration
    Claude Code reads and an OpenAI-compatible backend rejects.
    """
    doc = store.load() if doc is None else doc
    try:
        prof = profile_mod.require_selector(name)
    except ProfileError as exc:
        raise BriefingError(str(exc)) from exc
    try:
        spec = providers.spec_for(prof, doc=doc)
    except (providers.ProviderError, ProfileError) as exc:
        raise BriefingError(str(exc)) from exc
    try:
        endpoint = provider_spec.openai_urls(
            spec.endpoint("openai") or ""
        ).chat_completions_url
    except provider_spec.SpecError as exc:
        raise BriefingError(str(exc)) from exc
    if not endpoint:
        raise BriefingError(
            f"profile {prof.name!r} has no OpenAI-compatible endpoint "
            f"(set providers.<provider>.endpoints.openai)"
        )
    models = tuple(
        value
        for value in (
            provider_spec.split_model_tag(raw)[0] for raw in spec.model_list()
        )
        if value
    )
    return Backend(
        endpoint=endpoint,
        api_key=str(lineage.stored_auth_token(prof) or spec.api_key or ""),
        model=models[0] if models else "",
        models=models,
        provider=providers.resolve_name(prof, doc),
    )


def _clean_preset(raw: object) -> Optional[dict]:
    """One saved preset as a well-shaped dict, or ``None`` when unusable.

    A preset with no id, an id that is not a preset name, or not one of the
    known fields at all is dropped: a corrupt entry must cost its own row,
    not the picker. Values are stripped strings, and an absent one is simply
    absent (a preset that names only a profile is the common case).
    """
    if not isinstance(raw, dict):
        return None
    preset_id = str(raw.get("id") or "").strip()
    if not PRESET_ID_RE.match(preset_id):
        return None
    out: dict = {"id": preset_id}
    for field in PRESET_FIELDS:
        value = str(raw.get(field) or "").strip()
        if value:
            out[field] = value
    return out


def presets(doc: Optional[dict] = None) -> List[dict]:
    """Every saved preset, in stored order, cleaned.

    Duplicates are dropped after the first: the picker addresses a preset by
    its id, so a second row with the same id would be unreachable and the
    active pointer ambiguous.
    """
    doc = store.load() if doc is None else doc
    block = doc.get("llm")
    if not isinstance(block, dict):
        return []
    out: List[dict] = []
    seen = set()
    for raw in block.get("presets") or []:
        preset = _clean_preset(raw)
        if preset is None or preset["id"] in seen:
            continue
        seen.add(preset["id"])
        out.append(preset)
    return out


def active_preset(doc: Optional[dict] = None) -> str:
    """The preset id the block points at, or ``""`` for the block's own fields.

    A pointer that names no saved preset resolves to ``""`` rather than
    failing: the row it named was deleted or hand-renamed, and the card's job
    is to show that plainly beside the fields it falls back to.
    """
    doc = store.load() if doc is None else doc
    block = doc.get("llm")
    if not isinstance(block, dict):
        return ""
    wanted = str(block.get("preset") or "").strip()
    if not wanted:
        return ""
    return wanted if any(p["id"] == wanted for p in presets(doc)) else ""


def _block_fields(block: dict) -> dict:
    """The block's own four backend fields, blank when absent."""
    return {field: str(block.get(field) or "").strip() for field in PRESET_FIELDS}


def llm_config(doc: Optional[dict] = None) -> dict:
    """The effective ``llm`` settings (missing keys filled with defaults).

    Mirrors :func:`store.daemon_config`'s shape-tolerance: a missing or
    malformed block yields the disabled default rather than an error, because
    this file is hand-edited. A ``profile`` that cannot be resolved is the
    same case: the returned config is simply not configured, and
    ``GET /api/briefing/llm`` is where the reason is reported.

    The backend comes from the *active preset* when one is pointed at, and
    from the block's own ``profile``/``model``/``endpoint``/``api_key``
    otherwise. A preset is a whole saved backend, so it supplies all four of
    its fields at once, in the same pairing the block's own two use: a preset
    that names a profile reads that profile's endpoint and key and ignores
    any typed endpoint it also holds, and a preset with no profile is the
    direct form. ``max_tokens``, ``params`` and the preset list itself are
    the block's, never a preset's — switching backends must not change what
    the call is allowed to spend.

    A named profile supplies ``endpoint`` and ``api_key`` as one pair, and
    the block's own two fields are then left out of the result. The two
    identify one backend together, so filling one of them from the block
    would send a key typed for one endpoint to another. ``model`` is the
    exception: the chosen model wins, because the model is this feature's
    choice about the backend rather than a property of it, and the profile's
    ``default`` fills in only when the field is empty.

    ``provider`` is the provider name behind a named profile, and empty for
    the typed form — a typed endpoint belongs to no provider entry, so no
    per-provider request-header rule applies to it. :func:`call_llm` reads it
    to send what a shim would have sent (see
    :func:`briefing_request_headers`).
    """
    doc = store.load() if doc is None else doc
    block = doc.get("llm")
    if not isinstance(block, dict):
        block = {}
    params = block.get("params")
    chosen_id = active_preset(doc)
    source = None
    if chosen_id:
        source = next(p for p in presets(doc) if p["id"] == chosen_id)
    fields = _block_fields(source if source is not None else block)
    try:
        max_tokens = int(block.get("max_tokens") or DEFAULT_MAX_TOKENS)
    except (TypeError, ValueError):
        max_tokens = DEFAULT_MAX_TOKENS
    profile_name = fields["profile"]
    endpoint = fields["endpoint"]
    api_key = fields["api_key"]
    model = fields["model"]
    provider = ""
    if profile_name:
        try:
            backend = profile_backend(profile_name, doc)
        except BriefingError:
            endpoint, api_key = "", ""
        else:
            endpoint, api_key = backend.endpoint, backend.api_key
            model = model or backend.model
            provider = backend.provider
    return {
        "preset": chosen_id,
        "profile": profile_name,
        "provider": provider,
        "endpoint": endpoint,
        "model": model,
        "api_key": api_key,
        "max_tokens": max_tokens,
        "params": dict(params) if isinstance(params, dict) else {},
    }


def llm_configured(cfg: dict) -> bool:
    """Whether the feature is on: endpoint, model and api_key all present."""
    return bool(cfg.get("endpoint") and cfg.get("model") and cfg.get("api_key"))


class PresetError(Exception):
    """A preset name or saved preset that cannot be used."""


def presets_for_display(doc: Optional[dict] = None) -> List[dict]:
    """Every saved preset as the Settings card may see it — without keys.

    The key of each preset is reported as ``api_key_set`` and never as a
    value, for the same reason the block's own key is not returned: this
    payload crosses to a browser. A preset naming a profile is additionally
    resolved, so the card can show the endpoint and model a call would
    actually use and say when the profile cannot serve.
    """
    doc = store.load() if doc is None else doc
    rows: List[dict] = []
    for preset in presets(doc):
        # ``_clean_preset`` drops a field rather than storing it blank, so
        # every read here goes through ``.get``.
        profile_name = preset.get("profile", "")
        model = preset.get("model", "")
        endpoint = preset.get("endpoint", "")
        row: dict = {
            "id": preset["id"],
            "profile": profile_name,
            "model": model,
            "endpoint": endpoint,
            "api_key_set": bool(preset.get("api_key")),
            "resolved": {"endpoint": "", "model": "", "has_key": False,
                         "provider": ""},
            "error": "",
        }
        if profile_name:
            try:
                backend = profile_backend(profile_name, doc)
            except BriefingError as exc:
                row["error"] = str(exc)
            else:
                row["resolved"] = {
                    "endpoint": backend.endpoint,
                    "model": model or backend.model,
                    "has_key": bool(backend.api_key),
                    "provider": backend.provider,
                }
        else:
            row["resolved"] = {
                "endpoint": endpoint,
                "model": model,
                "has_key": bool(preset.get("api_key")),
                "provider": "",
            }
        rows.append(row)
    return rows


def check_preset_name(name: str) -> str:
    """``name`` as a storable preset id, or :class:`PresetError` saying why."""
    text = str(name or "").strip()
    if not PRESET_ID_RE.match(text):
        raise PresetError(
            f"preset name {name!r} must be lowercase letters, digits and "
            "hyphens, at most 32 characters, starting with a letter or digit"
        )
    return text


def check_preset_list(raw: object) -> List[dict]:
    """A submitted preset list as it may be stored, or :class:`PresetError`.

    Unlike reading a hand-edited file, a *submitted* list is refused rather
    than pruned: the card sends what the operator sees, so a row that cannot
    be stored means the form and the store disagree, and silently dropping it
    would lose an entry the operator believes they saved.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise PresetError("presets must be a list")
    if len(raw) > MAX_PRESETS:
        raise PresetError(f"at most {MAX_PRESETS} presets, got {len(raw)}")
    out: List[dict] = []
    seen = set()
    for position, item in enumerate(raw):
        where = f"preset {position + 1}"
        if not isinstance(item, dict):
            raise PresetError(f"{where}: must be an object")
        preset_id = check_preset_name(item.get("id"))
        if preset_id in seen:
            raise PresetError(f"{where}: duplicate preset name {preset_id!r}")
        seen.add(preset_id)
        entry: dict = {"id": preset_id}
        for field in PRESET_FIELDS:
            value = str(item.get(field) or "").strip()
            if value:
                entry[field] = value
        if not (entry.get("profile") or entry.get("endpoint")):
            raise PresetError(
                f"{where}: needs a profile or an endpoint — a preset that "
                "names neither identifies no backend"
            )
        out.append(entry)
    return out


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


def locate_transcript(sdef, *, deep: bool = True) -> Optional[Path]:
    """The session's transcript file, or ``None`` (no conversation, no file).

    ``deep`` is the search that runs when the direct address misses: claude's
    id scan reads every project directory (235 of them in this fleet), and
    codex has no direct address at all — its locator opens every rollout file
    to read the id out of it. Both cost a directory walk per session, which a
    caller resolving one session pays gladly and a caller resolving the whole
    fleet cannot. With ``deep`` off, claude answers from the direct address
    alone and codex answers nothing, so a conversation filed under a slug
    this module spells differently reads as absent to that caller and is
    still found when the session is asked for by name.

    What counts as on disk for claude is not spelled here: that branch calls
    :func:`claude_launcher.transcripts.locate`, the same answer
    :func:`claude_launcher.transcripts.exists` gives a restore. The two
    callers differ in what they do with it -- this one reads the file, that
    one runs ``--resume`` -- and agree on the case that separates a name from
    a conversation: a zero-byte jsonl has nothing to read and nothing to
    resume. Spelling it twice is how the two drifted, with the copy here
    answering "there is a transcript" where the canonical one answered "there
    is not" (claunch-fork-family-recheck-amnqg.1).
    """
    cid = getattr(sdef, "conversation_id", None)
    if not cid:
        return None
    if getattr(sdef, "harness", None) == "codex":
        if not deep:
            return None
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
        return transcripts.locate(cdir, cid, sdef.cwd, deep=deep)
    return transcripts.find(cdir, cid) if deep else None


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


def _clean_faq(rows) -> List[dict]:
    """Normalize the persisted FAQ representation."""
    if not isinstance(rows, list):
        return []
    clean = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        question = str(row.get("question") or "").strip()
        if question:
            clean.append({
                "id": str(row.get("id") or uuid.uuid4()),
                "question": question,
                "answer": str(row.get("answer") or "").strip(),
                "enabled": row.get("enabled", True) is not False,
            })
    return clean


def faq_entries() -> List[dict]:
    """Read this daemon instance's briefing FAQ.

    The old global setting is imported on first access, then the daemon-local
    copy is authoritative.  The legacy source is left untouched so migration
    never needs to rewrite a configuration file that may be open in an editor.
    """
    path = paths.briefing_faq_json()
    try:
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("the FAQ file must contain an object")
            return _clean_faq(data.get("faq"))
    except (OSError, ValueError) as exc:
        raise FaqError(f"cannot read briefing FAQ {path}: {exc}") from exc
    try:
        legacy = store.briefing_faq()
    except store.StoreError:
        legacy = []
    if legacy:
        return set_faq_entries(legacy)
    return []


def set_faq_entries(rows) -> List[dict]:
    """Persist user-defined FAQ entries in this daemon instance's state."""
    clean = _clean_faq(rows)
    path = paths.briefing_faq_json()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with atomic.scratch(path) as tmp:
            tmp.write_text(
                json.dumps({"faq": clean}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            atomic.replace(tmp, path)
    except OSError as exc:
        raise FaqError(f"cannot write briefing FAQ {path}: {exc}") from exc
    return clean


def gather_faq() -> List[dict]:
    """Read enabled FAQ entries for the summariser prompt."""
    try:
        return [row for row in faq_entries() if row.get("enabled", True)]
    except FaqError:
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


#: The id this feature sends under a provider's required session header
#: (``providers.<name>.session_header``). It is deliberately NOT the calling
#: session's name: a briefing is the daemon's own summariser call, not one of
#: a session's turns, and the user asked for it to be marked as such at the
#: upstream (2026-10-25). The shape is the same as
#: :data:`routing.SESSION_HEADER_FALLBACK` — one shared id for every launch of
#: this caller — so the two read as the same kind of value.
BRIEFING_SESSION_ID = "claunch-briefing"


def briefing_request_headers(cfg: dict) -> Dict[str, str]:
    """The request headers one briefing call goes out with.

    The OpenAI-compatible shape (``Accept``, ``Content-Type``, the bearer)
    plus whatever per-provider rule :mod:`claude_launcher.routing` declares
    for the provider this endpoint belongs to. The rules are read on every
    call, live, exactly as the shim reads them: a provider edited in
    ``~/.claude-launcher/.claunch.yaml`` (including from the Settings page) takes effect on the
    next briefing without a restart.

    A malformed provider value raises :class:`BriefingError` instead of
    skipping the header — the shim can degrade to "traffic still flows", but
    here there is nothing left to send: the upstream refuses the request
    anyway, and a silent skip would look like a provider bug. The message is
    the routing error's, which names the config key.

    The path is the endpoint's own, so the Messages-only auth move applies
    exactly when this call is on a Messages path — which it is not for the
    ``chat/completions`` calls this feature makes, but the rule is the rule.
    """
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {cfg.get('api_key', '')}",
    }
    provider = str(cfg.get("provider") or "")
    if not provider:
        return headers
    try:
        routing.apply_session_header(
            headers, provider, session=BRIEFING_SESSION_ID
        )
        routing.apply_anthropic_auth(
            headers, provider, path=str(cfg.get("endpoint") or "")
        )
    except routing.RoutingError as exc:
        raise BriefingError(str(exc)) from exc
    return headers


async def call_llm(cfg: dict, prompt: str, *, timeout: float = LLM_TIMEOUT) -> LlmAnswer:
    """POST the prompt to the OpenAI-compatible endpoint, return the answer.

    Any transport or shape failure becomes :class:`BriefingError`; the message
    carries at most a snippet of the *response* body — never the request, so
    the api_key cannot leak through an error path.

    An empty ``content`` is one of those failures. It is what a reasoning model
    returns when ``max_tokens`` ran out mid-thought, and it arrives as a
    perfectly ordinary HTTP 200 — so if it were passed on, the caller would
    cache "" and the UI would render a blank card with nothing to say why.

    The request headers are :func:`briefing_request_headers`'s, so a provider
    that refuses a request without its own session header (OpenCode Go) is
    answered the way a session on that provider is: the shim is not in this
    path, so the rules are applied here or nowhere.
    """
    body = {
        "model": cfg["model"],
        "max_tokens": cfg["max_tokens"],
        "messages": [{"role": "user", "content": prompt}],
    }
    body.update(cfg.get("params") or {})
    headers = briefing_request_headers(cfg)
    try:
        client_timeout = aiohttp.ClientTimeout(total=timeout)
        connector = (
            aiohttp.TCPConnector(ssl=_OS_TRUST_CONTEXT)
            if _OS_TRUST_CONTEXT is not None
            else None
        )
        async with aiohttp.ClientSession(
            timeout=client_timeout, connector=connector
        ) as http:
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
                "anything, so raise llm.max_tokens in ~/.claude-launcher/.claunch.yaml"
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


class Connection(NamedTuple):
    """What one connection probe learned about a backend.

    ``ok`` is whether a briefing sent to this backend right now would leave
    the machine and be answered — the status codes and the parse, not the
    text. ``detail`` is the sentence the Settings card shows, and it carries
    at most a snippet of the *response* body for the same reason
    :func:`call_llm`'s does: the request holds the key.
    """

    ok: bool
    status: Optional[int]
    detail: str
    model: str
    endpoint: str
    provider: str
    seconds: float


#: The completion budget a probe spends, and its ceiling: the probe reads the
#: status and the shape of the answer, never the answer itself, so a saved
#: budget of 4096 is not spent to learn whether the host is up.
CONNECTION_MAX_TOKENS = 16


async def probe_llm(cfg: dict, *, timeout: float = CONNECTION_TIMEOUT) -> Connection:
    """Send one minimal call to the backend ``cfg`` describes.

    This is the Settings card's "Test connection", and it deliberately walks
    the *same* path a briefing walks: the same resolved endpoint, model and
    key (:func:`llm_config`), the same body shape, and the same headers from
    :func:`briefing_request_headers`, per-provider rules included. A probe
    that built its own request would report a reachable endpoint for a
    provider that answers every real briefing with ``400 MissingSessionID``
    — which is the exact failure this feature exists to make visible before
    the operator waits for a briefing to fail.

    An unreachable host, a refused key, a bad provider rule and a body that
    is not a chat completion all come back as ``ok=False`` with the reason in
    ``detail``. They are results here rather than exceptions because every
    one of them is a thing the card has to *show*, not a fault in the request
    that asked for the probe.
    """
    started = time.monotonic()
    model = str(cfg.get("model") or "")
    endpoint = str(cfg.get("endpoint") or "")
    provider = str(cfg.get("provider") or "")

    def result(ok: bool, status: Optional[int], detail: str) -> Connection:
        return Connection(
            ok=ok,
            status=status,
            detail=detail,
            model=model,
            endpoint=endpoint,
            provider=provider,
            seconds=round(time.monotonic() - started, 3),
        )

    missing = [
        name
        for name, value in (
            ("endpoint", endpoint),
            ("model", model),
            ("api_key", str(cfg.get("api_key") or "")),
        )
        if not value
    ]
    if missing:
        return result(
            False,
            None,
            "not sent: " + ", ".join(missing) + " not set"
            + (" (a named profile supplies these)" if cfg.get("profile") else ""),
        )
    try:
        headers = briefing_request_headers(cfg)
    except BriefingError as exc:
        return result(False, None, f"not sent: {exc}")
    body = {
        "model": model,
        "max_tokens": CONNECTION_MAX_TOKENS,
        "messages": [{"role": "user", "content": CONNECTION_PROMPT}],
    }
    # The saved params are merged first so the cap goes on last: a
    # ``params.max_tokens`` of 4096 would otherwise ride over the budget this
    # probe promises to stay inside.
    body.update(cfg.get("params") or {})
    body["max_tokens"] = min(int(body.get("max_tokens") or CONNECTION_MAX_TOKENS),
                             CONNECTION_MAX_TOKENS)
    try:
        client_timeout = aiohttp.ClientTimeout(total=timeout)
        connector = (
            aiohttp.TCPConnector(ssl=_OS_TRUST_CONTEXT)
            if _OS_TRUST_CONTEXT is not None
            else None
        )
        async with aiohttp.ClientSession(
            timeout=client_timeout, connector=connector
        ) as http:
            async with http.post(endpoint, json=body, headers=headers) as resp:
                status = resp.status
                text = await resp.text()
                if status != 200:
                    return result(
                        False, status, f"answered {status}: {text[:300]}"
                    )
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        return result(False, None, f"could not reach {endpoint}: {exc}")
    try:
        data = json.loads(text)
        choice = data["choices"][0]
    except (ValueError, KeyError, IndexError, TypeError):
        return result(
            False,
            status,
            f"answered {status} but not with a chat completion "
            f"(no choices[0]): {text[:300]}",
        )
    finish = choice.get("finish_reason") if isinstance(choice, dict) else None
    content = ""
    if isinstance(choice, dict) and isinstance(choice.get("message"), dict):
        content = str(choice["message"].get("content") or "")
    elapsed = round(time.monotonic() - started, 3)
    if content.strip():
        detail = f"answered {status} in {elapsed}s — the backend replied"
    else:
        # A 200 with nothing in it is still a connected, authenticated call;
        # at this budget a reasoning model spends the tokens thinking, so it
        # is reported rather than failed.
        detail = (
            f"answered {status} in {elapsed}s — connected, but the reply "
            f"was empty (finish_reason={finish!r}); a real briefing uses the "
            "llm.max_tokens budget, not this probe's"
        )
    return Connection(
        ok=True,
        status=status,
        detail=detail,
        model=model,
        endpoint=endpoint,
        provider=provider,
        seconds=elapsed,
    )


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


#: Called, with nothing, after the cache is written — a fresh briefing is
#: part of a session's text in the search index's fleet corpus
#: (:meth:`daemon.rag.RagService.enqueue`), so the index follows it from
#: here. Module-level because the cache is; the daemon registers on build
#: and removes on shutdown.
persist_hooks: List[Callable[[], None]] = []


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
    for hook in list(persist_hooks):
        try:
            hook()
        except Exception:
            log.exception("briefing: persist hook %r failed", hook)


def _jsonable(value):
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    return value


def forget_all() -> None:
    """Drop every cached briefing, persisted copy included.

    Called when the backend behind the feature changes — a different profile,
    endpoint or model. The cache key is a session's *evidence* and not the
    model that read it, so without this a briefing written by the previous
    model would stay on the card until that session's transcript moved, and
    the change the operator just saved would look like it had done nothing.
    """
    _restore_cache()
    _cache.clear()
    _persist_cache()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _index_briefing(name: str, payload, at: str) -> None:
    """Mirror one composed briefing into the search index, never raising.

    The index is a derived store and this runs *after* ``_persist_cache`` has
    put the briefing on disk, so the answer the model wrote is already saved.
    Raising here does not become a ``BriefingError`` -- :func:`compose`'s
    callers catch that one and this is not one -- so it left the route as an
    uncaught exception and the card showed a 500 that said nothing about the
    index. The daemon's ``briefing`` trigger reported "could not be composed"
    through ``cflow_clock.recompose_briefing`` for a briefing that had in fact
    been composed and cached.

    ``search_records.database()`` opens SQLite, creates the table and commits,
    so the failure that arrives here is a ``sqlite3.Error`` -- not an
    ``OSError``, and ``sqlite3.Error`` is not an ``OSError`` subclass (MRO:
    OperationalError -> DatabaseError -> Error -> Exception). The guard is this
    wide on purpose, matching ``session_events.record`` (claunch-mxar3) and the
    observer's callers (claunch-remember-callers-guard).
    """
    from . import search_records

    try:
        search_records.capture(name, "briefing", payload, at)
    except (OSError, sqlite3.Error):
        log.exception("could not index the briefing for %s", name)


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
            f"{len(answer.text)} chars, not parseable) — raise the token "
            "budget in Settings ▸ Briefing model (llm.max_tokens)"
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
    _index_briefing(name, parsed or {"raw": answer.text}, result["generated_at"])
    return result


#: How much of one briefing field the session list is allowed to carry. The
#: poll holds one digest per session for as long as the fleet is up, and both
#: of its readers are a glance — an ellipsised rail line and a hover tooltip —
#: so a summariser that answers in paragraphs costs the poll a bounded amount
#: and the card (which fetches the full text) is where the rest is read.
DIGEST_FIELD_CHARS = 240


def cached_result(name: str) -> Optional[dict]:
    """Read the last complete briefing without checking changing evidence."""
    _restore_cache()
    hit = _cache.get(name)
    return {**hit[1], "cached": True} if hit is not None else None


def digest(name: str) -> Optional[dict]:
    """The cached briefing's short fields for the session list, never composed.

    Served on the ``/api/sessions`` poll so a rail row can show the briefing's
    one-line without opening the card — and without an LLM call — and so a
    session tab can put the summary itself on its hover tooltip. Reading the
    cache is what makes both survive a browser refresh without regeneration:
    the browser loses its in-memory copy, the daemon does not, and the list
    poll pours the digest straight back. ``None`` when no briefing has been
    composed for this session (the row then falls back to the recorded
    opening task), and also when a composed one holds no text at all.

    ``goal``/``now``/``progress`` are present only when the summariser filled
    them, so a reader can tell an empty field from one the older response
    shape never had.
    """
    _restore_cache()
    hit = _cache.get(name)
    if hit is None:
        return None
    brief = (hit[1] or {}).get("briefing")
    if not isinstance(brief, dict):
        return None
    out = {
        "one_line": _clip(
            str(brief.get("one-line-job-description") or ""), DIGEST_FIELD_CHARS
        ),
        "state": str(brief.get("state") or "").strip(),
    }
    for key in ("goal", "now", "progress"):
        value = _clip(str(brief.get(key) or ""), DIGEST_FIELD_CHARS)
        if value:
            out[key] = value
    if not any(out.get(key) for key in ("one_line", "goal", "now", "progress")):
        return None
    return out
