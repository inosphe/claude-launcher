"""Model tier presets: named tiers of ``(profile, model, harness)`` tuples.

A *tier* names a routing slot (``economy`` / ``standard`` / ``heavy``...);
its *entries* are ordered ``(profile, model, harness)`` tuples, the first
(unless ``active`` says otherwise) being what the tier resolves to today.
Consumers (a workflow's delegation step, the shim, a future switcher UI)
call :func:`resolve` and get one concrete tuple; editing the config — the
table or the active pointer — takes effect on the next call, no restart.

Storage is the top-level ``model_tiers:`` block of ``~/.claunch.yaml``::

    model_tiers:
      version: 1
      tiers:
        - id: economy
          entries:
            - {profile: glm-flash, model: small, harness: claude}
      active:
        economy: 0

``model`` may be a :data:`provider_spec.MODEL_ROLES` word or a model name the
harness takes; ``harness`` defaults to the profile's effective harness and
``model`` to the ``default`` role. Validation refuses combinations that
cannot launch (unknown profile, a harness the profile's policy denies, a
model the harness has no route for) — possible combinations only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .. import harness_policy, harnesses, input_options, lineage, profile as profile_mod, providers, store

#: The block's version. One reader exists; a newer file fails loudly here
#: rather than being half-read.
VERSION = 1

#: Tier ids: lowercase alphanumerics and hyphens, short enough for a select.
TIER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")

#: The settings editor keeps this many tiers; the request was "about three".
MAX_TIERS = 5

#: The model word an entry without one resolves to (the provider's default).
DEFAULT_MODEL = "default"


class TierError(Exception):
    """A ``model_tiers:`` block or one of its entries does not validate."""


@dataclass(frozen=True)
class TierEntry:
    """One ``(profile, model, harness)`` tuple; the last two may default."""

    profile: str
    model: Optional[str] = None
    harness: Optional[str] = None

    def as_dict(self) -> Dict[str, object]:
        out: Dict[str, object] = {"profile": self.profile}
        if self.model:
            out["model"] = self.model
        if self.harness:
            out["harness"] = self.harness
        return out


@dataclass(frozen=True)
class Tier:
    id: str
    entries: Tuple[TierEntry, ...] = ()

    def as_dict(self) -> Dict[str, object]:
        return {"id": self.id, "entries": [e.as_dict() for e in self.entries]}


# --------------------------------------------------------------------------- #
# cleaning (never raises — a corrupt block reads as empty)
# --------------------------------------------------------------------------- #

def _clean_entry(raw: object) -> Optional[TierEntry]:
    if not isinstance(raw, dict):
        return None
    profile = str(raw.get("profile") or "").strip()
    if not profile:
        return None
    model = str(raw.get("model") or "").strip() or None
    harness = str(raw.get("harness") or "").strip() or None
    return TierEntry(profile=profile, model=model, harness=harness)


def _clean(raw: object) -> Dict[str, object]:
    """The block as a well-shaped document, dropping what makes no sense."""
    out: Dict[str, object] = {"version": VERSION, "tiers": [], "active": {}}
    if not isinstance(raw, dict):
        return out
    tiers: List[Dict[str, object]] = []
    seen = set()
    for item in raw.get("tiers") or []:
        if not isinstance(item, dict):
            continue
        tier_id = str(item.get("id") or "").strip()
        if not TIER_ID_RE.match(tier_id) or tier_id in seen:
            continue
        seen.add(tier_id)
        entries = tuple(
            e for e in (_clean_entry(x) for x in item.get("entries") or []) if e
        )
        tiers.append({"id": tier_id, "entries": [e.as_dict() for e in entries]})
    out["tiers"] = tiers
    index_by_tier = {t["id"]: t for t in tiers}
    active: Dict[str, int] = {}
    raw_active = raw.get("active")
    if isinstance(raw_active, dict):
        for name, value in raw_active.items():
            tier = index_by_tier.get(str(name))
            if tier is None:
                continue
            try:
                index = int(value)
            except (TypeError, ValueError):
                continue
            if 0 <= index < len(tier["entries"]):
                active[str(name)] = index
    out["active"] = active
    return out


# --------------------------------------------------------------------------- #
# validation (raises TierError with the field and the reason)
# --------------------------------------------------------------------------- #

def _require_profile(name: str, what: str):
    try:
        return profile_mod.require(name)
    except profile_mod.ProfileError as exc:
        raise TierError(f"{what}: {exc}") from None


def _model_choices(profile: profile_mod.Profile, harness: str, doc: dict) -> List[str]:
    """The models ``harness`` for ``profile`` may be asked for.

    The same rule the spawn gate applies (``claude``: every preset plus the
    harness's choices; other harnesses: presets with a declared model plus
    choices), so a tier entry is exactly what a spawn could carry out.
    """
    entry = harnesses.registry(doc).get(harness)
    spec = providers.spec_for(profile, doc=doc)
    rule = input_options._MODEL_RULES.get(harness, input_options.declared_models)
    return rule(spec, entry)


def validate(raw: object, doc: Optional[dict] = None) -> Dict[str, object]:
    """``raw`` as a storable block, or :class:`TierError` naming the field.

    Every entry must be a combination that can actually launch: the profile
    must exist, the harness must be one the profile's policy allows, and the
    model must be one that harness can resolve for the profile.
    """
    doc = store.load() if doc is None else doc
    block = _clean(raw)
    tiers: List[dict] = block["tiers"]  # type: ignore[assignment]
    if not tiers:
        raise TierError("at least one tier is required")
    if len(tiers) > MAX_TIERS:
        raise TierError(f"at most {MAX_TIERS} tiers, got {len(tiers)}")
    for tier in tiers:
        what = f"tier {tier['id']!r}"
        entries = tier["entries"]
        if not entries:
            raise TierError(f"{what}: at least one entry is required")
        seen = set()
        for position, raw_entry in enumerate(entries):
            entry = _clean_entry(raw_entry)
            where = f"{what} entry {position + 1}"
            prof = _require_profile(entry.profile, where)
            try:
                harness = (
                    entry.harness
                    or lineage.effective_harness(prof, doc)
                )
            except lineage.LineageError as exc:
                raise TierError(f"{where}: {exc}") from None
            allowed = harness_policy.allowed_names(prof, doc)
            if harness not in allowed:
                raise TierError(
                    f"{where}: harness {harness!r} is not allowed for "
                    f"profile {prof.name!r} (allowed: {', '.join(allowed)})"
                )
            model = entry.model or DEFAULT_MODEL
            choices = _model_choices(prof, harness, doc)
            if model not in choices:
                raise TierError(
                    f"{where}: model {model!r} is not one harness {harness!r} "
                    f"accepts for profile {prof.name!r} (choose from: "
                    f"{', '.join(choices)})"
                )
            key = (entry.profile, harness, model)
            if key in seen:
                raise TierError(
                    f"{where}: duplicate of ({entry.profile!r}, {harness!r}, "
                    f"{model!r}) in this tier"
                )
            seen.add(key)
    return block


# --------------------------------------------------------------------------- #
# storage
# --------------------------------------------------------------------------- #

def load(doc: Optional[dict] = None) -> Dict[str, object]:
    """The stored block, cleaned. Absent or corrupt reads as empty."""
    doc = store.load() if doc is None else doc
    return _clean(doc.get("model_tiers"))


def save(raw: object) -> Dict[str, object]:
    """Validate ``raw`` and replace the stored block with it."""
    block = validate(raw)

    def _mutate(doc: dict) -> None:
        doc["model_tiers"] = block

    store.update(_mutate)
    return block


# --------------------------------------------------------------------------- #
# resolution
# --------------------------------------------------------------------------- #

def resolve(tier_id: str, doc: Optional[dict] = None) -> Optional[Tuple[str, str, str]]:
    """``(profile, harness, model)`` for ``tier_id`` right now, or ``None``.

    The active pointer picks the entry (default: the first); a missing
    ``model`` resolves to the ``default`` role word and a missing ``harness``
    to the profile's effective harness. Only ``store.load`` state is read,
    so every call sees the freshest config — that is what lets a caller
    switch entries inside one tier live.
    """
    doc = store.load() if doc is None else doc
    block = _clean(doc.get("model_tiers"))
    for tier in block["tiers"]:  # type: ignore[index]
        if tier["id"] != tier_id:
            continue
        entries = tier["entries"]
        if not entries:
            return None
        index = block["active"].get(tier_id, 0)  # type: ignore[union-attr]
        if index >= len(entries):
            index = 0
        entry = _clean_entry(entries[index])
        if entry is None:
            return None
        try:
            prof = profile_mod.require(entry.profile)
            harness = entry.harness or lineage.effective_harness(prof, doc)
        except (profile_mod.ProfileError, lineage.LineageError):
            return None
        return (entry.profile, harness, entry.model or DEFAULT_MODEL)
    return None


def tier_ids(doc: Optional[dict] = None) -> List[str]:
    """Every tier id currently stored, in order."""
    doc = store.load() if doc is None else doc
    return [t["id"] for t in _clean(doc.get("model_tiers"))["tiers"]]  # type: ignore[index]
