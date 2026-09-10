"""``claunch migrate-config``: rewrite ``~/.claunch.yaml`` to the spec schema.

Version 1 providers carry a Claude-vocabulary ``env``; version 2 providers
carry the harness-neutral fields of :mod:`provider_spec`.  The conversion is
the reverse translation :func:`provider_spec.from_legacy_env` plus a
per-key diff against the Claude translation of what it produced -- anything
the translation would not reproduce is kept verbatim under
``harness_options.claude.env`` (a model alias set to a different id than its
role, an unrelated ``CLAUDE_CODE_*`` flag), so the Claude harness receives
the same variables after migration as before, with two deliberate exceptions
that the report names:

- the empty pins ``ANTHROPIC_API_KEY: ""`` / ``CLAUDE_CODE_OAUTH_TOKEN: ""``
  are dropped -- the runner enforces both itself;
- a model alias the env never set but the roles imply (a provider that set
  ``ANTHROPIC_DEFAULT_OPUS_MODEL`` but not ``..._FABLE_MODEL``) is now set
  to the same id, since ``large`` covers both.

Profiles get the same treatment for their ``env``: ``CLAUDE_CODE_AUTO_COMPACT_WINDOW``
becomes ``auto_compact_at``, model pins become ``models`` (plus a shared tag
under ``harness_options.claude.model_tag``), and the rest moves to
``harness_options.claude.env``.  The round-1 ``harnesses.pi`` block (base
URL and model list declared for Pi alone) folds into ``endpoints.openai`` and
``models``.

The command is idempotent: a version-2 document comes back unchanged.
"""

from __future__ import annotations

import copy
import shutil
from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional, Tuple

from . import provider_spec, store, translators
from .provider_spec import (
    CLAUDE_COMPACT_WINDOW,
    CLAUDE_MODEL_KEYS,
    CLAUDE_REDUNDANT_PINS,
    HARNESS_OPTIONS_FIELD,
    ProviderSpec,
)

TARGET_VERSION = 2
BACKUP_SUFFIX = ".v1.bak"

#: Round-1 per-harness block name, absorbed by this migration.
LEGACY_HARNESSES_FIELD = "harnesses"


@dataclass
class Report:
    changed: bool = False
    lines: List[str] = field(default_factory=list)
    #: Things a person should look at after the rewrite.
    warnings: List[str] = field(default_factory=list)

    def say(self, line: str) -> None:
        self.lines.append(line)

    def warn(self, line: str) -> None:
        self.warnings.append(line)


class MigrateConfigError(Exception):
    pass


# --- provider entries --------------------------------------------------------


def _spec_to_entry(spec: ProviderSpec) -> dict:
    out: dict = {}
    if spec.api_key:
        out["api_key"] = spec.api_key
    if spec.endpoints:
        out["endpoints"] = dict(spec.endpoints)
    if spec.models:
        out["models"] = {
            role: spec.models[role]
            for role in provider_spec.MODEL_ROLES
            if role in spec.models
        }
    if spec.context_window:
        out["context_window"] = spec.context_window
    if spec.auto_compact_at:
        out["auto_compact_at"] = spec.auto_compact_at
    if spec.harness_options:
        out[HARNESS_OPTIONS_FIELD] = {
            h: dict(block) for h, block in spec.harness_options.items() if block
        }
    return out


def _leftover_env(
    legacy: Dict[str, str], spec: ProviderSpec, report: Report, what: str
) -> Dict[str, str]:
    """Legacy keys the Claude translation of ``spec`` would not reproduce."""
    translated = translators.claude(replace(spec, legacy_env=None)).env
    leftover: Dict[str, str] = {}
    for key, value in legacy.items():
        if key in CLAUDE_REDUNDANT_PINS and value == CLAUDE_REDUNDANT_PINS[key]:
            report.say(f"  {what}: drops {key}='' (the runner enforces it)")
            continue
        if translated.get(key) != value:
            leftover[key] = value
    for key in translated:
        if key not in legacy and key in CLAUDE_MODEL_KEYS:
            report.say(
                f"  {what}: adds {key}={translated[key]!r} (implied by its role)"
            )
    return leftover


