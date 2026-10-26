"""Request-body routing for providers that take their routing in the body.

Some backends decide *which upstream compute serves a request* from a field in
the request body rather than from the URL or a header. OpenRouter is the case
this exists for: pinning a provider (say CoreWeave) is
``provider: {"order": ["coreweave"], "allow_fallbacks": false}`` **in the JSON
body**, and nothing else does it — a ``:coreweave`` suffix on the model slug is
accepted and silently ignored (the request lands on whatever endpoint the
default router picks), and ``@coreweave`` is rejected as an invalid model id.

The launcher only ever hands the harness *environment variables*, so it cannot
reach the body. The gap is closed by a small local shim: a loopback HTTP proxy
that forwards to the real upstream and merges the configured spec into every
JSON request body. A provider that declares ``routing`` gets its
``ANTHROPIC_BASE_URL`` rewritten to the shim at launch::

    providers:
      openrouter:
        routing:
          order: [coreweave]
          allow_fallbacks: false
        env:
          ANTHROPIC_BASE_URL: "https://openrouter.ai/api/"
          ...

The spec is passed to the upstream verbatim, so every field the backend
understands (``order``, ``only``, ``ignore``, ``sort``, ``allow_fallbacks``,
``max_price``, ...) works without this module knowing the vocabulary.

One shim serves every session that wants the same (upstream, spec) pair, which
a fingerprint over that pair identifies. Sessions launching at the same moment
converge on one shim through an exclusive *start claim* file: whoever takes it
spawns, the rest wait for what it started. (Watching the port instead is not
enough — between binding and answering there is a window where a shim looks
like a stranger, and the next caller starts a duplicate beside it.) A changed
spec is a different fingerprint, so config edits are picked up by the next
launch instead of being served stale by a running shim. The shim's own
request protocol (:data:`SHIM_PROTOCOL`) is part of the fingerprint too: a
shim still running code from before an upgrade answers health for its old
fingerprint, so it is never handed to a launch that speaks the new protocol.

The same shim carries one more provider-declared behaviour: sticky session
affinity. Backends whose prompt cache lives on a single replica (Fireworks
serverless is the case this exists for — see their prompt-caching guide, which
recommends a stable ``user``/``x-session-affinity`` key) serve a session's
requests from cache only while those requests keep reaching that replica. A
provider that declares ``session_affinity: true`` gets the calling session's
name forwarded as the ``x-session-affinity`` header (see :data:`AFFINITY_HEADER`).

The flag is *not* part of the shim's identity. A shim is started with the
provider's name (``--provider``) and reads ``session_affinity`` from the live
config on every request (:func:`store.load` serves at most 100ms-stale state),
so toggling the flag in ``~/.claude-launcher/.claunch.yaml`` — including from the web Settings
page — changes what running shims and freshly launched sessions do without a
daemon restart, in both directions. A malformed flag logs to the shim's
stderr and skips the header rather than breaking traffic.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional

from . import config, metering, store

if TYPE_CHECKING:  # a borrow's lender, for the annotations only
    from .profile import Profile

#: Per-provider config key holding the body spec (``providers.<name>.routing``).
SPEC_KEY = "routing"

#: Per-provider config key enabling the sticky session-affinity header
#: (``providers.<name>.session_affinity``). Backends whose prompt cache lives
#: on one replica (Fireworks serverless, see their prompt-caching guide)
#: recommend a stable ``user``/``x-session-affinity`` key so a session's
#: requests keep reaching the replica that holds its cache.
AFFINITY_KEY = "session_affinity"

#: Header the shim adds when affinity is enabled. The session name rides in on
#: the base URL's ``/s/<session>`` prefix (:data:`SESSION_PATH_PREFIX`); the
#: shim forwards it under this name so the upstream can route stickily.
AFFINITY_HEADER = "x-session-affinity"

#: Per-provider config key naming a header the upstream *requires* to carry a
#: session id (``providers.<name>.session_header``). OpenCode Go
#: (``https://opencode.ai/zen/go/v1``) answers 400 ``MissingSessionID`` to any
#: request without ``x-opencode-session``. Unlike :data:`AFFINITY_KEY` this is
#: not an optimisation the provider may do without, so a provider that declares
#: it always goes through the shim (see :func:`front`).
SESSION_HEADER_KEY = "session_header"

#: The id sent under :data:`SESSION_HEADER_KEY` for a request that carries no
#: session name -- a plain ``claunch run`` or ``claunch validate``, launched
#: outside the daemon. The header is mandatory for such a provider, so it is
#: sent anyway; these launches share one id.
SESSION_HEADER_FALLBACK = "claunch"

#: Per-provider config key naming the header the upstream reads an Anthropic
#: Messages credential from (``providers.<name>.anthropic_auth_header``).
#: Claude Code is handed its token as ``ANTHROPIC_AUTH_TOKEN``, which it sends
#: as ``Authorization: Bearer``; OpenCode Go answers that on ``/v1/messages``
#: with 401 ``Go requires a credential`` and accepts only ``x-api-key`` there,
#: while its ``/v1/chat/completions`` accepts only the bearer. So the shim
#: moves the credential for Anthropic Messages paths alone.
ANTHROPIC_AUTH_KEY = "anthropic_auth_header"

#: The values :data:`ANTHROPIC_AUTH_KEY` accepts. ``authorization`` is the
#: default (the bearer goes out as sent) and only exists to be written down.
ANTHROPIC_AUTH_HEADERS = ("authorization", "x-api-key")

#: Body field the spec is merged into. OpenRouter's name for it.
BODY_FIELD = "provider"

#: Shim-only endpoints, namespaced so they cannot collide with an upstream path.
HEALTH_PATH = "/__claunch__/routing"
SHUTDOWN_PATH = "/__claunch__/shutdown"

#: Marker every shim health response carries, so a stranger listening on the
#: derived port is never mistaken for one of ours.
MARKER = "claunch-routing-shim"

#: What a launch expects the shim to understand, beyond the upstream's own API.
#: Bump it whenever a shim from the previous code would answer a request from
#: the current launcher wrongly. It is hashed into :func:`fingerprint`, so a
#: running shim from older code (it keeps the code it was started with across
#: an upgrade) no longer matches and :func:`reconcile` retires it instead of a
#: launch reusing it. 2: the session name arrives as the ``/s/<session>/`` path
#: prefix; a protocol-1 shim answers that path with 404.
SHIM_PROTOCOL = 2

# Ports are derived from the fingerprint (see module docstring). The window sits
# in the registered range, below the ephemeral ports both Windows and Linux hand
# out for outbound sockets — a shim must not lose its port to a random connect.
_PORT_BASE = 31500
_PORT_SPAN = 4000
_PORT_TRIES = 8

#: How long to wait for a freshly spawned shim to answer its health endpoint.
START_TIMEOUT = 15.0

#: How long to wait for a shim asked to shut down to release its port.
STOP_TIMEOUT = 5.0


class RoutingError(Exception):
    """Raised for a malformed ``routing`` block or a shim that will not start."""


class ShimUnavailable(RoutingError):
    """A process other than the daemon needed a shim, none was running, and
    the daemon could not be asked to start one. Never answered by launching
    against the upstream directly (claunch-19liw.2 decision): the caller
    fails instead."""


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #
def spec(provider_name: str, doc: Optional[dict] = None) -> Optional[Dict]:
    """The routing spec declared on ``provider_name``, or ``None``."""
    doc = store.load() if doc is None else doc
    section = doc.get("providers")
    if not isinstance(section, dict):
        return None
    entry = section.get(provider_name)
    if not isinstance(entry, dict):
        return None
    block = entry.get(SPEC_KEY)
    if block is None:
        return None
    if not isinstance(block, dict) or not block:
        raise RoutingError(
            f"providers.{provider_name}.{SPEC_KEY} must be a non-empty mapping "
            f"(got {type(block).__name__})"
        )
    return dict(block)


def configured(doc: Optional[dict] = None) -> Dict[str, Dict]:
    """Every provider that declares a routing spec, name -> spec."""
    doc = store.load() if doc is None else doc
    section = doc.get("providers")
    out: Dict[str, Dict] = {}
    if not isinstance(section, dict):
        return out
    for name in section:
        block = spec(str(name), doc)
        if block:
            out[str(name)] = block
    return out


def session_affinity(provider_name: str, doc: Optional[dict] = None) -> bool:
    """Whether ``provider_name`` declared the sticky session-affinity header.

    Absent key is off. A present non-bool is a config error, not something to
    guess at — the launch fails so a typo never silently drops the header.
    """
    doc = store.load() if doc is None else doc
    section = doc.get("providers")
    if not isinstance(section, dict):
        return False
    entry = section.get(provider_name)
    if not isinstance(entry, dict):
        return False
    value = entry.get(AFFINITY_KEY, False)
    if not isinstance(value, bool):
        raise RoutingError(
            f"providers.{provider_name}.{AFFINITY_KEY} must be a boolean "
            f"(got {type(value).__name__})"
        )
    return value


def session_header(provider_name: str, doc: Optional[dict] = None) -> Optional[str]:
    """The header name ``provider_name`` requires a session id under, or ``None``.

    Absent key is off. A present value that is not a header name is a config
    error, same policy as :func:`session_affinity`.
    """
    doc = store.load() if doc is None else doc
    section = doc.get("providers")
    if not isinstance(section, dict):
        return None
    entry = section.get(provider_name)
    if not isinstance(entry, dict):
        return None
    value = entry.get(SESSION_HEADER_KEY)
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9-]+", value):
        raise RoutingError(
            f"providers.{provider_name}.{SESSION_HEADER_KEY} must be a header "
            f"name such as x-opencode-session (got {value!r})"
        )
    return value


def anthropic_auth_header(provider_name: str, doc: Optional[dict] = None) -> Optional[str]:
    """``"x-api-key"`` when ``provider_name`` wants the Messages credential
    moved out of ``Authorization``, else ``None``.

    Absent key (or ``authorization``) is off. Any other value is a config
    error, same policy as :func:`session_affinity`.
    """
    doc = store.load() if doc is None else doc
    section = doc.get("providers")
    if not isinstance(section, dict):
        return None
    entry = section.get(provider_name)
    if not isinstance(entry, dict):
        return None
    value = entry.get(ANTHROPIC_AUTH_KEY)
    if value is None:
        return None
    if not isinstance(value, str) or value.lower() not in ANTHROPIC_AUTH_HEADERS:
        raise RoutingError(
            f"providers.{provider_name}.{ANTHROPIC_AUTH_KEY} must be one of "
            f"{', '.join(ANTHROPIC_AUTH_HEADERS)} (got {value!r})"
        )
    return "x-api-key" if value.lower() == "x-api-key" else None


def requires_shim(provider_name: str, doc: Optional[dict] = None) -> bool:
    """Whether the upstream refuses requests a harness sends unaided, so
    the provider must go through the shim even with metering off."""
    return bool(
        session_header(provider_name, doc)
        or anthropic_auth_header(provider_name, doc)
    )


def apply_session_header(
    headers,
    provider_name: Optional[str],
    *,
    session: Optional[str] = None,
    doc: Optional[dict] = None,
) -> None:
    """Send ``session`` under the provider's required session header.

    The value is ``session`` when the caller has one and
    :data:`SESSION_HEADER_FALLBACK` otherwise; a caller that set the header
    itself keeps its value. Raises :class:`RoutingError` for a malformed
    ``providers.<name>.session_header`` (see :func:`session_header`).
    """
    if not provider_name:
        return
    doc = store.load() if doc is None else doc
    required = session_header(provider_name, doc)
    if required and not _header_sent(headers, required):
        headers[required] = session or SESSION_HEADER_FALLBACK


def apply_anthropic_auth(
    headers,
    provider_name: Optional[str],
    *,
    path: str = "",
    doc: Optional[dict] = None,
) -> None:
    """Move the Messages credential where the provider reads it.

    On an Anthropic Messages path (any ``path`` containing ``/messages``) an
    ``Authorization: Bearer`` credential goes out as ``x-api-key`` when the
    provider declares ``anthropic_auth_header: x-api-key``; the OpenAI
    protocol (``/chat/completions``) keeps the bearer. Raises
    :class:`RoutingError` for a malformed value.
    """
    if not provider_name:
        return
    doc = store.load() if doc is None else doc
    if "/messages" in path and anthropic_auth_header(provider_name, doc):
        _move_bearer_to_api_key(headers)


def _header_sent(headers, name: str) -> bool:
    """Whether ``headers`` already carries ``name``, case-insensitively.

    ``CIMultiDict`` does this by itself; a plain dict the caller hands in does
    not, and the two callers of these rules use one each (the shim passes a
    ``CIMultiDict``, the briefing call a dict).
    """
    wanted = name.lower()
    return any(str(key).lower() == wanted for key in headers.keys())


def _header_value(headers, name: str):
    """The value ``headers`` holds under ``name``, case-insensitively."""
    wanted = name.lower()
    for key in headers.keys():
        if str(key).lower() == wanted:
            return headers[key]
    return None


def _move_bearer_to_api_key(headers) -> None:
    """Move an ``Authorization: Bearer`` credential to ``x-api-key``.

    A caller that already sent ``x-api-key`` keeps it as sent.
    """
    auth = _header_value(headers, "Authorization")
    if not auth or _header_sent(headers, "x-api-key"):
        return
    scheme, _, token = auth.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return
    try:
        headers.popall("Authorization", None)  # CIMultiDict
    except AttributeError:
        for key in [k for k in headers.keys() if str(k).lower() == "authorization"]:
            del headers[key]
    headers["x-api-key"] = token.strip()


#: Per-provider config key switching the shim to live resolution
#: (``providers.<name>.dynamic``). A dynamic provider's shim asks the config
#: file for the upstream URL, the api key and the model alias table on every
#: request, so changing ``profiles.<name>.provider`` (or the provider's own
#: endpoints/models) reaches already-running sessions without a relaunch.
DYNAMIC_KEY = "dynamic"


def dynamic(provider_name: str, doc: Optional[dict] = None) -> bool:
    """Whether ``provider_name`` resolves upstream and models per request.

    Absent key is off (the historical launch-time behaviour). A present
    non-bool is a config error, same policy as :func:`session_affinity`.
    """
    doc = store.load() if doc is None else doc
    section = doc.get("providers")
    if not isinstance(section, dict):
        return False
    entry = section.get(provider_name)
    if not isinstance(entry, dict):
        return False
    value = entry.get(DYNAMIC_KEY, False)
    if not isinstance(value, bool):
        raise RoutingError(
            f"providers.{provider_name}.{DYNAMIC_KEY} must be a boolean "
            f"(got {type(value).__name__})"
        )
    return value


def set_spec(provider_name: str, block: Optional[Dict]) -> None:
    """Write (or, with ``None``, drop) a provider's routing spec."""
    if block is not None and (not isinstance(block, dict) or not block):
        raise RoutingError("a routing spec must be a non-empty mapping")

    def _mutate(doc: dict) -> None:
        section = doc.get("providers")
        if not isinstance(section, dict):
            raise RoutingError(
                f"unknown provider {provider_name!r} (see 'claunch providers')"
            )
        entry = section.get(provider_name)
        if not isinstance(entry, dict):
            raise RoutingError(
                f"unknown provider {provider_name!r} (see 'claunch providers')"
            )
        if block is None:
            entry.pop(SPEC_KEY, None)
        else:
            entry[SPEC_KEY] = dict(block)

    store.update(_mutate)


