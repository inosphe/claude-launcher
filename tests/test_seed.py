"""Seeding must not carry an env block out of the SSOT into settings.json."""

from __future__ import annotations

import json

from claude_launcher import profile, seed


def test_seed_strips_env_keeps_other_keys(home, tmp_path, monkeypatch):
    src = tmp_path / "seedsrc"
    src.mkdir()
    (src / "settings.json").write_text(
        json.dumps({"env": {"LEAK": "1"}, "mcpServers": {"s": {"command": "x"}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAUDE_LAUNCHER_SEED", str(src))

    p = profile.create("work")
    seed.seed_profile(p)

    data = json.loads((p.config_dir / "settings.json").read_text(encoding="utf-8"))
    assert "env" not in data  # the hidden source is gone
    assert data["mcpServers"] == {"s": {"command": "x"}}  # native config kept


def test_seed_without_env_is_copied_verbatim(home, tmp_path, monkeypatch):
    src = tmp_path / "seedsrc"
    src.mkdir()
    (src / "settings.json").write_text(
        json.dumps({"mcpServers": {}}), encoding="utf-8"
    )
    monkeypatch.setenv("CLAUDE_LAUNCHER_SEED", str(src))
    p = profile.create("work")
    assert "settings.json" in seed.seed_profile(p)


def test_seed_missing_only_fills_what_the_profile_lacks(home, tmp_path, monkeypatch):
    src = tmp_path / "seedsrc"
    src.mkdir()
    (src / ".claude.json").write_text(json.dumps({"global": True}), encoding="utf-8")
    (src / "settings.json").write_text(json.dumps({"global": True}), encoding="utf-8")
    monkeypatch.setenv("CLAUDE_LAUNCHER_SEED", str(src))

    p = profile.create("work")
    own = p.config_dir / "settings.json"
    own.write_text(json.dumps({"own": True}), encoding="utf-8")

    assert seed.seed_profile(p, missing_only=True) == [".claude.json"]
    assert json.loads(own.read_text(encoding="utf-8")) == {"own": True}

    # A plain seed still replaces it: that is what a first create wants.
    assert set(seed.seed_profile(p)) == {".claude.json", "settings.json"}
    assert json.loads(own.read_text(encoding="utf-8")) == {"global": True}
