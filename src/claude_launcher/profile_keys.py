"""The keys a profile may hold, and the strategy that picks one per launch.

Until claunch-wllke a profile had exactly one launcher-managed token
(``credentials.TOKEN_FILENAME``), and every consumer read that one file. That
file stays the single *effective* token: it is what ``stored_token``,
``lineage``, ``borrowing``, the daemon's briefing/observer and ``get-token``
read, and this module never asks any of them to change. What it adds is a pool
of keys beside it (``.launcher-keys.json``, ``0600``) and a selection step at
launch time that writes the chosen key into the token file.

So the invariant is:

* the pool is the list, the strategy and the rotation cursor;
* ``.launcher-token`` is *this launch's* key, and is the only file the rest of
  the launcher reads;
* a profile with no pool behaves exactly as before -- one token, no selection.

Selection happens at the launch seams (``runner.run``, the daemon's session
launch), never inside ``credentials.stored_token``. Reads on display paths
(borrowing checks, the briefing, ``usage.fetch``) must not advance the
rotation: a dashboard refresh is not a launch.

Inheritance: a launch selects from the pool of the profile whose token it
actually reads -- its own pool, or an ancestor's pool while the profile itself
holds no token (the same chain ``lineage.injectable_token`` walks), and the
selection is written into *that* profile's token file. A profile that holds
its own token never rotates an ancestor's pool: the chosen key would not reach
its launch, and the ancestor's token file would change for a run that does not
use it.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import random
import secrets
import stat
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, List, Optional, Tuple

from . import atomic, credentials
from .profile import Profile

log = logging.getLogger(__name__)

#: The pool file, beside the token it feeds.
KEYS_FILENAME = ".launcher-keys.json"
#: Bumped only for a format the reader below cannot interpret.
FORMAT_VERSION = 1
#: Strategy names accepted on the command line and in the file.
STRATEGIES = ("rotation", "usage-min", "usage-max", "random")
DEFAULT_STRATEGY = "rotation"


class KeysError(Exception):
    """Raised for an unusable pool file or an unknown key/strategy."""


@dataclass(frozen=True)
class Key:
    """One stored token, addressed by a stable id that survives removals."""

    id: str
    token: str
    label: str = ""
    added_at: str = ""
    last_used_at: Optional[str] = None
    use_count: int = 0

    def masked(self) -> str:
        return mask(self.token)


@dataclass
class Pool:
    """A profile's keys in file order, plus how to pick from them."""

    strategy: str = DEFAULT_STRATEGY
    #: id of the key the last rotation landed on (``None`` before the first).
    cursor: Optional[str] = None
    keys: List[Key] = field(default_factory=list)


@dataclass(frozen=True)
class Selection:
    """What a launch would use, and where it came from."""

    key: Key
    strategy: str
    index: int
    owner: Profile
    #: Why a usage strategy fell back to rotation, when it did.
    fallback: Optional[str] = None


def path(profile: Profile) -> Path:
    """The pool file of ``profile`` (whether or not it exists)."""
    return profile.config_dir / KEYS_FILENAME


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_id(taken: List[str]) -> str:
    while True:
        candidate = f"k-{secrets.token_hex(3)}"
        if candidate not in taken:
            return candidate


def mask(token: str) -> str:
    """A token safe to print: first four and last four characters."""
    token = str(token or "")
    if len(token) <= 12:
        return "…" + token[-2:]
    return f"{token[:4]}…{token[-4:]}"


# --- reading and writing ----------------------------------------------------


def load(profile: Profile) -> Optional[Pool]:
    """The profile's pool, or ``None`` when it has no pool file.

    A file that cannot be read is an error rather than a silent empty pool:
    falling back to "no keys" would quietly authenticate with whatever
    ``.launcher-token`` happens to hold, which is the opposite of what the
    file asked for.
    """
    file = path(profile)
    if not file.is_file():
        return None
    try:
        raw = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise KeysError(f"cannot read key pool {file}: {exc}") from exc
    if not isinstance(raw, dict):
        raise KeysError(f"key pool {file} is not a JSON object")
    version = raw.get("version", FORMAT_VERSION)
    if version != FORMAT_VERSION:
        raise KeysError(
            f"key pool {file} has version {version!r}; this claunch reads "
            f"version {FORMAT_VERSION}"
        )
    strategy = str(raw.get("strategy") or DEFAULT_STRATEGY)
    if strategy not in STRATEGIES:
        raise KeysError(
            f"key pool {file} names unknown strategy {strategy!r} "
            f"(expected one of {', '.join(STRATEGIES)})"
        )
    keys: List[Key] = []
    for item in raw.get("keys") or []:
        if not isinstance(item, dict):
            raise KeysError(f"key pool {file} has a non-object key entry")
        token = str(item.get("token") or "").strip()
        if not token:
            raise KeysError(f"key pool {file} has a key with no token")
        keys.append(
            Key(
                id=str(item.get("id") or ""),
                token=token,
                label=str(item.get("label") or ""),
                added_at=str(item.get("added_at") or ""),
                last_used_at=item.get("last_used_at"),
                use_count=int(item.get("use_count") or 0),
            )
        )
    cursor = raw.get("cursor")
    return Pool(
        strategy=strategy,
        cursor=str(cursor) if cursor else None,
        keys=keys,
    )