# --------------------------------------------------------------------------- #
# the pure part: what the shim does to a body
# --------------------------------------------------------------------------- #
def canonical(block: Dict) -> str:
    """Stable JSON for a spec — the fingerprint and the merge both use it."""
    return json.dumps(block, sort_keys=True, separators=(",", ":"))


def fingerprint(upstream: str, block: Dict, route: Optional[str] = None) -> str:
    """Identity of one shim: what it fronts and the spec it merges.

    An empty spec is a valid identity — the metering-only shim for that
    upstream (see :mod:`metering`). Provider-declared *behaviour* (the
    session-affinity header) is deliberately not part of the identity: the
    shim reads the live flag per request, so one shim serves a provider in
    both flag states and a toggle reaches running traffic immediately.

    A ``route`` key names a dynamic provider instead of a concrete upstream.
    The shim then resolves the upstream from the live config per request, so
    swapping the profile's provider keeps one shim (and one port, one meter)
    for the route — the fingerprint must not see the upstream it would
    otherwise hash.
    """
    if route is not None:
        subject = f"route:{route}"
    else:
        subject = upstream
    return hashlib.sha256(
        f"{subject}\n{canonical(block)}\nprotocol:{SHIM_PROTOCOL}".encode("utf-8")
    ).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# dynamic routes: per-request upstream/auth/model resolution
