"""Template: the live layer under every profile; template.yaml seeds a fresh store."""

from __future__ import annotations

import yaml

from claude_launcher import lineage, profile, settings, store, template


def test_env_reads_live_store(home):
    store.set_template_env({"K": "v"})
    assert template.env() == {"K": "v"}


def test_set_env_writes_store(home):
    template.set_env({"A": "1"})
    assert store.template_env() == {"A": "1"}


def test_template_env_applies_without_copying_it(home):
    template.set_env({"D": "1"})
    p = profile.create("work")
    assert settings.get_env(p) == {}
    assert lineage.effective_env(p) == {"D": "1"}
    # A later change reaches the profile: nothing was copied at create.
    template.set_env({"D": "2"})
    assert lineage.effective_env(p) == {"D": "2"}


def test_resolve_layers_clears_downward_only():
    bottom = {"a": "1", "b": "1", "o": {"x": "1", "y": "1"}}
    middle = {"b": None, "o": {"x": None}}
    top = {"a": "3"}
    out = template.resolve_layers([bottom, middle, top])
    assert out == [{"a": "1", "o": {"y": "1"}}, {"o": {}}, {"a": "3"}]
    # The caller's documents are left as they were.
    assert middle == {"b": None, "o": {"x": None}}


def test_ensure_file_writes_template_yaml(home):
    path = template.ensure_file()
    assert path.name == "template.yaml"
    assert path.is_file()
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["template"] == template.DEFAULT_TEMPLATE


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
    assert doc["template"] == template.DEFAULT_TEMPLATE
    assert doc["profiles"] == {}
