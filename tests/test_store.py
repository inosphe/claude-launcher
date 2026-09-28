"""The store is the single source of truth: read/write of ~/.claunch.yaml."""

from __future__ import annotations

import os

import pytest
import yaml

from claude_launcher import atomic, store


def test_load_defaults_when_absent(config_file):
    assert not config_file.exists()
    doc = store.load()
    assert doc["version"] == store.VERSION
    # Reading must not create the file.
    assert not config_file.exists()


def test_save_then_load_roundtrip(config_file):
    store.save({"profiles": {"a": {"env": {"X": "1"}}}})
    assert config_file.exists()
    doc = store.load()
    assert doc["version"] == store.VERSION
    assert doc["profiles"]["a"]["env"] == {"X": "1"}


def test_set_profile_field_sets_and_clears(home):
    store.set_profile_field("work", "parent", "base")
    assert store.profile_entry("work") == {"parent": "base"}
    # Empty/None clears the field but keeps the entry.
    store.set_profile_field("work", "parent", None)
    assert store.profile_entry("work") == {}


def test_ensure_profile_registers_empty_entry(home):
    store.ensure_profile("solo")
    assert "solo" in store.profiles()
    assert store.profile_entry("solo") == {}
    # Idempotent: does not clobber existing config.
    store.set_profile_field("solo", "env", {"A": "1"})
    store.ensure_profile("solo")
    assert store.profile_entry("solo")["env"] == {"A": "1"}


def test_remove_profile(home):
    store.ensure_profile("gone")
    store.remove_profile("gone")
    assert "gone" not in store.profiles()


def test_template_env_helpers(home):
    assert store.template_env() == {}
    store.set_template_env({"K": "v"})
    assert store.template_env() == {"K": "v"}


def test_malformed_file_raises_not_overwrites(config_file):
    config_file.write_text("{ this: is: not: valid", encoding="utf-8")
    original = config_file.read_text(encoding="utf-8")
    # A present-but-unparseable file must raise, never be silently treated as
    # empty (which the next write would clobber).
    with pytest.raises(store.StoreError):
        store.load()
    assert config_file.read_text(encoding="utf-8") == original


def test_non_mapping_top_level_raises(config_file):
    config_file.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(store.StoreError):
        store.load()


def test_empty_file_is_ok(config_file):
    config_file.write_text("", encoding="utf-8")
    assert store.load()["version"] == store.VERSION


def test_save_is_stable_yaml(config_file):
    store.save({"profiles": {"b": {}, "a": {}}})
    text = config_file.read_text(encoding="utf-8")
    # sort_keys=True keeps a deterministic order.
    assert text.index("a:") < text.index("b:")
    assert yaml.safe_load(text)["version"] == store.VERSION


# --------------------------------------------------------------------------- #
# writing while somebody is reading
# --------------------------------------------------------------------------- #
def test_save_never_shows_a_reader_a_half_written_document(config_file, monkeypatch):
    """The file has concurrent readers, so it is renamed into place.

    :func:`load` reads fresh on every call and the daemon calls it on *every*
    ``/api/sessions`` poll. The plain write this replaced truncated the file
    before writing it, and an empty read is the dangerous one: it parses
    cleanly and simply has no ``llm`` block, so a configuration that was
    never wrong reported itself absent for that poll and the web UI's
    briefing controls went inert.

    The rename is the only moment the new bytes and the old file both exist,
    so that is where a reader is put — and what it must get is the whole
    previous document.
    """
    store.save({"llm": {"endpoint": "e1", "model": "m", "api_key": "k"}})
    seen = []
    real_replace = os.replace

    def spy(src, dst):
        seen.append(store.load())
        real_replace(src, dst)

    # the rename lives in claude_launcher.atomic now: patching store.os would
    # hook a module store no longer calls, and this test would pass blind.
    monkeypatch.setattr(atomic.os, "replace", spy)
    store.save({"llm": {"endpoint": "e2", "model": "m", "api_key": "k"}})

    assert len(seen) == 1
    assert seen[0]["llm"] == {"endpoint": "e1", "model": "m", "api_key": "k"}
    assert store.load()["llm"]["endpoint"] == "e2"


def test_save_leaves_no_scratch_file_behind(config_file):
    store.save({"profiles": {"a": {}}})
    assert not list(config_file.parent.glob(f"{config_file.name}.*.tmp"))