def _absorb_round1_block(entry: dict, spec: ProviderSpec, report: Report, what: str) -> ProviderSpec:
    blocks = entry.get(LEGACY_HARNESSES_FIELD)
    if not isinstance(blocks, dict):
        return spec
    block = blocks.get("pi")
    if isinstance(block, dict):
        base_url = str(block.get("base_url") or "").strip()
        if base_url and not spec.endpoint("openai"):
            spec = replace(spec, endpoints={**spec.endpoints, "openai": base_url})
            report.say(f"  {what}: harnesses.pi.base_url -> endpoints.openai")
        models = block.get("models")
        if isinstance(models, str):
            models = [models]
        default = str(block.get("default_model") or "").strip()
        if isinstance(models, (list, tuple)) and models:
            names = [str(m).strip() for m in models if str(m).strip()]
            if default and default in names:
                names.remove(default)
                names.insert(0, default)
            roles = dict(spec.models)
            if not roles.get("default") and names:
                roles["default"] = names[0]
            if not roles.get("large") and len(names) > 1:
                roles["large"] = names[-1]
            if names[0] != spec.model("default") and spec.model("default"):
                report.warn(
                    f"  {what}: harnesses.pi listed {names[0]!r} first but the "
                    f"Claude env's default is {spec.model('default')!r}; "
                    "models.default keeps the Claude value"
                )
            spec = replace(spec, models=roles)
            report.say(f"  {what}: harnesses.pi.models -> models")
    return spec


def convert_provider(name: str, entry: dict, report: Report) -> dict:
    what = f"providers.{name}"
    entry = dict(entry)
    legacy = entry.get("env")
    has_block = isinstance(entry.get(LEGACY_HARNESSES_FIELD), dict)
    if not (isinstance(legacy, dict) and legacy) and not has_block:
        return entry
    report.changed = True
    report.say(f"{what}:")
    legacy_env = {str(k): str(v) for k, v in (legacy or {}).items()}
    # Fields already written in the new schema next to the env win.
    declared = provider_spec.from_entry(
        {k: v for k, v in entry.items() if k != "env"}, what
    )
    spec = provider_spec.overlay(provider_spec.from_legacy_env(legacy_env), declared)
    spec = _absorb_round1_block(entry, spec, report, what)
    leftover = _leftover_env(legacy_env, spec, report, what) if legacy_env else {}
    if leftover:
        options = {h: dict(b) for h, b in spec.harness_options.items()}
        claude = options.setdefault("claude", {})
        claude["env"] = {**(claude.get("env") or {}), **leftover}
        spec = replace(spec, harness_options=options)
        report.say(
            f"  {what}: keeps {', '.join(sorted(leftover))} under "
            "harness_options.claude.env"
        )
    tag = spec.options("claude").get("model_tag")
    if tag == translators.CLAUDE_LONG_CONTEXT_TAG:
        report.warn(
            f"  {what}: the Claude env's '[1m]' tag is recorded as "
            "harness_options.claude.model_tag; the backend's context is a "
            "property of the model, so once known, set context_window "
            "(>= 1000000 derives the same tag) and drop model_tag"
        )
    if spec.endpoint("anthropic") and not spec.endpoint("openai"):
        report.warn(
            f"  {what}: endpoints.openai left empty -- the Anthropic URL "
            f"{spec.endpoint('anthropic')!r} has a path, so the OpenAI-"
            "compatible root could not be assumed; declare it before "
            "launching Pi on this provider"
        )
    for key in ("env", LEGACY_HARNESSES_FIELD, *provider_spec.SPEC_FIELDS, HARNESS_OPTIONS_FIELD):
        entry.pop(key, None)
    entry.update(_spec_to_entry(spec))
    return entry


# --- profile entries ---------------------------------------------------------


def _chain(name: str, profiles: dict) -> List[str]:
    """Root ancestor first, ``name`` last (cycles cut)."""
    out: List[str] = []
    seen = set()
    current: Optional[str] = name
    while current and current not in seen and isinstance(profiles.get(current), dict):
        seen.add(current)
        out.append(current)
        current = str(profiles[current].get("parent") or "") or None
    return list(reversed(out))


def _provider_of(name: str, doc: dict) -> str:
    profiles = doc.get("profiles") or {}
    for item in reversed(_chain(name, profiles)):
        sel = str((profiles.get(item) or {}).get("provider") or "")
        if sel:
            return sel
    return str(doc.get("provider") or "default")