# --------------------------------------------------------------------------- #
#: Route tokens name what a dynamic shim re-resolves per request. A
#: ``profile:<name>`` route follows the profile's *current* provider, so
#: ``claunch set-provider <profile> <other>`` reaches already-running
#: sessions; a ``provider:<name>`` route sticks to one provider's config
#: (the pi path, or any caller without profile context).
PROFILE_ROUTE_PREFIX = "profile:"
PROVIDER_ROUTE_PREFIX = "provider:"


def _split_route(route: str, doc: Optional[dict] = None):
    """``(provider_entry, profile_entry_or_None, profile_or_None)`` for a token."""
    if route.startswith(PROFILE_ROUTE_PREFIX):
        from . import profile as profile_mod

        name = route[len(PROFILE_ROUTE_PREFIX):]
        try:
            prof = profile_mod.require(name)
        except profile_mod.ProfileError as exc:
            raise RoutingError(str(exc)) from None
        from . import providers  # late: providers pulls translators

        provider_name = providers.resolve_name(prof, doc)
        section = doc.get("providers") if isinstance(doc, dict) else None
        entry = section.get(provider_name) if isinstance(section, dict) else None
        if not isinstance(entry, dict):
            raise RoutingError(
                f"profile {name!r} resolves to unknown provider "
                f"{provider_name!r} (see 'claunch providers')"
            )
        profiles = doc.get("profiles") if isinstance(doc, dict) else None
        profile_entry = profiles.get(name) if isinstance(profiles, dict) else None
        return entry, (profile_entry if isinstance(profile_entry, dict) else None), prof
    if route.startswith(PROVIDER_ROUTE_PREFIX):
        name = route[len(PROVIDER_ROUTE_PREFIX):]
        section = doc.get("providers") if isinstance(doc, dict) else None
        entry = section.get(name) if isinstance(section, dict) else None
        if not isinstance(entry, dict):
            raise RoutingError(f"unknown provider {name!r} (see 'claunch providers')")
        return entry, None, None
    # Bare token: a provider name (route shims started by older builds).
    section = doc.get("providers") if isinstance(doc, dict) else None
    entry = section.get(route) if isinstance(section, dict) else None
    if not isinstance(entry, dict):
        raise RoutingError(f"unknown provider {route!r} (see 'claunch providers')")
    return entry, None, None


def route_upstream(route: str, doc: Optional[dict] = None) -> str:
    """The anthropic endpoint a dynamic route points at right now.

    Read from the live config on every request, so editing the provider (or
    moving a profile onto another provider with ``claunch set-provider``)
    reaches running sessions without a relaunch.
    """
    doc = store.load() if doc is None else doc
    entry, _, _ = _split_route(route, doc)
    endpoints = entry.get("endpoints")
    url = (
        str(endpoints.get("anthropic") or "").strip()
        if isinstance(endpoints, dict)
        else ""
    )
    if not url:
        raise RoutingError(
            f"the provider behind route {route!r} sets no "
            "endpoints.anthropic; the dynamic route has nowhere to forward"
        )
    return url


def route_models(route: str, doc: Optional[dict] = None) -> Dict[str, str]:
    """The model alias table a dynamic route resolves against right now.

    A ``profile:`` route overlays the profile's own ``models:`` pins on the
    provider's table, matching the launch-time layering.
    """
    doc = store.load() if doc is None else doc
    entry, profile_entry, _ = _split_route(route, doc)
    out: Dict[str, str] = {}
    for raw in (entry.get("models"), (profile_entry or {}).get("models")):
        if isinstance(raw, dict):
            out.update(
                {str(k): str(v) for k, v in raw.items() if str(v).strip()}
            )
    return out


def resolve_model_alias(model: str, table: Dict[str, str]) -> str:
    """``model`` as the route's alias table reads it.

    The wire vocabulary is Claude's: role words (``small``...), alias words
    (``haiku``/``sonnet``/``opus``/``fable``) and Claude's own ids
    (``claude-opus-5``...). Resolution order:

    1. an exact table key (a table may pin full ids too);
    2. a ``claude-<alias>`` id normalized to its alias word;
    3. a role word followed through :data:`ROLE_FALLBACKS`;
    4. anything else passes through unchanged.

    Passthrough keeps every request a live session was already making valid
    after a provider swap, and leaves concrete ids (pi, codex) alone.
    """
    from . import provider_spec

    name = str(model or "").strip()
    if not name:
        return name
    hit = table.get(name)
    if hit:
        return hit
    for alias in provider_spec.CLAUDE_ALIAS_VARS:
        if name == f"claude-{alias}" or name.startswith(f"claude-{alias}-"):
            hit = table.get(alias)
            if hit:
                return hit
            break
    role = name
    seen = set()
    while role in provider_spec.MODEL_ROLES and role not in seen:
        seen.add(role)
        hit = table.get(role)
        if hit:
            return hit
        nxt = provider_spec.ROLE_FALLBACKS.get(role)
        if not nxt:
            break
        role = nxt
    # A role word the table never named lands on the default model — the
    # same place the role vocabulary points when a profile declares nothing
    # for it (see :meth:`ProviderSpec.model`'s fallbacks).
    if role and name in provider_spec.MODEL_ROLES:
        return table.get("default", name)
    return name


