"""Installing a provider from the dashboard: validate, write, and check it.

A provider has always been something a person typed into ``~/.claunch.yaml``.
This module is the server half of the Settings page that writes it instead
(``#/settings/providers``): the same entry, from a form, with the reasons a
value is refused rather than a silently inert field.

Three rules shape it:

* **Validation is the readers', not a copy of them.** :func:`validate` parses
  the entry with :func:`provider_spec.from_entry` and then asks the modules
  that actually consume the extra keys -- :mod:`routing` for the three header
  facts, :mod:`harness_policy` for ``harness``/``allowed_harnesses``. A rule
  written twice drifts; a rule asked of its owner cannot.
* **A refusal writes nothing.** Every problem is collected before
  :func:`save` touches the config file, so a 400 leaves the file byte-identical
  (:func:`save` is the only writer, and it validates first).
* **A failed connection check does not block the save.** :func:`probe` exists
  to answer "can this daemon reach it", and the answer can be no for reasons
  that are none of the entry's business (a key not on this machine yet, a
  backend that refuses unauthenticated ``/models``). It reports; it never
  refuses.
"""

from __future__ import annotations

import re
import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from . import harness_policy, lineage, profile as profile_mod, provider_presets
from . import provider_spec, providers, routing, store
from .profile import Profile
from .provider_spec import SpecError

#: Provider ids this module refuses to create: the two the launcher defines
#: itself, so a form cannot shadow them with an entry that means something else.
RESERVED = (providers.DEFAULT_PROVIDER, providers.CLAUDE_PROVIDER)

#: A provider id becomes a ``providers:`` key and travels through JSON, argv
#: and a YAML file; this is the shape every one of those accepts.
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

#: Seconds a connection check waits before it reports a timeout.
PROBE_TIMEOUT = 10.0


class InstallError(providers.ProviderError):
    """The entry cannot be stored as given."""


def check_name(name) -> str:
    """``name`` as a provider id, or :class:`InstallError` saying why not."""
    text = str(name or "").strip()
    if not text:
        raise InstallError("a provider id is required")
    if not NAME_RE.fullmatch(text):
        raise InstallError(
            f"invalid provider id {text!r}: use letters, digits, dot, dash or "
            "underscore, starting with a letter or digit"
        )
    if text in RESERVED:
        raise InstallError(f"{text!r} is a provider the launcher defines itself")
    return text


def _url_problem(what: str, value: str) -> List[str]:
    try:
        parts = urlsplit(str(value))
    except ValueError as exc:
        return [f"{what} is not a URL ({exc})"]
    if parts.scheme not in ("http", "https"):
        return [f"{what} must start with http:// or https:// (got {value!r})"]
    if not parts.netloc:
        return [f"{what} has no host (got {value!r})"]
    if parts.query or parts.fragment:
        return [
            f"{what} must be a base URL without query parameters or a fragment"
        ]
    return []


def _candidate_doc(name: str, entry: dict) -> dict:
    """The live config with ``entry`` put in place, for the readers to judge."""
    doc = store.load()
    section = doc.get("providers")
    section = dict(section) if isinstance(section, dict) else {}
    section[name] = dict(entry)
    doc["providers"] = section
    return doc


def _extra_problems(name: str, doc: dict) -> List[str]:
    """The keys this module does not own, judged by the modules that read them."""
    out: List[str] = []
    for reader in (
        routing.session_affinity,
        routing.session_header,
        routing.anthropic_auth_header,
    ):
        try:
            reader(name, doc)
        except routing.RoutingError as exc:
            out.append(str(exc))
    try:
        harness_policy.provider_constraint(name, doc)
        harness_policy.provider_default(name, doc)
    except harness_policy.HarnessPolicyError as exc:
        out.append(str(exc))
    service = doc["providers"][name].get("service")
    if service is not None and (
        not isinstance(service, str) or not service.strip()
    ):
        out.append(
            f"providers.{name}.service must be a non-empty name such as "
            f"{providers.ANTHROPIC_SERVICE} or {providers.CUSTOM_SERVICE}"
        )
    return out


