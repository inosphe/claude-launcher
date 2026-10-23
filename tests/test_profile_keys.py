"""Several launcher tokens per profile, and the strategy that picks one."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

import claude_launcher
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


def test_an_opencode_go_key_is_measured_with_its_own_token(home, monkeypatch):
    # claunch-wllke.4: the opencode-go reader landed in claunch-f5w4x.6, but
    # this path did not know its kind and answered None for every key, so a
    # usage strategy silently fell back to rotation on those profiles.
    p = profile.create("work")
    seen = []
    report = usage.UsageReport(
        windows=[
            usage.UsageWindow(name="five_hour", utilization=41.0, resets_at=None)
        ],
        raw={},
    )

    def fake_fetch(prof, token):
        seen.append(token)
        return report

    monkeypatch.setattr(usage, "reader", lambda prof: "opencode-go")
    monkeypatch.setattr(usage, "_fetch_opencode_go", fake_fetch)

    assert usage.utilization_for_token(p, "sk-OPENCODE1") == 41.0
    assert seen == ["sk-OPENCODE1"]


def test_every_keyed_reader_kind_has_a_branch_in_utilization_for_token(
    home, monkeypatch
):
    # The table and the dispatch have to agree: a kind listed as keyed but
    # missing from utilization_for_token's chain is exactly the opencode-go
    # gap this round closed, and nothing else would catch the next one.
    p = profile.create("work")
    report = usage.UsageReport(
        windows=[
            usage.UsageWindow(name="five_hour", utilization=33.0, resets_at=None)
        ],
        raw={},
    )
    monkeypatch.setattr(usage, "_fetch_claude", lambda prof, token=None: report)
    monkeypatch.setattr(usage, "_fetch_fireworks", lambda prof, token: report)
    monkeypatch.setattr(usage, "_fetch_opencode_go", lambda prof, token: report)
    monkeypatch.setattr(
        usage, "_kimi_provider_context", lambda prof, token=None: ("a", "b")
    )
    monkeypatch.setattr(usage, "_fetch_kimi_provider", lambda prof, *ctx: report)

    for kind in usage.KEYED_READERS:
        monkeypatch.setattr(usage, "reader", lambda prof, kind=kind: kind)
        assert usage.utilization_for_token(p, "sk-KEY") == 33.0, kind


def test_the_reader_table_says_which_kinds_are_measured_per_key():
    assert "opencode-go" in usage.KEYED_READERS
    for kind in usage.KEYED_READERS:
        assert usage.unmeasurable_reason(kind) is None
    # A login-scoped reader quotes its own sentence…
    assert usage.unmeasurable_reason("kimi-web-server") == (
        usage.LOGIN_SCOPED_READERS["kimi-web-server"]
    )
    # …and a reader that merely lacks a per-key path says that instead, so the
    # two "cannot measure" cases stay apart in the note.
    assert "no per-key usage path" in usage.unmeasurable_reason("some-new-reader")


def test_the_fallback_note_quotes_the_reader_table(home, monkeypatch):
    p = _pooled("sk-AAAA1111BBBB", "sk-CCCC2222DDDD")
    monkeypatch.setattr(usage, "reader", lambda prof: "kimi-web-server")
    profile_keys.set_strategy(p, "usage-min")

    selection = profile_keys.select_for_launch(p)

    assert selection.strategy == "rotation"
    assert selection.fallback == usage.LOGIN_SCOPED_READERS["kimi-web-server"]


def test_a_keyed_reader_selects_by_usage_instead_of_rotating(home, monkeypatch):
    p = _pooled("sk-AAAA1111BBBB", "sk-CCCC2222DDDD")
    monkeypatch.setattr(usage, "reader", lambda prof: "opencode-go")
    monkeypatch.setattr(
        usage,
        "utilization_for_token",
        lambda prof, token: {"sk-AAAA1111BBBB": 80.0, "sk-CCCC2222DDDD": 20.0}[token],
    )
    profile_keys.set_strategy(p, "usage-min")

    selection = profile_keys.select_for_launch(p)

    assert selection.key.token == "sk-CCCC2222DDDD"
    assert selection.fallback is None


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


def test_a_child_with_its_own_token_does_not_rotate_the_parent_pool(home):
    # `claunch set-token child <value>` leaves the child with a token and no
    # pool; its launch injects the child's token, so the parent's pool must
    # stay untouched (claunch-wllke).
    base = _pooled("sk-AAAA1111BBBB", "sk-CCCC2222DDDD", name="base")
    child = profile.create("child")
    credentials.save_token(child, "sk-CHILDOWNTOKEN")
    lineage.set_parent(child, "base")
    before = profile_keys.load(base)

    assert profile_keys.select_for_launch(child) is None

    after = profile_keys.load(base)
    assert [k.token for k in after.keys] == [k.token for k in before.keys]
    assert [k.use_count for k in after.keys] == [k.use_count for k in before.keys]
    assert after.cursor == before.cursor
    assert credentials.stored_token(base) == "sk-CCCC2222DDDD"
    assert lineage.injectable_token(child) == "sk-CHILDOWNTOKEN"


def test_a_child_with_its_own_pool_rotates_its_own(home):
    base = _pooled("sk-AAAA1111BBBB", "sk-CCCC2222DDDD", name="base")
    child = profile.create("child")
    profile_keys.add(child, "sk-CHILD1111")
    profile_keys.add(child, "sk-CHILD2222")
    lineage.set_parent(child, "base")
    base_before = profile_keys.load(base)

    selection = profile_keys.select_for_launch(child)

    assert selection.owner.name == "child"
    assert credentials.stored_token(child) == selection.key.token
    assert [
        (k.token, k.use_count) for k in profile_keys.load(base).keys
    ] == [(k.token, k.use_count) for k in base_before.keys]


def test_a_child_with_only_login_credentials_does_not_rotate_the_parent_pool(home):
    # owner()'s chain is the stored token, then the login credentials. A
    # login-only child has an own_token, so the parent's pool is inert for it
    # and no rotation happens (claunch-wllke.6).
    base = _pooled("sk-AAAA1111BBBB", "sk-CCCC2222DDDD", name="base")
    child = profile.create("child")
    (child.config_dir / credentials.CREDENTIALS_FILENAME).write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "login-only-token",
                    "expiresAt": 9999999999999,
                    "scopes": ["user:profile"],
                }
            }
        ),
        encoding="utf-8",
    )
    lineage.set_parent(child, "base")
    before = profile_keys.load(base)

    assert credentials.own_token(child) == "login-only-token"
    assert profile_keys.owner(child) is None
    assert profile_keys.select_for_launch(child) is None

    after = profile_keys.load(base)
    assert [(k.token, k.use_count) for k in after.keys] == [
        (k.token, k.use_count) for k in before.keys
    ]
    assert after.cursor == before.cursor
    assert credentials.stored_token(base) == "sk-CCCC2222DDDD"
    assert credentials.stored_token(child) is None


def test_a_grandparent_pool_is_rotated_when_the_parent_holds_nothing(home):
    # The chain walk does not stop at an empty parent: the nearest ancestor
    # with a token decides, and that ancestor's pool is the one that advances
    # (claunch-wllke.6).
    grand = _pooled("sk-AAAA1111BBBB", "sk-CCCC2222DDDD", name="grand")
    parent = profile.create("parent")
    child = profile.create("child")
    lineage.set_parent(parent, "grand")
    lineage.set_parent(child, "parent")

    first = profile_keys.select_for_launch(child)
    second = profile_keys.select_for_launch(child)

    assert first.owner.name == "grand"
    assert [first.key.token, second.key.token] == [
        "sk-AAAA1111BBBB",
        "sk-CCCC2222DDDD",
    ]
    # The grandparent's token file moved; the parent's and the child's did
    # not, because neither owns a token.
    assert credentials.stored_token(grand) == "sk-CCCC2222DDDD"
    assert credentials.stored_token(parent) is None
    assert credentials.stored_token(child) is None
    assert profile_keys.load(parent) is None
    assert profile_keys.load(child) is None


# --- removal and CLI --------------------------------------------------------


def test_removing_the_last_key_removes_the_pool_and_token(home):
    p = _pooled("sk-AAAA1111BBBB")
    key = profile_keys.load(p).keys[0]

    profile_keys.remove(p, key.id)

    assert profile_keys.load(p) is None
    assert credentials.stored_token(p) is None


def test_forgetting_the_pool_keeps_the_lock_file_and_leaves_no_phantom_pool(home):
    # Deleting <pool>.lock is what would break the lock: a writer waiting on
    # the old inode and the next writer on a new one would both hold it
    # (claunch-wllke.1.2.1). So the empty lock stays -- and it must never read
    # as "this profile still has a pool", which is the other half of the bug.
    p = _pooled("sk-AAAA1111BBBB")
    lock = Path(str(profile_keys.path(p)) + ".lock")
    assert lock.is_file()  # the pool's first write created it

    assert profile_keys.forget(p) is True

    assert not profile_keys.path(p).is_file()
    assert lock.is_file()
    assert profile_keys.load(p) is None
    assert profile_keys.owner(p) is None
    assert profile_keys.forget(p) is False  # nothing left to delete


def test_clearing_the_token_drops_the_pool_and_the_lock_frees_again(home):
    # `claunch keys set-token --clear` goes through credentials.clear_token,
    # which forgets the pool without holding the pool lock: the leftover lock
    # must not keep the next add() waiting for it (claunch-wllke.1.2.1).
    p = _pooled("sk-AAAA1111BBBB")
    lock = Path(str(profile_keys.path(p)) + ".lock")

    removed = credentials.clear_token(p)

    assert profile_keys.KEYS_FILENAME in removed
    assert profile_keys.load(p) is None
    assert lock.is_file()

    key = profile_keys.add(p, "sk-CCCC2222DDDD")
    assert [k.id for k in profile_keys.load(p).keys] == [key.id]


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


def test_replace_refuses_a_token_another_key_holds(home):
    # A second copy of a token makes the rotation spend one account twice and
    # makes a usage strategy compare a key with itself (claunch-wllke.5).
    p = _pooled("sk-AAAA1111BBBB", "sk-CCCC2222DDDD")
    first = profile_keys.load(p).keys[0]

    with pytest.raises(profile_keys.KeysError, match="already holds that key"):
        profile_keys.replace(p, "sk-CCCC2222DDDD", ident=first.id)

    assert [k.token for k in profile_keys.load(p).keys] == [
        "sk-AAAA1111BBBB",
        "sk-CCCC2222DDDD",
    ]


def test_replace_may_restate_the_token_of_the_key_it_replaces(home):
    # The rule is about *another* key: re-running set-token with the value a
    # key already holds is a no-op, not a duplicate.
    p = _pooled("sk-AAAA1111BBBB", "sk-CCCC2222DDDD")
    second = profile_keys.load(p).keys[1]

    key = profile_keys.replace(p, "sk-CCCC2222DDDD", ident=second.id)

    assert key.id == second.id
    assert [k.token for k in profile_keys.load(p).keys] == [
        "sk-AAAA1111BBBB",
        "sk-CCCC2222DDDD",
    ]


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


def test_keys_list_names_the_profile_whose_pool_a_child_rotates(home, capsys):
    # The child holds no token, so its every launch selects from and advances
    # the parent's pool. Listing the child's own (absent) pool would hide the
    # keys that are actually in play (claunch-wllke.5).
    _pooled("sk-AAAA1111BBBB", "sk-CCCC2222DDDD", name="base")
    child = profile.create("child")
    lineage.set_parent(child, "base")

    assert cli.main(["keys", "list", "child"]) == 0

    out = capsys.readouterr().out
    assert "2 keys, strategy rotation" in out
    assert "pool: profile 'base' (inherited" in out
    assert "sk-A…BBBB" in out


def test_a_deleted_token_file_is_reported_and_repaired_by_the_next_launch(
    home, capsys
):
    # The pool file is the durable record and .launcher-token is derived from
    # it: listing reports the gap without consuming a rotation, and the next
    # launch writes the key back (claunch-wllke.5).
    p = _pooled("sk-AAAA1111BBBB", "sk-CCCC2222DDDD")
    (p.config_dir / credentials.TOKEN_FILENAME).unlink()

    assert cli.main(["keys", "list", "work"]) == 0
    out = capsys.readouterr().out
    assert ".launcher-token names no key of this pool" in out
    assert credentials.stored_token(p) is None

    selection = profile_keys.select_for_launch(p)
    assert credentials.stored_token(p) == selection.key.token


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


def _checkout_src() -> Path:
    """``src`` of the checkout this test file lives in, or a loud failure.

    The baseline is read from this file, not from ``claude_launcher.__file__``
    (claunch-wllke.1.1.1). The import location is not the tree under test: a
    pytest started from a tool interpreter, or one whose ``PYTHONPATH`` names
    site-packages first, imports the installed package, and a baseline taken
    from that would bless "the child read site-packages, same as me".
    """
    src_root = Path(__file__).resolve().parents[1] / "src"
    if not (src_root / "claude_launcher" / "__init__.py").is_file():
        raise RuntimeError(
            f"this test file is not inside a checkout with src/claude_launcher "
            f"(looked for {src_root}); the gate cannot name the tree it measures"
        )
    return src_root


def _child_env():
    """Environment for a child process that must import *this* checkout.

    An editable install usually makes that automatic, but not always: under a
    tool install, a non-editable ``uv sync``, or a pytest launched from another
    interpreter, ``python -c`` in the child resolves ``claude_launcher`` to
    whatever is installed instead of the tree under test. A gate that measures
    a different tree than the one being reviewed is worse than no gate
    (claunch-wllke.1.1), so the path is set explicitly and the child reports
    where it landed — against this checkout, never against whatever this
    process happened to import (claunch-wllke.1.1.1).
    """
    src_root = _checkout_src()
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(src_root)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    )
    return env, src_root


def _assert_child_read_the_checkout(reported: str, src_root: Path) -> None:
    where = Path(reported).resolve()
    assert where.is_relative_to(src_root), (
        f"the child imported {where}, not the tree under test {src_root}"
    )


def test_the_child_assertion_does_not_trust_the_parents_import(home, monkeypatch, tmp_path):
    # The hole this pins (claunch-wllke.1.1.1): the baseline used to come from
    # claude_launcher.__file__, so a pytest that imported an installed package
    # compared the child against that install and passed when the child read
    # it -- green about a tree nobody edited. Pretend this process imported
    # site-packages and check the child is still pointed at the checkout and
    # judged against it.
    installed = tmp_path / "site-packages" / "claude_launcher" / "__init__.py"
    installed.parent.mkdir(parents=True)
    installed.write_text("")
    monkeypatch.setattr(claude_launcher, "__file__", str(installed))

    env, src_root = _child_env()

    assert src_root == Path(__file__).resolve().parents[1] / "src"
    assert env["PYTHONPATH"].split(os.pathsep)[0] == str(src_root)
    with pytest.raises(AssertionError, match="not the tree under test"):
        _assert_child_read_the_checkout(str(installed), src_root)


def test_concurrent_launches_each_take_their_own_step(home):
    """One pool, several launches at once, no lost update (claunch-wllke.2).

    Every launch reads the pool, picks, then writes the pool back. Without a
    lock two launches read the same cursor, pick the same key, and the later
    write erases the earlier one's use count — so the counts come out short of
    the number of launches. Twelve real processes against a four-key pool must
    leave twelve uses behind, three on each key.

    The child also prints where it imported ``claude_launcher`` from, so a run
    in which the children measured some other build fails here instead of
    passing on numbers that belong to that other build (claunch-wllke.1.1).
    """
    import subprocess

    tokens = [f"sk-K{i}AAAA1111BBBB" for i in range(4)]
    _pooled(*tokens)

    env, src_root = _child_env()
    script = (
        "import claude_launcher as m\n"
        "from claude_launcher import profile, profile_keys\n"
        "p = profile.require('work')\n"
        "selection = profile_keys.select_for_launch(p)\n"
        "print(selection.key.id)\n"
        "print(m.__file__)\n"
    )
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
        )
        for _ in range(12)
    ]
    chosen = []
    for proc in procs:
        out, err = proc.communicate(timeout=120)
        assert proc.returncode == 0, err
        key_id, where = out.strip().splitlines()
        _assert_child_read_the_checkout(where, src_root)
        chosen.append(key_id)

    pool = profile_keys.load(profile.require("work"))
    assert len(chosen) == 12
    assert sum(key.use_count for key in pool.keys) == 12
    assert [key.use_count for key in pool.keys] == [3, 3, 3, 3]
    assert set(chosen) == {key.id for key in pool.keys}


def test_concurrent_key_adds_keep_every_key(home):
    """Adding keys from several processes at once loses none (claunch-wllke.1.2).

    ``add`` is the same read-modify-write as a launch: load the pool, change
    it, save the whole object. Unlocked, the last writer wins and everything
    the others wrote is gone. Six processes add a distinct token while six
    more launch from the same pool; afterwards every added key must still be
    there and every launch must have taken its own step.
    """
    import subprocess

    _pooled("sk-BASE0000AAAA")

    env, src_root = _child_env()
    launch_script = (
        "import claude_launcher as m\n"
        "from claude_launcher import profile, profile_keys\n"
        "p = profile.require('work')\n"
        "profile_keys.select_for_launch(p)\n"
        "print(m.__file__)\n"
    )
    add_script = (
        "import sys\n"
        "import claude_launcher as m\n"
        "from claude_launcher import profile, profile_keys\n"
        "p = profile.require('work')\n"
        "profile_keys.add(p, sys.argv[1], label=sys.argv[1])\n"
        "print(m.__file__)\n"
    )
    added = [f"sk-ADD{i:04d}ZZZZ" for i in range(6)]
    commands = [["-c", add_script, token] for token in added]
    commands += [["-c", launch_script] for _ in range(6)]
    procs = [
        subprocess.Popen(
            [sys.executable, *args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
        )
        for args in commands
    ]
    for proc in procs:
        out, err = proc.communicate(timeout=120)
        assert proc.returncode == 0, err
        _assert_child_read_the_checkout(out.strip().splitlines()[-1], src_root)

    pool = profile_keys.load(profile.require("work"))
    assert pool is not None
    tokens = {key.token for key in pool.keys}
    missing = [token for token in added if token not in tokens]
    assert not missing, f"lost {len(missing)} of {len(added)} concurrent adds: {missing}"
    assert sum(key.use_count for key in pool.keys) == 6


def test_a_pinned_key_is_matched_by_id_not_by_position(home):
    """A pin names one key; a bare number is not a position (claunch-wllke.1.2).

    ``_index_of`` also reads a number as a 1-based position, which is the CLI's
    shorthand for "the second key". A pin is not shorthand — it names the key
    the user picked — so an id that is not in the pool must read as stale
    rather than materialising some other key behind the user's back.
    """
    p = _pooled("sk-AAAA1111BBBB", "sk-CCCC2222DDDD")
    before = credentials.stored_token(p)

    assert profile_keys.materialize(p, "1") is None
    assert profile_keys.materialize(p, "2") is None

    pool = profile_keys.load(p)
    assert [key.use_count for key in pool.keys] == [0, 0]
    assert credentials.stored_token(p) == before

    # The real id still materialises, and only that key.
    wanted = pool.keys[1]
    selection = profile_keys.materialize(p, wanted.id)
    assert selection is not None
    assert selection.key.id == wanted.id
    assert [key.use_count for key in profile_keys.load(p).keys] == [0, 1]


def test_a_launch_refused_before_the_spawn_does_not_advance_the_pool(home, monkeypatch):
    """A run that never happened must not spend a rotation step (claunch-wllke.3).

    ``--provider`` is refused for a harness that is not the builtin claude
    one, and that refusal lands before anything is spawned. The pool must be
    exactly where it was: otherwise a mistyped flag quietly walks the
    rotation to the next key and bumps the use count of a key that never ran.
    """
    from claude_launcher import harnesses, store

    store.update(
        lambda doc: doc.update(
            {"harnesses": {"pyx": {"command": [sys.executable, "-u", "-c", "pass"]}}}
        )
    )
    p = _pooled("sk-AAAA1111BBBB", "sk-CCCC2222DDDD")
    before = profile_keys.load(p)
    monkeypatch.setattr(runner, "profile_harness", lambda prof: harnesses.get("pyx"))
    monkeypatch.setattr(runner, "_spawn", lambda *a, **kw: pytest.fail("spawned"))
    monkeypatch.setattr(runner, "require_compact_fits", lambda *a, **kw: None)

    with pytest.raises(runner.RunnerError):
        runner.run(p, provider="glm")

    after = profile_keys.load(p)
    assert after.cursor == before.cursor
    assert [k.use_count for k in after.keys] == [k.use_count for k in before.keys]


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
