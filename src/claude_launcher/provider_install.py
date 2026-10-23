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

import copy
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

#: The top-level provider keys the dashboard form draws. :func:`save` keeps
#: every *other* key of a stored entry: a control that does not exist cannot be
#: cleared, so a form saving over a provider must not delete what it never
#: showed. This is the list, not a guess -- ``app.js`` builds its controls from
#: the same keys, and :func:`listing` publishes it so the page can say which
#: keys it is leaving alone.
FORM_FIELDS: Tuple[str, ...] = (
    "api_key",
    "endpoints",
    "models",
    "context_window",
    "auto_compact_at",
    "harness_options",
    "anthropic_auth_header",
    "session_header",
    "session_affinity",
    "allowed_harnesses",
    "service",
    "harness",
    "reasoning_effort",
    "openai_reasoning_format",
)


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


def merge_entry(previous, entry) -> Dict[str, object]:
    """``entry`` as it should be stored, keeping what the form cannot draw.

    The dashboard draws :data:`FORM_FIELDS` and nothing else. A stored entry
    can also carry keys no control renders -- ``routing``, ``dynamic``,
    ``env``, ``capabilities`` -- and a form that replaced the entry wholesale
    would delete them without a word while the page reported success. A
    control that does not exist cannot be cleared, so those keys are kept from
    ``previous`` unless the new entry names them.

    Two keys need more than that rule:

    * ``harness_options`` is drawn one leaf at a time (``claude.model_tag``),
      so it is merged a level deeper; ``claude.env`` and ``pi.settings``
      survive a save that only touched the tag.
    * ``api_key`` is never sent to the browser (:func:`listing` reports
      ``api_key_set`` instead). An entry without one therefore means "leave
      the stored key alone", not "remove it".

    The pre-schema ``env`` block needs no case of its own: it is not a
    :data:`FORM_FIELDS` member, so the rule above keeps it, and it is not sent
    for the form to post back.

    The pre-schema ``api-key`` does need one, and gets exactly one. It is kept
    by the rule above like any other undrawn key -- but when the same save
    carries a new ``api_key``, keeping it would leave the entry holding the old
    secret under one spelling and the new one under another.
    :func:`provider_spec.from_entry` prefers ``api_key``, so the copy left
    behind is unread and permanent: "typing a key replaces the stored one" was
    true of what the launcher used and false of the file. So a save that names
    a non-empty ``api_key`` drops ``api-key``; a save that names none keeps it,
    because the form still cannot clear what it never showed.

    Removing any of these by hand is a ``~/.claunch.yaml`` edit.
    """
    new = copy.deepcopy(entry) if isinstance(entry, dict) else {}
    old = previous if isinstance(previous, dict) else {}
    out: Dict[str, object] = {
        key: copy.deepcopy(value)
        for key, value in old.items()
        if key not in FORM_FIELDS
    }
    out.update(new)
    old_options = old.get("harness_options")
    if isinstance(old_options, dict):
        merged = copy.deepcopy(old_options)
        new_options = out.get("harness_options")
        if isinstance(new_options, dict):
            for harness, values in new_options.items():
                if isinstance(values, dict) and isinstance(merged.get(harness), dict):
                    merged[harness] = {**merged[harness], **copy.deepcopy(values)}
                else:
                    merged[harness] = copy.deepcopy(values)
        out["harness_options"] = merged
    if "api_key" not in new and old.get("api_key"):
        out["api_key"] = old["api_key"]
    if _holds_secret(new.get("api_key")) and LEGACY_CREDENTIAL_FIELD in out:
        # One secret, two spellings: the key this save wrote is the one the
        # launcher will read (``provider_spec.from_entry`` prefers ``api_key``),
        # so the pre-schema copy is not preserved -- it is superseded.
        out.pop(LEGACY_CREDENTIAL_FIELD, None)
    return out


