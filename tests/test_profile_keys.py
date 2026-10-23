"""Several launcher tokens per profile, and the strategy that picks one."""

from __future__ import annotations

import json

import pytest

from claude_launcher import (
    cli,
    credentials,
    lineage,
    profile,
    profile_keys,
    runner,
    usage,
)


def _pooled(*tokens, name="work", strategy=None):
    p = profile.create(name)
    for token in tokens:
        profile_keys.add(p, token)
    if strategy:
        profile_keys.set_strategy(p, strategy)
    return p


# --- storage and migration --------------------------------------------------


def test_pool_file_is_owner_only_and_lists_masked_keys(home):
    p = _pooled("sk-AAAA1111BBBB", "sk-CCCC2222DDDD")

    file = profile_keys.path(p)
    assert file.is_file()
    assert oct(file.stat().st_mode & 0o777) == oct(0o600)

    data = profile_keys.summary(p)
    assert data["pooled"] is True
    assert [k["masked"] for k in data["keys"]] == ["sk-A…BBBB", "sk-C…DDDD"]
    assert "sk-AAAA1111BBBB" not in json.dumps(data)


def test_adding_a_key_keeps_the_single_token_as_the_first_key(home):
    p = profile.create("work")
    credentials.save_token(p, "sk-SINGLE")

    added = profile_keys.add(p, "sk-SECOND")

    pool = profile_keys.load(p)
    assert [k.token for k in pool.keys] == ["sk-SINGLE", "sk-SECOND"]
    assert pool.keys[1].id == added.id
    assert credentials.stored_token(p) == "sk-SECOND"


def test_duplicate_and_empty_keys_are_refused(home):
    p = _pooled("sk-AAAA1111BBBB")
    with pytest.raises(profile_keys.KeysError, match="already holds"):
        profile_keys.add(p, "sk-AAAA1111BBBB")
    with pytest.raises(profile_keys.KeysError, match="empty"):
        profile_keys.add(p, "   ")


def test_a_profile_without_a_pool_reads_its_one_token(home):
    p = profile.create("work")
    credentials.save_token(p, "sk-ONLY")

    assert profile_keys.load(p) is None
    assert profile_keys.peek(p) is None
    assert profile_keys.summary(p)["pooled"] is False
    assert credentials.stored_token(p) == "sk-ONLY"


def test_unreadable_pool_is_an_error_not_an_empty_pool(home):
    p = profile.create("work")
    profile_keys.path(p).write_text("{not json", encoding="utf-8")

    with pytest.raises(profile_keys.KeysError, match="cannot read key pool"):
        profile_keys.load(p)


def test_unknown_strategy_in_the_file_is_refused(home):
    p = _pooled("sk-AAAA1111BBBB")
    raw = json.loads(profile_keys.path(p).read_text(encoding="utf-8"))
    raw["strategy"] = "weighted"
    profile_keys.path(p).write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(profile_keys.KeysError, match="unknown strategy"):
        profile_keys.load(p)


# --- rotation ---------------------------------------------------------------


def test_rotation_visits_every_key_in_order_and_writes_the_token(home):
    p = _pooled("sk-AAAA1111BBBB", "sk-CCCC2222DDDD", "sk-EEEE3333FFFF")

    seen = []
    for _ in range(4):
        selection = profile_keys.select_for_launch(p)
        seen.append(selection.key.token)
        assert credentials.stored_token(p) == selection.key.token

    assert seen == [
        "sk-AAAA1111BBBB",
        "sk-CCCC2222DDDD",
        "sk-EEEE3333FFFF",
        "sk-AAAA1111BBBB",
    ]
    pool = profile_keys.load(p)
    assert [k.use_count for k in pool.keys] == [2, 1, 1]
    assert all(k.last_used_at for k in pool.keys)


def test_peek_reports_without_consuming(home):
    p = _pooled("sk-AAAA1111BBBB", "sk-CCCC2222DDDD")
    before = credentials.stored_token(p)

    first = profile_keys.peek(p)
    second = profile_keys.peek(p)

    assert first.key.id == second.key.id
    assert credentials.stored_token(p) == before
    assert profile_keys.load(p).keys[0].use_count == 0


def test_rotation_starts_at_the_first_key_when_the_cursor_is_gone(home):
    p = _pooled("sk-AAAA1111BBBB", "sk-CCCC2222DDDD")
    pool = profile_keys.load(p)
    pool.cursor = "k-gone"
    profile_keys.save(p, pool)

    assert profile_keys.select_for_launch(p).key.token == "sk-AAAA1111BBBB"


# --- random -----------------------------------------------------------------


def test_random_picks_inside_the_pool(home, monkeypatch):
    p = _pooled("sk-AAAA1111BBBB", "sk-CCCC2222DDDD", "sk-EEEE3333FFFF")
    profile_keys.set_strategy(p, "random")

    picks = iter([0, 2, 2])
    monkeypatch.setattr(profile_keys.random, "randrange", lambda n: next(picks))

    assert profile_keys.select_for_launch(p).key.token == "sk-AAAA1111BBBB"
    assert profile_keys.select_for_launch(p).key.token == "sk-EEEE3333FFFF"
    assert profile_keys.select_for_launch(p).key.token == "sk-EEEE3333FFFF"


