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