def validate(name, entry) -> List[str]:
    """Every reason ``entry`` cannot be stored as ``providers.<name>`` (empty = ok).

    Ordered the way a person fills the form: the id, the fields that do not
    exist, the spec itself, then the facts the neighbouring modules own.
    """
    problems: List[str] = []
    try:
        name = check_name(name)
    except InstallError as exc:
        problems.append(str(exc))
        name = str(name or "").strip()
    if not isinstance(entry, dict) or not entry:
        return problems + ["the entry must be a mapping with at least one field"]

    unknown = provider_spec.unknown_fields(entry)
    if unknown:
        problems.append(
            "unknown field(s): "
            + ", ".join(unknown)
            + " (known: "
            + ", ".join(provider_spec.known_fields())
            + ")"
        )
        # The rest of the checks read fields by name; a typo'd key would make
        # them report a *missing* field that the person did type.
        return problems
    try:
        spec = provider_spec.from_entry(entry, f"providers.{name}")
    except SpecError as exc:
        return problems + [str(exc)]

    for protocol, url in sorted(spec.endpoints.items()):
        problems.extend(_url_problem(f"endpoints.{protocol}", url))
    if not spec.endpoints and not spec.legacy_env:
        # A provider with no endpoint is a *policy* entry -- the shape this
        # machine runs for ``claude``/``codex``/``devin``, which only carry an
        # allow-list for :mod:`harness_policy`. That shape is legal; anything
        # else without an endpoint is a backend that goes nowhere.
        policy_only = "allowed_harnesses" in entry or "harness" in entry
        if not policy_only:
            problems.append(
                "at least one endpoint is required: endpoints.anthropic (the "
                "Messages protocol), endpoints.openai (the OpenAI protocol), or "
                "both — or an allowed_harnesses/harness policy entry"
            )
    if spec.endpoints and not spec.model("default"):
        problems.append(
            "models.default is required: it is the id a session launches when "
            "neither the profile nor the harness names one"
        )

    try:
        doc = _candidate_doc(name, entry)
    except store.StoreError as exc:
        return problems + [f"the config file cannot be read: {exc}"]
    problems.extend(_extra_problems(name, doc))
    return problems


def save(name, entry, *, profile: Optional[str] = None) -> dict:
    """Store ``entry`` as ``providers.<name>``; optionally pin ``profile`` to it.

    One atomic write covers both: the provider entry and the profile's
    selection land together or not at all, so a form that names a profile
    cannot leave a selection pointing at a provider that failed to write.
    """
    problems = validate(name, entry)
    if problems:
        raise InstallError("; ".join(problems))
    name = check_name(name)
    doc = store.load()
    section = doc.get("providers")
    section = dict(section) if isinstance(section, dict) else {}
    section[name] = dict(entry)
    doc["providers"] = section
    if profile:
        target = _pin(doc, profile, name)
        try:
            lineage.effective_harness(target, doc)
        except lineage.LineageError as exc:
            raise InstallError(
                f"cannot pin profile {target.name!r} to {name!r}: {exc}"
            ) from exc
    store.save(doc)
    return doc


def _pin(doc: dict, profile: str, provider: str) -> Profile:
    """Write ``profile``'s ``provider:`` selection into ``doc``."""
    target = profile_mod.resolve(str(profile))
    section = doc.get("profiles")
    if not isinstance(section, dict):
        section = {}
        doc["profiles"] = section
    entry = section.get(target.name)
    if not isinstance(entry, dict):
        entry = {}
        section[target.name] = entry
    entry["provider"] = provider
    return target


def listing() -> dict:
    """What the Settings page reads: installed providers, presets, profiles."""
    doc = store.load()
    rows = []
    for name, entry in sorted(providers.entries(doc).items()):
        if name in RESERVED:
            continue
        rows.append(
            {"name": name, "entry": entry, "problems": validate(name, entry)}
        )
    profile_rows = []
    for item in profile_mod.list_all():
        entry = store.profile_entry(item.name, doc)
        profile_rows.append(
            {
                "name": item.name,
                "provider": entry.get("provider") or "",
                "effective": providers.resolve_name(item, doc),
            }
        )
    return {
        "providers": rows,
        "presets": [preset.to_dict() for preset in provider_presets.load_all()],
        "profiles": profile_rows,
        "global_provider": providers.active(doc) or "",
        "preset_dir": str(provider_presets.directory()),
        "fields": {
            "protocols": list(provider_spec.PROTOCOLS),
            "roles": list(provider_spec.MODEL_ROLES),
            "reasoning_efforts": list(provider_spec.REASONING_EFFORTS),
            "openai_reasoning_formats": (
                list(provider_spec.OPENAI_REASONING_FORMATS)
            ),
            "anthropic_auth_headers": list(routing.ANTHROPIC_AUTH_HEADERS),
            "fields": list(provider_spec.known_fields()),
            "reserved": list(RESERVED),
        },
    }