# --- usage ------------------------------------------------------------------


def test_usage_min_and_max_pick_the_measured_extremes(home, monkeypatch):
    p = _pooled("sk-AAAA1111BBBB", "sk-CCCC2222DDDD", "sk-EEEE3333FFFF")
    usage_by_token = {
        "sk-AAAA1111BBBB": 40.0,
        "sk-CCCC2222DDDD": 10.0,
        "sk-EEEE3333FFFF": 90.0,
    }
    monkeypatch.setattr(
        usage, "utilization_for_token", lambda prof, token: usage_by_token[token]
    )

    profile_keys.set_strategy(p, "usage-min")
    assert profile_keys.select_for_launch(p).key.token == "sk-CCCC2222DDDD"

    profile_keys.set_strategy(p, "usage-max")
    assert profile_keys.select_for_launch(p).key.token == "sk-EEEE3333FFFF"


def test_usage_strategy_skips_keys_without_a_reading(home, monkeypatch):
    p = _pooled("sk-AAAA1111BBBB", "sk-CCCC2222DDDD", "sk-EEEE3333FFFF")
    monkeypatch.setattr(
        usage,
        "utilization_for_token",
        lambda prof, token: {"sk-AAAA1111BBBB": 5.0, "sk-CCCC2222DDDD": None,
                             "sk-EEEE3333FFFF": 55.0}[token],
    )
    profile_keys.set_strategy(p, "usage-max")

    assert profile_keys.select_for_launch(p).key.token == "sk-EEEE3333FFFF"


def test_usage_strategy_falls_back_to_rotation_when_nothing_is_measurable(
    home, monkeypatch
):
    p = _pooled("sk-AAAA1111BBBB", "sk-CCCC2222DDDD")
    monkeypatch.setattr(usage, "utilization_for_token", lambda prof, token: None)
    profile_keys.set_strategy(p, "usage-min")

    selection = profile_keys.select_for_launch(p)

    assert selection.key.token == "sk-AAAA1111BBBB"
    assert selection.strategy == "rotation"
    assert "could be read" in selection.fallback


def test_usage_reader_does_not_measure_harness_login_readers(home, monkeypatch):
    p = _pooled("sk-AAAA1111BBBB")
    monkeypatch.setattr(usage, "reader", lambda prof: "codex-app-server")
    called = []
    monkeypatch.setattr(
        usage, "utilization_for_token", lambda prof, token: called.append(token) or 1.0
    )
    profile_keys.set_strategy(p, "usage-min")

    selection = profile_keys.select_for_launch(p)

    assert called == []
    assert selection.fallback


def test_utilization_for_token_returns_the_peak_window(home, monkeypatch):
    p = profile.create("work")
    report = usage.UsageReport(
        windows=[
            usage.UsageWindow(name="five_hour", utilization=12.5, resets_at=None),
            usage.UsageWindow(name="seven_day", utilization=61.0, resets_at=None),
        ],
        raw={},
    )
    monkeypatch.setattr(usage, "reader", lambda prof: "anthropic")
    monkeypatch.setattr(usage, "_fetch_claude", lambda prof, token=None: report)

    assert usage.utilization_for_token(p, "sk-ANY") == 61.0


# --- inheritance ------------------------------------------------------------


def test_a_child_profile_rotates_the_parent_pool(home):
    base = _pooled("sk-AAAA1111BBBB", "sk-CCCC2222DDDD", name="base")
    child = profile.create("child")
    lineage.set_parent(child, "base")

    first = profile_keys.select_for_launch(child)
    second = profile_keys.select_for_launch(child)

    assert first.owner.name == "base"
    assert [first.key.token, second.key.token] == [
        "sk-AAAA1111BBBB",
        "sk-CCCC2222DDDD",
    ]
    # The parent's token file is what the child's inheritance reads.
    assert credentials.stored_token(base) == "sk-CCCC2222DDDD"


# --- removal and CLI --------------------------------------------------------


def test_removing_the_last_key_removes_the_pool_and_token(home):
    p = _pooled("sk-AAAA1111BBBB")
    key = profile_keys.load(p).keys[0]

    profile_keys.remove(p, key.id)

    assert profile_keys.load(p) is None
    assert credentials.stored_token(p) is None


def test_removing_the_selected_key_promotes_a_survivor(home):
    p = _pooled("sk-AAAA1111BBBB", "sk-CCCC2222DDDD")
    selected = profile_keys.load(p).keys[1]

    profile_keys.remove(p, selected.id)

    assert credentials.stored_token(p) == "sk-AAAA1111BBBB"


def test_removing_another_key_leaves_the_effective_token_alone(home):
    p = _pooled("sk-AAAA1111BBBB", "sk-CCCC2222DDDD")
    # The second key was added last and is the effective token.
    profile_keys.remove(p, profile_keys.load(p).keys[0].id)

    assert credentials.stored_token(p) == "sk-CCCC2222DDDD"


