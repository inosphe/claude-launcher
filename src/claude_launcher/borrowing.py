"""Borrow another base profile's authentication without borrowing its harness.

The running profile selector owns the program and storage.  ``--borrow`` owns
only the credential source, and therefore always names a *base* profile.  This
module is the single answer to both questions every caller needs:

* is that combination legal for the selected harness (``allowed``)?
* is usable credential material present right now (``ready``)?

The first is a hard request boundary.  The second is deliberately live
validation: a stored session may outlive a token, an OAuth credential can
expire, and the Web detail rail must report that drift without exposing the
secret itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from . import (
    credentials,
    harness_policy,
    harnesses,
    lineage,
    profile as profile_mod,
    providers,
)
from .profile import Profile


class BorrowError(Exception):
    """Raised when a borrow request cannot name a legal credential source."""


@dataclass(frozen=True)
class BorrowValidation:
    """A secret-free, JSON-ready verdict for one runtime/lender pair."""

    allowed: bool
    ready: bool
    status: str
    message: str
    harness: str
    mode: str
    lender: Optional[str] = None
    provider: Optional[str] = None
    token_env: Optional[str] = None
    credential: Optional[str] = None
    source_profile: Optional[str] = None

    @property
    def valid(self) -> bool:
        return self.allowed and self.ready

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "ready": self.ready,
            "valid": self.valid,
            "status": self.status,
            "message": self.message,
            "harness": self.harness,
            "mode": self.mode,
            "lender": self.lender,
            "provider": self.provider,
            "token_env": self.token_env,
            "credential": self.credential,
            "source_profile": self.source_profile,
        }


def capability(entry: Optional[harnesses.Harness]) -> dict:
    """Return the declared borrow capability for a harness, without a lender."""
    if entry is None:
        return {
            "allowed": False,
            "mode": "none",
            "message": "the selected harness is not declared",
        }
    if entry.borrow_mode == "provider-token":
        return {
            "allowed": True,
            "mode": "provider-token",
            "message": "borrows the base profile's token and Claude provider/backend",
        }
    if entry.borrow_mode == "token":
        return {
            "allowed": True,
            "mode": "token",
            "message": (
                "borrows the base profile's shared token and exports it as "
                f"{entry.token_env}"
            ),
        }
    return {
        "allowed": False,
        "mode": "none",
        "message": (
            f"harness {entry.name!r} uses {entry.auth} auth in its own storage; "
            "select that base profile's harness instead of borrowing a token"
        ),
    }


def _canonical_token_source(profile: Profile) -> Optional[Profile]:
    """Nearest profile that contributes the shared launcher token."""
    for candidate in reversed(lineage.chain(profile)):
        if credentials.stored_token(candidate):
            return candidate
    return None


def _claude_default_credential(profile: Profile) -> Tuple[str, Optional[Profile]]:
    """Credential kind/source selected by Claude's existing token precedence."""
    for candidate in reversed(lineage.chain(profile)):
        if credentials.stored_token(candidate):
            return "launcher-token", candidate
        if credentials.credentials_token(candidate):
            state = credentials.token_state(candidate)
            kind = "expired-oauth" if state == "expired" else "claude-oauth"
            return kind, candidate
    return "missing", None


def _base_lender(
    value: str,
) -> Tuple[Optional[Profile], Optional[str], Optional[str]]:
    """Resolve a base lender, returning ``(profile, status, message)``."""
    raw = str(value or "").strip()
    if not raw:
        return None, "missing-lender", "borrow needs a base profile name"
    try:
        name, override = profile_mod.split_selector(raw)
    except profile_mod.ProfileError as exc:
        return None, "invalid-lender", str(exc)
    if override:
        return (
            None,
            "base-profile-required",
            f"borrow targets a base profile; use {name!r}, not {raw!r}",
        )
    try:
        return profile_mod.require(name), None, None
    except profile_mod.ProfileError as exc:
        return None, "missing-profile", str(exc)


