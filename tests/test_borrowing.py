"""The base-profile credential source shared by Claude and API-key harnesses."""

from __future__ import annotations

import json

import pytest

from claude_launcher import borrowing, credentials, harnesses, lineage, profile, store


def test_capability_comes_from_the_harness_auth_contract(home):
    assert borrowing.capability(harnesses.get("claude"))["mode"] == "provider-token"
    assert borrowing.capability(harnesses.get("pi"))["mode"] == "token"
    for name in ("codex", "kimi", "agent"):
        report = borrowing.capability(harnesses.get(name))
        assert report["allowed"] is False
        assert report["mode"] == "none"


def test_borrow_requires_a_base_profile_not_a_harness_selector(home):
    runtime = profile.create("work")
    profile.create("ds4")

    report = borrowing.validate(runtime, "ds4:claude")

    assert report.allowed is False
    assert report.status == "base-profile-required"
    assert "use 'ds4'" in report.message
    with pytest.raises(borrowing.BorrowError, match="base profile"):
        borrowing.require_allowed(runtime, "ds4:claude")


def test_claude_default_borrow_reports_the_route_without_the_secret(home):
    runtime = profile.create("work")
    lender = profile.create("ds4")
    credentials.save_token(lender, "secret-never-serialize-me")

    report = borrowing.validate(runtime, "ds4")
    body = report.to_dict()

    assert report.allowed and report.ready and report.valid
    assert body["mode"] == "provider-token"
    assert body["provider"] == "default"
    assert body["token_env"] == "CLAUDE_CODE_OAUTH_TOKEN"
    assert body["credential"] == "launcher-token"
    assert body["source_profile"] == "ds4"
    assert "secret-never-serialize-me" not in json.dumps(body)


def test_claude_borrow_marks_an_expired_native_login_not_ready(home):
    runtime = profile.create("work")
    lender = profile.create("ds4")
    (lender.config_dir / credentials.CREDENTIALS_FILENAME).write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "expired-secret",
                    "expiresAt": 1,
                    "scopes": ["user:profile"],
                }
            }
        ),
        encoding="utf-8",
    )

    report = borrowing.validate(runtime, "ds4")

    assert report.allowed is True
    assert report.ready is False
    assert report.valid is False
    assert report.status == "expired-token"


def test_claude_provider_borrow_accepts_token_or_configured_auth(home):
    runtime = profile.create("work")
    lender = profile.create("ds4")
    store.update(
        lambda doc: doc.update(
            {
                "providers": {
                    "kimi": {
                        "env": {
                            "ANTHROPIC_BASE_URL": "https://example.invalid",
                            "ANTHROPIC_AUTH_TOKEN": "configured-secret",
                        }
                    }
                }
            }
        )
    )
    store.set_profile_field("ds4", "provider", "kimi")

    configured = borrowing.validate(runtime, "ds4")
    assert configured.valid
    assert configured.provider == "kimi"
    assert configured.token_env == "ANTHROPIC_AUTH_TOKEN"
    assert configured.credential == "configured-env"
    assert "configured-secret" not in json.dumps(configured.to_dict())

    credentials.save_token(lender, "canonical-secret")
    canonical = borrowing.validate(runtime, "ds4")
    assert canonical.valid
    assert canonical.credential == "launcher-token"


def test_api_key_harness_borrows_only_the_inherited_shared_token(home):
    runtime = profile.create("work")
    lender = profile.create("ds4")
    parent = profile.create("account")
    lineage.set_parent(lender, parent.name)
    credentials.save_token(parent, "pi-secret")
    selected = profile.resolve_selector("work:pi")

    report = borrowing.validate(selected, "ds4")

    assert report.valid
    assert report.mode == "token"
    assert report.token_env == "ANTHROPIC_API_KEY"
    assert report.source_profile == "account"
    assert report.provider is None


def test_missing_token_is_allowed_but_live_validation_is_not_ready(home):
    runtime = profile.create("work")
    profile.create("ds4")

    lender, report = borrowing.require_allowed(runtime, "ds4")

    assert lender.name == "ds4"
    assert report.allowed is True
    assert report.ready is False
    assert report.status == "missing-token"


def test_oauth_harness_cannot_borrow_even_when_lender_has_a_token(home):
    profile.create("work")
    lender = profile.create("ds4")
    credentials.save_token(lender, "secret")
    runtime = profile.resolve_selector("work:kimi")

    report = borrowing.validate(runtime, "ds4")

    assert not report.allowed
    assert report.status == "unsupported-harness"
    assert "oauth" in report.message