def _dump(pool: Pool) -> dict:
    return {
        "version": FORMAT_VERSION,
        "strategy": pool.strategy,
        "cursor": pool.cursor,
        "keys": [
            {
                "id": k.id,
                "label": k.label,
                "token": k.token,
                "added_at": k.added_at,
                "last_used_at": k.last_used_at,
                "use_count": k.use_count,
            }
            for k in pool.keys
        ],
    }


def save(profile: Profile, pool: Pool) -> None:
    """Write the pool atomically with owner-only permissions."""
    file = path(profile)
    file.parent.mkdir(parents=True, exist_ok=True)
    with atomic.scratch(file) as tmp:
        tmp.write_text(
            json.dumps(_dump(pool), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        try:
            os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        atomic.replace(tmp, file)


def forget(profile: Profile) -> bool:
    """Delete the pool file under the pool lock; return whether there was one.

    The delete holds the same lock as the writes because every write is a
    read-modify-write over the whole file: a launch or an ``add`` that loaded
    the pool just before this call would otherwise save it straight back and
    the removal would not hold -- the cleared pool would come back
    (claunch-wllke.1.2.1.1). A caller that already holds the lock uses
    :func:`forget_locked`.

    ``<pool>.lock`` is left in place (claunch-wllke.1.2.1). It is an empty
    owner-only file that holds no state, and unlinking it is the one thing
    that can break the serialisation the rest of this module stands on: a
    writer may already hold the file open and be waiting for its lock, so
    removing the path lets that waiter lock the old inode while the next
    writer creates and locks a new one -- two holders of one pool, which is
    the lost update :func:`pool_lock` exists to prevent. On Windows the
    unlink fails outright while the descriptor is open. Nothing reads the
    lock as "this profile has a pool": :func:`load` and :func:`owner` ask
    :func:`path`, so an empty lock beside no pool file is inert.
    """
    with pool_lock(profile):
        return forget_locked(profile)


def forget_locked(profile: Profile) -> bool:
    """Delete the pool file; the caller holds :func:`pool_lock`."""
    file = path(profile)
    if not file.is_file():
        return False
    file.unlink()
    return True


# --- the write lock ---------------------------------------------------------

#: How long a launch waits for another process's pool write before going
#: ahead unlocked. A launch's read-modify-write is milliseconds, so this is
#: only ever reached by a wedged or killed holder.
LOCK_TIMEOUT = 5.0


@contextlib.contextmanager
def pool_lock(profile: Profile) -> Iterator[None]:
    """Serialise a pool's read-modify-write against other processes.

    Every write to the pool is a read-modify-write: the caller loads the file,
    changes one thing, and saves the whole object back -- or, for
    :func:`forget` and ``credentials.clear_token``, deletes it, which is the
    same race from the other side. The writers are separate processes -- a
    session spawn in the daemon, ``claunch run`` in a shell, ``claunch keys
    add`` in another terminal -- so without a lock the later save overwrites
    everything the earlier one changed, and a delete is undone by a save that
    read the pool first: a cursor that skips a key, a use count that
    undercounts, a key that was just added disappearing again, a cleared pool
    coming back (claunch-wllke.2, claunch-wllke.1.2, claunch-wllke.1.2.1.1).
    The file itself is replaced atomically, so this is only ever a lost
    update, never a torn one.

    The lock is ``<pool>.lock`` beside the pool. On timeout the caller
    proceeds unlocked with a warning: losing one update to a wedged holder is
    worse than losing the launch itself, but it is not silent.
    """
    file = path(profile)
    file.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(file) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    held = False
    try:
        try:
            import msvcrt  # type: ignore[import-not-found]
        except ImportError:
            msvcrt = None  # type: ignore[assignment]
        if msvcrt is not None:

            def take() -> None:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

            def drop() -> None:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            def take() -> None:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

            def drop() -> None:
                fcntl.flock(fd, fcntl.LOCK_UN)

        deadline = time.monotonic() + LOCK_TIMEOUT
        while True:
            try:
                take()
                held = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    log.warning(
                        "key pool lock %s.lock is held by another process; "
                        "writing the pool without it",
                        file,
                    )
                    break
                time.sleep(0.02)
        yield
    finally:
        if held:
            with contextlib.suppress(OSError):
                drop()
        os.close(fd)


# --- editing the pool -------------------------------------------------------


def adopt_single_token(profile: Profile) -> Optional[Pool]:
    """Start a pool from the profile's existing single token, if it has one.

    Called by every mutating operation, so a profile that has been using one
    ``set-token`` value keeps that value as its first key instead of losing it
    the moment a second key is added.
    """
    pool = load(profile)
    if pool is not None:
        return pool
    token = credentials.stored_token(profile)
    if not token:
        return None
    pool = Pool(
        strategy=DEFAULT_STRATEGY,
        cursor=None,
        keys=[Key(id=_new_id([]), token=token, label="", added_at=_now())],
    )
    save(profile, pool)
    return pool


def add(profile: Profile, token: str, label: str = "") -> Key:
    """Append ``token`` to the pool and make it the effective token."""
    token = str(token or "").strip()
    if not token:
        raise KeysError("refusing to store an empty key")
    with pool_lock(profile):
        pool = adopt_single_token(profile) or Pool()
        if any(k.token == token for k in pool.keys):
            raise KeysError(f"profile {profile.name!r} already holds that key")
        key = Key(
            id=_new_id([k.id for k in pool.keys]),
            token=token,
            label=str(label or "").strip(),
            added_at=_now(),
        )
        pool.keys.append(key)
        pool.cursor = key.id
        save(profile, pool)
        credentials.save_token(profile, token)
        return key


def remove(profile: Profile, ident: str) -> Key:
    """Remove the key named by id or 1-based index; promote a survivor."""
    with pool_lock(profile):
        pool = load(profile)
        if pool is None:
            raise KeysError(f"profile {profile.name!r} has no key pool")
        index = _index_of(pool, ident)
        gone = pool.keys.pop(index)
        if gone.id == pool.cursor:
            pool.cursor = None
        if not pool.keys:
            # The whole clear -- token files and pool file -- under the lock we
            # already hold, so no launch can put a token back between the two
            # halves (claunch-wllke.1.2.1.1).
            credentials.clear_token_locked(profile)
            return gone
        save(profile, pool)
        # Keep the effective token one of the remaining keys, so the next
        # launch does not run on a token the pool no longer lists -- but leave
        # it alone while it is still listed: removing some other key is not a
        # re-selection.
        current = credentials.stored_token(profile)
        if current not in {key.token for key in pool.keys}:
            credentials.save_token(profile, pool.keys[0].token)
        return gone


def replace(profile: Profile, token: str, ident: Optional[str] = None) -> Key:
    """Replace one key's token in place (``set-token`` on a pooled profile).

    Two keys hold the same token is a pool whose rotation spends the same
    account twice and whose usage strategies compare a key with itself, so
    this refuses it exactly as :func:`add` does -- the key being replaced is
    the only one whose token may repeat (claunch-wllke.5).

    The effective token file is rewritten only when ``ident`` names the key
    the profile is currently on; replacing some other key leaves the token
    file alone, so it keeps naming the key the last launch actually used.
    """
    token = str(token or "").strip()
    if not token:
        raise KeysError("refusing to store an empty key")
    with pool_lock(profile):
        pool = load(profile)
        if pool is None:
            raise KeysError(f"profile {profile.name!r} has no key pool")
        current = _selected_index(pool)
        index = _index_of(pool, ident) if ident else current
        if any(
            other.token == token
            for position, other in enumerate(pool.keys)
            if position != index
        ):
            raise KeysError(f"profile {profile.name!r} already holds that key")
        old = pool.keys[index]
        key = Key(
            id=old.id,
            token=token,
            label=old.label,
            added_at=old.added_at,
            last_used_at=old.last_used_at,
            use_count=old.use_count,
        )
        pool.keys[index] = key
        save(profile, pool)
        # Rewrite the effective token only when the key being replaced is the
        # one the profile is currently on: editing another key is not a
        # re-selection, and overwriting the token file would leave it naming a
        # key the last launch did not use (the rule remove() follows).
        if index == current:
            credentials.save_token(profile, token)
        return key


def set_strategy(profile: Profile, strategy: str) -> Pool:
    """Set the strategy of the pool this profile's launches select from.

    The pool is the one :func:`strategy_owner` names: the profile's own, or
    the nearest ancestor's while the profile holds no token of its own. The
    display half of the same command already reads that pool
    (claunch-wllke.5.1), so writing the profile's own (absent) pool here made
    one command answer about two different pools -- the child was told which
    strategy it obeys and then refused a change to it, with advice to add a
    key the child does not need (claunch-wllke.5.1.1).

    This is not the ancestor rotation the module docstring forbids: that rule
    is about a *launch* advancing a cursor on a pool whose key it never reads.
    Setting is an explicit command that names the profile, and it lands on the
    pool that profile's launches actually select from. Callers that print the
    result should name ``strategy_owner(profile)`` when it is not the profile
    itself, because another profile's file is what changed.
    """
    strategy = str(strategy or "").strip().lower()
    if strategy not in STRATEGIES:
        raise KeysError(
            f"unknown strategy {strategy!r} (expected one of {', '.join(STRATEGIES)})"
        )
    source = strategy_owner(profile) or profile
    with pool_lock(source):
        pool = adopt_single_token(source)
        if pool is None:
            raise KeysError(
                f"profile {source.name!r} has no stored key; add one with "
                f"'claunch keys add {source.name}'"
            )
        pool.strategy = strategy
        save(source, pool)
        return pool


def strategy_owner(profile: Profile) -> Optional[Profile]:
    """The profile whose pool :func:`set_strategy` reads and writes.

    ``None`` when no profile in the chain holds a pool or a token, which is
    the same answer :func:`owner` gives and the reason the caller falls back
    to the profile itself: the error then names the profile that was asked
    about rather than an ancestor that does not exist.
    """
    return _token_source(profile)


def _index_of(pool: Pool, ident: str) -> int:
    text = str(ident or "").strip()
    for position, key in enumerate(pool.keys):
        if key.id == text:
            return position
    try:
        number = int(text)
    except (TypeError, ValueError):
        number = 0
    if 1 <= number <= len(pool.keys):
        return number - 1
    raise KeysError(_no_key(pool, text))


def _index_of_id(pool: Pool, key_id: str) -> int:
    """The position of the key whose id is exactly ``key_id``.

    Unlike ``_index_of`` this never falls back to a 1-based index. A pin
    (``SessionDef.key_id``) names the one key the user picked in the rail, so
    a pool without that id means the pin is stale — not that the user meant
    the second key (claunch-wllke.1.2).
    """
    text = str(key_id or "").strip()
    for position, key in enumerate(pool.keys):
        if key.id == text:
            return position
    raise KeysError(_no_key(pool, text))


def _no_key(pool: Pool, text: str) -> str:
    return (
        f"no key {text!r} in this pool (ids: "
        f"{', '.join(k.id for k in pool.keys)})"
    )


def _selected_index(pool: Pool) -> int:
    if not pool.keys:
        raise KeysError("the key pool is empty")
    for position, key in enumerate(pool.keys):
        if key.id == pool.cursor:
            return position
    return 0


# --- inheritance ------------------------------------------------------------


def owner(profile: Profile) -> Optional[Profile]:
    """The profile whose token this launch reads, if it owns a key pool.

    The chain walk follows ``lineage.injectable_token``: the profile's own
    stored token, its own login credentials, else the nearest ancestor that
    has either. A pool counts as a token source of its own, because the launch
    writes the selected key into the token file -- which is also what repairs
    a deleted ``.launcher-token`` on the next launch (claunch-wllke.5). A pool
    on any other profile is inert for this launch: it would pick a key the
    launch never uses and would rewrite a token file that belongs to a
    different profile (claunch-wllke).
    """
    source = _token_source(profile)
    if source is None or load(source) is None:
        return None
    return source


def _token_source(profile: Profile) -> Optional[Profile]:
    """The profile whose token ``lineage`` would inject for this launch.

    A pool counts even when the token file is missing: the pool is then the
    only record of which tokens this profile has, and the launch writes the
    selected one back into the file. Without this a deleted ``.launcher-token``
    made the pool unreachable -- the profile read as tokenless, so no launch
    ever selected from it and the pool stayed dead (claunch-wllke.5).
    """
    for item in [profile, *_ancestors(profile)]:
        # The pool file decides, not its contents: a pool that exists but
        # cannot be read is an error the caller should see (load()'s rule),
        # not a reason to walk past it and use an ancestor's token.
        if credentials.own_token(item) is not None or path(item).is_file():
            return item
    return None


def _ancestors(profile: Profile) -> List[Profile]:
    # Imported here: lineage imports this module's neighbours, and a pool is
    # only read at launch and on the CLI, never during module import.
    from . import lineage

    try:
        return [
            p
            for p in reversed(lineage.chain(profile)[:-1])
            if p.exists()
        ]
    except lineage.LineageError:
        return []


# --- selection --------------------------------------------------------------


def _rotation_index(pool: Pool) -> int:
    if not pool.keys:
        raise KeysError("the key pool is empty")
    if pool.cursor is None:
        return 0
    for position, key in enumerate(pool.keys):
        if key.id == pool.cursor:
            return (position + 1) % len(pool.keys)
    return 0


def _utilization_reader(
    source: Profile,
) -> Tuple[Callable[[Key], Optional[float]], Optional[str]]:
    """A per-key usage lookup for the profile's provider, and why it may be blind.

    The reader is chosen once (it depends on the provider and harness, not on
    the key) and then asked about each key's own token. When the provider's
    usage cannot be measured per key the lookup finds ``None`` for every key
    -- which makes the caller fall back to rotation -- and the second value
    carries the reason, quoted in the fallback note. The reason comes from
    :func:`usage.unmeasurable_reason`, one table for every reader, so a
    provider that is unmeasurable is named there rather than at each call
    site (claunch-wllke.4).
    """
    # Local imports: usage imports runner, which imports this module, and
    # neither is needed until a launch actually asks for a usage strategy.
    from . import lineage, providers, usage
    from . import profile as profile_mod

    try:
        kind = usage.reader(source)
    except (
        usage.UsageError,
        profile_mod.ProfileError,
        providers.ProviderError,
        lineage.LineageError,
    ) as exc:
        # An unreadable setup is "cannot measure", not a failed launch.
        return (lambda key: None), f"the profile's usage reader is not available ({exc})"

    reason = usage.unmeasurable_reason(kind)
    if reason is not None:
        return (lambda key: None), reason

    def measure(key: Key) -> Optional[float]:
        try:
            return usage.utilization_for_token(source, key.token)
        except (
            usage.UsageError,
            credentials.CredentialsError,
            providers.ProviderError,
            lineage.LineageError,
            OSError,
        ):
            # One key's endpoint failing must not decide the launch; the
            # caller skips that key and, if none is measurable, rotates.
            return None

    return measure, None


def _pick(pool: Pool, source: Profile, owner_profile: Profile) -> Selection:
    strategy = pool.strategy
    if not pool.keys:
        raise KeysError("the key pool is empty")
    if strategy == "random" and len(pool.keys) > 1:
        index = random.randrange(len(pool.keys))
        return Selection(pool.keys[index], strategy, index, owner_profile)
    if strategy in ("usage-min", "usage-max"):
        measure, unmeasurable = _utilization_reader(source)
        scored = []
        for position, key in enumerate(pool.keys):
            value = measure(key)
            if value is not None:
                scored.append((value, position))
        if scored:
            if strategy == "usage-min":
                value, index = min(scored)
            else:
                value, index = max(scored)
            return Selection(pool.keys[index], strategy, index, owner_profile)
        index = _rotation_index(pool)
        return Selection(
            pool.keys[index],
            DEFAULT_STRATEGY,
            index,
            owner_profile,
            fallback=unmeasurable
            or f"no key's usage could be read ({strategy} needs a "
            "key-based usage reader)",
        )
    index = _rotation_index(pool) if strategy == "rotation" else _selected_index(pool)
    return Selection(pool.keys[index], strategy, index, owner_profile)


def peek(profile: Profile) -> Optional[Selection]:
    """What the next launch of ``profile`` would pick, without consuming it."""
    owner_profile = owner(profile)
    if owner_profile is None:
        return None
    pool = load(owner_profile)
    if pool is None or not pool.keys:
        return None
    return _pick(pool, profile, owner_profile)


def materialize(profile: Profile, key_id: str) -> Optional[Selection]:
    """Write the named key into the token file without consuming a rotation.

    A session that pinned a key (``SessionDef.key_id``, claunch-wllke.1) calls
    this on *every* launch, restores included. The token file is shared by
    every launch of the pool, so between two launches of the pinned session
    another launch may have put its own key there; the pin is re-applied
    rather than assumed to still hold.

    This is not a rotation: the cursor stays where the last strategy pick left
    it, so unpinning resumes from that point instead of jumping. The key's
    ``use_count`` and ``last_used_at`` do move — they count launches, and this
    was one.

    Returns ``None`` when nothing could be materialized: no pool in the
    profile's chain, or a pool that no longer holds ``key_id`` (the key was
    removed with ``claunch keys remove`` while a session was pinned to it).
    The caller decides what a stale pin means. ``key_id`` is matched by id
    only -- never as a 1-based position (claunch-wllke.1.2).
    """
    owner_profile = owner(profile)
    if owner_profile is None:
        return None
    with pool_lock(owner_profile):
        pool = load(owner_profile)
        if pool is None or not pool.keys:
            return None
        try:
            index = _index_of_id(pool, key_id)
        except KeysError:
            return None
        used = pool.keys[index]
        stamp = _now()
        pool.keys[index] = Key(
            id=used.id,
            token=used.token,
            label=used.label,
            added_at=used.added_at,
            last_used_at=stamp,
            use_count=used.use_count + 1,
        )
        save(owner_profile, pool)
        credentials.save_token(owner_profile, used.token)
        return Selection(used, pool.strategy, index, owner_profile)


def select_for_launch(profile: Profile) -> Optional[Selection]:
    """Pick a key, make it the effective token and advance the pool.

    The token file is written on every call, so a pool whose
    ``.launcher-token`` was deleted is repaired by the profile's next launch:
    the pool file is the durable record and the token file is derived from it.
    Nothing reads the pool back into a token file ahead of a launch --
    ``claunch keys list`` reports the missing file instead of consuming a
    rotation to fix it (claunch-wllke.5).

    Returns ``None`` when no profile in the chain owns a pool, which is the
    pre-claunch-wllke behaviour: ``.launcher-token`` is simply left alone.
    """
    owner_profile = owner(profile)
    if owner_profile is None:
        return None
    # The whole read-pick-write under the pool's lock: two launches that read
    # the same cursor would otherwise pick the same key and one cursor update
    # would be lost (claunch-wllke.2).
    with pool_lock(owner_profile):
        pool = load(owner_profile)
        if pool is None or not pool.keys:
            return None
        selection = _pick(pool, profile, owner_profile)
        index = selection.index
        used = pool.keys[index]
        stamp = _now()
        pool.keys[index] = Key(
            id=used.id,
            token=used.token,
            label=used.label,
            added_at=used.added_at,
            last_used_at=stamp,
            use_count=used.use_count + 1,
        )
        pool.cursor = used.id
        save(owner_profile, pool)
        credentials.save_token(owner_profile, used.token)
        return selection


def view(profile: Profile) -> dict:
    """The pool a launch of ``profile`` would select from, for the API.

    ``summary`` answers about one profile's own pool; this answers about the
    pool a launch actually reads, which for a child holding no token of its
    own is its nearest ancestor's (see :func:`owner`). The modal lists the
    keys the session's next launch would use, so it asks this one
    (claunch-wllke.1).

    ``owner`` is the profile whose pool it is, or ``None`` when no profile in
    the chain has one. Masks only: the plaintext token never leaves the
    daemon through this route.
    """
    source = owner(profile)
    if source is None:
        return {"profile": profile.name, "owner": None, "pooled": False, "keys": []}
    data = summary(source)
    data["profile"] = profile.name
    data["owner"] = source.name
    return data


def summary(profile: Profile) -> dict:
    """A JSON-safe description of the profile's pool for the CLI and the web."""
    pool = load(profile)
    if pool is None:
        return {"profile": profile.name, "pooled": False, "keys": []}
    current = credentials.stored_token(profile)
    return {
        "profile": profile.name,
        "pooled": True,
        "strategy": pool.strategy,
        "cursor": pool.cursor,
        "keys": [
            {
                "id": key.id,
                "label": key.label,
                "masked": key.masked(),
                "added_at": key.added_at,
                "last_used_at": key.last_used_at,
                "use_count": key.use_count,
                "current": key.token == current,
            }
            for key in pool.keys
        ],
    }