def route_auth(route: str, session: Optional[str], doc: Optional[dict] = None) -> Optional[str]:
    """The api key a dynamic route injects right now, or ``None``.

    Order: a ``profile:`` route tries the profile's stored ``set-token``
    secret first (the runner's precedence: a stored token beats a plaintext
    config key), then the provider's declared ``api_key``; a bare provider
    route has only the latter. ``None`` means "resolve nothing": the shim
    forwards the caller's own credentials, which then fail against the new
    upstream exactly as they would have directly.
    """
    doc = store.load() if doc is None else doc
    entry, _, prof = _split_route(route, doc)
    if prof is not None:
        try:
            from . import lineage

            stored = lineage.stored_auth_token(prof)
        except Exception:  # noqa: BLE001 - auth fallback must never break traffic
            stored = None
        if stored:
            return stored
    key = str(entry.get("api_key") or "").strip()
    if key:
        return key
    if prof is not None or not session:
        return None
    # A bare provider route with a known session: the session's profile's
    # stored secret is the same fallback the runner would use at launch.
    try:
        from .daemon import db as session_db
        from . import lineage, profile as profile_mod

        for record in session_db.open_default().load_all():
            if str(record.get("name") or "") != session:
                continue
            selector = str((record.get("def") or {}).get("profile") or "")
            if not selector:
                return None
            try:
                token_profile = profile_mod.require_selector(selector)
            except profile_mod.ProfileError:
                return None
            return lineage.stored_auth_token(token_profile)
    except Exception:  # noqa: BLE001 - auth fallback must never break traffic
        return None
    return None


def merge_body_model(raw: bytes, block: Dict, table: Dict[str, str]) -> bytes:
    """``raw`` with the routing spec merged in and the model alias resolved.

    One parse serves both rewrites. A body that already carries the routing
    field is left alone there; the model field is rewritten whenever the
    alias table has an answer, because the caller cannot know the table.
    Non-JSON bodies pass through unchanged, same as :func:`merge_body`.
    """
    if not raw or (not block and not table):
        return raw
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return raw
    if not isinstance(doc, dict):
        return raw
    if block and BODY_FIELD not in doc:
        doc[BODY_FIELD] = json.loads(canonical(block))
    model = doc.get("model")
    if isinstance(model, str) and table:
        resolved = resolve_model_alias(model, table)
        if resolved != model:
            doc["model"] = resolved
    return json.dumps(doc).encode("utf-8")


def merge_body(raw: bytes, block: Dict) -> bytes:
    """``raw`` with ``block`` merged in as the routing field.

    Anything that is not a JSON object is returned untouched, so the shim stays
    a transparent proxy for uploads, form posts and malformed bodies alike. A
    body that already carries the field is left alone: an explicit choice by
    whoever built the request outranks the launcher's default.
    """
    if not raw or not block:
        return raw
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return raw
    if not isinstance(doc, dict) or BODY_FIELD in doc:
        return raw
    doc[BODY_FIELD] = json.loads(canonical(block))
    return json.dumps(doc).encode("utf-8")


# --------------------------------------------------------------------------- #
# shim discovery and lifecycle
# --------------------------------------------------------------------------- #
def state_dir() -> Path:
    """Where running shims record themselves (per-machine runtime state)."""
    return config.launcher_home() / "routing"


def log_file(fp: str) -> Path:
    return state_dir() / f"{fp}.log"


def _record_file(fp: str, port: int) -> Path:
    """One record per *shim*, not per fingerprint.

    Keying on the fingerprint alone loses shims: if two ever end up serving the
    same pair (a crash between spawn and claim release, an orphan left by an
    older build), the second record overwrites the first and ``stop`` can no
    longer reach what it cannot see. A leaked proxy holding a port is not
    something to leave for the next person to find.
    """
    return state_dir() / f"{fp}-{port}.json"


def candidate_ports(fp: str) -> List[int]:
    """The ports a shim with this fingerprint may occupy, in preference order."""
    start = _PORT_BASE + int(fp[:8], 16) % _PORT_SPAN
    return [start + i for i in range(_PORT_TRIES)]


def local_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/"


def is_shim_url(url: Optional[str]) -> bool:
    """Whether ``url`` is one :func:`local_url` could have produced.

    A loopback URL on a port inside the shim window. Only the daemon uses it,
    to decide whether a session's ``/s/<session>`` prefix has a shim to
    read it — a user's own loopback upstream on another port is left alone.
    """
    if not url or not url.startswith("http://127.0.0.1:"):
        return False
    rest = url[len("http://127.0.0.1:"):]
    port_text = rest.split("/", 1)[0]
    if not port_text.isdigit():
        return False
    port = int(port_text)
    return _PORT_BASE <= port < _PORT_BASE + _PORT_SPAN + _PORT_TRIES


#: Path prefix that names the calling session to a shim: a session's base URL
#: is ``http://127.0.0.1:<port>/s/<session>`` and the harness appends its own
#: path (``/v1/messages`` for Claude Code, ``/chat/completions`` for Pi). The
#: shim takes the name off and forwards the rest. It replaces the
#: ``X-Claunch-Session`` header the daemon used to put into
#: ``ANTHROPIC_CUSTOM_HEADERS``/``CLAUNCH_PI_HEADERS``: a header variable is
#: inherited by every process the session starts, including a daemon, and
#: from there by unrelated sessions (claunch-19liw.1). A base URL is a backend
#: key (:data:`runner.BACKEND_ENV_KEYS`), dropped from the inherited env and
#: recomputed on every launch (claunch-19liw.2.4).
SESSION_PATH_PREFIX = "/s/"

#: The env keys a harness reads its base URL from: Claude Code's, and Pi's
#: (``pi_provider.ENV_BASE_URL``, spelled out here because ``pi_provider``
#: imports this module).
BASE_URL_ENV_KEYS = ("ANTHROPIC_BASE_URL", "CLAUNCH_PI_BASE_URL")


def split_session_path(raw_path: str):
    """``(session, rest)`` of a request path that arrived at a shim.

    ``/s/s1/v1/messages`` is ``("s1", "/v1/messages")``. A path without the
    prefix (an older session still sending the header) comes back as
    ``(None, raw_path)``.
    """
    if not raw_path.startswith(SESSION_PATH_PREFIX):
        return None, raw_path
    name, _, tail = raw_path[len(SESSION_PATH_PREFIX):].partition("/")
    if not name:
        return None, raw_path
    return urllib.parse.unquote(name), "/" + tail


