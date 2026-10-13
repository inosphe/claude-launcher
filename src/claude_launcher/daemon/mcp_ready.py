"""When a claude session's MCP servers are up, so the opening can go out.

Claude Code sends a positional-prompt first turn before it has connected the
MCP servers it was configured with: the request goes out with ``tools=[]`` and
the model answers in text ``<invoke>`` blocks instead of tool calls
(claunch-xixag; measured by s328 on 16 jobs -- connection first: 6/6 correct,
connection later: 9/9 broken). A session that declares MCP servers therefore
takes its opening typed in (bracketed paste) once they are connected, not on
argv.

Claude Code prints no "MCP ready" marker the daemon can read, so readiness is
assembled from what can be known:

* ``claunch mcp`` announces itself (:func:`announce`, on ``initialized``), and
  the daemon records it on the session;
* every other declared server (the jobs' own ``--mcp-config`` entries, e.g.
  gds6-plane, stdio, about 500 ms to connect) has no such channel, so it gets
  a settle time counted from the spawn of the session.

The wait is bounded (:data:`CAP`): a declared server that never shows up must
not hold the opening back for good.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

log = logging.getLogger(__name__)

#: The server key ``claunch install`` registers (``install.MCP_NAME``).
CLAUNCH = "claunch"
#: Upper bound on the whole wait, seconds.
CAP = 10.0
#: Seconds after the session's spawn that servers without a ready channel are
#: given to connect (measured about 0.5 s for a stdio server).
SETTLE = 2.0

MCP_FLAG = "--mcp-config"
STRICT_FLAG = "--strict-mcp-config"


@dataclass(frozen=True)
class Plan:
    """What the opening waits for. An empty plan means: do not wait."""

    servers: tuple = ()
    signal: bool = False  # the claunch server's own announcement
    settle: float = 0.0   # seconds after spawn, for the servers with no channel

    @property
    def waits(self) -> bool:
        return bool(self.servers)


def _read_json(path: Path) -> dict:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _servers_of(doc: dict) -> Dict[str, dict]:
    block = doc.get("mcpServers")
    if not isinstance(block, dict):
        return {}
    return {str(k): v for k, v in block.items() if isinstance(v, dict)}


def _flag_values(args: Sequence[str]) -> List[str]:
    """Every value given to ``--mcp-config`` (variadic: values run to the next flag)."""
    values: List[str] = []
    i = 0
    while i < len(args):
        arg = str(args[i])
        if arg == MCP_FLAG:
            i += 1
            while i < len(args) and not str(args[i]).startswith("--"):
                values.append(str(args[i]))
                i += 1
            continue
        if arg.startswith(MCP_FLAG + "="):
            values.append(arg[len(MCP_FLAG) + 1:])
        i += 1
    return values


def _flag_servers(args: Sequence[str]) -> Dict[str, dict]:
    found: Dict[str, dict] = {}
    for value in _flag_values(args):
        text = value.strip()
        if text.startswith("{"):
            try:
                doc = json.loads(text)
            except ValueError:
                continue
            doc = doc if isinstance(doc, dict) else {}
        else:
            doc = _read_json(Path(text).expanduser())
        found.update(_servers_of(doc))
    return found


def _project_files(cwd: str) -> Iterable[Path]:
    here = Path(cwd).resolve() if cwd else Path.cwd()
    for folder in (here, *here.parents):
        yield folder / ".mcp.json"


def declared_servers(
    args: Sequence[str], cwd: str, user_json: Optional[Path]
) -> Dict[str, dict]:
    """The MCP servers a claude session starts with.

    ``--mcp-config`` entries; unless ``--strict-mcp-config`` is given, also the
    project ``.mcp.json`` (the cwd and its parents) and the user scope
    ``.claude.json`` (its top-level and per-project ``mcpServers``).
    """
    servers = _flag_servers(args)
    if STRICT_FLAG in args:
        return servers
    for path in _project_files(cwd):
        servers.update({k: v for k, v in _servers_of(_read_json(path)).items()
                        if k not in servers})
    if user_json is not None:
        doc = _read_json(user_json)
        for k, v in _servers_of(doc).items():
            servers.setdefault(k, v)
        projects = doc.get("projects")
        entry = projects.get(str(Path(cwd).resolve())) if (
            isinstance(projects, dict) and cwd) else None
        if isinstance(entry, dict):
            for k, v in _servers_of(entry).items():
                servers.setdefault(k, v)
    return servers


def plan_for_servers(servers: Dict[str, dict]) -> Plan:
    if not servers:
        return Plan()
    others = [k for k in servers if k != CLAUNCH]
    return Plan(
        servers=tuple(sorted(servers)),
        signal=CLAUNCH in servers,
        settle=SETTLE if others else 0.0,
    )


def plan_for(sdef) -> Plan:
    """The wait for ``sdef``'s claude session; empty when no MCP is attached."""
    from .. import config, profile as profile_mod

    user_json = None
    try:
        if getattr(sdef, "profile", None):
            user_json = profile_mod.require_selector(sdef.profile).config_dir / ".claude.json"
        else:
            user_json = config.user_claude_json()
    except Exception:  # an unreadable profile is the launch's error to raise
        user_json = None
    return plan_for_servers(
        declared_servers(tuple(sdef.args), sdef.cwd or "", user_json)
    )


async def wait(session, plan: Plan, *, cap: float = CAP) -> bool:
    """Wait until ``plan`` is met or ``cap`` seconds have passed.

    Returns whether it was met; the caller delivers either way.
    """
    deadline = time.monotonic() + cap
    if plan.signal:
        try:
            await asyncio.wait_for(session.mcp_ready_event().wait(), cap)
        except asyncio.TimeoutError:
            log.warning(
                "no claunch MCP announcement from %r within %.0fs; "
                "delivering the opening anyway", session.sdef.name, cap,
            )
            return False
    if plan.settle:
        started = getattr(session, "_started_mono", None) or time.monotonic()
        until = min(started + plan.settle, deadline)
        delay = until - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
    return True


# --- the claunch mcp process's side ------------------------------------------

def announce() -> None:
    """Tell the daemon this session's ``claunch mcp`` is up (best effort).

    Off the stdio loop: the daemon call must never delay an MCP reply.
    """
    name = os.environ.get("CLAUNCH_SESSION")
    if not name:
        return

    def post() -> None:
        try:
            from .. import daemon_client

            client = daemon_client.connect()
            if client is not None:
                client.post(f"/api/sessions/{name}/mcp-ready", {})
        except Exception:
            pass  # the opening's wait is bounded; a lost announcement costs the cap

    threading.Thread(target=post, name="mcp-ready", daemon=True).start()
