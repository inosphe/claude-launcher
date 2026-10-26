"""Two thread pools for the work the daemon moves off its event loop (claunch-d18v8).

Every ``asyncio.to_thread`` in the daemon used to land on the loop's one
default executor. A request handler that waits on a file read and a clock that
walks every worktree for several seconds then queued behind each other: when
all workers were busy with the clock's scans, a dashboard request found no
free worker. On a GIL build that was masked by the GIL itself, since only one
of those threads ran Python at a time anyway. On a free-threaded build the
threads run in parallel, so the same pile-up shows up as CPU contention with
the event loop thread, and the split below matters.

* ``interactive`` -- the loop's default executor. Short work a request or a
  terminal is waiting on. ``to_thread`` keeps using it.
* ``background`` -- clocks, sweeps, cleanup and scans that run for seconds and
  are not on any caller's critical path. Capped well below the core count so
  they cannot take every core from the loop thread and the interactive pool.

Sizes come from ``daemon.thread_pool_interactive`` / ``thread_pool_background``
(0 = derive from the core count).
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import os
import sys
import sysconfig
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional, TypeVar

T = TypeVar("T")

_background: Optional[ThreadPoolExecutor] = None


def gil_state() -> dict:
    """Which interpreter build this daemon runs on and whether its GIL is on.

    ``build`` is ``free-threaded`` for a 3.14t interpreter and ``gil`` for the
    regular one. ``gil`` is the live state: a free-threaded build turns it back
    on when it imports an extension that does not declare itself safe without
    it (zvec's ``_zvec``), unless ``PYTHON_GIL=0`` is set. ``reenabled`` is that
    case, a free-threaded build that is running with the GIL on.
    """
    free_threaded = bool(sysconfig.get_config_var("Py_GIL_DISABLED"))
    enabled = bool(sys._is_gil_enabled()) if hasattr(sys, "_is_gil_enabled") else True
    return {
        "build": "free-threaded" if free_threaded else "gil",
        "gil": enabled,
        "reenabled": free_threaded and enabled,
        "python": ".".join(str(p) for p in sys.version_info[:3]),
    }


def interactive_size(configured: int = 0) -> int:
    """Workers for the interactive pool: ``configured``, else cores + 4 up to 32.

    That is what asyncio picks for its default executor; it is spelled out here
    so the setting and the log line state the number in force.
    """
    if configured and configured > 0:
        return int(configured)
    return min(32, (os.cpu_count() or 1) + 4)


def background_size(configured: int = 0) -> int:
    """Workers for the background pool: ``configured``, else a quarter of the cores, 2..8."""
    if configured and configured > 0:
        return int(configured)
    return max(2, min(8, (os.cpu_count() or 1) // 4))


def install(loop: asyncio.AbstractEventLoop, cfg: dict) -> dict:
    """Size the loop's default executor and create the background pool.

    Returns the sizes in force. Called once from ``_serve``; a second call
    replaces the background pool and leaves the old one to drain.
    """
    global _background
    n_inter = interactive_size(int(cfg.get("thread_pool_interactive") or 0))
    n_back = background_size(int(cfg.get("thread_pool_background") or 0))
    loop.set_default_executor(
        ThreadPoolExecutor(max_workers=n_inter, thread_name_prefix="interactive")
    )
    old, _background = _background, ThreadPoolExecutor(
        max_workers=n_back, thread_name_prefix="background"
    )
    if old is not None:
        old.shutdown(wait=False)
    return {"interactive": n_inter, "background": n_back}


def shutdown() -> None:
    """Stop accepting background work. Does not join: a worker may be in a subprocess."""
    global _background
    pool, _background = _background, None
    if pool is not None:
        pool.shutdown(wait=False, cancel_futures=True)


async def background(func: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """``asyncio.to_thread`` on the background pool.

    Falls back to the default executor when no pool is installed (tests that
    drive a component without ``_serve``), so callers need no guard.
    """
    pool = _background
    if pool is None:
        return await asyncio.to_thread(func, *args, **kwargs)
    ctx = contextvars.copy_context()
    call = functools.partial(ctx.run, func, *args, **kwargs)
    return await asyncio.get_running_loop().run_in_executor(pool, call)