# --------------------------------------------------------------------------- #
# connection check
# --------------------------------------------------------------------------- #
def _messages_url(base: str) -> str:
    """``<anthropic base>/v1/messages``, tolerating a base that already says v1."""
    text = str(base).strip().rstrip("/")
    if text.endswith("/v1"):
        return text + "/messages"
    return text + "/v1/messages"


def probe_request(
    entry: dict, api_key: Optional[str] = None
) -> Tuple[str, str, Dict[str, str], Optional[dict]]:
    """``(method, url, headers, body)`` for one connection check.

    OpenAI-protocol providers are asked for their model list -- a GET that
    costs no tokens. Anthropic-protocol providers are asked for one message
    with ``max_tokens: 1``, because the Messages protocol has no cheaper
    endpoint that proves a key works.

    The credential goes where that protocol puts it: ``Authorization: Bearer``
    for the OpenAI protocol, and for the Messages protocol wherever the entry's
    ``anthropic_auth_header`` says (``x-api-key`` when it says so, otherwise
    ``Authorization``).
    """
    try:
        spec = provider_spec.from_entry(entry, "probe")
    except SpecError as exc:
        raise InstallError(str(exc)) from exc
    openai = spec.endpoint("openai")
    if openai:
        base = provider_spec.openai_urls(openai).base_url
        headers = {"Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return "GET", f"{base.rstrip('/')}/models", headers, None
    anthropic = spec.endpoint("anthropic")
    if not anthropic:
        raise InstallError(
            "no endpoint to check: set endpoints.openai or endpoints.anthropic"
        )
    model = spec.model("default") or spec.model("small") or ""
    if not model:
        raise InstallError("no model to check: set models.default")
    headers = {
        "content-type": "application/json",
        "anthropic-version": "2023-06-01",
    }
    if api_key:
        entry_auth = str(entry.get("anthropic_auth_header") or "").strip()
        if entry_auth.lower() == "x-api-key":
            headers["x-api-key"] = api_key
        else:
            headers["Authorization"] = f"Bearer {api_key}"
    body = {
        "model": model,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "ping"}],
    }
    return "POST", _messages_url(anthropic), headers, body


async def probe(entry: dict, api_key: Optional[str] = None) -> dict:
    """One real call to the backend; never raises for a backend's answer."""
    started = time.monotonic()
    try:
        method, url, headers, body = probe_request(entry, api_key)
    except InstallError as exc:
        return {
            "ok": False, "status": None, "url": "", "detail": str(exc),
            "elapsed_ms": 0,
        }
    try:
        import aiohttp
    except ImportError:  # pragma: no cover - aiohttp ships with the daemon
        return {
            "ok": False, "status": None, "url": url,
            "detail": "aiohttp is not available to this daemon",
            "elapsed_ms": 0,
        }
    try:
        timeout = aiohttp.ClientTimeout(total=PROBE_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as http:
            kwargs = {"headers": headers}
            if body is not None:
                kwargs["json"] = body
            async with http.request(method, url, **kwargs) as response:
                text = await response.text()
                ok = 200 <= response.status < 300
                detail = f"HTTP {response.status}"
                if not ok:
                    detail += ": " + " ".join(text.split())[:400]
                return {
                    "ok": ok,
                    "status": response.status,
                    "url": url,
                    "detail": detail,
                    "elapsed_ms": int((time.monotonic() - started) * 1000),
                }
    except Exception as exc:  # noqa: BLE001 - the answer is the report
        return {
            "ok": False,
            "status": None,
            "url": url,
            "detail": f"{type(exc).__name__}: {exc}",
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }
