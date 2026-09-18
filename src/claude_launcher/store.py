"""The launcher's single source of truth: ``~/.claunch.yaml``, read live.

Every launcher-managed setting lives here in one YAML file the launcher reads at
each launch — each profile's ``env``/``parent``/``provider``, the default
``template`` env, and provider definitions plus the active selection. Profile
*existence* is still the directory on disk (Claude Code needs a real
``CLAUDE_CONFIG_DIR``); this file only holds the config attached to a profile.

Login tokens are deliberately **not** here: they are secrets, kept per-profile
and per-machine (see :mod:`credentials`).

Schema::

    version: 1
    template:
      env: {KEY: VALUE, ...}
    provider: <name>            # global default provider (optional)
    providers:
      <name>:
        service: <name>        # optional; authentication/usage service identity
        env: {KEY: VALUE, ...}
        allowed_harnesses: [claude, ...]  # optional; missing = unrestricted
    profiles:
      <name>:
        parent: <other>         # optional
        harness: <name>         # optional; inherited, default claude
        provider: <name>        # optional; Claude Code only
        allowed_harnesses: [claude, pi]  # optional; inherited by intersection
        env: {KEY: VALUE, ...}
    shared:                     # applied to every profile; see :mod:`plugins`
      marketplaces: [<source>, ...]
      plugins: [<plugin@marketplace>, ...]
      settings: {<settings.json key>: <value>, ...}
    briefing:                  # legacy FAQ source; imported by the daemon
      faq: [{id: <id>, question: <text>, answer: <optional reference>, enabled: true}, ...]
    rag:                        # semantic search over the board and the fleet; see daemon/rag.py
      base_url: https://host/v1  # OpenAI-compatible base: /embeddings and /rerank hang off it
      api_key: <key>            # empty = the feature is off (or CLAUNCH_RAG_API_KEY)
      embedding_model: <model id>
      rerank_model: <model id>  # optional; empty = vector ranking only
      verify_tls: true          # false for a self-signed or mis-chained certificate
      dimensions: 0             # 0 = the model's own width; a smaller value truncates (Matryoshka)
    workspaces:                 # machine-local; see :mod:`workspaces`
      <name>: <absolute path>

The on-disk file is first created from a bootstrap *template* (``template.yaml``,
see :mod:`template`); after that this file is authoritative and is read live —
nothing else stores these settings, so there is no separate "export" step.
"""

from __future__ import annotations

import copy
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
import uuid

import yaml

from . import atomic, config

#: Schema version this build writes. 1: providers carry a Claude ``env``;
#: 2: providers carry the harness-neutral spec (see ``migrate_config``).
#: Version-1 documents are still read; a newer one is refused.
VERSION = 2

#: libyaml's parser when the wheel ships it — an order of magnitude faster than
#: the pure-Python scanner on this file — and the pure one otherwise. Both are
#: the *safe* loader: no tags, no object construction.
_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)

#: The last document parsed, beside the exact text it was parsed from. See
#: :func:`load` — the text is compared, not the mtime, so no filesystem's
#: timestamp granularity can serve a stale document.
_parsed: Optional[Tuple[str, dict]] = None

#: How long :func:`load` may serve ``_parsed`` without re-opening the file, and
#: the path/timestamp of the read that is still within that window (``None``
#: forces a fresh read). Board ``claunch-snhl`` measured this process's own
#: repeated opens -- not one external holder -- as a driver of the Windows
#: os.replace conflict in :mod:`atomic`: a daemon request handler that walks
#: profile/harness lineage calls :func:`load` many times over a few
#: milliseconds to answer one poll, and each call reopened the file. 100ms is
#: well under the daemon's fastest poll interval (2s, see :func:`save`) and
#: short enough that a person watching a CLI command cannot perceive the
#: delay -- long enough to collapse a burst like that into one open.
_DISK_TTL = 0.1
_disk_read_at: Optional[float] = None
_disk_read_path: Optional[Path] = None


class StoreError(Exception):
    """Raised for an unreadable or malformed config file."""


class TransientStoreError(StoreError):
    """:func:`save` could not land: a Windows sharing conflict outlasted the
    retry budget in :mod:`atomic` (``winerror`` 5 or 32 -- see its docstring).

    Not a broken config: the previous document on disk is untouched
    (``atomic.replace`` never got past the rename), and measurement on this
    machine (150+ concurrent sessions, board ``claunch-qd9q``) found the
    conflict is not one holder that lets go -- retrying inside the same
    process call did not clear it even after 20s/200 attempts. So this is
    the caller's cue to stop retrying *here* and let the next command try
    again, not to wait longer or treat the run as broken.
    """


