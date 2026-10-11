"""The daemon's simulation registry: records, ownership, model, rounds,
findings.

The daemon is the only writer. Sessions reach it through the API (the MCP
tools are thin clients), so a session in any worktree reads the same record,
and a run session that dies holds no state that a restart would lose.

One directory per simulation under :func:`paths.simulations_dir`::

    sim.json        the record: title, goal, root, owner, state, settings
    model.json      the world model (see :mod:`.model`)
    findings.json   the ledger, by finding key
    rounds.jsonl    one summary line per round
    journal.jsonl   who did what, append-only
    submits.json    idempotency keys of accepted submissions -> result

Ownership. ``owner`` is a session name or empty. The owner may start, stop,
run rounds, change the model and settings, and resolve findings; any other
session may only read. The user (``actor == USER``, the web UI) may do all
of that and also transfer or release ownership. A session may claim a
simulation nobody owns. When the owner session exits the daemon calls
:meth:`SimStore.session_exited`: ownership is released and a running
campaign stops.

Campaign state. ``idle`` (nothing running), ``running`` (every model change
reruns a round; the run stops itself after ``max_rounds`` rounds or when a
round leaves nothing open) and ``stopped`` (someone stopped it, or the limit
was reached). ``state_reason`` says why.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import secrets
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from .. import atomic
from . import engine, model as M

USER = "user"
STATES = ("idle", "running", "stopped")
FINDING_STATUS = ("open", "resolved", "dismissed", "question", "answered")
REASONING = ("none", "low", "medium", "high")
#: Settings a simulation carries, with defaults (the daemon's
#: ``simulation_*`` config keys override the profile/reasoning defaults).
DEFAULT_SETTINGS = {
    "interpreter_profile": "",   # B: pi profile that formalizes and interprets
    "judge_profile": "",         # C: pi profile that judges (a different model)
    "interpreter_model": "",     # a model of the interpreter profile ("" = its default)
    "judge_model": "",           # a model of the judge profile, or of the interpreter's when no judge profile
    "interpreter_reasoning": "low",
    "judge_reasoning": "low",
    "seed": 1,
    "budget": 2000,
    "horizon": 12,
    "max_rounds": 20,
    "triage": True,              # the judge sorts each gap batch before the interpreter answers it
    "max_edits": 0,              # edit proposals to the sources at the end of a run (0 = none)
    "language": "한국어",         # what the user reads: questions, options, reasons, the owner's report
}
_ID_RE = re.compile(r"^sim-[0-9a-f]{6,12}$")
_SUBMIT_CAP = 5000
_UNVERIFIED_REF = re.compile(r"^(issue|user|session|url|https?):", re.I)


class SimError(Exception):
    """A request the store refuses; the message is for the caller."""

    status = 400


class NotFound(SimError):
    status = 404


class Forbidden(SimError):
    status = 403


class Conflict(SimError):
    status = 409


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        atomic.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_json(path: Path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _append(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _read_lines(path: Path, limit: Optional[int] = None) -> List[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            rows = [json.loads(line) for line in f if line.strip()]
    except (OSError, ValueError):
        return []
    return rows[-limit:] if limit else rows


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def model_hash(m: dict) -> str:
    return hashlib.sha256(json.dumps(M.normalize(m), sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]


def check_sources(m: dict, root: str) -> List[str]:
    """Every ``source`` quote must appear in its file under ``root``
    (whitespace-insensitive). Refs like ``issue:<id>`` or ``url:...`` are
    taken as given -- the daemon cannot read them."""
    out: List[str] = []
    base = Path(root).resolve() if root else None
    cache: Dict[str, Optional[str]] = {}

    def text_of(ref: str) -> Optional[str]:
        if ref in cache:
            return cache[ref]
        path = ref.split("#", 1)[0]
        txt = None
        if base is not None:
            p = (base / path).resolve()
            if p == base or base in p.parents:
                try:
                    txt = _squash(p.read_text(encoding="utf-8", errors="replace"))
                except OSError:
                    txt = None
        cache[ref] = txt
        return txt

    for section in ("rules", "invariants"):
        for item in m.get(section) or []:
            if item.get("provisional") is not None:
                continue
            for i, s in enumerate(item.get("source") or []):
                ref, quote = str(s.get("ref") or ""), str(s.get("quote") or "")
                if _UNVERIFIED_REF.match(ref):
                    continue
                if base is None:
                    out.append(f"{section[:-1]} {item.get('id')}.source[{i}]: the simulation has no root to read {ref} from")
                    continue
                txt = text_of(ref)
                if txt is None:
                    out.append(f"{section[:-1]} {item.get('id')}.source[{i}]: {ref} is not a readable file under {root}")
                elif _squash(quote) not in txt:
                    out.append(f"{section[:-1]} {item.get('id')}.source[{i}]: the quote is not in {ref} "
                               "word for word -- quote the file, or mark the rule provisional with a why")
    return out


def _touched(m: dict, patch: dict) -> dict:
    """The rules and invariants of ``m`` that ``patch`` upserts."""
    out = {}
    for section in ("rules", "invariants"):
        ids = {str(x.get("id")) for x in patch.get(section) or [] if isinstance(x, dict)}
        out[section] = [x for x in m.get(section) or [] if x.get("id") in ids]
    return out


def source_digests(root: str, sources: Iterable[str]) -> Dict[str, str]:
    """sha256 (12 hex) of each file the simulation names, globs expanded,
    under ``root``. A question records these; when one changes, the
    question is looked at again."""
    out: Dict[str, str] = {}
    if not root:
        return out
    base = Path(root).resolve()
    for pat in sources:
        for p in sorted(base.glob(pat)) if any(c in pat for c in "*?[") else [base / pat]:
            try:
                p = p.resolve()
                if p.is_file() and (base in p.parents):
                    out[p.relative_to(base).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()[:12]
            except (OSError, ValueError):
                continue
    return out


class SimStore:
    def __init__(self, root: Path, live: Optional[Callable[[str], bool]] = None):
        """``live(session)`` says whether a session still runs; claims and
        transfers check it, and :meth:`sweep` releases owners it denies."""
        self.root = Path(root)
        self.live = live or (lambda name: True)
        self._lock = threading.RLock()

    # -- files ---------------------------------------------------------------

    def _dir(self, sim_id: str) -> Path:
        if not isinstance(sim_id, str) or not _ID_RE.match(sim_id):
            raise NotFound(f"no simulation {sim_id!r}")
        return self.root / sim_id

    def _meta(self, sim_id: str) -> dict:
        meta = _read_json(self._dir(sim_id) / "sim.json", None)
        if not meta:
            raise NotFound(f"no simulation {sim_id!r}")
        meta["settings"] = {**DEFAULT_SETTINGS, **(meta.get("settings") or {})}
        return meta

    def _save_meta(self, meta: dict) -> None:
        meta["updated_at"] = _now()
        _write_json(self._dir(meta["id"]) / "sim.json", meta)

    def _journal(self, sim_id: str, actor: str, event: str, **extra) -> None:
        _append(self._dir(sim_id) / "journal.jsonl", {"at": _now(), "by": actor, "event": event, **extra})

    def model(self, sim_id: str) -> dict:
        self._meta(sim_id)
        return M.normalize(_read_json(self._dir(sim_id) / "model.json", None))

    def findings(self, sim_id: str) -> Dict[str, dict]:
        return _read_json(self._dir(sim_id) / "findings.json", {})

    def rounds(self, sim_id: str, limit: Optional[int] = None) -> List[dict]:
        return _read_lines(self._dir(sim_id) / "rounds.jsonl", limit)

    def journal(self, sim_id: str, limit: int = 200) -> List[dict]:
        return _read_lines(self._dir(sim_id) / "journal.jsonl", limit)

    # -- authority -----------------------------------------------------------

    @staticmethod
    def _require_owner(meta: dict, actor: str, what: str) -> None:
        if actor == USER:
            return
        if not actor:
            raise Forbidden(f"{what}: no calling session")
        if (meta.get("runner") or {}).get("session") == actor:
            return  # the run session the daemon launched acts for the owner
        if meta.get("owner") != actor:
            who = meta.get("owner") or "nobody (claim it first)"
            raise Forbidden(f"{what}: {meta['id']} is owned by {who}; only the owner or the user may do this")

    # -- submissions (idempotent by key) -------------------------------------

    def _seen(self, sim_id: str, key: Optional[str]):
        if not key:
            return None
        return _read_json(self._dir(sim_id) / "submits.json", {}).get(key)

    def _remember(self, sim_id: str, key: Optional[str], result: dict) -> None:
        if not key:
            return
        path = self._dir(sim_id) / "submits.json"
        seen = _read_json(path, {})
        seen[key] = result
        if len(seen) > _SUBMIT_CAP:
            seen = dict(list(seen.items())[-_SUBMIT_CAP:])
        _write_json(path, seen)

    # -- views ---------------------------------------------------------------

    def summary(self, meta: dict) -> dict:
        f = self.findings(meta["id"])
        counts = {s: 0 for s in FINDING_STATUS}
        for x in f.values():
            counts[x.get("status", "open")] = counts.get(x.get("status", "open"), 0) + 1
        last = self.rounds(meta["id"], 1)
        props = _read_json(self._dir(meta["id"]) / "proposals.json", {})
        return {**meta, "counts": counts, "last_round": last[0] if last else None,
                "proposals": {st: sum(1 for p in props.values() if p["status"] == st)
                              for st in ("proposed", "accepted", "rejected")},
                "model_stats": M.stats(self.model(meta["id"]))}

    def get(self, sim_id: str) -> dict:
        with self._lock:
            return self.summary(self._meta(sim_id))

    def list(self, project: Optional[str] = None, owner: Optional[str] = None) -> List[dict]:
        with self._lock:
            out = []
            if not self.root.is_dir():
                return out
            for d in sorted(self.root.iterdir()):
                if not _ID_RE.match(d.name):
                    continue
                try:
                    meta = self._meta(d.name)
                except NotFound:
                    continue
                if project and meta.get("project") != project:
                    continue
                if owner is not None and (meta.get("owner") or "") != owner:
                    continue
                out.append(self.summary(meta))
            out.sort(key=lambda m: m.get("updated_at") or "", reverse=True)
            return out

    # -- lifecycle -----------------------------------------------------------

    def create(self, *, title: str, goal: str = "", root: str = "", project: str = "",
               owner: str = "", actor: str = USER, sources: Iterable[str] = (),
               settings: Optional[dict] = None, issue: str = "",
               defaults: Optional[dict] = None, from_sim: str = "") -> dict:
        """``from_sim`` starts from another simulation's model, verdicts and
        answered questions (its settings are not copied); the first start
        checks them against this one's root (see :meth:`_carry`)."""
        title = (title or "").strip()
        if not title:
            raise SimError("a simulation needs a title")
        if owner and owner != USER and not self.live(owner):
            raise SimError(f"session {owner!r} is not running; it cannot own a simulation")
        with self._lock:
            base_meta = self._meta(from_sim) if from_sim else None
            sim_id = "sim-" + secrets.token_hex(3)
            while (self.root / sim_id).exists():
                sim_id = "sim-" + secrets.token_hex(3)
            base = {**DEFAULT_SETTINGS, **(defaults or {})}
            meta = {
                "id": sim_id, "title": title[:200], "goal": (goal or "")[:20000],
                "root": root or "", "project": project or "",
                "sources": [str(s) for s in sources if str(s).strip()][:200],
                "owner": owner if owner != USER else "", "created_by": actor or USER,
                "created_at": _now(), "state": "idle", "state_reason": "created",
                "run_rounds": 0, "issue": issue or "", "runner": None,
                "settings": self._clean_settings(base, settings or {}),
            }
            if base_meta:
                meta["from"] = base_meta["id"]
                if not meta["sources"]:
                    meta["sources"] = list(base_meta.get("sources") or [])
            self._save_meta(meta)
            if base_meta:
                answered = {k: {**f, "first_round": 0, "last_round": 0}
                            for k, f in self.findings(base_meta["id"]).items() if f.get("status") == "answered"}
                _write_json(self._dir(sim_id) / "model.json", self.model(base_meta["id"]))
                _write_json(self._dir(sim_id) / "findings.json", answered)
                _write_json(self._dir(sim_id) / "verdicts.json", self.verdicts(base_meta["id"]))
            else:
                _write_json(self._dir(sim_id) / "model.json", M.empty())
                _write_json(self._dir(sim_id) / "findings.json", {})
            self._journal(sim_id, actor, "created", owner=meta["owner"], **({"from": from_sim} if from_sim else {}))
            return self.summary(meta)

    @staticmethod
    def _clean_settings(base: dict, change: dict) -> dict:
        out = dict(base)
        for k, v in change.items():
            if k not in DEFAULT_SETTINGS:
                raise SimError(f"unknown setting {k!r}")
            if isinstance(DEFAULT_SETTINGS[k], bool):
                if not isinstance(v, bool):
                    raise SimError(f"{k} must be true or false")
                out[k] = v
            elif k.endswith("_profile") or k.endswith("_model"):
                if not isinstance(v, str):
                    raise SimError(f"{k} must be a {'profile name' if k.endswith('_profile') else 'model id'}")
                out[k] = v.strip()
            elif k == "language":
                if not isinstance(v, str) or not v.strip() or len(v.strip()) > 40:
                    raise SimError("language must be a language name of at most 40 characters")
                out[k] = v.strip()
            elif k.endswith("_reasoning"):
                if v not in REASONING:
                    raise SimError(f"{k} must be one of {', '.join(REASONING)}")
                out[k] = v
            else:
                least = 0 if k == "max_edits" else 1
                if not isinstance(v, int) or isinstance(v, bool) or v < least:
                    raise SimError(f"{k} must be {'a non-negative' if least == 0 else 'a positive'} integer")
                limit = {"budget": 200000, "horizon": 200, "max_rounds": 500, "max_edits": 50}.get(k)
                out[k] = min(v, limit) if limit else v
        return out

    def configure(self, sim_id: str, actor: str, *, settings: Optional[dict] = None,
                  title: Optional[str] = None, goal: Optional[str] = None,
                  sources: Optional[List[str]] = None, issue: Optional[str] = None) -> dict:
        with self._lock:
            meta = self._meta(sim_id)
            self._require_owner(meta, actor, "configure")
            if settings:
                meta["settings"] = self._clean_settings(meta["settings"], settings)
            if title is not None and title.strip():
                meta["title"] = title.strip()[:200]
            if goal is not None:
                meta["goal"] = goal[:20000]
            if sources is not None:
                meta["sources"] = [str(s) for s in sources if str(s).strip()][:200]
            if issue is not None:
                meta["issue"] = issue
            self._save_meta(meta)
            self._journal(sim_id, actor, "configured", settings=settings or {})
            return self.summary(meta)

    def delete(self, sim_id: str, actor: str) -> None:
        with self._lock:
            meta = self._meta(sim_id)
            if actor != USER:
                raise Forbidden("only the user deletes a simulation")
            import shutil
            shutil.rmtree(self._dir(meta["id"]))

    # -- ownership -----------------------------------------------------------

    def transfer(self, sim_id: str, actor: str, to: str) -> dict:
        with self._lock:
            meta = self._meta(sim_id)
            if actor != USER:
                raise Forbidden("only the user transfers ownership; a session may claim an unowned simulation")
            if not to or not self.live(to):
                raise SimError(f"session {to!r} is not running")
            prev = meta.get("owner") or ""
            meta["owner"] = to
            self._save_meta(meta)
            self._journal(sim_id, actor, "transferred", owner=to, prev=prev)
            return self.summary(meta)

    def release(self, sim_id: str, actor: str, reason: str = "released") -> dict:
        with self._lock:
            meta = self._meta(sim_id)
            self._require_owner(meta, actor, "release")
            return self._release(meta, actor, reason)

    def _release(self, meta: dict, actor: str, reason: str) -> dict:
        prev = meta.get("owner") or ""
        meta["owner"] = ""
        if meta["state"] == "running":
            meta["state"], meta["state_reason"] = "stopped", f"owner gone: {reason}"
        self._save_meta(meta)
        self._journal(meta["id"], actor, "released", prev=prev, reason=reason)
        return self.summary(meta)

    def claim(self, sim_id: str, actor: str) -> dict:
        with self._lock:
            meta = self._meta(sim_id)
            if not actor or actor == USER:
                raise SimError("claim is for sessions; the user transfers")
            if meta.get("owner") == actor:
                return self.summary(meta)
            if meta.get("owner") and self.live(meta["owner"]):
                raise Conflict(f"{sim_id} is owned by {meta['owner']}, which is still running; "
                               "ask the user to transfer it")
            prev = meta.get("owner") or ""
            meta["owner"] = actor
            self._save_meta(meta)
            self._journal(sim_id, actor, "claimed", prev=prev)
            return self.summary(meta)

    def session_exited(self, name: str) -> List[str]:
        """Release every simulation ``name`` owned. Returns their ids."""
        out = []
        with self._lock:
            for meta in self._metas():
                if meta.get("owner") == name:
                    self._release(meta, "daemon", f"session {name} exited")
                    out.append(meta["id"])
        return out

    # -- run session ---------------------------------------------------------

    def attach_runner(self, sim_id: str, session: str) -> dict:
        """Record the run session the daemon launched for this campaign. It
        acts with the owner's authority while attached."""
        with self._lock:
            meta = self._meta(sim_id)
            meta["runner"] = {"session": session, "started_at": _now()}
            meta["runner_count"] = int(meta.get("runner_count") or 0) + 1
            self._save_meta(meta)
            self._journal(sim_id, "daemon", "runner", session=session)
            return self.summary(meta)

    def runner_of(self, sim_id: str) -> str:
        return ((self._meta(sim_id).get("runner") or {}).get("session")) or ""

    def runner_done(self, sim_id: str, actor: str, reason: str = "", failed: bool = False) -> dict:
        """The run session finished its loop (or gave up). A campaign still
        running ends here: ``stopped`` with the runner's reason. Returns the
        summary and ``ended``."""
        with self._lock:
            meta = self._meta(sim_id)
            runner = (meta.get("runner") or {}).get("session")
            if actor not in (USER, "daemon") and actor != runner:
                raise Forbidden(f"runner_done: {actor} is not the run session of {sim_id}")
            was = meta["state"]
            if was == "running":
                meta["state"] = "stopped"
                meta["state_reason"] = ("run session failed: " if failed else "run session finished: ") + (
                    reason or "no reason given")
            if runner:
                meta["last_runner"] = {**(meta.get("runner") or {}), "ended_at": _now(),
                                       "reason": reason, "failed": bool(failed)}
            meta["runner"] = None
            owed = bool(meta.pop("notice_owed", False))
            self._save_meta(meta)
            self._journal(sim_id, actor, "runner_done", reason=reason, failed=bool(failed))
            if was == "running":
                self._journal(sim_id, actor, "ended", state=meta["state"], reason=meta["state_reason"])
            # ``ended``: the owner is told now -- the campaign ends here, or it
            # ended in a round while this run session was attached.
            return {**self.summary(meta), "ended": was == "running" or owed}

    def runner_exited(self, name: str) -> List[dict]:
        """``name`` was a run session and its process is gone: detach it
        and end the campaign it drove. Returns the summaries that ended."""
        out = []
        with self._lock:
            for meta in self._metas():
                if (meta.get("runner") or {}).get("session") == name:
                    res = self.runner_done(meta["id"], "daemon", f"session {name} exited", failed=True)
                    if res["ended"]:
                        out.append(res)
        return out

    # -- judge verdicts --------------------------------------------------------

    VERDICTS = ("confirmed", "fabricated", "ambiguous")

    @staticmethod
    def rule_digest(item: dict) -> str:
        """What a verdict is about: the rule as written. A changed rule needs
        a new verdict."""
        body = {k: item.get(k) for k in ("on", "when", "do", "source", "priority")}
        return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]

    def verdicts(self, sim_id: str) -> Dict[str, dict]:
        return _read_json(self._dir(sim_id) / "verdicts.json", {})

    def unjudged(self, sim_id: str) -> List[dict]:
        """Sourced rules and invariants without a verdict on their current
        text (provisional ones have no quote to check)."""
        m = self.model(sim_id)
        seen = self.verdicts(sim_id)
        out = []
        for section in ("rules", "invariants"):
            for item in m.get(section) or []:
                if item.get("provisional") is not None or not item.get("source"):
                    continue
                v = seen.get(f"{section}:{item['id']}")
                if v and v.get("digest") == self.rule_digest(item):
                    continue
                out.append({"section": section, **item})
        return out

    def judge(self, sim_id: str, actor: str, items: List[dict], key: Optional[str] = None) -> dict:
        """Record the judge's verdicts: ``confirmed`` (the quote says it),
        ``fabricated`` (it does not: the rule is retracted from the model),
        ``ambiguous`` (kept, marked disputed). Each item is
        ``{section?, id, verdict, why}``."""
        with self._lock:
            meta = self._meta(sim_id)
            self._require_owner(meta, actor, "record verdicts")
            seen = self._seen(sim_id, key)
            if seen is not None:
                return {**seen, "duplicate": True}
            m = self.model(sim_id)
            ledger = self.verdicts(sim_id)
            done, errors, retract = [], [], {"rules": [], "invariants": []}
            for it in items or []:
                section = it.get("section") or "rules"
                rid = str(it.get("id") or "")
                verdict = it.get("verdict")
                if section not in retract:
                    errors.append(f"{rid}: section must be rules or invariants")
                    continue
                item = next((x for x in m.get(section) or [] if x.get("id") == rid), None)
                if item is None:
                    errors.append(f"no {section[:-1]} {rid!r}")
                    continue
                if verdict not in self.VERDICTS:
                    errors.append(f"{rid}: verdict must be one of {', '.join(self.VERDICTS)}")
                    continue
                ledger[f"{section}:{rid}"] = {"verdict": verdict, "why": str(it.get("why") or "")[:4000],
                                             "digest": self.rule_digest(item), "by": actor, "at": _now()}
                if verdict == "fabricated":
                    retract[section].append(rid)
                done.append(rid)
            if any(retract.values()):
                new = M.apply_patch(m, {"remove": {k: v for k, v in retract.items() if v}})
                _write_json(self._dir(sim_id) / "model.json", new)
                self._journal(sim_id, actor, "retracted", ids=[*retract["rules"], *retract["invariants"]])
            _write_json(self._dir(sim_id) / "verdicts.json", ledger)
            if done:
                self._journal(sim_id, actor, "verdicts", ids=done[:50])
            result = {"ok": not errors, "done": done, "errors": errors,
                      "retracted": [*retract["rules"], *retract["invariants"]]}
            self._remember(sim_id, key, result)
            return result

    # -- edit proposals (the editor, D) ------------------------------------------

    EDIT_KINDS = ("clarify", "complete", "decide")

    def proposals(self, sim_id: str) -> Dict[str, dict]:
        return _read_json(self._dir(sim_id) / "proposals.json", {})

    def edit_targets(self, sim_id: str) -> List[dict]:
        """Findings an edit to the sources would settle: questions (the
        sources are silent) and answers (the user decided; the sources do
        not say so yet), less those a proposal already covers."""
        covered = {k for p in self.proposals(sim_id).values() if p["status"] != "rejected" for k in p["findings"]}
        return [f for f in self.findings(sim_id).values()
                if f.get("status") in ("question", "answered") and f["key"] not in covered]

    def propose(self, sim_id: str, actor: str, items: List[dict], key: Optional[str] = None) -> dict:
        """Record edit proposals to the source files. The daemon never
        applies one: it checks that ``old`` is in ``file`` word for word
        (empty ``old`` appends), applies it to a copy, and lists the quoted
        rules that would lose their quote (``check.breaks``). The user
        accepts or rejects; an accepted one goes to the owner to apply."""
        with self._lock:
            meta = self._meta(sim_id)
            self._require_owner(meta, actor, "propose edits")
            seen = self._seen(sim_id, key)
            if seen is not None:
                return {**seen, "duplicate": True}
            root = meta.get("root") or ""
            base = Path(root).resolve() if root else None
            ledger, m = self.findings(sim_id), self.model(sim_id)
            stored = self.proposals(sim_id)
            done, errors = [], []
            for it in items or []:
                rel = str(it.get("file") or "")
                old, new = str(it.get("old") or ""), str(it.get("new") or "")
                kind, reason = it.get("kind"), str(it.get("reason") or "").strip()
                refs = [str(k) for k in it.get("findings") or []]
                where = f"edit to {rel or '?'}"
                if base is None:
                    errors.append(f"{where}: the simulation has no root")
                    continue
                path = (base / rel).resolve()
                if not rel or base not in path.parents:
                    errors.append(f"{where}: not a file under the root")
                    continue
                try:
                    text = path.read_text(encoding="utf-8")
                except OSError:
                    errors.append(f"{where}: not a readable file")
                    continue
                if kind not in self.EDIT_KINDS:
                    errors.append(f"{where}: kind must be one of {', '.join(self.EDIT_KINDS)}")
                    continue
                if not reason or not new.strip() or new == old:
                    errors.append(f"{where}: needs a reason and a changed text")
                    continue
                if old and old not in text:
                    errors.append(f"{where}: old is not in the file word for word")
                    continue
                missing = [k for k in refs if k not in ledger]
                if not refs or missing:
                    errors.append(f"{where}: must name the findings it settles" + (f" (unknown: {missing})" if missing else ""))
                    continue
                after = text.replace(old, new, 1) if old else text.rstrip("\n") + "\n" + new.rstrip("\n") + "\n"
                breaks = []
                for section in ("rules", "invariants"):
                    for item in m.get(section) or []:
                        if item.get("provisional") is not None:
                            continue
                        for s_ in item.get("source") or []:
                            if str(s_.get("ref") or "").split("#", 1)[0] == rel and \
                                    _squash(str(s_.get("quote") or "")) not in _squash(after):
                                breaks.append(f"{section[:-1]} {item['id']}")
                pid = "e-" + hashlib.sha256(f"{rel}\0{old}\0{new}".encode()).hexdigest()[:8]
                stored[pid] = {"id": pid, "file": rel, "old": old, "new": new, "kind": kind, "reason": reason[:4000],
                               "findings": refs, "by": actor, "at": _now(), "status": "proposed",
                               "base": hashlib.sha256(text.encode()).hexdigest()[:12],
                               "check": {"breaks": sorted(set(breaks))}}
                done.append(pid)
            _write_json(self._dir(sim_id) / "proposals.json", stored)
            if done:
                self._journal(sim_id, actor, "proposed", ids=done)
            result = {"ok": not errors, "done": done, "errors": errors}
            self._remember(sim_id, key, result)
            return result

    def decide_proposal(self, sim_id: str, actor: str, pid: str, accept: bool, note: str = "") -> dict:
        with self._lock:
            meta = self._meta(sim_id)
            if actor != USER:
                raise Forbidden("edit proposals are accepted or rejected by the user")
            stored = self.proposals(sim_id)
            p = stored.get(pid)
            if p is None:
                raise NotFound(f"no edit proposal {pid!r}")
            p["status"] = "accepted" if accept else "rejected"
            p["decision"] = {"by": actor, "at": _now(), "note": note[:2000]}
            _write_json(self._dir(sim_id) / "proposals.json", stored)
            self._journal(sim_id, actor, "proposal_" + p["status"], id=pid)
            return {"proposal": p, "owner": meta.get("owner") or ""}

    TRIAGE = ("model", "spec", "intended")

    def triage(self, sim_id: str, actor: str, items: List[dict], key: Optional[str] = None) -> dict:
        """The judge's sort of open findings, before the interpreter answers
        them: ``model`` (the model misreads the sources: fix the model),
        ``spec`` (the sources are silent: a person decides, unless a quote
        turns up), ``intended`` (not a defect: dismissed with the judge's
        reason). Each item is ``{key, verdict, why}``."""
        with self._lock:
            meta = self._meta(sim_id)
            self._require_owner(meta, actor, "triage findings")
            seen = self._seen(sim_id, key)
            if seen is not None:
                return {**seen, "duplicate": True}
            ledger = self.findings(sim_id)
            last = self.rounds(sim_id, 1)
            n = last[0]["round"] if last else 0
            done, errors = [], []
            for it in items or []:
                f = ledger.get(it.get("key"))
                verdict, why = it.get("verdict"), str(it.get("why") or "").strip()[:4000]
                if f is None or f.get("status") != "open":
                    errors.append(f"{it.get('key')!r} is not an open finding")
                    continue
                if verdict not in self.TRIAGE:
                    errors.append(f"{f['key']}: verdict must be one of {', '.join(self.TRIAGE)}")
                    continue
                if not why:
                    errors.append(f"{f['key']}: a triage verdict needs a why")
                    continue
                f["triage"] = {"verdict": verdict, "why": why, "by": actor, "at": _now(), "round": n}
                if verdict == "intended":
                    f["status"] = "dismissed"
                    f["resolution"] = {"by": actor, "at": _now(), "reason": "triage: " + why}
                done.append(f["key"])
            _write_json(self._dir(sim_id) / "findings.json", ledger)
            if done:
                self._journal(sim_id, actor, "triage", keys=done[:50])
            result = {"ok": not errors, "done": done, "errors": errors}
            self._remember(sim_id, key, result)
            return result

    def work(self, sim_id: str, gaps: int = 4) -> dict:
        """Everything a run session reads to take its next step."""
        with self._lock:
            sim = self.get(sim_id)
            answered = [f for f in self.findings(sim_id).values()
                        if f.get("status") == "answered" and not f.get("applied")]
            m = self.model(sim_id)
            # Rules carried as stale, less those a later patch re-quoted or removed.
            live = {(sec, x["id"]): x for sec in ("rules", "invariants") for x in m.get(sec) or []}
            stale = []
            for s in sim.get("stale_sources") or []:
                item = live.get((s["section"], s["id"]))
                if item and item.get("provisional") is None and \
                        check_sources({s["section"]: [item]}, sim.get("root") or ""):
                    stale.append({"section": s["section"], **item})
            return {"sim": sim, "model": m, "stale": stale, "edit_targets": self.edit_targets(sim_id),
                    "gaps": self.gap_batches(sim_id, gaps)
                    if sim["counts"].get("open") else [], "answered": answered,
                    "unjudged": self.unjudged(sim_id)}

    def sweep(self) -> List[str]:
        """Release owners that are no longer running (daemon start)."""
        out = []
        with self._lock:
            for meta in self._metas():
                owner = meta.get("owner")
                if owner and not self.live(owner):
                    self._release(meta, "daemon", f"session {owner} is gone")
                    out.append(meta["id"])
                runner = (meta.get("runner") or {}).get("session")
                if runner and not self.live(runner):
                    self.runner_done(meta["id"], "daemon", f"run session {runner} is gone", failed=True)
                    if meta["id"] not in out:
                        out.append(meta["id"])
        return out

    def _metas(self) -> List[dict]:
        if not self.root.is_dir():
            return []
        out = []
        for d in sorted(self.root.iterdir()):
            if _ID_RE.match(d.name):
                try:
                    out.append(self._meta(d.name))
                except NotFound:
                    pass
        return out

    # -- model ---------------------------------------------------------------

    def patch_model(self, sim_id: str, actor: str, patch: dict, key: Optional[str] = None) -> dict:
        """Apply a patch. Rejected as a whole when the result fails the shape
        check or a quote is not in its file. Idempotent by ``key``."""
        with self._lock:
            meta = self._meta(sim_id)
            self._require_owner(meta, actor, "patch the model")
            seen = self._seen(sim_id, key)
            if seen is not None:
                return {**seen, "duplicate": True}
            try:
                new = M.apply_patch(self.model(sim_id), patch)
            except ValueError as exc:
                raise SimError(str(exc))
            # Quotes are checked for what this patch writes: a rule whose file
            # changed since is carried as stale (see carry), and must not block
            # every other change until it is re-quoted.
            problems = M.check_model(new) or check_sources(_touched(new, patch), meta.get("root") or "")
            if problems:
                return {"ok": False, "rejected": problems[:50], "count": len(problems)}
            _write_json(self._dir(sim_id) / "model.json", new)
            self._journal(sim_id, actor, "model", hash=model_hash(new),
                          changed={k: len(v) for k, v in patch.items() if isinstance(v, (list, dict))})
            result = {"ok": True, "hash": model_hash(new), "stats": M.stats(new)}
            applied = self._mark_applied(sim_id, patch)
            if applied:
                result["applied"] = applied
            self._remember(sim_id, key, result)
            return result

    def _mark_applied(self, sim_id: str, patch: dict) -> List[str]:
        """An answered question whose finding a patch cites (``user:<key>``)
        has been put into the model; the run session stops offering it."""
        refs = set()
        for section in ("rules", "invariants"):
            for item in patch.get(section) or []:
                for s in item.get("source") or []:
                    ref = str(s.get("ref") or "")
                    if ref.startswith("user:"):
                        refs.add(ref[5:])
        if not refs:
            return []
        ledger = self.findings(sim_id)
        out = [k for k in refs if (ledger.get(k) or {}).get("status") == "answered"]
        for k in out:
            ledger[k]["applied"] = _now()
        if out:
            _write_json(self._dir(sim_id) / "findings.json", ledger)
        return out

    # -- rounds --------------------------------------------------------------

    def start(self, sim_id: str, actor: str, round: bool = True) -> dict:
        """Start a campaign. ``round=False`` leaves the first round to the
        run session, which formalizes the sources first."""
        with self._lock:
            meta = self._meta(sim_id)
            self._require_owner(meta, actor, "start")
            carried = self._carry(meta, actor)
            meta = self._meta(sim_id)
            meta["state"], meta["state_reason"], meta["run_rounds"] = "running", f"started by {actor}", 0
            self._save_meta(meta)
            self._journal(sim_id, actor, "started")
            if not round:
                return {**self.summary(meta), "carry": carried}
        return {**self.run_round(sim_id, actor), "carry": carried}

    def _carry(self, meta: dict, actor: str) -> dict:
        """What a new run takes over from the last one, checked against the
        sources and model as they are now:

        * rules and invariants whose quote is gone from its file are listed
          in ``meta.stale_sources``; the run session re-quotes or retracts
          them (the walk still uses them meanwhile);
        * a question whose witness the current model no longer reproduces
          is resolved; one whose sources changed since it was asked goes
          back to open, its question kept as ``prev_question``;
        * a finding whose witness no longer replays is marked ``stale``.
        """
        sim_id = meta["id"]
        m = self.model(sim_id)
        root = meta.get("root") or ""
        stale = []
        for section in ("rules", "invariants"):
            for item in m.get(section) or []:
                if item.get("provisional") is None and item.get("source") and \
                        check_sources({section: [item]}, root):
                    stale.append({"section": section, "id": item["id"]})
        meta["stale_sources"] = stale
        self._save_meta(meta)
        ledger = self.findings(sim_id)
        walk = engine.prepare(m) if not M.check_model(m) else None
        docs_now = source_digests(root, meta.get("sources") or [])
        resolved, reopened, marked = [], [], []
        for key, f in ledger.items():
            if f.get("status") not in ("open", "question"):
                continue
            if f["status"] == "question":
                then = (f.get("question") or {}).get("docs") or {}
                if then and then != {k: docs_now.get(k) for k in then}:
                    f["prev_question"] = f.pop("question")
                    f["status"] = "open"
                    f["reopened"] = {"at": _now(), "why": "the sources changed since the question was asked"}
                    reopened.append(key)
                    continue
            if walk is None or not f.get("witness"):
                continue
            try:
                engine.replay(walk, f["witness"])
            except (engine.StaleWitness, ValueError, KeyError) as exc:
                f["stale"] = str(exc)
                marked.append(key)
                continue
            f.pop("stale", None)
            if f["status"] == "question" and not engine.reproduces(walk, f["witness"], key):
                f["status"] = "resolved"
                f["resolution"] = {"by": "carry", "at": _now(),
                                   "reason": "the current model no longer reaches it"}
                resolved.append(key)
        _write_json(self._dir(sim_id) / "findings.json", ledger)
        out = {"stale_sources": stale, "resolved": resolved, "reopened": reopened, "stale_witness": marked}
        if any(out.values()):
            self._journal(sim_id, actor, "carry", **{k: v[:50] for k, v in out.items()})
        return out

    def stop(self, sim_id: str, actor: str, reason: str = "") -> dict:
        with self._lock:
            meta = self._meta(sim_id)
            self._require_owner(meta, actor, "stop")
            was = meta["state"]
            if was == "running":
                meta["state"], meta["state_reason"] = "stopped", reason or f"stopped by {actor}"
                self._save_meta(meta)
                self._journal(sim_id, actor, "stopped", reason=reason)
            return {**self.summary(meta), "ended": was == "running"}

    def run_round(self, sim_id: str, actor: str) -> dict:
        """Walk the model from the start state and every checkpoint, update
        the ledger, and return the round summary."""
        with self._lock:
            meta = self._meta(sim_id)
            self._require_owner(meta, actor, "run a round")
            stored = self.model(sim_id)
            problems = M.check_model(stored)
            if problems:
                raise SimError("the model does not check: " + "; ".join(problems[:5]))
            walk = engine.prepare(stored)
            st = meta["settings"]
            found: Dict[str, dict] = {}
            steps = cells = assumptions = 0
            tried: set = set()
            fired: Dict[str, int] = {}
            for cp in [None, *sorted(walk["checkpoints"])]:
                r = engine.explore(walk, seed=st["seed"], budget=st["budget"], horizon=st["horizon"], checkpoint=cp)
                steps += r["steps"]
                cells += r["cells"]
                assumptions += r["assumptions"]
                tried.update(r["tried"])
                for k, v in r["fired"].items():
                    fired[k] = fired.get(k, 0) + v
                for p in r["problems"]:
                    key = p["finding"]["key"]
                    if key in found:
                        found[key]["count"] += p["count"]
                    else:
                        found[key] = p
            cov = engine.coverage(walk, sorted(tried))
            n = len(self.rounds(sim_id)) + 1
            ledger = self.findings(sim_id)
            new = reopened = resolved = 0
            for key, p in found.items():
                f = ledger.get(key)
                base = {**p["finding"], "witness": p["witness"], "count": p["count"],
                        "assumed": p.get("assumed") or [], "provisional": p.get("provisional") or [],
                        "last_round": n}
                if f is None:
                    ledger[key] = {**base, "status": "open", "first_round": n}
                    new += 1
                else:
                    if f.get("status") == "resolved":
                        f["status"] = "open"
                        f.pop("resolution", None)
                        reopened += 1
                    f.update(base)
            for key, f in ledger.items():
                # An answered question closes too once the model handles it.
                if key not in found and f.get("status") in ("open", "answered"):
                    f["status"] = "resolved"
                    f["resolution"] = {"by": "walk", "round": n, "at": _now(),
                                       "reason": "the walk no longer reaches it"}
                    resolved += 1
            _write_json(self._dir(sim_id) / "findings.json", ledger)
            open_now = sum(1 for f in ledger.values() if f.get("status") == "open")
            summary = {
                "round": n, "at": _now(), "by": actor, "model": model_hash(stored),
                "steps": steps, "cells": cells, "assumptions": assumptions,
                "coverage": {"cells": cov["cells"], "reached": cov["reached"]},
                "found": len(found), "new": new, "reopened": reopened, "resolved": resolved,
                "open": open_now, "fired_rules": len(fired),
                "knobs": {k: st[k] for k in ("seed", "budget", "horizon")},
            }
            _append(self._dir(sim_id) / "rounds.jsonl", summary)
            if meta["state"] == "running":
                meta["run_rounds"] = int(meta.get("run_rounds") or 0) + 1
                if open_now == 0:
                    meta["state"], meta["state_reason"] = "idle", f"round {n} left nothing open"
                elif meta["run_rounds"] >= st["max_rounds"]:
                    meta["state"], meta["state_reason"] = "stopped", f"max_rounds ({st['max_rounds']}) reached"
                if meta["state"] != "running":
                    # The campaign ended in this round: the API tells the owner --
                    # unless a run session is attached, which still has work after
                    # the last round (edit proposals); then the notice waits for
                    # runner_done or the run session's exit.
                    summary["ended"] = {"state": meta["state"], "reason": meta["state_reason"]}
                    self._journal(sim_id, actor, "ended", **summary["ended"])
                    if (meta.get("runner") or {}).get("session"):
                        meta["notice_owed"] = True
                        summary["ended"]["deferred"] = True
            self._save_meta(meta)
            self._journal(sim_id, actor, "round", round=n, new=new, open=open_now)
            return summary

    def coverage(self, sim_id: str) -> dict:
        with self._lock:
            walk = engine.prepare(self.model(sim_id))
            ledger = self.findings(sim_id)
            reached = set()
            for f in ledger.values():
                if f.get("kind") == "unhandled" and f.get("machine"):
                    reached.add(f"{f['machine']}\x00{f['state']}\x00{f['event']}")
            cov = engine.coverage(walk, [])
            return {"grid": cov["grid"], "handled": cov["handled_cells"],
                    "gaps": {k: (ledger.get("unhandled|" + k.replace(chr(0), "|")) or {}).get("status", "open")
                             for k in sorted(reached)}}

    # -- findings ------------------------------------------------------------

    def list_findings(self, sim_id: str, status: Optional[str] = None, kind: Optional[str] = None,
                      limit: int = 100) -> dict:
        with self._lock:
            self._meta(sim_id)
            rows = list(self.findings(sim_id).values())
            if status:
                want = set(status.split(","))
                rows = [r for r in rows if r.get("status") in want]
            if kind:
                rows = [r for r in rows if r.get("kind") == kind]
            rows.sort(key=lambda r: (r.get("machine") or "~", r.get("state") or "", -(r.get("count") or 0), r["key"]))
            return {"total": len(rows), "findings": rows[:max(1, min(limit, 1000))]}

    def gap_batches(self, sim_id: str, limit: int = 8) -> List[dict]:
        """Open findings grouped by place (machine and state), the unit a
        reader interprets in one go."""
        rows = self.list_findings(sim_id, "open", limit=1000)["findings"]
        groups: Dict[str, dict] = {}
        for r in rows:
            place = f"{r.get('machine') or '-'}/{r.get('state') or '-'}" if r.get("kind") == "unhandled" else r["kind"]
            g = groups.setdefault(place, {"place": place, "findings": []})
            g["findings"].append(r)
        out = sorted(groups.values(), key=lambda g: -len(g["findings"]))
        return out[:max(1, limit)]

    def resolve(self, sim_id: str, actor: str, items: List[dict], key: Optional[str] = None) -> dict:
        """Close findings by hand. Each item is ``{key, action, ...}``:
        ``dismiss`` (``reason``: why this is intended or impossible) or
        ``ask`` (``question``, optional ``options``: a human must decide)."""
        with self._lock:
            meta = self._meta(sim_id)
            self._require_owner(meta, actor, "resolve findings")
            seen = self._seen(sim_id, key)
            if seen is not None:
                return {**seen, "duplicate": True}
            ledger = self.findings(sim_id)
            done, errors = [], []
            for it in items or []:
                f = ledger.get(it.get("key"))
                if f is None:
                    errors.append(f"no finding {it.get('key')!r}")
                    continue
                action = it.get("action")
                if action == "dismiss":
                    reason = str(it.get("reason") or "").strip()
                    if not reason:
                        errors.append(f"{f['key']}: dismiss needs a reason")
                        continue
                    f["status"] = "dismissed"
                    f["resolution"] = {"by": actor, "at": _now(), "reason": reason}
                elif action == "ask":
                    q = str(it.get("question") or "").strip()
                    if not q:
                        errors.append(f"{f['key']}: ask needs a question")
                        continue
                    opts = [str(o) for o in (it.get("options") or []) if str(o).strip()][:8]
                    f["status"] = "question"
                    f["question"] = {"text": q, "options": opts, "by": actor, "at": _now(),
                                     "docs": source_digests(meta.get("root") or "", meta.get("sources") or [])}
                    f.pop("answer", None)
                elif action == "reopen":
                    f["status"] = "open"
                    f.pop("resolution", None)
                else:
                    errors.append(f"{f['key']}: action must be dismiss, ask or reopen")
                    continue
                done.append(f["key"])
            _write_json(self._dir(sim_id) / "findings.json", ledger)
            if done:
                self._journal(sim_id, actor, "resolve", keys=done[:50])
            result = {"ok": not errors, "done": done, "errors": errors}
            self._remember(sim_id, key, result)
            return result

    def answer(self, sim_id: str, actor: str, finding_key: str, text: str) -> dict:
        """The user's answer to a question. The finding becomes ``answered``;
        whoever owns the simulation turns it into rules."""
        with self._lock:
            meta = self._meta(sim_id)
            if actor != USER:
                raise Forbidden("questions are answered by the user")
            ledger = self.findings(sim_id)
            f = ledger.get(finding_key)
            if f is None or f.get("status") not in ("question", "answered"):
                raise SimError(f"{finding_key!r} is not a question")
            text = (text or "").strip()
            if not text:
                raise SimError("an answer needs text")
            f["status"] = "answered"
            f["answer"] = {"text": text[:8000], "by": actor, "at": _now()}
            _write_json(self._dir(sim_id) / "findings.json", ledger)
            self._journal(sim_id, actor, "answered", key=finding_key)
            return {"ok": True, "finding": f, "owner": meta.get("owner") or ""}

    def replay(self, sim_id: str, finding_key: str) -> dict:
        with self._lock:
            f = self.findings(sim_id).get(finding_key)
            if f is None:
                raise NotFound(f"no finding {finding_key!r}")
            walk = engine.prepare(self.model(sim_id))
            w = f["witness"]
            try:
                r = engine.replay(walk, w)
            except engine.StaleWitness as exc:
                return {"stale": True, "why": str(exc), "finding": f}
            except (ValueError, KeyError) as exc:
                return {"stale": True, "why": f"cannot replay on the current model: {exc}", "finding": f}
            start = M.initial_state(walk, w.get("checkpoint"), w.get("world_seed", 1))
            start["vars"].update(w.get("inputs") or {})
            prev = {"sm": dict(start["sm"]), "vars": dict(start["vars"])}
            steps = []
            for i, (t, s) in enumerate(zip(w.get("steps") or [], r["steps"])):
                changed = {f"{m}": [prev["sm"].get(m), v] for m, v in s["sm"].items() if prev["sm"].get(m) != v}
                changed.update({f"${k}": [prev["vars"].get(k), v] for k, v in s["vars"].items()
                                if prev["vars"].get(k) != v})
                steps.append({"n": i + 1, "event": t["event"], "assume": t.get("assume") or {},
                              "fired": s.get("fired") or [], "unresolved": s.get("unresolved") or [],
                              "findings": [x["key"] for x in s.get("findings") or []],
                              "changed": changed, "sm": s["sm"], "vars": s["vars"]})
                prev = {"sm": s["sm"], "vars": s["vars"]}
            raised = next((x["n"] for x in steps if finding_key in x["findings"]), None)
            return {"stale": False, "finding": f, "start": {"sm": start["sm"], "vars": start["vars"]},
                    "checkpoint": w.get("checkpoint"), "steps": steps, "fired": r["fired"],
                    "raised_at": raised, "reproduces": engine.reproduces(walk, w, finding_key)}
