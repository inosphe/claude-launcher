"""The VM guidance pack follows the VM grants of a session (vm-setup-8s6, s2-vmprompt).

A session that holds a grant on some VM is sent the prompt pack ``mcp-vm`` through
the mechanism the packs pane uses: the block is typed into the terminal once and
*held* (``session_packs.record_sent``), so the session reminder names the pack id
on every interval and ``rebrief`` serves the text back after a compaction. When the
last grant is revoked the hold is dropped and the reminder stops naming it. A
session without a grant is never sent the pack. No delivery path of its own.
"""

from __future__ import annotations

import asyncio
import logging

from . import session_packs

log = logging.getLogger("claunch.daemon.vm-pack")

PACK = "mcp-vm"


async def sync(manager, reg, session: str) -> None:
    """Make the hold of :data:`PACK` for ``session`` match whether it has a grant.
    Never raises: guidance is decoration and must not fail a grant or a revoke."""
    if manager is None or not session:
        return
    try:
        live = manager.get(session)
        sdef = live.sdef
        if any(isinstance(r, dict) and r.get("id") == PACK for r in getattr(sdef, "prompt_packs", ()) or ()):
            return                                    # the opening carries it already
        kept = PACK in await asyncio.to_thread(session_packs.held, session)
        wanted = reg.has_grant(session)
        if wanted and not kept:
            block, how, text = await asyncio.to_thread(session_packs.block_for, session, PACK, manager=manager)

            def done(delivered: bool) -> None:
                row = session_packs.sent_row(PACK, how, block, delivered)
                session_packs.record_sent(session, row, text)
            live.queue_delivery(block, on_done=done)
        elif kept and not wanted:
            await asyncio.to_thread(session_packs.drop, session, PACK)
    except Exception:  # noqa: BLE001 - see the docstring
        log.warning("vm guidance pack sync failed for %r", session, exc_info=True)