def path() -> Path:
    """The config file backing the store (``~/.claunch.yaml`` by default)."""
    return config.sync_file()


def load() -> dict:
    """Return the live config document (an empty default if the file is absent).

    Reads the file fresh on every call **outside** the ``_DISK_TTL`` window
    described above, so every command and every daemon poll still sees state
    at most 100ms old. Within that window a repeat call is served from
    ``_parsed`` without touching the filesystem at all -- one process's own
    burst of calls no longer reopens the file once per call. What a fresh
    read does not do twice is *parse* it: the text is compared with the last
    one parsed and the document is copied out of that parse when they match.
    The parse was the cost -- 27ms of pure-Python YAML for this file. The
    copy keeps callers free to mutate what they are handed (``update`` does).

    A *missing* file is fine (a fresh install). A file that is present but
    unparseable raises :class:`StoreError` rather than being silently treated as
    empty — this is now the only state file, so a transient parse error must not
    let the next write clobber it.
    """
    global _parsed, _disk_read_at, _disk_read_path
    p = path()
    now = time.monotonic()
    if (
        _disk_read_at is not None
        and _disk_read_path == p
        and now - _disk_read_at < _DISK_TTL
    ):
        return copy.deepcopy(_parsed[1]) if _parsed is not None else {"version": VERSION}
    if not p.is_file():
        _parsed = None
        _disk_read_at = now
        _disk_read_path = p
        return {"version": VERSION}
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise StoreError(f"cannot read config file {p}: {exc}") from exc
    _disk_read_at = now
    _disk_read_path = p
    hit = _parsed
    if hit is not None and hit[0] == text:
        return copy.deepcopy(hit[1])
    try:
        data = yaml.load(text, Loader=_LOADER)
    except yaml.YAMLError as exc:
        raise StoreError(f"cannot read config file {p}: {exc}") from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise StoreError(f"config file {p} must be a mapping at the top level")
    try:
        found = int(data.get("version") or VERSION)
    except (TypeError, ValueError):
        found = VERSION
    if found > VERSION:
        raise StoreError(
            f"config file {p} is schema version {found}; this claunch reads "
            f"up to {VERSION} -- upgrade claunch"
        )
    data.setdefault("version", VERSION)
    _parsed = (text, copy.deepcopy(data))
    return data


def save(doc: dict) -> None:
    """Persist ``doc`` as the config file (stable key order, like the old export).

    Written to a temporary file beside it and renamed into place, because this
    file has concurrent readers. :func:`load` reads it fresh on every call
    (subject to its own ``_DISK_TTL`` window) and the daemon calls it on
    **every** ``/api/sessions`` poll (two seconds, per open browser tab, to
    answer whether the briefing summariser is configured -- see
    ``daemon.api.h_sessions_list``). The plain ``write_text`` this replaced
    truncated the file before writing it, so a reader landing inside that
    window saw an empty or half-written document. Empty is the dangerous
    one: it parses cleanly and simply has no ``llm`` block, so a configuration
    that was never wrong reported itself absent for that poll and the web UI's
    briefing controls went inert until the next one.

    On success this also seeds :func:`load`'s cache with exactly the document
    just written, so this same process's next call reads it back without
    reopening the file *and* without waiting out the TTL window -- a caller
    that just wrote a value must never read its own stale cache. A failed
    write (below) leaves the cache untouched, since nothing changed on disk.

    ``os.replace`` is atomic on POSIX and on Windows, so a reader sees either
    the whole old document or the whole new one -- never a state between them.
    The temporary carries this process's pid so two writers cannot land on the
    same scratch name, and it is cleaned up if the rename never happens.

    A Windows sharing conflict that outlasts :mod:`atomic`'s retry budget
    raises :class:`TransientStoreError` (a :class:`StoreError`, chained to
    the original ``OSError``) rather than letting that ``OSError`` escape --
    ``cli.main`` already knows to report any ``StoreError`` as ``error: ...``
    and exit 1 instead of an unhandled traceback, and this one is worded so
    the caller reads it as "run the command again", not "something is
    broken". Every other failure (a read-only file, a wrong ACL) is still
    raised as the bare ``OSError`` it always was.
    """
    global _parsed, _disk_read_at, _disk_read_path
    doc.setdefault("version", VERSION)
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(
        doc, sort_keys=True, allow_unicode=True, default_flow_style=False
    )
    with atomic.scratch(p) as tmp:
        tmp.write_text(text, encoding="utf-8")
        try:
            atomic.replace(tmp, p)
        except OSError as exc:
            if getattr(exc, "winerror", None) not in atomic.TRANSIENT:
                raise
            raise TransientStoreError(
                f"config file {p} is temporarily locked by another process "
                f"and could not be updated ({exc}); this is not a broken "
                "config -- run the command again"
            ) from exc
    _parsed = (text, copy.deepcopy(doc))
    _disk_read_at = time.monotonic()
    _disk_read_path = p


def update(mutator: Callable[[dict], None]) -> dict:
    """Load, apply ``mutator(doc)`` in place, then save. Returns the saved doc."""
    doc = load()
    mutator(doc)
    save(doc)
    return doc


# --------------------------------------------------------------------------- #
# profiles section
# --------------------------------------------------------------------------- #
def profiles(doc: Optional[dict] = None) -> Dict[str, dict]:
    """The ``profiles`` mapping (read-only view; ``{}`` if absent)."""
    doc = load() if doc is None else doc
    section = doc.get("profiles")
    if not isinstance(section, dict):
        return {}
    return {str(k): v for k, v in section.items() if isinstance(v, dict)}


def profile_entry(name: str, doc: Optional[dict] = None) -> dict:
    """A single profile's config entry (``{}`` if it has none)."""
    return profiles(doc).get(name, {})


def _writable_entry(doc: dict, name: str) -> dict:
    section = doc.get("profiles")
    if not isinstance(section, dict):
        section = {}
        doc["profiles"] = section
    entry = section.get(name)
    if not isinstance(entry, dict):
        entry = {}
        section[name] = entry
    return entry


def set_profile_field(name: str, key: str, value) -> None:
    """Set (or, when ``value`` is ``None``/empty, clear) one field on a profile."""

    def _mutate(doc: dict) -> None:
        entry = _writable_entry(doc, name)
        if value in (None, "", {}):
            entry.pop(key, None)
        else:
            entry[key] = value

    update(_mutate)


def ensure_profile(name: str) -> None:
    """Register ``name`` in the store (empty entry) so it is a known profile.

    Existence is still the directory on disk, but the store's ``profiles`` map is
    the authoritative *list* of profiles the launcher manages — so a profile with
    no config yet still gets an entry (and is never mistaken for an orphan dir).
    """

    def _mutate(doc: dict) -> None:
        section = doc.get("profiles")
        if not isinstance(section, dict):
            section = {}
            doc["profiles"] = section
        section.setdefault(name, {})

    update(_mutate)


def remove_profile(name: str) -> None:
    """Drop a profile's config entry entirely (used by ``remove``)."""

    def _mutate(doc: dict) -> None:
        section = doc.get("profiles")
        if isinstance(section, dict):
            section.pop(name, None)

    update(_mutate)


# --------------------------------------------------------------------------- #
# daemon / harnesses sections
# --------------------------------------------------------------------------- #
#: Defaults for the ``daemon`` config block. ``host`` stays loopback unless the
#: user opts into LAN exposure; auth is mandatory either way.
DAEMON_DEFAULTS = {
    "host": "127.0.0.1",
    "port": 8378,
    "idle_threshold": 2.0,
    "scrollback_lines": 5000,
    # Active terminal viewers get normal process priority and unpaced screen
    # rendering. Sessions without a focused viewer use below-normal Windows
    # process priority and render one screen slice per this interval.
    # These settings are read when the daemon starts.
    "focused_session_scheduling": True,
    "background_render_delay": 0.05,
    # Ceiling on the *sum* of rendering done for sessions nobody is attached
    # to, in KiB per second (a token bucket shared by all of them). Attached
    # sessions are not subject to it. pyte manages ~660 KiB/s on this class
    # of machine; thirty unattended pi sessions asked for more than that.
    "background_render_budget_kib": 256,
    "restore": True,
    # How long an agent-requested daemon restart may wait on the web UI's
    # approval before it counts as approved and goes out (see
    # daemon/restart_gate.py). Seconds. Read when the daemon constructs its
    # gate, i.e. at daemon start, like 'shell' below.
    "restart_approval_timeout": 300.0,
    # How long a leader's request to move a child session's cflow run waits
    # on the web UI's approval before it counts as approved and the move is
    # applied (see daemon/goto_gate.py). Seconds; same start-time wiring as
    # the restart gate's timeout above.
    "goto_approval_timeout": 300.0,
    # The cflow reminder clock's machine defaults: whether runs get their
    # current step's instructions re-typed into the driving session, and
    # after how many seconds without progress. Per-run overrides live in run
    # state (engine.set_reminder). Read LIVE by the daemon on every clock
    # tick — unlike the keys above, editing these needs no restart.
    "cflow_reminder": True,
    "cflow_reminder_interval": 600.0,
    # Session-level role recovery is scheduled independently from cflow.  A
    # role-bearing session with no run still receives a short stance-id check,
    # while a cflow reminder that arrives first satisfies the same debt in the
    # combined Session reminder.
    "role_reminder": True,
    "role_reminder_interval": 600.0,
    "score_goal_default": False,
    # The run event clock's machine switch: whether an overseer session (the
    # driver's spawn parent, else its mesh leader) is told when a run it
    # oversees hits a human gate, finishes a recurring round, or loses its
    # driver. Read LIVE like the reminder keys.
    "cflow_events": True,
    # Kill-on-end: when the same clock sees a finished ONE-SHOT run (status
    # done, no recur, no pending next start), the daemon records a final
    # "session ended" block into the driving session's own transcript (durably
    # append+flush — the WAL) and then, after an idle wait of at most
    # cflow_kill_on_end_grace seconds, terminates the session and returns its
    # slot. Recurring workflows are never touched; a session whose record
    # carries keep_alive is recorded but left running; a record that could not
    # be made durable means no kill. Read LIVE like the keys above.
    "cflow_kill_on_end": True,
    "cflow_kill_on_end_grace": 120.0,
    # The stall ping clock: whether a session that has STOPPED at a step that
    # is its own to move — no gate, no selection, no delegated answer holding
    # it — is pinged after this many seconds, and with what text. The reminder
    # above deliberately never lands here (it types only into a *working*
    # session), so this is the one clock that opens a fresh turn in a session
    # that has gone quiet. OFF by default for exactly that reason: a run may
    # be idle at an actionable step because its workflow parks there waiting
    # for a human to hand it a goal, and pinging a fleet of those burns tokens
    # to tell agents something they already know. Read LIVE like the keys
    # above — a config edit or the web UI's PUT applies within one tick.
    "cflow_ping": False,
    "cflow_ping_interval": 900.0,
    # English like every other block the daemon types into a session; the
    # point of the setting is that an operator replaces it with their own.
    "cflow_ping_message": (
        "This run has not moved in a long time and nothing is holding it. "
        "Pick it back up, or say what you are waiting for."
    ),
    # The measurement window (daemon/window.py): how many test runs of each
    # class may hold the window at once. "sweep" (a full suite) is exclusive
    # against everything regardless of this number; "targeted" (a
    # nodeid-selected run) shares up to the cap. Interim values set by the
    # operator 2026-08-29 (board claunch-8y5j); the measured revision belongs
    # to claunch-bl0e. Deliberately not CPU-derived — this suite's cost axes
    # are process spawn and PTY/daemon waits, not cores (claunch-95fa, s159:
    # 32 cores at 15% under 22 pytest processes). Read LIVE on every acquire.
    "window_sweep_cap": 1,
    "window_targeted_cap": 5,
    # The resume nudge's machine switch: after a daemon restart, whether the
    # sessions that were mid-turn when it went down are told to carry on.
    # Restored sessions come back alive but idle — nothing is driving them —
    # and this is what re-starts them. Read LIVE, like the keys above, but
    # only ever at restore, so an edit applies to the next restart.
    "resume_nudge": True,
    # The dashboard CLI tab's raw shell: the command (a string or an argv
    # list) and the directory it starts in. None = the platform's default
    # shell (COMSPEC / $SHELL) in the daemon's own working directory. Read
    # when the daemon constructs its shell, i.e. at daemon start.
    "shell": None,
    "shell_cwd": None,
    # The board (beads) hooks on a session's life — see daemon/beads.py. All
    # read LIVE. beads_auto_issue: a session created with a task gets an
    # issue minted and assigned to it (one it names as `issue: <id>` is
    # adopted instead). beads_winddown: a kill of a session holding active
    # issues first types a wind-down block into it and waits for that turn
    # (at most beads_winddown_grace seconds) before terminating; 0 or false
    # kills at once, as before. The exit sweep (in_progress -> open with a
    # comment) is not a setting: a board that says a gone session is still
    # working on something is wrong, whatever the operator's preferences.
    "beads_auto_issue": True,
    "beads_winddown": True,
    "beads_winddown_grace": 120.0,
    # A merge/handoff completion (daemon/handoff.py) waits this long for the
    # target to take the report before it gives up and KEEPS the source —
    # the target's keyboard may be busy, and the report is the only thing of
    # the source that survives, so a timeout is "try again", never "end it
    # anyway". 0 waits without limit.
    "handoff_deliver_timeout": 120.0,
}


def daemon_config(doc: Optional[dict] = None) -> dict:
    """The effective ``daemon`` settings (defaults merged under the file's)."""
    doc = load() if doc is None else doc
    block = doc.get("daemon")
    merged = dict(DAEMON_DEFAULTS)
    if isinstance(block, dict):
        merged.update(block)
    return merged


def _normalize_relay(entry: dict, index: int) -> dict:
    """One relay entry with its local handle (``id``) filled in.

    ``id`` is how the CLI and the API address this relay among the others. It
    is NOT the backend name the relay directory sees (``name``): two relays may
    legitimately register this daemon under the same name, so the name cannot
    identify a config row. An entry that does not carry one gets ``relay1``,
    ``relay2`` ... by position.
    """
    out = dict(entry)
    ident = str(out.get("id") or "").strip()
    out["id"] = ident or f"relay{index + 1}"
    return out


def relays_config(doc: Optional[dict] = None) -> List[dict]:
    """Every configured relay uplink, in config order.

    Two shapes are read. ``daemon.relays`` is a LIST of blocks and is the one
    to write for more than one relay; ``daemon.relay`` is the original single
    block and is still honoured so an existing config keeps working untouched.
    When both are present the list wins and the single block is ignored — one
    file must not describe the same daemon's uplinks two ways.

    Recognized keys per entry: ``url`` (relay ws/wss address), ``token``
    (backend registration token), ``name`` (directory label; defaults to
    hostname), ``verify_tls``, and ``id`` (local handle, see
    :func:`_normalize_relay`).
    """
    doc = load() if doc is None else doc
    daemon = daemon_config(doc)
    rows = daemon.get("relays")
    if isinstance(rows, list):
        return [
            _normalize_relay(row, i)
            for i, row in enumerate(r for r in rows if isinstance(r, dict))
        ]
    block = daemon.get("relay")
    if isinstance(block, dict) and block:
        return [_normalize_relay(block, 0)]
    return []


def relay_config(doc: Optional[dict] = None) -> dict:
    """The FIRST configured relay uplink (empty dict if none).

    Kept for callers that only ever meant "the relay": with a single-relay
    config it answers exactly what it always did. Code that must see every
    uplink uses :func:`relays_config`.
    """
    rows = relays_config(doc)
    return rows[0] if rows else {}


def _daemon_block(doc: dict) -> dict:
    block = doc.get("daemon")
    if not isinstance(block, dict):
        block = {}
        doc["daemon"] = block
    return block


def _uses_relay_list(doc: dict) -> bool:
    return isinstance(_daemon_block(doc).get("relays"), list)


def _migrate_to_relay_list(daemon: dict) -> List[dict]:
    """Move a legacy ``daemon.relay`` block into ``daemon.relays``.

    Called only when a write actually needs the list shape, so a
    single-relay config file is never rewritten just for being read.
    """
    rows = daemon.get("relays")
    if not isinstance(rows, list):
        rows = []
        legacy = daemon.pop("relay", None)
        if isinstance(legacy, dict) and legacy:
            rows.append(legacy)
        daemon["relays"] = rows
    return rows


def _find_relay(rows: List[dict], relay: str) -> Optional[dict]:
    """The entry addressed by ``relay`` — its ``id``, else its position."""
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        ident = str(row.get("id") or "").strip() or f"relay{i + 1}"
        if ident == relay:
            return row
    return None


def set_relay_field(key: str, value, *, relay: Optional[str] = None) -> None:
    """Set (or clear, when ``value`` is ``None``) one relay uplink setting.

    Without ``relay`` this addresses the first (or only) uplink and keeps the
    file in whatever shape it already has: a config that never had more than
    one relay stays a plain ``daemon.relay`` block. Naming a ``relay`` handle
    switches the file to the ``daemon.relays`` list, migrating the legacy
    block into it first, because that is the only shape that can hold a second
    entry.
    """

    def _mutate(doc: dict) -> None:
        daemon = _daemon_block(doc)
        if relay is None and not isinstance(daemon.get("relays"), list):
            _set_legacy_relay_field(doc, daemon, key, value)
            return
        rows = _migrate_to_relay_list(daemon)
        target = _find_relay(rows, relay) if relay is not None else (
            rows[0] if rows else None
        )
        if target is None:
            if value is None:
                return
            target = {"id": relay} if relay is not None else {}
            rows.append(target)
        if value is None:
            target.pop(key, None)
            # An entry left with nothing but its handle is not a relay.
            if not [k for k in target if k != "id"]:
                rows.remove(target)
            if not rows:
                daemon.pop("relays", None)
        else:
            target[key] = value

    update(_mutate)


def _set_legacy_relay_field(doc: dict, daemon: dict, key: str, value) -> None:
    block = daemon.get("relay")
    if not isinstance(block, dict):
        if value is None:
            return
        block = {}
        daemon["relay"] = block
    if value is None:
        block.pop(key, None)
        if not block:
            daemon.pop("relay", None)
    else:
        block[key] = value


def add_relay(relay: str, **fields) -> None:
    """Append a relay uplink under the local handle ``relay``.

    Raises ``ValueError`` when the handle is already taken — silently merging
    into an existing entry would let a typo point two commands at one uplink.
    """
    ident = str(relay or "").strip()
    if not ident:
        raise ValueError("a relay handle is required")

    def _mutate(doc: dict) -> None:
        rows = _migrate_to_relay_list(_daemon_block(doc))
        if _find_relay(rows, ident) is not None:
            raise ValueError(f"relay {ident!r} already exists")
        entry = {"id": ident}
        entry.update({k: v for k, v in fields.items() if v is not None})
        rows.append(entry)

    update(_mutate)


def remove_relay(relay: str) -> bool:
    """Drop the relay uplink addressed by ``relay``; False if there was none."""
    removed = False

    def _mutate(doc: dict) -> None:
        nonlocal removed
        daemon = _daemon_block(doc)
        # Look before migrating: a handle that is not there must leave the
        # file exactly as it was, legacy single block included.
        if _find_relay(relays_config(doc), relay) is None:
            return
        rows = _migrate_to_relay_list(daemon)
        target = _find_relay(rows, relay)
        if target is None:
            return
        rows.remove(target)
        removed = True
        if not rows:
            daemon.pop("relays", None)

    update(_mutate)
    return removed


def set_daemon_field(key: str, value) -> None:
    """Set (or clear, when ``value`` is ``None``) one ``daemon`` setting."""

    def _mutate(doc: dict) -> None:
        block = doc.get("daemon")
        if not isinstance(block, dict):
            block = {}
            doc["daemon"] = block
        if value is None:
            block.pop(key, None)
            if not block:
                doc.pop("daemon", None)
        else:
            block[key] = value

    update(_mutate)


# --------------------------------------------------------------------------- #
# briefing FAQ
# --------------------------------------------------------------------------- #
def briefing_faq(doc: Optional[dict] = None) -> List[dict]:
    """Return legacy global FAQ entries for one-time daemon import."""
    doc = load() if doc is None else doc
    block = doc.get("briefing")
    rows = block.get("faq") if isinstance(block, dict) else None
    # Accept the original single-entry shape while reading so upgrading the
    # setting does not discard an FAQ that was already configured.
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list):
        return []
    out = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        question = str(row.get("question") or "").strip()
        answer = str(row.get("answer") or "").strip()
        if question:
            out.append({
                "id": str(row.get("id") or f"faq-{index + 1}"),
                "question": question,
                "answer": answer,
                "enabled": row.get("enabled", True) is not False,
            })
    return out


def set_briefing_faq(rows: List[dict]) -> List[dict]:
    """Replace the briefing FAQ after validating its persisted shape."""
    clean = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        question = str(row.get("question") or "").strip()
        answer = str(row.get("answer") or "").strip()
        if not question:
            continue
        clean.append({
            "id": str(row.get("id") or uuid.uuid4()),
            "question": question,
            "answer": answer,
            "enabled": row.get("enabled", True) is not False,
        })

    def _mutate(doc: dict) -> None:
        block = doc.get("briefing")
        if not isinstance(block, dict):
            block = {}
            doc["briefing"] = block
        if clean:
            block["faq"] = clean
        else:
            block.pop("faq", None)
            if not block:
                doc.pop("briefing", None)

    update(_mutate)
    return clean


# --------------------------------------------------------------------------- #
# rag — the embedding/reranker endpoint behind semantic search
# --------------------------------------------------------------------------- #
#: Defaults for the top-level ``rag`` block. ``dimensions`` 0 keeps the model's
#: own width; ``batch`` is how many texts one embeddings call carries (the
#: endpoint measured at ~1000 tokens/s, so a batch of 16 board issues is a
#: 10-15 s request); ``candidates`` is how many vector hits feed the reranker
#: and ``rerank_top`` how many of those it is asked to score (about 0.2-0.35 s
#: per document on the measured endpoint, so this bounds a search's latency).
#: ``watch_interval`` is how often (seconds) the daemon stats each known
#: board's ``.beads/beads.db`` / ``issues.jsonl`` for a write it did not make
#: itself (``claunch beads …`` runs ``br`` directly); 0 turns that watcher off.
RAG_DEFAULTS = {
    "base_url": "",
    "api_key": "",
    "embedding_model": "",
    "rerank_model": "",
    "verify_tls": True,
    "dimensions": 0,
    "timeout": 120.0,
    "batch": 16,
    "candidates": 40,
    "rerank_top": 12,
    "watch_interval": 30.0,
}


def rag_config(doc: Optional[dict] = None) -> dict:
    """The effective ``rag`` settings (missing keys filled with defaults).

    Shape-tolerant like :func:`daemon_config`: the file is hand-edited, so a
    missing or malformed block is the disabled default, not an error. The
    api key may come from ``CLAUNCH_RAG_API_KEY`` instead of the file, the
    same arrangement the relay token has, so a synced config need not carry
    it. It never leaves this dict except as an ``Authorization`` header.
    """
    import os

    doc = load() if doc is None else doc
    block = doc.get("rag")
    if not isinstance(block, dict):
        block = {}
    out = dict(RAG_DEFAULTS)
    for key in ("base_url", "api_key", "embedding_model", "rerank_model"):
        out[key] = str(block.get(key) or "").strip()
    env_key = os.environ.get("CLAUNCH_RAG_API_KEY")
    if env_key:
        out["api_key"] = env_key.strip()
    out["verify_tls"] = block.get("verify_tls", True) is not False
    for key in ("dimensions", "batch", "candidates", "rerank_top"):
        try:
            value = int(block.get(key) if block.get(key) is not None else RAG_DEFAULTS[key])
        except (TypeError, ValueError):
            value = RAG_DEFAULTS[key]
        out[key] = max(0, value) if key == "dimensions" else max(1, value)
    try:
        out["timeout"] = float(block.get("timeout") or RAG_DEFAULTS["timeout"])
    except (TypeError, ValueError):
        out["timeout"] = RAG_DEFAULTS["timeout"]
    # 0 is a real answer here (watcher off), so only a missing or malformed
    # value falls back to the default.
    try:
        raw = block.get("watch_interval")
        out["watch_interval"] = max(
            0.0, float(RAG_DEFAULTS["watch_interval"] if raw is None else raw)
        )
    except (TypeError, ValueError):
        out["watch_interval"] = RAG_DEFAULTS["watch_interval"]
    return out


def rag_configured(cfg: dict) -> bool:
    """Whether search is on: base_url, embedding_model and api_key all present."""
    return bool(cfg.get("base_url") and cfg.get("embedding_model") and cfg.get("api_key"))


def harnesses(doc: Optional[dict] = None) -> Dict[str, dict]:
    """User-defined harnesses (``claude`` is built-in and need not be listed)."""
    doc = load() if doc is None else doc
    section = doc.get("harnesses")
    if not isinstance(section, dict):
        return {}
    return {str(k): v for k, v in section.items() if isinstance(v, dict)}


# --------------------------------------------------------------------------- #
# template section
# --------------------------------------------------------------------------- #
def template_block(doc: Optional[dict] = None) -> dict:
    """The whole live ``template`` block (``{}`` if absent)."""
    doc = load() if doc is None else doc
    tmpl = doc.get("template")
    return dict(tmpl) if isinstance(tmpl, dict) else {}


def template_env(doc: Optional[dict] = None) -> Dict[str, str]:
    """The default env applied to new profiles (live ``template.env`` block)."""
    doc = load() if doc is None else doc
    tmpl = doc.get("template")
    block = tmpl.get("env") if isinstance(tmpl, dict) else None
    return {str(k): str(v) for k, v in block.items()} if isinstance(block, dict) else {}


def set_template_env(env: Dict[str, str]) -> None:
    def _mutate(doc: dict) -> None:
        tmpl = doc.get("template")
        if not isinstance(tmpl, dict):
            tmpl = {}
            doc["template"] = tmpl
        tmpl["env"] = {str(k): str(v) for k, v in env.items()}

    update(_mutate)


# --------------------------------------------------------------------------- #
# shared section
# --------------------------------------------------------------------------- #
#: The ``shared`` block: harness-global state that a profile keeps in its own
#: ``CLAUDE_CONFIG_DIR`` and therefore holds one copy of per profile. Declared
#: once here, converged onto every profile by :mod:`plugins`.
SHARED_KEYS = ("marketplaces", "plugins", "settings")