def session_url(url: str, session: str) -> str:
    """Shim base URL ``url`` with ``session``'s path prefix.

    A prefix already there is replaced, so applying it twice (or for a
    restored session under a new name) leaves one. A trailing slash on
    ``url`` is kept: Claude's base URL has one, Pi's does not.
    """
    parts = urllib.parse.urlsplit(url)
    _, rest = split_session_path(parts.path or "/")
    prefix = SESSION_PATH_PREFIX + urllib.parse.quote(session, safe="")
    path = prefix + (rest if rest != "/" else ("/" if url.endswith("/") else ""))
    return urllib.parse.urlunsplit(parts._replace(path=path))


def apply_session(env: dict, session: Optional[str]) -> None:
    """Name ``session`` in the base URL of every harness in ``env`` that talks
    to a shim. A base URL that is not a shim is left alone; so is everything
    when there is no session."""
    if not session:
        return
    for key in BASE_URL_ENV_KEYS:
        if is_shim_url(env.get(key)):
            env[key] = session_url(env[key], session)
            if _OWNER:
                # The daemon keeps the shim up while this session lives
                # (:func:`reconcile`).
                add_user(_port_of(env[key]), session=session)


def health(port: int, timeout: float = 0.6) -> Optional[dict]:
    """Ask whatever listens on ``port`` whether it is a claunch shim."""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}{HEALTH_PATH}", timeout=timeout
        ) as resp:
            info = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if not isinstance(info, dict) or info.get("marker") != MARKER:
        return None
    return info


def _port_free(port: int) -> bool:
    sock = socket.socket()
    if os.name != "nt":
        # Bind as the shim's server does (asyncio sets SO_REUSEADDR on POSIX):
        # every health probe the shim answers leaves a TIME_WAIT on its port,
        # and without this the port reads as taken for a minute after the
        # shim exits. On Windows the option would bind over a live listener.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _shim_env() -> dict:
    """Child env that pins the shim to *this* checkout's source tree.

    The launcher frequently runs out of a git worktree while an editable
    install points at the main checkout; ``-m claude_launcher.routing_shim``
    would then load the other tree's code. Prepending this package's parent to
    ``PYTHONPATH`` makes the shim always the sibling of the code that spawned
    it — which is also what lets the test suite exercise a real shim process.
    """
    env = {
        k: v
        for k, v in os.environ.items()
        # The shim proxies whatever credentials the caller sends and needs none
        # of its own; a long-lived process should not hold the token as well.
        if not k.startswith(("ANTHROPIC_", "CLAUDE_CODE_"))
    }
    pkg_parent = str(Path(__file__).resolve().parent.parent)
    parts = [pkg_parent] + [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p]
    env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(parts))
    return env


def _spawn(upstream: str, block: Dict, port: int, fp: str, provider: Optional[str],
           route: Optional[str] = None) -> "subprocess.Popen":
    """Start a detached shim process; its output goes to the fingerprint's log."""
    state_dir().mkdir(parents=True, exist_ok=True)
    log = open(log_file(fp), "ab")
    argv = [
        sys.executable,
        "-m",
        "claude_launcher.routing_shim",
        "--upstream", upstream,
        "--spec", canonical(block),
        "--port", str(port),
        "--fingerprint", fp,
    ]
    if provider:
        argv.extend(["--provider", provider])
    if route:
        argv.extend(["--route", route])
    kwargs: dict = {}
    if sys.platform == "win32":
        CREATE_NO_WINDOW = 0x08000000
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        kwargs["creationflags"] = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        return subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            close_fds=True,
            env=_shim_env(),
            **kwargs,
        )
    finally:
        log.close()


def _record(fp: str, port: int, upstream: str, block: Dict, pid,
            route: Optional[str] = None) -> None:
    state_dir().mkdir(parents=True, exist_ok=True)
    path = _record_file(fp, port)
    with _LOCK:
        _write_record(path, fp, port, upstream, block, pid, route)


def _write_record(path: Path, fp: str, port: int, upstream: str, block: Dict,
                  pid, route: Optional[str]) -> None:
    previous = _read_json(path)
    _write_atomic(
        path,
        json.dumps(
            {
                "fingerprint": fp,
                "port": port,
                "upstream": upstream,
                "spec": block,
                "pid": pid if pid is not None else previous.get("pid"),
                "url": local_url(port),
                "route": route,
                # When this process last handed the shim out (every
                # ensure_shim rewrites the record): :func:`reconcile` keeps a
                # shim for :data:`GRACE_S` after that, so a launch between
                # ensuring the shim and registering its session
                # (:func:`add_user`) is not raced.
                "ensured": time.time(),
            },
            indent=2,
            sort_keys=True,
        ),
    )


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _start_diagnosis(child, port: int) -> str:
    """What is known about a shim that never answered: whether its process is
    still running (and its exit code if not), whether the port is bound, and
    what a health request gets now. Said in the error so a timeout that was
    the machine's load and one that was a dead process read differently."""
    code = child.poll() if child is not None else None
    state = "process not tracked" if child is None else (
        f"process {child.pid} still running" if code is None
        else f"process {child.pid} exited with code {code}")
    health_now = health(port, timeout=1.0)
    return (f"{state}; port {port} {'free' if _port_free(port) else 'bound'}; "
            f"health now {'answers' if health_now else 'silent'}")


def _log_tail(fp: str, lines: int = 12) -> str:
    try:
        text = log_file(fp).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.strip().splitlines()[-lines:])


def _claim_file(fp: str) -> Path:
    return state_dir() / f"{fp}.claim"


def _try_claim(fp: str) -> bool:
    """Win the right to *start* this fingerprint's shim, or report that another
    process already holds it.

    Testing the port is not enough on its own: between a shim binding its socket
    and answering its health endpoint there is a window where the port reads as
    busy but no shim answers, and a second caller would take that for a stranger
    and start a duplicate on the next port. An exclusive file makes the start
    itself the thing that is claimed, so exactly one process spawns and the rest
    wait for it.
    """
    state_dir().mkdir(parents=True, exist_ok=True)
    path = _claim_file(fp)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:  # it went away underneath us — the holder finished
            return _try_claim(fp)
        if age < START_TIMEOUT + 5:
            return False
        # Older than any start could possibly be: the holder died mid-spawn.
        path.unlink(missing_ok=True)
        return _try_claim(fp)
    try:
        os.write(fd, str(os.getpid()).encode("ascii"))
    finally:
        os.close(fd)
    return True


def _find(fp: str) -> Optional[int]:
    """The port a live shim for ``fp`` answers on, if one is up.

    A port nobody listens on is skipped by a bind probe before any HTTP
    request goes out. On Windows a connect to a closed loopback port does
    not fail fast -- it sits out ``health``'s whole timeout -- so probing
    all ``_PORT_TRIES`` candidates over HTTP cost ~5s on every launch that
    had no shim to find (measured: 4.9s of a 5.5s ``ensure_shim``).
    """
    for port in candidate_ports(fp):
        if _port_free(port):
            continue  # nothing listens there; no request to make
        info = health(port)
        # A shim told to shut down (:func:`retire`) is skipped: handing it out
        # would register a session on a shim about to go away.
        if info is not None and info.get("fingerprint") == fp \
                and not info.get("stopping"):
            return port
    return None


