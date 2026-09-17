"""Live relay configuration for the authenticated Settings surface."""

from __future__ import annotations

import asyncio
import re
import socket
from urllib.parse import urlsplit

from .. import store
from . import paths, relay_uplink


class RelaySettings:
    def __init__(self, port: int, on_change) -> None:
        self.port = port
        self.on_change = on_change
        self.pool = relay_uplink.RelayPool([])
        self.tasks = {}
        self.lock = asyncio.Lock()

    def _build(self, rows):
        name = f"{socket.gethostname()}-{paths.instance()}" if paths.instance() else ""
        pool = relay_uplink.pool_from_config(
            rows, local_host="127.0.0.1", local_port=self.port, default_name=name,
        )
        return pool.uplinks if pool else []

    async def start(self):
        await self._apply(self._build(store.relays_config()))

    @staticmethod
    def _identity(up):
        return up.id, up.url, up.name, up.token, up.verify_tls

    async def _apply(self, candidates):
        old = {up.id: up for up in self.pool.uplinks}
        kept = []
        for up in candidates:
            previous = old.pop(up.id, None)
            if previous and self._identity(previous) == self._identity(up):
                kept.append(previous)
                continue
            if previous:
                await self._stop(previous)
            kept.append(up)
            self.tasks[up.id] = asyncio.create_task(up.run())
        for up in old.values():
            await self._stop(up)
        self.pool.uplinks = kept
        self.pool._routes.clear()
        if kept:
            self.on_change(self.pool)

    async def _stop(self, up):
        up.stop()
        task = self.tasks.pop(up.id, None)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def close(self):
        for up in list(self.pool.uplinks):
            await self._stop(up)

    def state(self):
        live = {up.id: up for up in self.pool.uplinks}
        rows = store.relays_config()
        result = []
        for row in rows:
            up = live.pop(row["id"], None)
            result.append({
                "id": row["id"], "url": row.get("url", ""),
                "name": row.get("name", ""),
                "verify_tls": row.get("verify_tls", True),
                "token_set": bool(row.get("token")) or bool(up),
                "connected": bool(up and up.connected),
                "effective_url": up.url if up else None,
                "effective_name": up.name if up else None,
            })
        # An environment-only connection has no editable config row.
        for up in live.values():
            result.append({"id": up.id, "url": up.url, "name": up.name,
                           "verify_tls": up.verify_tls, "token_set": True,
                           "connected": up.connected, "environment_only": True})
        return {"relays": result, "relay": self.pool.state()}

    async def save(self, body):
        ident = body.get("id")
        if not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", ident):
            raise ValueError("Relay ID must contain 1–64 letters, digits, underscores or hyphens")
        fields = {}
        for key in ("url", "name", "token"):
            value = body.get(key, "")
            if not isinstance(value, str):
                raise ValueError(f"{key} must be text")
            fields[key] = value.strip() if key != "token" else value
        try:
            url = urlsplit(fields["url"])
            valid = (not any(c.isspace() for c in fields["url"]) and
                     url.scheme in ("ws", "wss") and url.hostname and
                     not url.username and not url.password and not url.fragment)
            url.port
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("Relay URL must be a ws:// or wss:// address without credentials or a fragment")
        if len(fields["name"].encode("utf-8")) > 255:
            raise ValueError("Backend name must fit in 255 UTF-8 bytes")
        if not isinstance(body.get("verify_tls", True), bool):
            raise ValueError("verify_tls must be a boolean")
        fields["verify_tls"] = body.get("verify_tls", True)
        if not fields["token"]:
            fields.pop("token")  # An empty password field preserves the saved secret.
        async with self.lock:
            candidates = []

            def mutate(doc):
                nonlocal candidates
                rows = store.relays_config(doc)
                target = next((row for row in rows if row["id"] == ident), None)
                if target is None:
                    target = {"id": ident}
                    rows.append(target)
                target.update(fields)
                candidates = self._build(rows)
                ids = {up.id for up in candidates}
                if ident not in ids:
                    raise ValueError("A backend token is required to connect this relay")
                if any(up.id not in ids for up in self.pool.uplinks):
                    raise ValueError("Adding this relay would disable an existing environment-configured relay. Save its settings with a per-relay token first.")
                daemon = doc.setdefault("daemon", {})
                daemon["relays"] = rows
                daemon.pop("relay", None)

            store.update(mutate)
            await self._apply(candidates)
        return self.state()