def save(name, entry, *, profile: Optional[str] = None) -> dict:
    """Store ``entry`` as ``providers.<name>``; optionally pin ``profile`` to it.

    The stored entry is merged with what is already there (:func:`merge_entry`),
    so a key the form does not draw survives a save that did not mean to touch
    it. One atomic write covers the provider and the profile selection
    together: a form that names a profile cannot leave a selection pointing at
    a provider that failed to write.
    """
    doc = store.load()
    existing = doc.get("providers")
    previous = (
        existing.get(name) if isinstance(existing, dict) and isinstance(name, str)
        else None
    )
    merged = merge_entry(previous, entry)
    problems = validate(name, merged)
    if problems:
        raise InstallError("; ".join(problems))
    name = check_name(name)
    section = doc.get("providers")
    section = dict(section) if isinstance(section, dict) else {}
    section[name] = merged
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


#: The pre-schema spelling of ``api_key``, still carried by an unmigrated entry
#: (:data:`provider_spec.LEGACY_PROVIDER_FIELDS`). It names the same secret.
LEGACY_CREDENTIAL_FIELD = "api-key"

#: Top-level spellings a stored credential can take. ``api_key`` is the schema
#: field the form draws; :data:`LEGACY_CREDENTIAL_FIELD` is the older one. Both
#: name the same secret -- :func:`provider_spec.from_entry` reads one as the
#: other -- so both are treated as one.
CREDENTIAL_FIELDS: Tuple[str, ...] = ("api_key", LEGACY_CREDENTIAL_FIELD)

#: The pre-schema ``env`` block. ``claunch migrate-config`` retires it, but a
#: config that has not been migrated still carries one, and its
#: ``ANTHROPIC_AUTH_TOKEN`` is the credential under Claude's own name
#: (:func:`provider_spec.from_legacy_env` reads it back into ``api_key``).
LEGACY_ENV_FIELD = "env"


def _holds_secret(value) -> bool:
    """Whether a credential field carries something rather than nothing."""
    return bool(str(value or "").strip())


def _stored_credential(entry) -> bool:
    """Whether the stored entry holds a credential, under any spelling above."""
    if not isinstance(entry, dict):
        return False
    if any(_holds_secret(entry.get(key)) for key in CREDENTIAL_FIELDS):
        return True
    legacy = entry.get(LEGACY_ENV_FIELD)
    if isinstance(legacy, dict):
        return _holds_secret(legacy.get(provider_spec.CLAUDE_AUTH_TOKEN))
    return False


def _public_entry(entry: dict) -> dict:
    """A stored entry as it may be sent to the browser: no secret in it.

    Every spelling of the credential stays out of the payload: ``api_key``, the
    pre-schema ``api-key``, the pre-schema ``env`` block whose
    ``ANTHROPIC_AUTH_TOKEN`` is the same secret, and the values of
    ``harness_options.claude.env``, which is the harness's environment and can
    hold the token too. The page shows whether one is stored (the row's
    ``api_key_set``) and never receives its value, the way the profile and
    LLM-settings pages already treat a stored token.

    Withholding costs the round trip nothing, and each key is put back a
    different way. ``api-key`` and ``env`` are not :data:`FORM_FIELDS` members,
    so :func:`merge_entry` copies them from the stored entry as keys the form
    cannot draw. ``api_key`` it restores by name, since an absent key field
    means "unchanged". ``harness_options`` is drawn, so it is merged a level
    deeper and ``claude.env`` survives a save that named only the tag.

    The flag belongs to the **row**, not the entry: the page posts the entry it
    loaded back, so a key that travelled inside it would be posted back too and
    refused as an unknown field on the next save.
    """
    out = copy.deepcopy(entry) if isinstance(entry, dict) else {}
    for key in CREDENTIAL_FIELDS:
        out.pop(key, None)
    out.pop(LEGACY_ENV_FIELD, None)
    options = out.get("harness_options")
    if isinstance(options, dict):
        claude = options.get("claude")
        if isinstance(claude, dict):
            claude.pop("env", None)
            if not claude:
                options.pop("claude")
        if not options:
            out.pop("harness_options")
    return out


def listing() -> dict:
    """What the Settings page reads: installed providers, presets, profiles."""
    doc = store.load()
    rows = []
    for name, entry in sorted(providers.entries(doc).items()):
        if name in RESERVED:
            continue
        rows.append(
            {
                "name": name,
                "entry": _public_entry(entry),
                "api_key_set": _stored_credential(entry),
                "problems": validate(name, entry),
                "kept": sorted(set(entry) - set(FORM_FIELDS)),
            }
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
            "form_fields": list(FORM_FIELDS),
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