def test_briefing_faq_supports_multiple_entries_and_legacy_single_entry(home):
    store.save({
        "briefing": {"faq": {"question": "기존 질문", "answer": "기존 답변"}}
    })

    legacy = store.briefing_faq()
    assert len(legacy) == 1
    assert legacy[0]["question"] == "기존 질문"

    saved = store.set_briefing_faq([
        *legacy,
        {"question": "새 질문", "answer": "새 답변"},
    ])
    assert [row["question"] for row in saved] == ["기존 질문", "새 질문"]
    assert [row["question"] for row in store.briefing_faq()] == [
        "기존 질문", "새 질문"
    ]
    assert all(row["id"] for row in saved)


def test_a_save_that_cannot_land_keeps_the_old_document_and_cleans_up(
    config_file, monkeypatch
):
    """A failed rename must not leave the config gone, nor litter beside it —
    the whole point of writing beside the file is that a failure costs
    nothing."""
    store.save({"llm": {"endpoint": "e1"}})

    def boom(src, dst):
        raise OSError("rename refused")

    monkeypatch.setattr(atomic.os, "replace", boom)
    with pytest.raises(OSError):
        store.save({"llm": {"endpoint": "e2"}})

    assert store.load()["llm"]["endpoint"] == "e1"
    assert not list(config_file.parent.glob(f"{config_file.name}.*.tmp"))


# --------------------------------------------------------------------------- #
# a transient Windows sharing conflict that outlasts atomic's retry budget
# --------------------------------------------------------------------------- #
def _perm(winerror: int) -> OSError:
    """The exception ``os.replace`` raises when a holder blocks the delete."""
    return OSError(13, "Access is denied", None, winerror)


def test_save_wraps_an_unrelenting_sharing_conflict(config_file, monkeypatch):
    """A holder that never lets go raises TransientStoreError, not the bare
    OSError -- worded as "try again", not "broken" -- and the previous
    document survives untouched (the rename never got past the conflict)."""
    store.save({"llm": {"endpoint": "e1"}})

    def denied(a, b):
        raise _perm(5)

    monkeypatch.setattr(atomic.os, "replace", denied)
    monkeypatch.setattr(atomic, "BACKOFF", (0.0, 0.0, 0.0))

    with pytest.raises(store.TransientStoreError) as caught:
        store.save({"llm": {"endpoint": "e2"}})

    assert isinstance(caught.value, store.StoreError)
    assert "run the command again" in str(caught.value)
    assert store.load()["llm"]["endpoint"] == "e1"
    assert not list(config_file.parent.glob(f"{config_file.name}.*.tmp"))


def test_save_does_not_wrap_a_non_transient_os_error(config_file, monkeypatch):
    """A wrong ACL (or any winerror atomic does not retry) is a real answer,
    not a transient conflict -- it must reach the caller as the bare
    OSError it always was, not get relabeled as "try again"."""
    store.save({"llm": {"endpoint": "e1"}})

    def denied(a, b):
        raise _perm(19)  # ERROR_WRITE_PROTECT -- not in atomic.TRANSIENT

    monkeypatch.setattr(atomic.os, "replace", denied)

    with pytest.raises(OSError) as caught:
        store.save({"llm": {"endpoint": "e2"}})

    assert not isinstance(caught.value, store.StoreError)


def test_load_parses_once_per_text_and_hands_out_copies(config_file, monkeypatch):
    """The parse is skipped while the file's text is unchanged, and it is the
    TEXT that decides -- a rewrite of the same size lands within the same
    timestamp on a coarse filesystem, so a stat-keyed cache could serve the
    old document. Each call still hands back its own copy: ``update`` mutates
    what ``load`` returns, and that must not edit the cached parse.

    The ``_DISK_TTL`` window (see test_load_disk_ttl below) exists precisely
    to skip a same-instant re-open, so it is disabled here: this test is
    about the *parse* cache surviving a real re-read, not about the TTL."""
    monkeypatch.setattr(store, "_DISK_TTL", 0.0)
    store.save({"profiles": {"a": {"env": {"X": "1"}}}})
    first = store.load()
    first["profiles"]["a"]["env"]["X"] = "mutated"
    assert store.load()["profiles"]["a"]["env"] == {"X": "1"}
    # Same length, different content, written straight past ``save``.
    text = config_file.read_text(encoding="utf-8")
    config_file.write_text(text.replace("X: '1'", "X: '2'"), encoding="utf-8")
    assert store.load()["profiles"]["a"]["env"] == {"X": "2"}
    config_file.write_text("- not a mapping\n", encoding="utf-8")
    with pytest.raises(store.StoreError):
        store.load()