def _reuse(fp: str, upstream: str, block: Dict, route: Optional[str]) -> Optional[str]:
    """The URL of a running shim for ``fp``, stamped as handed out.

    Found and stamped under :data:`_LOCK`, the lock :func:`reconcile` takes
    for its last look and the retire request. Otherwise a pass could retire
    the shim between the two (it would see the old "ensured" stamp) and the
    launch would get the URL of a shim that is about to stop.
    """
    with _LOCK:
        port = _find(fp)
        if port is None:
            return None
        _record(fp, port, upstream, block, None, route=route)
        return local_url(port)


def _free_port(fp: str) -> Optional[int]:
    for port in candidate_ports(fp):
        if _port_free(port):
            return port
    return None


def ensure_shim(upstream: str, block: Dict, provider: Optional[str] = None,
                route: Optional[str] = None) -> str:
    """Base URL of a live shim fronting ``upstream`` with ``block`` merged in.

    Only the daemon starts shims (:func:`own_lifecycle`, claunch-19liw.2.3):
    in any other process this asks the daemon for the shim
    (:func:`_ensure_via_daemon`) and raises :class:`ShimUnavailable` when
    neither a running shim nor the daemon is there. Below is the daemon's
    half.

    Reuses a running shim for the same pair and starts one otherwise. Callers
    that arrive together converge on a single shim: one of them takes the start
    claim and the others wait for what it starts. ``provider`` is the config
    name of the provider this shim fronts; the shim reads that provider's
    session-affinity flag from the live config per request, so a toggle
    reaches running traffic without a restart (see the module docstring).

    ``route`` names a dynamic provider: the fingerprint keys on the route,
    not the upstream, and the shim resolves upstream, credentials and the
    model alias table from the live config on every request.
    """
    if not _OWNER:
        return _ensure_via_daemon(upstream, block, provider, route)
    fp = fingerprint(upstream, block, route=route)
    url = _reuse(fp, upstream, block, route)
    if url is not None:
        return url
    if not _try_claim(fp):
        # Another launch is starting this exact shim right now. Wait for it
        # rather than adding a second one beside it.
        deadline = time.monotonic() + START_TIMEOUT + 5
        while time.monotonic() < deadline:
            url = _reuse(fp, upstream, block, route)
            if url is not None:
                return url
            time.sleep(0.1)
        raise RoutingError(
            f"another process holds the start claim for the routing shim to "
            f"{upstream} but none came up (stale {_claim_file(fp)}?)"
        )
    try:
        # Look once more now that the claim is ours: a caller whose first
        # ``_find`` ran before a peer's shim bound, and whose claim came after
        # that peer released it, would otherwise start a second shim on the
        # next port (observed as two live shims for one pair under four
        # simultaneous launches).
        url = _reuse(fp, upstream, block, route)
        if url is not None:
            return url
        start_port = _free_port(fp)
        if start_port is None:
            raise RoutingError(
                f"no free loopback port for the routing shim in "
                f"{candidate_ports(fp)[0]}..{candidate_ports(fp)[-1]}"
            )
        child = _spawn(upstream, block, start_port, fp, provider, route=route)
        deadline = time.monotonic() + START_TIMEOUT
        while time.monotonic() < deadline:
            info = health(start_port, timeout=0.5)
            if info is not None and info.get("fingerprint") == fp:
                _record(fp, start_port, upstream, block, info.get("pid"), route=route)
                return local_url(start_port)
            time.sleep(0.1)
        tail = _log_tail(fp)
        raise RoutingError(
            f"routing shim for {upstream} did not come up on port {start_port} "
            f"within {int(START_TIMEOUT)}s (see {log_file(fp)}); "
            f"{_start_diagnosis(child, start_port)}"
            + (("\n" + tail) if tail else "")
        )
    finally:
        _claim_file(fp).unlink(missing_ok=True)


def front(upstream: str, provider_name: str, doc: Optional[dict] = None,
          profile: Optional[str] = None,
          borrow: Optional["Profile"] = None) -> str:
    """The base URL a harness should talk to for ``provider_name``'s ``upstream``.

    Two reasons send a provider through the shim, and they differ in what a
    shim that will not start means:

    * a ``routing`` block — the shim *must* come up: falling back to the
      direct URL would silently drop the pin, which is precisely the failure
      this feature exists to prevent, so the launch fails instead;
    * a ``session_header`` or ``anthropic_auth_header`` — same: the
      upstream refuses what the harness sends unaided (see
      :func:`requires_shim`), so a direct launch would fail on its first
      request;
    * metering (:mod:`metering`, on by default for every provider that is not
      the Anthropic OAuth route) — a record is observability, not
      correctness, so a shim that cannot start is reported on stderr and the
      session launches against the upstream directly.

    ``upstream`` is returned unchanged when neither applies. The claude path
    (:func:`apply`) and the Pi path (``pi_provider.apply_env``) both come
    through here so the two harnesses share one policy and one shim per
    upstream.
    """
    doc = store.load() if doc is None else doc
    block = spec(provider_name, doc)
    if dynamic(provider_name, doc):
        # A dynamic provider resolves its upstream, credentials and model
        # table at the shim per request, so the profile's provider can change
        # mid-run. With profile context the route is keyed on the profile and
        # follows its provider selection live; without, it sticks to this
        # provider's own config. The shim is mandatory — same failure class
        # as a routing pin, not as metering.
        #
        # A borrow keys the route on the *lender* when the lender's provider is
        # the one in play: the shim reads the model table off the route, so a
        # route naming the borrower would resolve the borrower's ``models:``
        # per request — the one table a borrow is supposed to replace. This
        # mirrors :func:`providers.launch_spec`, whose rule is that the table
        # comes from the lender only while the backend is the lender's own.
        #
        # An explicit ``--provider`` swings the backend away from both
        # profiles, and ``provider_name`` already carries that choice (the
        # caller resolves it as ``provider_override or lender's provider``).
        # A ``profile:`` token would override it with the token's own provider
        # (``_split_route`` derives the provider from the named profile), so in
        # that case the route must name the provider — otherwise the override
        # loses upstream *and* credentials to the lender's backend
        # (claunch-75kfl.1 review). Both token forms already exist, so a route
        # on the wire keeps resolving either way.
        # A ``profile:`` token also lets the route follow a live provider
        # change (``claunch set-provider``), which is why it is the form used
        # whenever the profile's own provider is the one in play. A borrow
        # whose provider is not, is on an explicit override: then nothing may
        # re-derive the provider from a profile, so the route names it.
        route = None
        if borrow is not None:
            from . import providers  # late: providers pulls translators

            if providers.resolve_name(borrow, doc) == provider_name:
                route = f"{PROFILE_ROUTE_PREFIX}{borrow.name}"
            else:
                route = f"{PROVIDER_ROUTE_PREFIX}{provider_name}"
        elif profile is not None:
            route = f"{PROFILE_ROUTE_PREFIX}{profile}"
        else:
            route = f"{PROVIDER_ROUTE_PREFIX}{provider_name}"
        return ensure_shim(upstream, block or {}, provider_name, route=route)
    if block or requires_shim(provider_name, doc):
        return ensure_shim(upstream, block or {}, provider_name)
    if not metering.provider_enabled(provider_name, doc):
        return upstream
    try:
        return ensure_shim(upstream, {}, provider_name)
    except ShimUnavailable:
        raise  # outside the daemon: no direct fallback (claunch-19liw.2)
    except RoutingError as exc:
        print(
            f"claunch: metering shim for provider {provider_name!r} unavailable, "
            f"launching against {upstream} directly (no TPS records): {exc}",
            file=sys.stderr,
        )
        return upstream