def test_remove_accepts_a_one_based_index(home):
    p = _pooled("sk-AAAA1111BBBB", "sk-CCCC2222DDDD")

    with pytest.raises(profile_keys.KeysError, match="no key '9'"):
        profile_keys.remove(p, "9")

    gone = profile_keys.remove(p, "1")
    assert gone.token == "sk-AAAA1111BBBB"


def test_cli_add_list_and_strategy(home, capsys):
    profile.create("work")

    assert cli.main(["keys", "add", "work", "sk-AAAA1111BBBB", "--label", "home"]) == 0
    assert cli.main(["keys", "add", "work", "sk-CCCC2222DDDD", "--label", "office"]) == 0
    out = capsys.readouterr().out
    assert "added key k-" in out and "sk-C…DDDD" in out

    assert cli.main(["keys", "list", "work"]) == 0
    out = capsys.readouterr().out
    assert "2 keys, strategy rotation" in out
    assert "home" in out and "office" in out

    assert cli.main(["keys", "strategy", "work", "usage-min"]) == 0
    assert "strategy usage-min" in capsys.readouterr().out


def test_cli_select_is_a_dry_run_without_apply(home, capsys):
    p = _pooled("sk-AAAA1111BBBB", "sk-CCCC2222DDDD")

    assert cli.main(["keys", "select", "work"]) == 0
    assert "applied" not in capsys.readouterr().out
    assert profile_keys.load(p).keys[0].use_count == 0

    assert cli.main(["keys", "select", "work", "--apply"]) == 0
    assert "applied" in capsys.readouterr().out
    assert profile_keys.load(p).keys[0].use_count == 1


def test_cli_list_reports_a_single_token_profile(home, capsys):
    p = profile.create("work")
    credentials.save_token(p, "sk-SINGLE")

    assert cli.main(["keys", "list", "work"]) == 0

    out = capsys.readouterr().out
    assert "holds one token (…LE)" in out
    assert "claunch keys add work" in out


def test_set_token_replaces_the_selected_key_in_a_pool(home, capsys):
    p = _pooled("sk-AAAA1111BBBB", "sk-CCCC2222DDDD")

    assert cli.main(["set-token", "work", "sk-NEWNEWNEW1"]) == 0

    assert "replaced key" in capsys.readouterr().out
    pool = profile_keys.load(p)
    assert [k.token for k in pool.keys] == ["sk-AAAA1111BBBB", "sk-NEWNEWNEW1"]
    assert credentials.stored_token(p) == "sk-NEWNEWNEW1"


def test_set_token_with_key_leaves_the_effective_token_alone(home, capsys):
    # ``add`` leaves the cursor on the last key, so key 1 is the one the
    # profile is *not* running on (claunch-wllke).
    p = _pooled("sk-AAAA1111BBBB", "sk-CCCC2222DDDD")
    first = profile_keys.load(p).keys[0]

    assert cli.main(["set-token", "work", "sk-NEWNEWNEW1", "--key", first.id]) == 0

    capsys.readouterr()
    pool = profile_keys.load(p)
    assert [k.token for k in pool.keys] == ["sk-NEWNEWNEW1", "sk-CCCC2222DDDD"]
    assert pool.cursor == pool.keys[1].id
    assert credentials.stored_token(p) == "sk-CCCC2222DDDD"


def test_clear_removes_the_pool_too(home):
    p = _pooled("sk-AAAA1111BBBB", "sk-CCCC2222DDDD")

    removed = credentials.clear_token(p)

    assert profile_keys.KEYS_FILENAME in removed
    assert profile_keys.load(p) is None
    assert credentials.stored_token(p) is None


# --- launch seams -----------------------------------------------------------


def test_runner_run_advances_the_pool(home, monkeypatch):
    p = _pooled("sk-AAAA1111BBBB", "sk-CCCC2222DDDD")
    launched = []
    monkeypatch.setattr(runner, "_spawn", lambda *a, **kw: launched.append(kw) or 0)
    monkeypatch.setattr(runner, "require_compact_fits", lambda *a, **kw: None)

    assert runner.run(p) == 0

    assert launched
    assert credentials.stored_token(p) == "sk-AAAA1111BBBB"
    assert profile_keys.load(p).keys[0].use_count == 1


def test_runner_run_with_null_token_does_not_advance_the_pool(home, monkeypatch):
    p = _pooled("sk-AAAA1111BBBB", "sk-CCCC2222DDDD")
    monkeypatch.setattr(runner, "_spawn", lambda *a, **kw: 0)
    monkeypatch.setattr(runner, "require_compact_fits", lambda *a, **kw: None)

    assert runner.run(p, null_token=True) == 0

    assert profile_keys.load(p).keys[0].use_count == 0


def test_login_and_display_paths_do_not_advance_the_pool(home):
    p = _pooled("sk-AAAA1111BBBB", "sk-CCCC2222DDDD")

    lineage.stored_auth_token(p)
    lineage.injectable_token(p)
    credentials.stored_token(p)
    profile_keys.summary(p)

    assert profile_keys.load(p).keys[0].use_count == 0
