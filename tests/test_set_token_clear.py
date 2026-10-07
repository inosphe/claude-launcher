"""``claunch set-token NAME --clear``: drop the stored token, inherit again."""

from __future__ import annotations

import pytest

from claude_launcher import cli, credentials, lineage, profile


def _family():
    base = profile.create("base")
    child = profile.create("child")
    lineage.set_parent(child, "base")
    credentials.save_token(base, "sk-PARENT")
    credentials.save_token(child, "sk-CHILD")
    return base, child


def test_clear_falls_back_to_parent_token(home, capsys):
    base, child = _family()
    assert lineage.stored_auth_token(child) == "sk-CHILD"

    assert cli.main(["set-token", "child", "--clear"]) == 0

    assert "cleared stored token" in capsys.readouterr().out
    assert credentials.stored_token(child) is None
    assert lineage.stored_auth_token(child) == "sk-PARENT"
    assert lineage.lookup_token(child) == "sk-PARENT"
    assert credentials.stored_token(base) == "sk-PARENT"


def test_clear_removes_legacy_key_so_it_is_not_migrated_back(home):
    p = profile.create("work")
    (p.config_dir / credentials.LEGACY_API_KEY_FILENAME).write_text("sk-OLD\n")

    assert credentials.clear_token(p) == [credentials.LEGACY_API_KEY_FILENAME]
    assert credentials.migrate_legacy_api_key(p) is False
    assert credentials.stored_token(p) is None


def test_clear_keeps_native_login_credentials(home):
    p = profile.create("work")
    native = p.config_dir / credentials.CREDENTIALS_FILENAME
    native.write_text("{}")
    credentials.save_token(p, "sk-X")

    assert credentials.clear_token(p) == [credentials.TOKEN_FILENAME]
    assert native.is_file()


def test_clear_without_stored_token_reports_and_succeeds(home, capsys):
    profile.create("work")
    assert cli.main(["set-token", "work", "--clear"]) == 0
    assert "has no stored token" in capsys.readouterr().out


def test_clear_with_token_value_is_refused(home):
    p = profile.create("work")
    credentials.save_token(p, "sk-KEEP")
    with pytest.raises(credentials.CredentialsError):
        cli._cmd_set_token(
            cli.argparse.Namespace(name="work", token="sk-NEW", clear=True)
        )
    assert credentials.stored_token(p) == "sk-KEEP"
