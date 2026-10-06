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
"""

from __future__ import annotations

from typing import Any, Callable, List, Mapping, Optional

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


def dispatch(delegation: dict, run: Any) -> bool:
    """Hand a ``headless``/``session`` delegation to the registered dispatcher.

    Returns ``False`` when nothing is registered (the caller falls back);
    otherwise updates ``delegation['status']`` and returns ``True``.
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