def _legacy_provider_env(provider: str, orig: dict) -> Dict[str, str]:
    entry = (orig.get("providers") or {}).get(provider)
    env = entry.get("env") if isinstance(entry, dict) else None
    return {str(k): str(v) for k, v in env.items()} if isinstance(env, dict) else {}


def _profile_layer(entry: dict, what: str) -> ProviderSpec:
    """A converted (or being-converted) profile entry as a spec layer."""
    legacy = entry.get("env")
    legacy_env = {str(k): str(v) for k, v in legacy.items()} if isinstance(legacy, dict) else {}
    backend_keys = set(CLAUDE_MODEL_KEYS) | {CLAUDE_COMPACT_WINDOW}
    reverse = provider_spec.from_legacy_env(
        {k: v for k, v in legacy_env.items() if k in backend_keys}
    )
    declared = provider_spec.from_entry(
        {k: v for k, v in entry.items() if k != "env"}, what, profile=True
    )
    return provider_spec.overlay(reverse, declared)


def convert_profile(
    name: str, entry: dict, report: Report, *, orig: dict, new_doc: dict
) -> dict:
    """Move a profile's Claude ``env`` into spec fields, keeping the outcome.

    The test is the whole Claude environment this profile ends up with: the
    provider's (legacy) env under its ancestors' env under its own, versus
    the layered translation of the converted entries. Every variable the
    translation would change is pinned back under
    ``harness_options.claude.env`` of this profile.
    """
    what = f"profiles.{name}"
    entry = dict(entry)
    legacy = entry.get("env")
    has_block = isinstance(entry.get(LEGACY_HARNESSES_FIELD), dict)
    if not (isinstance(legacy, dict) and legacy) and not has_block:
        return entry
    legacy_env = {str(k): str(v) for k, v in (legacy or {}).items()}
    backend_keys = set(CLAUDE_MODEL_KEYS) | {CLAUDE_COMPACT_WINDOW}
    touched = {k for k in legacy_env if k in backend_keys}
    if not touched and not has_block:
        return entry
    report.changed = True
    report.say(f"{what}:")

    provider = _provider_of(name, orig)
    orig_profiles = orig.get("profiles") or {}
    chain = _chain(name, orig_profiles)
    before: Dict[str, str] = dict(_legacy_provider_env(provider, orig))
    for item in chain[:-1]:
        raw = (orig_profiles.get(item) or {}).get("env")
        if isinstance(raw, dict):
            before.update({str(k): str(v) for k, v in raw.items()})
    before.update(legacy_env)

    try:
        base = provider_spec.from_entry(
            (new_doc.get("providers") or {}).get(provider) or {}, f"providers.{provider}"
        ) if provider != "default" else ProviderSpec()
    except provider_spec.SpecError as exc:
        raise MigrateConfigError(f"providers.{provider}: {exc}") from exc
    new_profiles = new_doc.get("profiles") or {}
    layers = [
        _profile_layer(new_profiles.get(item) or {}, f"profiles.{item}")
        for item in chain[:-1]
    ]
    own = _profile_layer({k: v for k, v in entry.items() if k != "env"} | {"env": legacy_env}, what)
    own = replace(own, harness_options={
        h: {c: v for c, v in b.items() if c != "env"} for h, b in own.harness_options.items()
    })
    own_raw = {k: v for k, v in legacy_env.items() if k not in backend_keys}
    after = translators.claude_layered(base, [*layers, own])
    after.update(own_raw)

    leftover: Dict[str, str] = dict(own_raw)
    for key, value in before.items():
        if key in CLAUDE_REDUNDANT_PINS and value == CLAUDE_REDUNDANT_PINS[key]:
            continue
        if after.get(key) != value:
            leftover[key] = value
    for key in after:
        if key not in before and key in CLAUDE_MODEL_KEYS:
            report.say(f"  {what}: adds {key}={after[key]!r} (implied by its role)")
    moved = sorted(k for k in touched if k not in leftover)
    if moved:
        report.say(f"  {what}: {', '.join(moved)} -> models/auto_compact_at")
    if leftover:
        options = {h: dict(b) for h, b in own.harness_options.items()}
        claude = options.setdefault("claude", {})
        claude["env"] = {**(claude.get("env") or {}), **leftover}
        own = replace(own, harness_options=options)
        report.say(
            f"  {what}: keeps {', '.join(sorted(leftover))} under "
            "harness_options.claude.env"
        )
    block = (entry.get(LEGACY_HARNESSES_FIELD) or {}).get("pi") if has_block else None
    if isinstance(block, dict) and block.get("default_model"):
        report.warn(
            f"  {what}: harnesses.pi.default_model={block['default_model']!r} "
            "dropped -- set models.default on the profile if Pi should "
            "launch with a different model than Claude"
        )
    for key in ("env", LEGACY_HARNESSES_FIELD, *provider_spec.PROFILE_SPEC_FIELDS, HARNESS_OPTIONS_FIELD):
        entry.pop(key, None)
    entry.update(_spec_to_entry(replace(own, api_key=None, endpoints={})))
    return entry


