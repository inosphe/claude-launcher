"""Sync ``~/.claunch.yaml`` with a remote profile sync server.

The config file is the launcher's source of truth (see :mod:`store`); this
module keeps that file in step with the same document held by a sync server, so
several machines share one profile set. What travels is *configuration only* —
profiles and their ``env``/``parent``/``provider``, provider definitions, the
template and any harnesses. Login tokens never leave the machine (they live in
:mod:`credentials`), and the ``daemon`` block stays local too: ports, bind host
and the relay token describe *this* machine, not the profile set.

Configure the server in the config file::

    sync:
      url: https://sync.example.com
      namespace: alice            # which document on the server
      token: "..."                # prefer the CLAUNCH_SYNC_TOKEN env var
      sections: [profiles, providers, provider, template, harnesses]

Three modes, all reachable as ``claunch sync --mode ...``:

``up``
    Local wins: push this machine's sections as the new server document.
``down``
    Server wins: overwrite the local sections with the server's.
``merge`` (default)
    Three-way merge. The last state this machine agreed on is cached as the
    *base* (:func:`config.sync_base_file`), so a key deleted on one machine
    propagates as a deletion rather than being resurrected by the other side.
    Only keys that both sides changed *differently* are conflicts; those are
    reported and resolved by ``--prefer`` (local by default).

Concurrency is optimistic: every document carries a revision, a push states the
revision it was based on, and the server rejects a push built on a stale one.
``merge`` re-reads and retries once when that happens.
"""

from __future__ import annotations

import copy
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import yaml

from . import bootstrap, config, store

#: Sections synced when the config file does not say otherwise. ``daemon`` is
#: deliberately absent (machine-local), and ``sync`` can never be included.
DEFAULT_SECTIONS: Tuple[str, ...] = (
    "template",
    "provider",
    "providers",
    "profiles",
    "harnesses",
)

#: Never syncable: ``sync`` is this machine's link to the server (syncing it
#: would let one machine rewrite every other machine's credentials), and
#: ``version`` is the file's own schema marker, not shared state.
RESERVED_SECTIONS = frozenset({"sync", "version"})

MODES = ("merge", "up", "down")

#: Sentinel for "this key is absent", distinct from a key present with value
#: ``None`` — the merge has to tell deletion from an explicit null.
_MISSING = object()


class SyncError(Exception):
    """Raised for missing/invalid sync config and for server-side failures."""


class RevisionConflict(SyncError):
    """The server's document moved on since the revision the push was based on."""

    def __init__(self, message: str, remote: "RemoteDoc") -> None:
        super().__init__(message)
        self.remote = remote


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SyncConfig:
    """The effective ``sync:`` block (env overrides already applied)."""

    url: str
    namespace: str
    token: str
    sections: Tuple[str, ...] = DEFAULT_SECTIONS
    verify_tls: bool = True
    allow_insecure: bool = False
    timeout: float = 30.0


def _sections_from(raw: Any) -> Tuple[str, ...]:
    if raw is None:
        return DEFAULT_SECTIONS
    if not isinstance(raw, (list, tuple)) or not all(isinstance(s, str) for s in raw):
        raise SyncError("sync.sections must be a list of section names")
    names = [s.strip() for s in raw if s.strip()]
    bad = sorted(set(names) & RESERVED_SECTIONS)
    if bad:
        raise SyncError(
            f"sync.sections may not include {', '.join(bad)} "
            "(machine-local / schema-internal)"
        )
    if not names:
        raise SyncError("sync.sections is empty; nothing would be synced")
    # de-duplicate, keep the file's order
    seen: List[str] = []
    for name in names:
        if name not in seen:
            seen.append(name)
    return tuple(seen)


