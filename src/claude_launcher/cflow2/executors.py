"""Delegation executors for cflow v2 (``docs/cflow-v2.md`` sections 6.4, 7).

The runtime decides *which* executor a delegation point gets (``caps.plan_node``)
and owns the delegation record; this module is the hook through which a
``headless`` or ``session`` delegation is actually started.

- ``inline``   -- no dispatch: the driver runs the subtree itself.
- ``subagent`` -- no dispatch: the driver reads the brief from ``flow_status``
  / ``flow_delegate``, runs it with its harness's sub-agent feature and
  submits the result with ``flow_delegate``.
- ``headless`` / ``session`` -- need a process the daemon starts. The daemon
  registers a dispatcher with :func:`register`; until one is registered
  (stage 3/4 of the plan, or a run driven without a daemon) :class:`InlineFallback`
  turns the delegation into the next executor the node's preference list
  allows that does not need a dispatcher, and the runtime journals that as a
  ``fallback`` event.

A dispatcher is ``fn(delegation: dict, run) -> str | None``: it starts the work
asynchronously and returns the delegation's new status (``queued`` or
``running``). The result comes back through ``runtime.delegate_submit``.

**Dispatch availability across processes.** The runtime usually runs in the
agent's MCP server or the CLI, not in the daemon, so an in-process
:func:`register` is not what decides whether a ``headless``/``session``
delegation can be served. The daemon's cflow2 clock
(``daemon/cflow2_clock.py``) writes a heartbeat file every tick
(:func:`heartbeat_path`: ``<daemon dir>/cflow2_clock.json``) listing the run
directories it scans. :func:`available` is true when a dispatcher is
registered in this process, or when that heartbeat is fresh and names the
run's directory; the delegation is then left ``queued`` and the clock picks
it up on its next tick. A stale or missing heartbeat (no daemon, the clock
switched off, or a directory no live session stands in) keeps the old
behaviour: the runtime falls back at once and journals why.

The second half of this module is the harness-independent part of the
headless executor: the JSON schema handed to a schema flag, the prompt, the
argv built from capability ``args`` (:func:`build_argv`), and result
extraction from each return route (:func:`extract_result`). The process
itself is the daemon's (``daemon/cflow2_exec.py``).
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from . import ir

Dispatcher = Callable[[dict, Any], Optional[str]]

#: Executors that need a dispatcher (a process someone else starts).
DISPATCHED = (ir.EXEC_HEADLESS, ir.EXEC_SESSION)

_dispatcher: Optional[Dispatcher] = None


def register(fn: Optional[Dispatcher]) -> None:
    """Install (or with ``None`` remove) the process-wide dispatcher."""
    global _dispatcher
    _dispatcher = fn


def registered() -> bool:
    return _dispatcher is not None


HEARTBEAT_FILE = "cflow2_clock.json"
#: A heartbeat older than this (or than three of its own poll intervals,
#: whichever is longer) means no clock is serving.
HEARTBEAT_STALE = 90.0


def heartbeat_path() -> Path:
    from ..daemon import paths

    return paths.daemon_dir() / HEARTBEAT_FILE


def write_heartbeat(cwds: List[str], poll: float, path: Optional[Path] = None) -> None:
    from .. import atomic

    path = path or heartbeat_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {"pid": os.getpid(), "ts": time.time(), "poll": float(poll),
           "cwds": sorted({str(c) for c in cwds})}
    with atomic.scratch(path) as tmp:
        tmp.write_text(json.dumps(doc), encoding="utf-8")
        atomic.replace(tmp, path)


def clear_heartbeat(path: Optional[Path] = None) -> None:
    try:
        (path or heartbeat_path()).unlink()
    except OSError:
        pass


def read_heartbeat(path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    try:
        doc = json.loads((path or heartbeat_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def available(cwd: Optional[str] = None, now: Optional[float] = None) -> bool:
    """Whether a queued ``headless``/``session`` delegation in ``cwd`` will
    be served (see the module docstring)."""
    if _dispatcher is not None:
        return True
    try:
        doc = read_heartbeat()
    except Exception:  # noqa: BLE001 - no daemon paths: nothing serves
        return False
    if not doc:
        return False
    try:
        age = (time.time() if now is None else now) - float(doc.get("ts") or 0)
        limit = max(HEARTBEAT_STALE, 3 * float(doc.get("poll") or 0))
    except (TypeError, ValueError):
        return False
    if age > limit:
        return False
    if cwd is None:
        return True
    try:
        want = str(Path(cwd).resolve())
    except OSError:
        want = str(cwd)
    return want in set(doc.get("cwds") or [])


def dispatch(delegation: dict, run: Any) -> bool:
    """Hand a ``headless``/``session`` delegation to the registered dispatcher.

    Returns ``False`` when nothing is registered in this process (the
    delegation stays ``queued`` for the daemon's clock, which the caller
    checked with :func:`available`); otherwise updates
    ``delegation['status']`` and returns ``True``.
    """
    if _dispatcher is None:
        return False
    status = _dispatcher(delegation, run)
    delegation["status"] = status or "queued"
    return True


class InlineFallback:
    """What a dispatched executor becomes when no dispatcher is registered."""

    @staticmethod
    def next_executor(node: ir.Node, current: str, caps: Mapping[str, Any]) -> str:
        """The first executor after ``current`` in the node's preference list
        that does not need a dispatcher and whose capabilities hold; ``inline``
        when none does."""
        from . import caps as caps_mod

        prefs: List[str] = list(node.executor or [])
        rest = prefs[prefs.index(current) + 1:] if current in prefs else []
        rest = [ex for ex in rest if ex not in DISPATCHED]
        return caps_mod.choose_executor(rest, caps, needs=node.needs or []).executor


# ---------------------------------------------------------------------------
# headless: schema, prompt, argv, result extraction

#: Tools a read-only headless run may use, per harness, for the
#: ``headless.tools.restrict`` route (an allow-list without any write tool).
READONLY_TOOLS: Dict[str, List[str]] = {
    "claude": ["Read", "Grep", "Glob", "LS", "WebFetch", "WebSearch", "TodoWrite"],
    "pi": ["read", "grep", "find", "ls"],
}

#: Above this many characters the prompt goes to stdin instead of argv.
ARGV_PROMPT_MAX = 24000

_JSON_TYPES = {"str": "string", "int": "integer", "float": "number", "bool": "boolean",
               "list": "array", "dict": "object"}


def json_schema(returns: Mapping[str, Any]) -> Dict[str, Any]:
    """A JSON schema for ``{"outcome": ..., "value": {...}}`` from a
    ``returns`` map (``{outcome: {field: type}}``).

    Written for the strictest consumer (OpenAI structured outputs, which
    codex's ``--output-schema`` uses): every property is listed in
    ``required`` and ``additionalProperties`` is false, so a field one
    outcome does not have, or that is optional, is nullable instead.
    """
    outcomes = list(returns or {ir.OUTCOME_OK: {}})
    if ir.OUTCOME_FAIL not in outcomes:
        outcomes.append(ir.OUTCOME_FAIL)
    fields: Dict[str, str] = {}
    for spec in (returns or {}).values():
        for name, ftype in (spec or {}).items():
            fields.setdefault(str(name), str(ftype))
    props: Dict[str, Any] = {}
    for name, ftype in fields.items():
        base = ftype.rstrip("?")
        jt = _JSON_TYPES.get(base)
        if jt is None:
            props[name] = {}
            continue
        prop: Dict[str, Any] = {"type": [jt, "null"]}
        if jt == "array":
            prop["items"] = {}
        props[name] = prop
    value = {"type": "object", "properties": props, "required": list(props),
             "additionalProperties": False}
    return {
        "type": "object",
        "properties": {"outcome": {"type": "string", "enum": outcomes}, "value": value},
        "required": ["outcome", "value"],
        "additionalProperties": False,
    }


def return_instruction(returns: Mapping[str, Any]) -> str:
    return (
        "When you are done, end your final answer with exactly one fenced block "
        "```json\n{\"outcome\": \"<outcome>\", \"value\": {...}}\n```\n"
        "where <outcome> is one of the declared outcomes (or \"fail\") and value "
        "carries that outcome's fields with the declared types (a type ending in "
        f"'?' is optional). Declared returns: {json.dumps(dict(returns or {}))}"
    )


def build_prompt(brief: Mapping[str, Any], *, errors: Optional[List[str]] = None,
                 readonly: bool = False, extra: str = "",
                 result: Optional[str] = None) -> str:
    """The text a delegate gets: the brief's tasks in order, the rules, the
    return format; and on a re-run, the previous attempt's problems."""
    lines: List[str] = [
        f"You are running delegation {brief.get('delegation')} of a cflow workflow "
        f"(node {brief.get('label') or brief.get('node')}). Work only on what is "
        "below; you have no other context from the main run.",
    ]
    if brief.get("with"):
        lines.append("Inputs: " + json.dumps(brief["with"], ensure_ascii=False))
    for i, task in enumerate(brief.get("tasks") or [], 1):
        title = task.get("title") or task.get("label") or task.get("node")
        lines.append(f"\n## Task {i}: {title}\n{task.get('prompt') or ''}")
        if task.get("done_when"):
            lines.append(f"Done when: {task['done_when']}")
        if task.get("verify"):
            lines.append(f"Verify: {task['verify']}")
    rules = [r for r in brief.get("rules") or [] if r.get("text")]
    if rules:
        lines.append("\n## Rules")
        lines.extend(f"- {r['id']}: {r['text']}" for r in rules)
    if readonly:
        lines.append("\nThis delegation is READ-ONLY: do not modify, create or delete "
                     "files, and do not commit.")
    if extra:
        lines.append("\n" + extra)
    lines.append("\n## Result\n" + (result or return_instruction(brief.get("returns") or {})))
    if errors:
        lines.append("\n## Previous attempt was refused\nFix these problems: "
                     + "; ".join(errors))
    return "\n".join(lines)


def _cap(caps: Mapping[str, Any], cap_id: str) -> Any:
    from . import caps as caps_mod

    value = caps.get(cap_id) if caps else None
    if isinstance(value, caps_mod.Capability):
        return value
    if isinstance(value, dict):
        return caps_mod.Capability(enabled=bool(value.get("enabled")),
                                   args=list(value.get("args") or []))
    return caps_mod.Capability(enabled=bool(value), args=[])


def _route(plan: Mapping[str, Any], part: str) -> Optional[str]:
    sub = plan.get(part) if plan else None
    return sub.get("route") if isinstance(sub, dict) else None


def build_argv(command: List[str], caps: Mapping[str, Any], plan: Mapping[str, Any],
               prompt: str, *, harness: str = "claude",
               schema: Optional[Mapping[str, Any]] = None,
               schema_file: Optional[str] = None,
               extra_args: Optional[List[str]] = None) -> Tuple[List[str], Optional[str]]:
    """``(argv, stdin text or None)`` for one headless attempt.

    ``command`` is the harness executable (``[claude]``, ``[codex]`` ...);
    ``plan`` is the delegation's ``route`` (``caps.NodePlan.to_dict()``).
    The prompt is the positional argument right after ``headless.run``'s
    args (so a variadic flag like claude's ``--allowedTools`` cannot swallow
    it), or stdin when it is too long for a command line.
    """
    from . import caps as caps_mod

    argv = list(command) + caps_mod.render_args(_cap(caps, "headless.run"))
    stdin: Optional[str] = None
    if len(prompt) > ARGV_PROMPT_MAX:
        stdin = prompt
        if harness == "codex":
            argv.append("-")
    else:
        argv.append(prompt)
    returns = _route(plan, "returns")
    schema_text = json.dumps(schema) if schema is not None else None
    if returns == "schema-flag":
        argv += caps_mod.render_args(_cap(caps, "headless.output.schema"),
                                     schema=schema_text, schema_file=schema_file)
    if caps_mod.enabled(caps, "headless.output.json"):
        argv += caps_mod.render_args(_cap(caps, "headless.output.json"))
    readonly = _route(plan, "readonly")
    if readonly == "sandbox-readonly":
        argv += caps_mod.render_args(_cap(caps, "headless.sandbox.readonly"))
    elif readonly == "tools-restrict":
        argv += caps_mod.render_args(_cap(caps, "headless.tools.restrict"),
                                     tools=READONLY_TOOLS.get(harness, READONLY_TOOLS["claude"]))
    budget = plan.get("budget") if plan else None
    if isinstance(budget, dict) and budget.get("usd_enforced") and budget.get("usd") is not None:
        argv += caps_mod.render_args(_cap(caps, "headless.budget.usd"), usd=f"{float(budget['usd']):g}")
    argv += list(extra_args or [])
    return argv, stdin


_FENCE = re.compile(r"```json[ \t]*\r?\n(.*?)```", re.S | re.I)


def _assistant_text(obj: Any) -> Optional[str]:
    """The agent text in one JSON event of a JSONL stream, if it is one."""
    if not isinstance(obj, dict):
        return None
    item = obj.get("item")
    if isinstance(item, dict) and item.get("type") in ("agent_message", "assistant_message"):
        text = item.get("text")
        return text if isinstance(text, str) else None
    msg = obj.get("msg")
    if isinstance(msg, dict) and msg.get("type") == "agent_message":
        text = msg.get("message")
        return text if isinstance(text, str) else None
    if obj.get("type") == "result" and isinstance(obj.get("result"), str):
        return obj["result"]
    message = obj.get("message") if isinstance(obj.get("message"), dict) else obj
    if message.get("role") == "assistant":
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [c.get("text") for c in content
                     if isinstance(c, dict) and c.get("type") == "text"
                     and isinstance(c.get("text"), str)]
            return "".join(parts) if parts else None
    return None


def final_text(stdout: str, *, json_output: bool) -> Tuple[str, Any]:
    """``(final text, structured value or None)`` of a headless run's stdout.

    With JSON output: claude ``--output-format json`` is one object whose
    ``result`` is the final text (and ``structured_output`` the schema-forced
    value); a JSONL stream (codex ``--json``, pi ``--mode json``) gives the
    last agent message. Anything unparsable is taken as plain text.
    """
    text = stdout or ""
    if not json_output:
        return text, None
    stripped = text.strip()
    try:
        doc = json.loads(stripped)
    except ValueError:
        doc = None
    if isinstance(doc, list):
        results = [d for d in doc if isinstance(d, dict) and d.get("type") == "result"]
        doc = results[-1] if results else None
    if isinstance(doc, dict):
        structured = doc.get("structured_output")
        if isinstance(doc.get("result"), str):
            return doc["result"], structured
        if structured is not None:
            return "", structured
        got = _assistant_text(doc)  # a one-event JSONL stream
        return (got if got is not None else stripped), None
    last: Optional[str] = None
    for line in stripped.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        got = _assistant_text(obj)
        if got:
            last = got
    return (last if last is not None else text), None


def _split(doc: Any) -> Tuple[str, Any]:
    if isinstance(doc, dict) and isinstance(doc.get("outcome"), str):
        return doc["outcome"], doc.get("value")
    return ir.OUTCOME_OK, doc


def extract_result(stdout: str, *, json_output: bool) -> Tuple[Optional[Tuple[str, Any]], str]:
    """``((outcome, value) or None, how)`` from a headless run's stdout.

    Tried in order: a structured value from JSON output, the final text as
    a whole JSON document (what a schema flag yields), the last ```json
    block of the final text. ``how`` names the step that worked, or why
    none did.
    """
    text, structured = final_text(stdout, json_output=json_output)
    if structured is not None:
        return _split(structured), "structured_output"
    body = (text or "").strip()
    if body.startswith("{"):
        try:
            return _split(json.loads(body)), "json-document"
        except ValueError:
            pass
    blocks = _FENCE.findall(text or "")
    if blocks:
        try:
            return _split(json.loads(blocks[-1])), "fenced-block"
        except ValueError as exc:
            return None, f"the last ```json block is not valid JSON: {exc}"
    return None, "no JSON result found (no JSON document and no ```json block)"


def session_task(brief: Mapping[str, Any], *, cwd: str, scope: str, receipt_path: str,
                 mcp: bool = True) -> str:
    """The opening task of a child session running a ``session`` delegation."""
    did = brief.get("delegation")
    if mcp:
        submit = (
            f"When done, submit ONE result with the claunch MCP tool flow_delegate("
            f"delegation=\"{did}\", scope=\"{scope}\", cwd=\"{cwd}\", outcome=..., "
            "value={...}). The run belongs to your parent session, so scope and cwd "
            "are required. If the result is refused, fix the listed problems and "
            "submit once more."
        )
    else:
        submit = (
            f"When done, write {{\"outcome\": ..., \"value\": {{...}}}} as JSON to "
            f"{receipt_path}."
        )
    shape = (f"The result is {{\"outcome\": ..., \"value\": {{...}}}} matching the declared "
             f"returns: {json.dumps(dict(brief.get('returns') or {}))}. ")
    return build_prompt(brief, result=shape + submit)