def convert_template(entry: dict, report: Report) -> dict:
    """The ``template`` block is a profile layer with no provider under it."""
    what = "template"
    entry = dict(entry)
    legacy_env = {str(k): str(v) for k, v in entry["env"].items()}
    report.changed = True
    report.say(f"{what}:")
    backend_keys = set(CLAUDE_MODEL_KEYS) | {CLAUDE_COMPACT_WINDOW}
    layer = _profile_layer(entry, what)
    layer = replace(layer, harness_options={
        h: {c: v for c, v in b.items() if c != "env"} for h, b in layer.harness_options.items()
    })
    after = translators.claude_layered(ProviderSpec(), [layer])
    leftover = {k: v for k, v in legacy_env.items() if after.get(k) != v}
    moved = sorted(k for k in legacy_env if k in backend_keys and k not in leftover)
    if moved:
        report.say(f"  {what}: {', '.join(moved)} -> models/auto_compact_at")
    if leftover:
        options = {h: dict(b) for h, b in layer.harness_options.items()}
        claude = options.setdefault("claude", {})
        claude["env"] = {**(claude.get("env") or {}), **leftover}
        layer = replace(layer, harness_options=options)
        report.say(
            f"  {what}: keeps {', '.join(sorted(leftover))} under "
            "harness_options.claude.env"
        )
    for key in ("env", *provider_spec.PROFILE_SPEC_FIELDS, HARNESS_OPTIONS_FIELD):
        entry.pop(key, None)
    entry.update(_spec_to_entry(replace(layer, api_key=None, endpoints={})))
    return entry


# --- whole document ----------------------------------------------------------


def convert(doc: dict) -> Tuple[dict, Report]:
    """Return the version-2 document and what changed. Idempotent."""
    report = Report()
    out = copy.deepcopy(doc)
    version = int(out.get("version") or 1)
    if version > TARGET_VERSION:
        raise MigrateConfigError(
            f"config file is version {version}; this claunch knows up to "
            f"{TARGET_VERSION} -- upgrade claunch instead"
        )
    providers = out.get("providers")
    if isinstance(providers, dict):
        for name in list(providers):
            entry = providers[name]
            if isinstance(entry, dict):
                providers[name] = convert_provider(str(name), entry, report)
    profiles = out.get("profiles")
    if isinstance(profiles, dict):
        # Parents first: a child's outcome is judged over its converted
        # ancestors.
        order = sorted(
            (str(n) for n in profiles if isinstance(profiles[n], dict)),
            key=lambda n: len(_chain(n, profiles)),
        )
        for name in order:
            profiles[name] = convert_profile(
                name, profiles[name], report, orig=doc, new_doc=out
            )
    template = out.get("template")
    if isinstance(template, dict) and isinstance(template.get("env"), dict) and template["env"]:
        out["template"] = convert_template(template, report)
    if version < TARGET_VERSION:
        out["version"] = TARGET_VERSION
        report.changed = True
        report.say(f"version: {version} -> {TARGET_VERSION}")
    return out, report


def run(*, dry_run: bool) -> Report:
    """Convert the live config file; a backup is written beside it first."""
    doc = store.load()
    new_doc, report = convert(doc)
    if not report.changed:
        report.say("config file is already at the current schema")
        return report
    if dry_run:
        return report
    path = store.path()
    backup = path.with_name(path.name + BACKUP_SUFFIX)
    if path.is_file() and not backup.exists():
        shutil.copyfile(path, backup)
        report.say(f"backup: {backup}")
    store.save(new_doc)
    report.say(f"wrote {path}")
    return report
