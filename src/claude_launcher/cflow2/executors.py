"""Delegation executors for cflow v2 (``docs/cflow-v2.md`` sections 6.4, 7).

The runtime decides *which* executor a delegation point gets (``caps.plan_node``)
and owns the delegation record; this module is the hook through which a
``session`` delegation is actually started.

- ``inline``   -- no dispatch: the driver runs the subtree itself.
- ``subagent`` -- no dispatch: the driver reads the brief from ``flow_status``
  / ``flow_delegate``, runs it with its harness's sub-agent feature and
  submits the result with ``flow_delegate``.
- ``fork``     -- like ``session``, but the child is a quick-fork of the
  run's driver (``daemon/handoff.py``): it starts with a copy of the driver's
  whole conversation and stands in the driver's own checkout. Its opening is
  :func:`fork_task`, and it ends itself with a quick-fork merge once its
  result is in.
- ``session`` -- needs a child session the daemon starts. The daemon
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
:func:`register` is not what decides whether a ``session``
delegation can be served. The daemon's cflow2 clock
(``daemon/cflow2_clock.py``) writes a heartbeat file every tick
(:func:`heartbeat_path`: ``<daemon dir>/cflow2_clock.json``) listing the run
directories it scans. :func:`available` is true when a dispatcher is
registered in this process, or when that heartbeat is fresh and names the
run's directory; the delegation is then left ``queued`` and the clock picks
it up on its next tick. A stale or missing heartbeat (no daemon, the clock
switched off, or a directory no live session stands in) keeps the old
behaviour: the runtime falls back at once and journals why.

The second half of this module is the prompt a ``session`` delegate's child
gets (:func:`build_prompt`, :func:`session_task`). The ``headless`` executor
(a one-shot harness process) was removed (claunch-xjbrh).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

from . import ir

Dispatcher = Callable[[dict, Any], Optional[str]]

#: Executors that need a dispatcher (a process someone else starts).
DISPATCHED = (ir.EXEC_SESSION, ir.EXEC_FORK)

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
    """Whether a queued ``session`` delegation in ``cwd`` will
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
    """Hand a ``session`` delegation to the registered dispatcher.

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
    def next_executor(node: ir.Node, current: str, caps: Mapping[str, Any],
                      flat: bool = True) -> str:
        """The first executor after ``current`` in the node's preference list
        that does not need a dispatcher and whose capabilities hold (and, for
        a subtree that is not ``flat``, that can run one); ``inline`` when
        none does."""
        from . import caps as caps_mod

        prefs: List[str] = list(node.executor or [])
        rest = prefs[prefs.index(current) + 1:] if current in prefs else []
        rest = [ex for ex in rest if ex not in DISPATCHED]
        return caps_mod.choose_executor(rest, caps, needs=node.needs or [],
                                        flat=flat).executor


# ---------------------------------------------------------------------------
# the delegate's prompt


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
                 result: Optional[str] = None, intro: Optional[str] = None) -> str:
    """The text a delegate gets: the brief's tasks in order, the rules, the
    return format; and on a re-run, the previous attempt's problems.
    ``intro`` replaces the opening line (a fork has the main run's context)."""
    lines: List[str] = [
        intro or (
            f"You are running delegation {brief.get('delegation')} of a cflow workflow "
            f"(node {brief.get('label') or brief.get('node')}). Work only on what is "
            "below; you have no other context from the main run."),
    ]
    if brief.get("with"):
        lines.append("Inputs: " + json.dumps(brief["with"], ensure_ascii=False))
    for i, task in enumerate(brief.get("tasks") or [], 1):
        title = task.get("title") or task.get("label") or task.get("node")
        lines.append(f"\n## Task {i}: {title}\n{task.get('prompt') or ''}")
        if task.get("done_when"):
            lines.append(f"Done when: {task['done_when']}")
        if task.get("verify"):
            v = task["verify"]
            run = v.get("run", v.get("command")) if isinstance(v, dict) else v
            lines.append(f"Verify: {' '.join(run) if isinstance(run, list) else run}")
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


def session_task(brief: Mapping[str, Any], *, cwd: str, scope: str, receipt_path: str,
                 mcp: bool = True, receipt_token: Optional[str] = None,
                 workflow: Optional[str] = None) -> str:
    """The opening task of a child session running a ``session`` delegation.
    ``workflow`` is the run the child is started on (the node's
    ``session_workflow``): the task then says that finishing that run, after
    the result is in, is what ends the session."""
    did = brief.get("delegation")
    if brief.get("nested"):
        # A nested delegation run is driven with flow_*; its finish is the
        # result, so there is nothing to submit (the planner keeps a
        # non-flat subtree off this executor when MCP is unavailable).
        return build_prompt(brief, result=str(brief.get("submit") or ""))
    if mcp:
        submit = (
            f"When done, submit ONE result with the claunch MCP tool flow_delegate("
            f"delegation=\"{did}\", scope=\"{scope}\", cwd=\"{cwd}\", outcome=..., "
            "value={...}). The run belongs to your parent session, so scope and cwd "
            "are required. If the result is refused, fix the listed problems and "
            "submit once more."
        )
    else:
        key = (f", \"key\": \"{receipt_token}\"" if receipt_token else "")
        submit = (
            f"When done, write {{\"outcome\": ..., \"value\": {{...}}{key}}} as JSON to "
            f"{receipt_path}.tmp first and then rename (mv) it to {receipt_path}: the "
            "daemon reads that file as soon as it appears, so it must never exist "
            "half-written."
            + (" The \"key\" field is required; a receipt without it is refused. Do not "
               "show the key to anyone." if receipt_token else "")
        )
    shape = (f"This delegation belongs to the run of session {scope!r} in {cwd}: a command "
             f"that reads that run's records (its journal, commits or round report) names "
             f"that session explicitly (e.g. -t/--session {scope}), since your own "
             f"$CLAUNCH_SESSION is not it. "
             f"The result is {{\"outcome\": ..., \"value\": {{...}}}} matching the declared "
             f"returns: {json.dumps(dict(brief.get('returns') or {}))}. ")
    if workflow:
        submit += (
            f" This session was started on a cflow v2 run of its own, {workflow!r}: its "
            f"scope is your own session name, so the flow_* calls that drive it take no "
            f"scope (only flow_delegate above names your parent's run). This delegation is that run's job. Once your "
            f"result is accepted (or the delegation was ended for you), finish that run: "
            f"the daemon ends this session when it is done, so nobody has to kill it."
        )
    return build_prompt(brief, result=shape + submit)


def fork_task(brief: Mapping[str, Any], *, cwd: str, scope: str, driver: str) -> str:
    """The task under the marker block of a quick-fork running a ``fork``
    delegation (claunch-dm09i.1).

    The fork has the driver's conversation up to the fork, so the brief does
    not say "no other context"; what it has to learn is where its own part
    starts and ends. It is told three things the driver's conversation does
    not: the delegation is its whole job, its result reaches the main run on
    its own (the nested run's finish, or one ``flow_delegate``), and once that
    is in it ends itself with the ``handoff`` tool, whose text is typed into
    the driver. A fork forks nothing itself: the daemon refuses a ``fork``
    delegation whose driver is a quick-fork.
    """
    did = brief.get("delegation")
    label = brief.get("label") or brief.get("node")
    intro = (
        f"You are a quick-fork of {driver!r}, started to carry out delegation {did} "
        f"(node {label}) of {driver!r}'s cflow run in {cwd} (scope {scope!r}). The "
        f"conversation above is {driver!r}'s up to the moment of the fork: use it, but "
        f"from here on this delegation is your whole job. {driver!r} goes on with the "
        f"rest of its run (and other forks may be working beside you in this same "
        f"checkout) -- do not carry on {driver!r}'s own tasks."
    )
    if brief.get("nested"):
        result = str(brief.get("submit") or "")
    else:
        result = (
            f"When done, submit ONE result with the claunch MCP tool flow_delegate("
            f"delegation=\"{did}\", scope=\"{scope}\", cwd=\"{cwd}\", outcome=..., "
            "value={...}). If the result is refused, fix the listed problems and submit "
            "once more. Declared returns: " + json.dumps(dict(brief.get("returns") or {}))
        )
    result += (
        "\n\nWhen your result is in (the nested run finished, or flow_delegate accepted "
        f"it), end this session with the claunch MCP tool handoff(text=...): a few lines "
        f"for {driver!r} -- the outcome, the files you changed, anything left open. "
        "That text is typed into its terminal and this copy then ends; nobody else ends "
        "it. Do not call handoff before the result is in: a fork that ends without a "
        "result fails its delegation. If the flow tools answer that the delegation "
        "was cancelled or its nested run aborted, stop editing at once and end with "
        "handoff, saying what you had changed."
    )
    return build_prompt(brief, result=result, intro=intro)