def validate(
    runtime_profile: Profile,
    lender_value: str,
    *,
    entry: Optional[harnesses.Harness] = None,
    provider_override: Optional[str] = None,
) -> BorrowValidation:
    """Validate one borrow without returning or serializing any secret value."""
    if entry is None:
        try:
            entry = harnesses.get(lineage.effective_harness(runtime_profile))
        except lineage.LineageError as exc:
            return BorrowValidation(
                False, False, "invalid-runtime-profile", str(exc), "?", "none"
            )
    harness_name = entry.name if entry else "?"
    cap = capability(entry)
    if not cap["allowed"]:
        return BorrowValidation(
            False,
            False,
            "unsupported-harness",
            cap["message"],
            harness_name,
            cap["mode"],
            lender=str(lender_value or "").strip() or None,
        )

    try:
        runtime_policy = harness_policy.evaluate(
            runtime_profile,
            harness_name,
            provider_override=provider_override,
        )
    except (
        harness_policy.HarnessPolicyError,
        lineage.LineageError,
        providers.ProviderError,
    ) as exc:
        return BorrowValidation(
            False,
            False,
            "invalid-harness-policy",
            str(exc),
            harness_name,
            cap["mode"],
            lender=str(lender_value or "").strip() or None,
        )
    if not runtime_policy.allowed:
        return BorrowValidation(
            False,
            False,
            "harness-policy-denied",
            runtime_policy.reason,
            harness_name,
            cap["mode"],
            lender=str(lender_value or "").strip() or None,
        )

    lender, status, message = _base_lender(lender_value)
    if lender is None:
        return BorrowValidation(
            False,
            False,
            status or "invalid-lender",
            message or "invalid borrow profile",
            harness_name,
            cap["mode"],
            lender=str(lender_value or "").strip() or None,
            token_env=entry.token_env if entry else None,
        )

    try:
        lender_policy = harness_policy.evaluate(
            lender,
            harness_name,
            provider_override=provider_override,
        )
    except (
        harness_policy.HarnessPolicyError,
        lineage.LineageError,
        providers.ProviderError,
    ) as exc:
        return BorrowValidation(
            False,
            False,
            "invalid-harness-policy",
            str(exc),
            harness_name,
            cap["mode"],
            lender=lender.name,
        )
    if not lender_policy.allowed:
        return BorrowValidation(
            False,
            False,
            "harness-policy-denied",
            lender_policy.reason,
            harness_name,
            cap["mode"],
            lender=lender.name,
        )

    try:
        if cap["mode"] == "token":
            source = _canonical_token_source(lender)
            route = entry.token_env if entry else None
            if source is None:
                return BorrowValidation(
                    True,
                    False,
                    "missing-token",
                    (
                        f"profile {lender.name!r} has no shared token for "
                        f"harness {harness_name!r}"
                    ),
                    harness_name,
                    cap["mode"],
                    lender=lender.name,
                    token_env=route,
                    credential="missing",
                )
            return BorrowValidation(
                True,
                True,
                "ready",
                (
                    f"the shared token from profile {lender.name!r} will be "
                    f"exported as {route}"
                ),
                harness_name,
                cap["mode"],
                lender=lender.name,
                token_env=route,
                credential="launcher-token",
                source_profile=source.name,
            )

        provider = provider_override or providers.resolve_name(lender)
        if providers.uses_anthropic_oauth(provider):
            kind, source = _claude_default_credential(lender)
            if kind == "missing":
                return BorrowValidation(
                    True,
                    False,
                    "missing-token",
                    f"profile {lender.name!r} has no usable Claude token to borrow",
                    harness_name,
                    cap["mode"],
                    lender=lender.name,
                    provider=provider,
                    token_env="CLAUDE_CODE_OAUTH_TOKEN",
                    credential=kind,
                )
            if kind == "expired-oauth":
                return BorrowValidation(
                    True,
                    False,
                    "expired-token",
                    f"profile {lender.name!r} has an expired Claude OAuth token",
                    harness_name,
                    cap["mode"],
                    lender=lender.name,
                    provider=provider,
                    token_env="CLAUDE_CODE_OAUTH_TOKEN",
                    credential=kind,
                    source_profile=source.name if source else None,
                )
            return BorrowValidation(
                True,
                True,
                "ready",
                (
                    f"the {kind} from profile {lender.name!r} will be injected as "
                    "CLAUDE_CODE_OAUTH_TOKEN"
                ),
                harness_name,
                cap["mode"],
                lender=lender.name,
                provider=provider,
                token_env="CLAUDE_CODE_OAUTH_TOKEN",
                credential=kind,
                source_profile=source.name if source else None,
            )

        route = entry.token_env if entry else "ANTHROPIC_AUTH_TOKEN"
        source = _canonical_token_source(lender)
        configured = False
        if source is None:
            provider_env = providers.provider_env(provider)
            lender_env = lineage.effective_env(lender)
            configured = bool(lender_env.get(route) or provider_env.get(route))
        if source is None and not configured:
            return BorrowValidation(
                True,
                False,
                "missing-token",
                (
                    f"profile {lender.name!r} has no shared token or configured "
                    f"{route} for provider {provider!r}"
                ),
                harness_name,
                cap["mode"],
                lender=lender.name,
                provider=provider,
                token_env=route,
                credential="missing",
            )
        kind = "launcher-token" if source is not None else "configured-env"
        return BorrowValidation(
            True,
            True,
            "ready",
            (
                f"the {kind} from profile {lender.name!r} will authenticate "
                f"provider {provider!r} "
                f"through {route}"
            ),
            harness_name,
            cap["mode"],
            lender=lender.name,
            provider=provider,
            token_env=route,
            credential=kind,
            source_profile=source.name if source else lender.name,
        )
    except (lineage.LineageError, providers.ProviderError) as exc:
        return BorrowValidation(
            False,
            False,
            "invalid-auth-config",
            str(exc),
            harness_name,
            cap["mode"],
            lender=lender.name,
            token_env=entry.token_env if entry else None,
        )


def require_allowed(
    runtime_profile: Profile,
    lender_value: str,
    *,
    entry: Optional[harnesses.Harness] = None,
    provider_override: Optional[str] = None,
) -> Tuple[Profile, BorrowValidation]:
    """Return the canonical lender or raise for a structurally illegal borrow."""
    report = validate(
        runtime_profile,
        lender_value,
        entry=entry,
        provider_override=provider_override,
    )
    if not report.allowed or not report.lender:
        raise BorrowError(report.message)
    return profile_mod.require(report.lender), report