def test_load_copies_a_plain_document_without_deepcopy(config_file, monkeypatch):
    """Each ``load`` still hands out its own copy, but a document of dicts and
    lists over scalars is copied by walking it, not by ``copy.deepcopy``: its
    memo bookkeeping was a quarter of a millisecond per call, and the daemon's
    session list calls ``load`` several times per row (claunch-2t37a). What the
    safe loader can build beyond that (a ``!!set``) still goes through
    ``deepcopy`` and still comes out as its own object."""
    store.save({"profiles": {"a": {"env": {"X": "1"}, "tools": ["t1", "t2"]}}})
    store.load()
    real_deepcopy = store.copy.deepcopy

    def no_deepcopy(value, memo=None):
        raise AssertionError("a plain document must not go through copy.deepcopy")

    monkeypatch.setattr(store.copy, "deepcopy", no_deepcopy)
    first = store.load()
    first["profiles"]["a"]["env"]["X"] = "mutated"
    first["profiles"]["a"]["tools"].append("t3")
    again = store.load()
    assert again["profiles"]["a"] == {"env": {"X": "1"}, "tools": ["t1", "t2"]}
    monkeypatch.setattr(store.copy, "deepcopy", real_deepcopy)

    monkeypatch.setattr(store, "_DISK_TTL", 0.0)
    config_file.write_text(
        "version: 2\nextra: !!set {a: null, b: null}\n", encoding="utf-8"
    )
    one = store.load()
    one["extra"].add("c")
    assert store.load()["extra"] == {"a", "b"}


def test_load_disk_ttl_collapses_a_same_instant_burst(config_file, monkeypatch):
    """Board claunch-snhl: a daemon request handler calling load() many times
    to answer one poll used to reopen the file every time. Within the TTL
    window, repeat calls must not touch the filesystem at all -- not even a
    read that would have returned the same text."""
    store.save({"profiles": {"a": {"env": {"X": "1"}}}})
    calls = []
    real_read_text = type(config_file).read_text

    def counting_read_text(self, *a, **kw):
        calls.append(1)
        return real_read_text(self, *a, **kw)

    monkeypatch.setattr(type(config_file), "read_text", counting_read_text)
    for _ in range(5):
        assert store.load()["profiles"]["a"]["env"] == {"X": "1"}
    # save() above already seeded the cache, so a correct implementation may
    # legitimately need zero further reads; what it must never do is one per
    # call (five).
    assert len(calls) <= 1


def test_load_disk_ttl_expires(config_file, monkeypatch):
    """Past the TTL window, load() re-opens the file and sees a change made
    without going through save() (e.g. a hand edit)."""
    store.save({"profiles": {"a": {"env": {"X": "1"}}}})
    store.load()
    text = config_file.read_text(encoding="utf-8")
    config_file.write_text(text.replace("X: '1'", "X: '2'"), encoding="utf-8")
    assert store.load()["profiles"]["a"]["env"] == {"X": "1"}  # still within TTL
    monkeypatch.setattr(store, "_disk_read_at", store._disk_read_at - store._DISK_TTL)
    assert store.load()["profiles"]["a"]["env"] == {"X": "2"}


def test_save_seeds_the_cache_so_the_same_process_never_reads_its_own_write_stale(
    config_file,
):
    """A caller that just wrote a value must see it back immediately, TTL
    window or not -- save() must not make its own writer wait out the
    window it introduced for *other* repeat callers."""
    store.save({"profiles": {"a": {"env": {"X": "1"}}}})
    assert store.load()["profiles"]["a"]["env"] == {"X": "1"}
    store.save({"profiles": {"a": {"env": {"X": "2"}}}})
    assert store.load()["profiles"]["a"]["env"] == {"X": "2"}


def test_load_disk_ttl_ignores_a_stale_cache_from_a_different_path(
    config_file, home, monkeypatch
):
    """A cache keyed only by TTL, with no path check, would serve one file's
    document for another once ``config.sync_file()`` changes mid-process
    (as it does between tests sharing this module's globals)."""
    store.save({"profiles": {"a": {"env": {"X": "1"}}}})
    store.load()
    other = home / "other.claunch.yaml"
    monkeypatch.setenv("CLAUDE_LAUNCHER_SYNC_FILE", str(other))
    assert store.load() == {"version": store.VERSION}