def load_config(doc: Optional[dict] = None) -> SyncConfig:
    """Read the ``sync:`` block, layering env overrides on top.

    Raises :class:`SyncError` when no server is configured — the CLI turns that
    into a message pointing at the config file.
    """
    doc = store.load() if doc is None else doc
    block = doc.get("sync")
    if block is None:
        block = {}
    if not isinstance(block, dict):
        raise SyncError(f"'sync' in {store.path()} must be a mapping")

    url = (config.sync_url() or str(block.get("url") or "")).strip().rstrip("/")
    if not url:
        raise SyncError(
            f"no sync server configured; add a 'sync:' block to {store.path()} "
            "(url, namespace, token) or set CLAUNCH_SYNC_URL"
        )
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise SyncError(f"sync.url must be an http(s) URL, got {url!r}")

    allow_insecure = bool(block.get("allow_insecure", False))
    if parsed.scheme == "http" and not _is_loopback(parsed.hostname) and not allow_insecure:
        raise SyncError(
            f"refusing to sync over plain http to {parsed.hostname} — the document "
            "carries provider auth tokens. Use https, or set "
            "'sync.allow_insecure: true' if the link is already private."
        )

    namespace = (config.sync_namespace() or str(block.get("namespace") or "")).strip()
    if not namespace:
        raise SyncError(
            "no sync namespace set; add 'namespace:' to the sync block or set "
            "CLAUNCH_SYNC_NAMESPACE"
        )

    token = (config.sync_token() or str(block.get("token") or "")).strip()
    if not token:
        raise SyncError(
            "no sync token; set CLAUNCH_SYNC_TOKEN or add 'token:' to the sync block "
            "(get one from the server's 'claunch sync-server user add')"
        )

    timeout = block.get("timeout", 30.0)
    try:
        timeout = float(timeout)
    except (TypeError, ValueError):
        raise SyncError("sync.timeout must be a number of seconds") from None

    return SyncConfig(
        url=url,
        namespace=namespace,
        token=token,
        sections=_sections_from(block.get("sections")),
        verify_tls=bool(block.get("verify_tls", True)),
        allow_insecure=allow_insecure,
        timeout=timeout,
    )


def _is_loopback(host: str) -> bool:
    return host in ("localhost", "127.0.0.1", "::1", "[::1]")


# --------------------------------------------------------------------------- #
# transport
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RemoteDoc:
    """A document as the server holds it."""

    revision: int
    doc: Dict[str, Any]
    updated_at: Optional[str] = None
    updated_by: Optional[str] = None


