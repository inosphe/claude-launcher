"""Template: live default-env in the store; template.yaml seeds a fresh store."""

from __future__ import annotations

import yaml

from claude_launcher import profile, settings, store, template


def test_env_reads_live_store(home):
    store.set_template_env({"K": "v"})
    assert template.env() == {"K": "v"}


def test_set_env_writes_store(home):
    template.set_env({"A": "1"})
    assert store.template_env() == {"A": "1"}


def test_apply_to_merges_template_into_profile(home):
    template.set_env({"D": "1"})
    p = profile.create("work")
    template.apply_to(p)
    assert settings.get_env(p)["D"] == "1"


def test_ensure_file_writes_template_yaml(home):
    path = template.ensure_file()
    assert path.name == "template.yaml"
    assert path.is_file()
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["template"]["env"] == template.DEFAULT_ENV


def test_default_document_uses_template_yaml(home):
    path = template.template_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump({"template": {"env": {"FROM": "file"}}}), encoding="utf-8"
    )
    doc = template.default_document()
    assert doc["template"]["env"] == {"FROM": "file"}
    # Always a complete, valid skeleton.
    assert doc["version"] == store.VERSION
    assert doc["profiles"] == {}


def test_default_document_builtin_when_no_file(home):
    doc = template.default_document()
    assert doc["template"]["env"] == template.DEFAULT_ENV
    assert doc["profiles"] == {}
