"""Profile/provider allow-lists for selecting an execution harness.

``allowed_harnesses`` is an optional additive policy in ``~/.claunch.yaml``.
Every profile constraint in the inheritance chain and the effective provider's
constraint must admit a harness.  Missing fields add no restriction; an empty
list deliberately admits none.  Keeping the verdict here gives CLI, daemon,
borrow validation and Web selector generation one answer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from . import harnesses, lineage, providers, store
from .profile import Profile

FIELD = "allowed_harnesses"
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class HarnessPolicyError(Exception):
    """Raised for malformed policy or a harness denied by it."""


@dataclass(frozen=True)
class HarnessPolicy:
    allowed: bool
    profile: str
    harness: str
    provider: str
    reason: str
    profile_constraints: Tuple[Tuple[str, Tuple[str, ...]], ...] = ()
    provider_constraint: Optional[Tuple[str, ...]] = None

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "profile": self.profile,
            "harness": self.harness,
            "provider": self.provider,
            "reason": self.reason,
            "profile_constraints": [
                {"profile": name, FIELD: list(names)}
                for name, names in self.profile_constraints
            ],
            "provider_constraint": (
                list(self.provider_constraint)
                if self.provider_constraint is not None
                else None
            ),
        }


def _names(entry: dict, where: str) -> Optional[Tuple[str, ...]]:
    """Parse one optional allow-list, preserving an explicit empty list."""
    if FIELD not in entry or entry.get(FIELD) is None:
        return None
    raw = entry.get(FIELD)
    if not isinstance(raw, (list, tuple)):
        raise HarnessPolicyError(f"{where}.{FIELD} must be a list")
    out: List[str] = []
    for value in raw:
        name = str(value).strip()
        if not name or not _NAME_RE.fullmatch(name):
            raise HarnessPolicyError(
                f"{where}.{FIELD} contains invalid harness name {name!r}"
            )
        if name not in out:
            out.append(name)
    return tuple(out)


def profile_constraints(
    profile: Profile, doc: Optional[dict] = None
) -> Tuple[Tuple[str, Tuple[str, ...]], ...]:
    doc = store.load() if doc is None else doc
    constraints = []
    for item in lineage.chain(profile, doc):
        names = _names(
            store.profile_entry(item.name, doc), f"profiles.{item.name}"
        )
        if names is not None:
            constraints.append((item.name, names))
    return tuple(constraints)


def provider_constraint(
    name: str, doc: Optional[dict] = None
) -> Optional[Tuple[str, ...]]:
    doc = store.load() if doc is None else doc
    section = doc.get("providers")
    spec = section.get(name) if isinstance(section, dict) else None
    if not isinstance(spec, dict):
        return None
    return _names(spec, f"providers.{name}")


def evaluate(
    profile: Profile,
    harness_name: str,
    *,
    doc: Optional[dict] = None,
    provider_override: Optional[str] = None,
    include_provider: bool = True,
) -> HarnessPolicy:
    """Return why ``profile:harness`` is allowed or denied."""
    doc = store.load() if doc is None else doc
    name = str(harness_name or "").strip()
    provider = (
        provider_override
        if provider_override is not None
        else providers.resolve_name(profile, doc)
    )
    # Resolve an override now even when it names the built-in default; an
    # unknown provider is a config error, not an unrestricted provider.
    providers.provider_env(provider, doc)
    constraints = profile_constraints(profile, doc)
    pconstraint = provider_constraint(provider, doc) if include_provider else None

    for owner, allowed in constraints:
        if name not in allowed:
            shown = ", ".join(allowed) or "(none)"
            return HarnessPolicy(
                False,
                profile.name,
                name,
                provider,
                (
                    f"profile {owner!r} allows only harnesses [{shown}]; "
                    f"{name!r} is not allowed for {profile.selector!r}"
                ),
                constraints,
                pconstraint,
            )
    if pconstraint is not None and name not in pconstraint:
        shown = ", ".join(pconstraint) or "(none)"
        return HarnessPolicy(
            False,
            profile.name,
            name,
            provider,
            (
                f"provider {provider!r} allows only harnesses [{shown}]; "
                f"{name!r} is not allowed for {profile.selector!r}"
            ),
            constraints,
            pconstraint,
        )
    return HarnessPolicy(
        True,
        profile.name,
        name,
        provider,
        "allowed",
        constraints,
        pconstraint,
    )


def require(
    profile: Profile,
    harness_name: str,
    *,
    doc: Optional[dict] = None,
    provider_override: Optional[str] = None,
    include_provider: bool = True,
) -> HarnessPolicy:
    report = evaluate(
        profile,
        harness_name,
        doc=doc,
        provider_override=provider_override,
        include_provider=include_provider,
    )
    if not report.allowed:
        raise HarnessPolicyError(report.reason)
    return report


def allowed_names(profile: Profile, doc: Optional[dict] = None) -> List[str]:
    """Declared harnesses admitted by all effective constraints."""
    doc = store.load() if doc is None else doc
    return [
        name
        for name in harnesses.names(doc)
        if evaluate(profile, name, doc=doc).allowed
    ]