def apply(env: dict, provider_name: str, doc: Optional[dict] = None,
          profile: Optional[str] = None,
          borrow: Optional["Profile"] = None) -> None:
    """Point ``env``'s ``ANTHROPIC_BASE_URL`` at a shim when ``provider_name``
    goes through one (see :func:`front` for the policy).

    ``default``/``claude`` (the OAuth route) never come here: their env has
    no ``ANTHROPIC_BASE_URL`` from the config file, and that key is the whole
    handle this function has. ``profile`` (a profile name) lets a dynamic
    route follow the profile's provider selection; callers without profile
    context pass ``None`` and the route sticks to the provider. ``borrow``
    (the lender) makes a dynamic route resolve the *lender's* model table per
    request, matching what :func:`providers.launch_spec` does at launch time.
    """
    doc = store.load() if doc is None else doc
    block = spec(provider_name, doc)
    if (
        not block
        and not dynamic(provider_name, doc)
        and not requires_shim(provider_name, doc)
        and not metering.provider_enabled(provider_name, doc)
    ):
        return
    upstream = env.get("ANTHROPIC_BASE_URL")
    if not upstream:
        if block:
            raise RoutingError(
                f"provider {provider_name!r} declares {SPEC_KEY} but sets no "
                "ANTHROPIC_BASE_URL to route to"
            )
        if dynamic(provider_name, doc):
            raise RoutingError(
                f"provider {provider_name!r} is dynamic but sets no "
                "ANTHROPIC_BASE_URL to front"
            )
        return  # nothing to front: the harness talks to its built-in default
    # No "is this already a shim?" guard: ``ANTHROPIC_BASE_URL`` is a backend
    # key, stripped from the inherited environment before any layer is applied
    # (see :data:`runner.BACKEND_ENV_KEYS`), so the value here always comes
    # from the config file. A guard keyed on "looks like loopback" would
    # instead skip the rewrite for a locally hosted upstream — silently
    # dropping the pin, which is the failure this whole module exists to stop.
    env["ANTHROPIC_BASE_URL"] = front(
        upstream, provider_name, doc, profile=profile, borrow=borrow
    )