#: ``settings.json`` keys claunch converges unless the file says otherwise.
#:
#: ``permissions.defaultMode`` is here because a profile *is* the file Claude
#: Code reads it from (``$CLAUDE_CONFIG_DIR/settings.json`` is user scope, the
#: only scope where ``auto`` and ``bypassPermissions`` take effect at all), and
#: because the alternative default is expensive in exactly the place this
#: project runs: a profile with no value asks before *every* tool call, so an
#: agent session spends its round answering prompts. ``seed`` copies the
#: global ``settings.json`` at creation time, which gives a new profile
#: whatever that file happens to say and nothing at all when there is none --
#: profiles created before the value was set, or seeded from a different
#: source, stay on the asking default with nothing reporting it.
#:
#: ``auto`` is the mode this project's own sessions run in: a classifier
#: approves what it can and defers the rest, rather than ``bypassPermissions``
#: (no prompts at all, but refused outright in sessions where Claude Code
#: declines the mode). A profile that should keep asking declares
#: ``"default"``; ``claunch shared --unset permissions.defaultMode`` returns it
#: to this.
SHARED_SETTINGS_DEFAULTS: Dict[str, object] = {
    "permissions.defaultMode": "auto",
}


def shared(doc: Optional[dict] = None) -> dict:
    """The ``shared`` block (``{}`` if absent or malformed)."""
    doc = load() if doc is None else doc
    section = doc.get("shared")
    return section if isinstance(section, dict) else {}


def _shared_list(key: str, doc: Optional[dict]) -> List[str]:
    block = shared(doc).get(key)
    if not isinstance(block, list):
        return []
    return [str(item) for item in block if str(item).strip()]


def shared_marketplaces(doc: Optional[dict] = None) -> List[str]:
    """Marketplace sources every profile should know (URL, path or ``owner/repo``)."""
    return _shared_list("marketplaces", doc)


def shared_plugins(doc: Optional[dict] = None) -> List[str]:
    """Plugin ids (``plugin@marketplace``) every profile should have installed."""
    return _shared_list("plugins", doc)


def shared_settings(doc: Optional[dict] = None) -> Dict[str, object]:
    """The ``settings.json`` keys the FILE declares (e.g. ``outputStyle``).

    This is the declaration as written, not the effective set: a key claunch
    ships a default for but nobody declared is absent here (see
    :func:`effective_shared_settings`). The distinction matters because this
    mapping is what ``claunch shared`` writes back — materializing a default
    into the user's file as a side effect of an unrelated edit would record a
    decision they did not make.
    """
    block = shared(doc).get("settings")
    return {str(k): v for k, v in block.items()} if isinstance(block, dict) else {}


def effective_shared_settings(doc: Optional[dict] = None) -> Dict[str, object]:
    """``settings.json`` keys every Claude Code profile should carry.

    :data:`SHARED_SETTINGS_DEFAULTS` merged under the file's declaration, the
    same shape as :func:`daemon_config`. This is what convergence reads: a
    declared key replaces the default (including with a value that switches
    the feature off), and ``claunch shared --unset`` returns the key to it.
    """
    return {**SHARED_SETTINGS_DEFAULTS, **shared_settings(doc)}


def profile_settings(name: str, doc: Optional[dict] = None) -> Dict[str, object]:
    """Native settings declared for this profile only, without parent inheritance."""
    block = profile_entry(name, doc).get("settings")
    return dict(block) if isinstance(block, dict) else {}


def effective_profile_settings(name: str, doc: Optional[dict] = None) -> Dict[str, object]:
    """Profile declarations take precedence over shared and packaged defaults."""
    doc = load() if doc is None else doc
    return {**effective_shared_settings(doc), **profile_settings(name, doc)}


def set_profile_setting(name: str, key: str, value) -> None:
    """Set one native setting; None removes the override and restores sharing."""
    def _mutate(doc: dict) -> None:
        entry = _writable_entry(doc, name)
        values = profile_settings(name, doc)
        if value is None:
            values.pop(key, None)
        else:
            values[key] = value
        if values:
            entry["settings"] = values
        else:
            entry.pop("settings", None)

    update(_mutate)


def set_shared_field(key: str, value) -> None:
    """Set (or, when ``value`` is empty, clear) one key of the ``shared`` block."""
    if key not in SHARED_KEYS:
        raise StoreError(f"unknown shared key {key!r} (known: {', '.join(SHARED_KEYS)})")

    def _mutate(doc: dict) -> None:
        section = doc.get("shared")
        if not isinstance(section, dict):
            section = {}
            doc["shared"] = section
        if value in (None, "", [], {}):
            section.pop(key, None)
        else:
            section[key] = value
        if not section:
            doc.pop("shared", None)

    update(_mutate)