class SyncClient:
    """Minimal stdlib HTTP client for the sync server's JSON API."""

    def __init__(self, cfg: SyncConfig) -> None:
        self.cfg = cfg

    def _context(self) -> Optional[ssl.SSLContext]:
        if not self.cfg.url.startswith("https://"):
            return None
        if self.cfg.verify_tls:
            return ssl.create_default_context()
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def _request(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        url = self.cfg.url + path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.cfg.token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(
                req, timeout=self.cfg.timeout, context=self._context()
            ) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as exc:
            detail = _payload_of(exc)
            if exc.code == 409:
                raise RevisionConflict(
                    detail.get("error") or "document changed on the server",
                    _remote_from(detail),
                ) from None
            message = detail.get("error") or f"HTTP {exc.code}"
            if exc.code == 401:
                message = f"{message} (check the sync token)"
            raise SyncError(f"{method} {path}: {message}") from None
        except (urllib.error.URLError, OSError) as exc:
            raise SyncError(f"cannot reach sync server at {self.cfg.url}: {exc}") from None
        try:
            parsed = json.loads(payload.decode("utf-8"))
        except ValueError:
            raise SyncError(f"{method} {path}: server sent a non-JSON response") from None
        if not isinstance(parsed, dict):
            raise SyncError(f"{method} {path}: server sent an unexpected payload")
        return parsed

    def fetch(self) -> RemoteDoc:
        """Read the namespace's current document (revision 0 when it has none)."""
        return _remote_from(
            self._request("GET", f"/api/sync/doc/{_quote(self.cfg.namespace)}")
        )

    def push(self, doc: Dict[str, Any], base_revision: int) -> RemoteDoc:
        """Replace the document, provided the server is still at ``base_revision``."""
        return _remote_from(
            self._request(
                "PUT",
                f"/api/sync/doc/{_quote(self.cfg.namespace)}",
                {"revision": base_revision, "doc": doc},
            )
        )


def _quote(namespace: str) -> str:
    return urllib.parse.quote(namespace, safe="")


def _payload_of(exc: urllib.error.HTTPError) -> dict:
    try:
        parsed = json.loads(exc.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - any unreadable error body is just "no detail"
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _remote_from(payload: dict) -> RemoteDoc:
    doc = payload.get("doc")
    return RemoteDoc(
        revision=int(payload.get("revision") or 0),
        doc=doc if isinstance(doc, dict) else {},
        updated_at=payload.get("updated_at"),
        updated_by=payload.get("updated_by"),
    )


# --------------------------------------------------------------------------- #
# the merge base (per-machine bookkeeping)
# --------------------------------------------------------------------------- #
@dataclass
class Base:
    """The last state this machine and the server agreed on."""

    revision: int = 0
    namespace: str = ""
    url: str = ""
    doc: Dict[str, Any] = field(default_factory=dict)

    def matches(self, cfg: SyncConfig) -> bool:
        """Whether this base was recorded against the same server document."""
        return self.namespace == cfg.namespace and self.url == cfg.url


def base_path() -> Path:
    return config.sync_base_file()


def load_base(cfg: SyncConfig) -> Base:
    """The cached merge base, or an empty one if absent or from another server.

    A base recorded against a different URL/namespace is *not* a valid base for
    this one — treating it as one would read unrelated deletions into the merge.
    Falling back to an empty base degrades merge to a plain union, which is the
    safe direction.
    """
    path = base_path()
    if not path.is_file():
        return Base()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return Base()
    if not isinstance(data, dict):
        return Base()
    doc = data.get("doc")
    base = Base(
        revision=int(data.get("revision") or 0),
        namespace=str(data.get("namespace") or ""),
        url=str(data.get("url") or ""),
        doc=doc if isinstance(doc, dict) else {},
    )
    return base if base.matches(cfg) else Base()


def save_base(cfg: SyncConfig, revision: int, doc: Dict[str, Any]) -> None:
    path = base_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "url": cfg.url,
        "namespace": cfg.namespace,
        "revision": revision,
        "doc": doc,
    }
    path.write_text(
        yaml.safe_dump(payload, sort_keys=True, allow_unicode=True, default_flow_style=False),
        encoding="utf-8",
    )


# --------------------------------------------------------------------------- #
# three-way merge
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Conflict:
    """A key both sides changed, differently, since the base."""

    path: str
    local: Any
    remote: Any
    winner: str


def three_way_merge(
    base: Dict[str, Any],
    local: Dict[str, Any],
    remote: Dict[str, Any],
    *,
    prefer: str = "local",
) -> Tuple[Dict[str, Any], List[Conflict]]:
    """Merge ``local`` and ``remote`` over their common ``base``.

    Per key: whoever changed it since the base wins; if both changed it the same
    way there is nothing to decide; if both changed it differently the values are
    merged recursively when both are mappings, and otherwise reported as a
    conflict resolved by ``prefer``. Absence is a value, so a key deleted on one
    side and untouched on the other stays deleted.
    """
    if prefer not in ("local", "remote"):
        raise SyncError(f"unknown conflict preference {prefer!r} (use local or remote)")
    conflicts: List[Conflict] = []
    merged = _merge_node(base, local, remote, prefer, "", conflicts)
    return merged, conflicts


def _merge_node(
    base: Dict[str, Any],
    local: Dict[str, Any],
    remote: Dict[str, Any],
    prefer: str,
    prefix: str,
    conflicts: List[Conflict],
) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key in sorted(set(base) | set(local) | set(remote)):
        b = base.get(key, _MISSING)
        l = local.get(key, _MISSING)
        r = remote.get(key, _MISSING)
        path = f"{prefix}{key}"
        if l == r:  # both sides agree (including "both deleted it")
            if l is not _MISSING:
                out[key] = copy.deepcopy(l)
            continue
        if b == l:  # only the server moved
            if r is not _MISSING:
                out[key] = copy.deepcopy(r)
            continue
        if b == r:  # only this machine moved
            if l is not _MISSING:
                out[key] = copy.deepcopy(l)
            continue
        if isinstance(l, dict) and isinstance(r, dict):
            out[key] = _merge_node(
                b if isinstance(b, dict) else {}, l, r, prefer, f"{path}.", conflicts
            )
            continue
        winner = l if prefer == "local" else r
        conflicts.append(
            Conflict(
                path=path,
                local=None if l is _MISSING else l,
                remote=None if r is _MISSING else r,
                winner=prefer,
            )
        )
        if winner is not _MISSING:
            out[key] = copy.deepcopy(winner)
    return out


# --------------------------------------------------------------------------- #
# local document <-> synced subset
# --------------------------------------------------------------------------- #
def local_subset(sections: Sequence[str], doc: Optional[dict] = None) -> Dict[str, Any]:
    """The part of the config file that this sync covers."""
    doc = store.load() if doc is None else doc
    return {name: copy.deepcopy(doc[name]) for name in sections if name in doc}


def apply_subset(merged: Dict[str, Any], sections: Sequence[str]) -> None:
    """Write ``merged`` back into the config file, leaving other sections alone.

    Sections in scope but absent from ``merged`` are removed: the merge result is
    authoritative for everything it covers, so a profile deleted elsewhere really
    does go away here.
    """

    def _mutate(doc: dict) -> None:
        for name in sections:
            if name in merged:
                doc[name] = copy.deepcopy(merged[name])
            else:
                doc.pop(name, None)

    store.update(_mutate)


# --------------------------------------------------------------------------- #
# diffing (for the report)
# --------------------------------------------------------------------------- #
def diff_lines(old: Dict[str, Any], new: Dict[str, Any], prefix: str = "") -> List[str]:
    """Human-readable ``+`` / ``-`` / ``~`` lines describing ``old`` -> ``new``.

    Recurses into mappings so a single changed env var reads as
    ``~ profiles.work.env.FOO`` rather than "the whole profile changed".
    """
    lines: List[str] = []
    for key in sorted(set(old) | set(new)):
        o = old.get(key, _MISSING)
        n = new.get(key, _MISSING)
        if o == n:
            continue
        path = f"{prefix}{key}"
        if o is _MISSING:
            lines.append(f"+ {path}")
        elif n is _MISSING:
            lines.append(f"- {path}")
        elif isinstance(o, dict) and isinstance(n, dict):
            lines.extend(diff_lines(o, n, f"{path}."))
        else:
            lines.append(f"~ {path}")
    return lines


# --------------------------------------------------------------------------- #
# the sync itself
# --------------------------------------------------------------------------- #
@dataclass
class SyncResult:
    """What one ``claunch sync`` run did (or would do, under ``dry_run``)."""

    mode: str
    url: str
    namespace: str
    revision: int
    pushed: bool = False
    local_changes: List[str] = field(default_factory=list)
    remote_changes: List[str] = field(default_factory=list)
    conflicts: List[Conflict] = field(default_factory=list)
    dry_run: bool = False
    retried: bool = False
    #: Profiles this sync stopped declaring locally. Their directories survive
    #: (only ``claunch prune`` deletes those), so the CLI calls them out.
    dropped_profiles: List[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.local_changes or self.remote_changes)


def run(
    mode: str = "merge",
    *,
    prefer: str = "local",
    dry_run: bool = False,
    cfg: Optional[SyncConfig] = None,
    client: Optional[SyncClient] = None,
) -> SyncResult:
    """Synchronize the config file with the server and return what changed."""
    if mode not in MODES:
        raise SyncError(f"unknown sync mode {mode!r} (use {', '.join(MODES)})")
    cfg = load_config() if cfg is None else cfg
    client = SyncClient(cfg) if client is None else client

    remote = client.fetch()
    local = local_subset(cfg.sections)
    base = load_base(cfg)

    if mode == "up":
        target, conflicts = copy.deepcopy(local), []
    elif mode == "down":
        if remote.revision == 0:
            # "Server wins" against a server that has nothing would silently
            # undeclare every local profile. Nothing to pull is an error, not a
            # licence to wipe.
            raise SyncError(
                f"the sync server has no document for namespace {cfg.namespace!r} "
                "yet — nothing to pull (publish this machine's config first with "
                "'claunch sync --mode up')"
            )
        target, conflicts = _scoped(remote.doc, cfg.sections), []
    else:
        target, conflicts = three_way_merge(
            _scoped(base.doc, cfg.sections),
            local,
            _scoped(remote.doc, cfg.sections),
            prefer=prefer,
        )

    result = SyncResult(
        mode=mode,
        url=cfg.url,
        namespace=cfg.namespace,
        revision=remote.revision,
        local_changes=diff_lines(local, target),
        remote_changes=[]
        if mode == "down"
        else diff_lines(_scoped(remote.doc, cfg.sections), target),
        conflicts=conflicts,
        dry_run=dry_run,
        dropped_profiles=_dropped_profiles(local, target),
    )
    if dry_run:
        return result

    if result.local_changes:
        apply_subset(target, cfg.sections)
        # A pulled profile is only usable once it has a CLAUDE_CONFIG_DIR.
        bootstrap.reconcile()

    revision = remote.revision
    if mode != "down" and result.remote_changes:
        try:
            saved = client.push(target, remote.revision)
        except RevisionConflict as exc:
            saved, target, result = _retry_after_conflict(
                cfg, client, exc, mode, prefer, local, base, result
            )
        revision = saved.revision
        # After a retry the re-merge may leave nothing to push, so report what
        # actually happened rather than assuming the push went out.
        result.pushed = bool(result.remote_changes)
    result.revision = revision
    save_base(cfg, revision, target)
    return result


def _retry_after_conflict(
    cfg: SyncConfig,
    client: SyncClient,
    conflict: RevisionConflict,
    mode: str,
    prefer: str,
    local: Dict[str, Any],
    base: Base,
    result: SyncResult,
) -> Tuple[RemoteDoc, Dict[str, Any], SyncResult]:
    """Another machine pushed while we were merging — redo the merge on top of it.

    Retried once. ``up`` still means "local wins", so it simply re-pushes on the
    newer revision; ``merge`` folds the newcomer's changes in properly. A second
    conflict means the document is being written faster than we can merge, and
    the error propagates rather than looping.
    """
    newer = conflict.remote
    # The 409 body carries the winning document, but only a fetch is guaranteed
    # to be authoritative if the server omitted it.
    if not newer.doc and newer.revision == 0:
        newer = client.fetch()
    if mode == "up":
        target = copy.deepcopy(local)
        conflicts: List[Conflict] = []
    else:
        target, conflicts = three_way_merge(
            _scoped(base.doc, cfg.sections),
            local,
            _scoped(newer.doc, cfg.sections),
            prefer=prefer,
        )
    result.conflicts = conflicts
    result.retried = True
    result.local_changes = diff_lines(local, target)
    result.remote_changes = diff_lines(_scoped(newer.doc, cfg.sections), target)
    result.dropped_profiles = _dropped_profiles(local, target)
    if result.local_changes:
        apply_subset(target, cfg.sections)
        bootstrap.reconcile()
    if not result.remote_changes:
        return newer, target, result
    return client.push(target, newer.revision), target, result


def _dropped_profiles(before: Dict[str, Any], after: Dict[str, Any]) -> List[str]:
    """Profile names that ``before`` declared and ``after`` no longer does.

    Read from the documents rather than parsed back out of the diff lines: a
    profile name may itself contain dots, which makes ``- profiles.a.b``
    ambiguous as text.
    """
    old = before.get("profiles")
    new = after.get("profiles")
    if not isinstance(old, dict):
        return []
    kept = new if isinstance(new, dict) else {}
    return sorted(set(old) - set(kept))


def _scoped(doc: Dict[str, Any], sections: Sequence[str]) -> Dict[str, Any]:
    """Restrict a document to the synced sections (and drop reserved keys).

    Applied to both server and base documents: a server whose document carries
    sections this machine does not sync must not have them leak into the merge.
    """
    return {
        name: copy.deepcopy(doc[name])
        for name in sections
        if name in doc and name not in RESERVED_SECTIONS
    }