# --------------------------------------------------------------------------- #
# inspection / teardown (`claunch routing`)
# --------------------------------------------------------------------------- #
def instances() -> List[dict]:
    """Every recorded shim that still answers, newest record state cleaned up."""
    out: List[dict] = []
    try:
        records = sorted(state_dir().glob("*.json"))
    except OSError:
        return out
    for rec in records:
        if rec == _users_file():
            continue  # who uses which shim (add_user), not a shim record
        try:
            info = json.loads(rec.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        port = info.get("port")
        live = health(int(port)) if isinstance(port, int) else None
        if live is None or live.get("fingerprint") != info.get("fingerprint"):
            rec.unlink(missing_ok=True)  # stale record: the shim is gone
            continue
        info["pid"] = live.get("pid", info.get("pid"))
        if "inflight" in live:
            info["inflight"] = live["inflight"]
        out.append(info)
    return out


def stop(fingerprints: Optional[List[str]] = None) -> List[str]:
    """Ask shims to shut down; returns the fingerprints that were stopped."""
    stopped = []
    for info in instances():
        fp = info.get("fingerprint")
        if fingerprints is not None and fp not in fingerprints:
            continue
        req = urllib.request.Request(
            f"http://127.0.0.1:{info['port']}{SHUTDOWN_PATH}", method="POST", data=b""
        )
        try:
            urllib.request.urlopen(req, timeout=5.0).close()
        except (urllib.error.URLError, OSError):
            pass
        # The shim answers the request and *then* unwinds, so wait for the port
        # to actually go quiet: a caller that immediately relaunches must not
        # find the old process still holding it.
        deadline = time.monotonic() + STOP_TIMEOUT
        while time.monotonic() < deadline and health(int(info["port"])) is not None:
            time.sleep(0.05)
        _record_file(str(fp), int(info["port"])).unlink(missing_ok=True)
        stopped.append(str(fp))
    return stopped


# --------------------------------------------------------------------------- #
# the daemon owns the shims (claunch-19liw.2.3)
# --------------------------------------------------------------------------- #
# Shims used to be started by whichever process launched a harness first: a
# session shell's ``claunch run``, a test, the daemon. Nobody stopped them, so
# they piled up (13 measured on 2026-10-22: five for one upstream, two left by
# a test, two running a worktree's code). Now the daemon is the only process
# that starts one, with its own interpreter and code root (``_shim_env``
# follows this module's file), and the only one that stops one. A shim stays a
# process of its own (``start_new_session``), so a daemon restart cuts no
# stream: the next daemon adopts what is running.

_OWNER = False

#: Serialises every write of the users file and the shim records, and
#: :func:`reconcile`'s last look before it retires a shim: a session added on
#: the launch path (``harness.build_command`` → :func:`apply_session`, or the
#: ensure API) while a pass is running is never lost or retired under it.
#: Reentrant: ``reconcile`` holds it across ``_record``-writing calls.
_LOCK = threading.RLock()

#: How long a shim is kept after this daemon last handed it out
#: (``ensured`` in its record), whatever else :func:`reconcile` finds: covers
#: a launch between ensuring the shim and registering its session.
GRACE_S = 60.0

#: How often the daemon reconciles (``daemon/shims.py``).
RECONCILE_S = 30.0

#: The daemon API a process other than the daemon asks for a shim through.
ENSURE_API = "/api/routing/ensure"


def own_lifecycle(on: bool = True) -> None:
    """Make this process the one that starts and stops shims. The daemon
    calls it as it boots; tests that run real shims call it too."""
    global _OWNER
    _OWNER = on


def owns_lifecycle() -> bool:
    return _OWNER


def _port_of(url: str) -> Optional[int]:
    rest = url[len("http://127.0.0.1:"):] if url.startswith("http://127.0.0.1:") else ""
    text = rest.split("/", 1)[0]
    return int(text) if text.isdigit() else None


def _ensure_via_daemon(upstream: str, block: Dict, provider: Optional[str],
                       route: Optional[str]) -> str:
    """The shim for this pair, asked of the daemon, which starts it if needed
    and keeps it while this process lives. With no daemon to ask, a shim that
    is already running is still used; with neither, :class:`ShimUnavailable`
    (no direct fallback, claunch-19liw.2)."""
    from . import daemon_client  # the daemon client imports half the package

    client = daemon_client.connect()
    if client is not None:
        try:
            answer = client.post(ENSURE_API, {
                "upstream": upstream, "spec": block, "provider": provider,
                "route": route, "pid": os.getpid(),
            }, timeout=START_TIMEOUT + 10)
        except daemon_client.DaemonClientError as exc:
            raise ShimUnavailable(f"the daemon could not provide the routing shim "
                                  f"for {upstream}: {exc}") from exc
        url = answer.get("url") if isinstance(answer, dict) else None
        if not url:
            raise ShimUnavailable(f"the daemon answered no routing shim URL for {upstream}")
        return url
    port = _find(fingerprint(upstream, block, route=route))
    if port is not None:
        return local_url(port)
    raise ShimUnavailable(
        f"no routing shim for {upstream} is running and the daemon is not "
        "running to start one: shims are started by the daemon only "
        "(start it with 'claunch daemon start')"
    )


def _users_file() -> Path:
    return state_dir() / "users.json"


def _load_users() -> Dict[str, dict]:
    return _read_json(_users_file())


def add_user(port: Optional[int], *, session: Optional[str] = None,
             pid: Optional[int] = None) -> None:
    """Record that ``session`` (a daemon session) or ``pid`` (a ``claunch
    run`` outside the daemon) talks to the shim on ``port``. Kept on disk,
    since shims outlive a daemon restart and so must the reasons to keep
    them."""
    if port is None or not (session or pid):
        return
    with _LOCK:
        users = _load_users()
        entry = users.setdefault(str(port), {})
        if session:
            entry["sessions"] = sorted(set(entry.get("sessions") or []) | {session})
        if pid:
            entry["pids"] = sorted(set(entry.get("pids") or []) | {int(pid)})
        state_dir().mkdir(parents=True, exist_ok=True)
        _write_atomic(_users_file(), json.dumps(users, indent=2, sort_keys=True))


def _pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        from .daemon_client import process_alive

        return bool(process_alive(pid))
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def desired(doc: Optional[dict] = None) -> List[dict]:
    """The shims the config asks for on its own: for every provider whose
    Claude base URL goes through a shim (a ``routing`` block, a required
    header, metering), the shim fronting that URL. A dynamic provider's shim
    is keyed on the profile that launches it and Pi's on its OpenAI endpoint;
    those are started by the launch and kept while it lives."""
    from . import providers  # providers imports this module

    doc = store.load() if doc is None else doc
    out: List[dict] = []
    for name, env in providers.registry(doc).items():
        upstream = env.get("ANTHROPIC_BASE_URL")
        if not upstream or providers.uses_anthropic_oauth(name, doc):
            continue
        try:
            if dynamic(name, doc):
                continue
            block = spec(name, doc)
            if not (block or requires_shim(name, doc)
                    or metering.provider_enabled(name, doc)):
                continue
        except RoutingError:
            continue
        out.append({"provider": name, "upstream": upstream, "spec": block or {},
                    "fingerprint": fingerprint(upstream, block or {})})
    return out


def retire(info: dict) -> bool:
    """Ask the shim in ``info`` (an :func:`instances` row) to stop once its
    in-flight requests are done. A shim from a build without that request
    (no ``inflight`` in its health answer) is left running: it cannot say
    whether it is mid-stream. That is also what keeps the sessions that were
    already running when the daemon was upgraded safe: their shims come from
    the older build and they have no :func:`add_user` entry. True when the
    request was sent."""
    if "inflight" not in info:
        return False
    req = urllib.request.Request(
        f"http://127.0.0.1:{info['port']}{SHUTDOWN_PATH}?when=idle",
        method="POST", data=b"")
    try:
        urllib.request.urlopen(req, timeout=5.0).close()
    except (urllib.error.URLError, OSError):
        return False
    return True


def reconcile(live_sessions, doc: Optional[dict] = None,
              now: Optional[float] = None) -> dict:
    """One pass of the daemon's shim upkeep: start what the config wants,
    retire what nothing needs.

    A shim is kept while the config wants its fingerprint (:func:`desired`),
    a user registered with :func:`add_user` is alive (a session in
    ``live_sessions``, a pid still running), it has requests in flight, or it
    was handed out within :data:`GRACE_S`. Anything else is retired. So a
    config edit starts the new shim here and lets the old one go once the
    sessions launched against it have ended. Returns what it did, for the
    daemon log.

    ``live_sessions`` is a snapshot taken before the pass, and the launch
    path keeps registering users while it runs (ensuring a shim can take
    seconds). So the users file is read once as that snapshot, each
    retirement is decided again under :data:`_LOCK` just before it is sent,
    and the file is written back merged: a user added during the pass is
    kept as it is, only the snapshot's users are pruned.
    """
    now = time.time() if now is None else now
    live_sessions = set(live_sessions)
    started: List[str] = []
    errors: List[str] = []
    wanted = set()
    for want in desired(doc):
        wanted.add(want["fingerprint"])
        try:
            ensure_shim(want["upstream"], want["spec"], want["provider"])
            started.append(want["fingerprint"])
        except RoutingError as exc:
            errors.append(f"{want['provider']}: {exc}")
    with _LOCK:
        snapshot = _load_users()
    retired: List[str] = []
    seen_fps = set()
    for info in instances():
        port = str(info.get("port"))
        fp = info.get("fingerprint")
        entry = snapshot.get(port) or {}
        sessions = [s for s in entry.get("sessions") or [] if s in live_sessions]
        pids = [p for p in entry.get("pids") or [] if _pid_alive(int(p))]
        # A second shim for a wanted fingerprint is a duplicate: new launches
        # find the first one, so only its own users keep it.
        duplicate = fp in seen_fps
        seen_fps.add(fp)
        if (fp in wanted and not duplicate) or sessions or pids or info.get("inflight"):
            continue
        with _LOCK:
            # The last look: a user registered, or the shim handed out, since
            # the snapshot keeps it. Sent under the lock, so neither can slip
            # in between this look and the request (a loopback POST).
            current = _load_users().get(port) or {}
            if set(current.get("sessions") or []) - set(entry.get("sessions") or []) \
                    or set(current.get("pids") or []) - set(entry.get("pids") or []):
                continue
            record = _read_json(_record_file(str(fp), int(info["port"])))
            if now - float(record.get("ensured") or 0) < GRACE_S:
                continue
            if retire(info):
                retired.append(f"{fp}:{port}")
    with _LOCK:
        merged: Dict[str, dict] = {}
        for port, entry in _load_users().items():
            before = snapshot.get(port) or {}
            sessions = [s for s in entry.get("sessions") or []
                        if s in live_sessions or s not in (before.get("sessions") or [])]
            pids = [p for p in entry.get("pids") or []
                    if p not in (before.get("pids") or []) or _pid_alive(int(p))]
            if sessions or pids:
                merged[port] = {k: v for k, v in
                                (("sessions", sessions), ("pids", pids)) if v}
        state_dir().mkdir(parents=True, exist_ok=True)
        _write_atomic(_users_file(), json.dumps(merged, indent=2, sort_keys=True))
    return {"started": started, "retired": retired, "errors": errors}
