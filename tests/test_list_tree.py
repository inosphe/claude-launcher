"""'claunch ls' prints profiles as an indented inheritance tree with providers."""

from __future__ import annotations

from claude_launcher import cli, lineage, profile, providers, store


def run(*argv):
    return cli.main(list(argv))


def test_list_is_indented_by_lineage(home, capsys):
    for name in ("company", "personal", "work", "deep", "zeta"):
        profile.create(name)
    lineage.set_parent(profile.require("work"), "company")
    lineage.set_parent(profile.require("deep"), "work")
    capsys.readouterr()

    assert run("ls") == 0
    out = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    names = [line.split("[")[0].rstrip() for line in out]
    assert names == ["company", "  work", "    deep", "personal", "zeta"]


def _cells(out: str) -> dict:
    """Map profile name -> provider cell from an 'ls' listing."""
    cells = {}
    for line in out.splitlines():
        if not line.strip():
            continue
        name, rest = line.split("[", 1)
        cells[name.strip()] = rest.split("]", 1)[1].split()[0]
    return cells


def test_provider_column_shows_pinned_inherited_and_default(home, capsys):
    store.update(
        lambda doc: doc.update(
            {"providers": {"glm": {"env": {"ANTHROPIC_BASE_URL": "https://glm"}}}}
        )
    )
    base = profile.create("base")
    child = profile.create("child")
    plain = profile.create("plain")
    lineage.set_parent(child, "base")
    providers.set_profile_selection(base, "glm")
    capsys.readouterr()

    assert run("ls") == 0
    cells = _cells(capsys.readouterr().out)
    assert cells["base"] == "glm"  # pinned on this profile
    assert cells["child"] == "(glm)"  # inherited from the parent
    assert cells["plain"] == "-"  # plain anthropic, nothing pinned


def test_provider_column_reflects_global_selection(home, capsys):
    store.update(
        lambda doc: doc.update(
            {"providers": {"glm": {"env": {"ANTHROPIC_BASE_URL": "https://glm"}}}}
        )
    )
    profile.create("solo")
    pinned = profile.create("pinned")
    providers.set_active("glm")
    providers.set_profile_selection(pinned, providers.DEFAULT_PROVIDER)
    capsys.readouterr()

    assert run("ls") == 0
    cells = _cells(capsys.readouterr().out)
    assert cells["solo"] == "(glm)"  # from the global provider
    assert cells["pinned"] == "default"  # explicitly pinned back to anthropic


def test_missing_and_cycle_parents_become_roots(home, capsys):
    for name in ("a", "b", "lost"):
        profile.create(name)
    store.set_profile_field("lost", "parent", "ghost")
    store.set_profile_field("a", "parent", "b")
    store.set_profile_field("b", "parent", "a")
    capsys.readouterr()

    assert run("ls") == 0
    out = capsys.readouterr().out
    assert "(parent: ghost, missing)" in out
    assert "cycle" in out
    assert len([line for line in out.splitlines() if line.strip()]) == 3
