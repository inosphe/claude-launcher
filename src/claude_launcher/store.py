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
        env: {KEY: VALUE, ...}
        allowed_harnesses: [claude, ...]  # optional; missing = unrestricted
    profiles:
      <name>:
        parent: <other>         # optional
        harness: <name>         # optional; inherited, default claude
        provider: <name>        # optional; Claude Code only
        allowed_harnesses: [claude, pi]  # optional; inherited by intersection
        env: {KEY: VALUE, ...}
    workspaces:                 # machine-local; see :mod:`workspaces`
      <name>: <absolute path>

The on-disk file is first created from a bootstrap *template* (``template.yaml``,
see :mod:`template`); after that this file is authoritative and is read live —
nothing else stores these settings, so there is no separate "export" step.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict, Optional

import yaml

from . import atomic, config

VERSION = 1


class StoreError(Exception):
    """Raised for an unreadable or malformed config file."""


def path() -> Path:
    """The config file backing the store (``~/.claunch.yaml`` by default)."""
    return config.sync_file()


def load() -> dict:
    """Return the live config document (an empty default if the file is absent).

    Reads fresh each call — the file is small and the CLI is short-lived, so this
    keeps every command seeing the current state without a cache to invalidate.

    A *missing* file is fine (a fresh install). A file that is present but
    unparseable raises :class:`StoreError` rather than being silently treated as
    empty — this is now the only state file, so a transient parse error must not
    let the next write clobber it.
    """
    p = path()
    if not p.is_file():
        return {"version": VERSION}
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise StoreError(f"cannot read config file {p}: {exc}") from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise StoreError(f"config file {p} must be a mapping at the top level")
    data.setdefault("version", VERSION)
    return data


def save(doc: dict) -> None:
    """Persist ``doc`` as the config file (stable key order, like the old export).

    Written to a temporary file beside it and renamed into place, because this
    file has concurrent readers. :func:`load` reads it fresh on every call and
    the daemon calls it on **every** ``/api/sessions`` poll (two seconds, per
    open browser tab, to answer whether the briefing summariser is configured
    -- see ``daemon.api.h_sessions_list``). The plain ``write_text`` this
    replaced truncated the file before writing it, so a reader landing inside
    that window saw an empty or half-written document. Empty is the dangerous
    one: it parses cleanly and simply has no ``llm`` block, so a configuration
    that was never wrong reported itself absent for that poll and the web UI's
    briefing controls went inert until the next one.

    ``os.replace`` is atomic on POSIX and on Windows, so a reader sees either
    the whole old document or the whole new one -- never a state between them.
    The temporary carries this process's pid so two writers cannot land on the
    same scratch name, and it is cleaned up if the rename never happens.
    """
    doc.setdefault("version", VERSION)
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(
        doc, sort_keys=True, allow_unicode=True, default_flow_style=False
    )
    with atomic.scratch(p) as tmp:
        tmp.write_text(text, encoding="utf-8")
        atomic.replace(tmp, p)


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
    "port": 8377,
    "idle_threshold": 2.0,
    "scrollback_lines": 5000,
    "restore": True,
    # The cflow reminder clock's machine defaults: whether runs get their
    # current step's instructions re-typed into the driving session, and
    # after how many seconds without progress. Per-run overrides live in run
    # state (engine.set_reminder). Read LIVE by the daemon on every clock
    # tick — unlike the keys above, editing these needs no restart.
    "cflow_reminder": True,
    "cflow_reminder_interval": 600.0,
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
}


def daemon_config(doc: Optional[dict] = None) -> dict:
    """The effective ``daemon`` settings (defaults merged under the file's)."""
    doc = load() if doc is None else doc
    block = doc.get("daemon")
    merged = dict(DAEMON_DEFAULTS)
    if isinstance(block, dict):
        merged.update(block)
    return merged


def relay_config(doc: Optional[dict] = None) -> dict:
    """The ``daemon.relay`` uplink block (empty dict if unset).

    Recognized keys: ``url`` (relay ws/wss address), ``token`` (backend
    registration token — prefer the ``CLAUNCH_RELAY_TOKEN`` env var),
    ``name`` (directory label; defaults to hostname), ``verify_tls``.
    """
    doc = load() if doc is None else doc
    block = daemon_config(doc).get("relay")
    return dict(block) if isinstance(block, dict) else {}


def set_relay_field(key: str, value) -> None:
    """Set (or clear, when ``value`` is ``None``) one ``daemon.relay`` setting."""

    def _mutate(doc: dict) -> None:
        daemon = doc.get("daemon")
        if not isinstance(daemon, dict):
            daemon = {}
            doc["daemon"] = daemon
        block = daemon.get("relay")
        if not isinstance(block, dict):
            block = {}
            daemon["relay"] = block
        if value is None:
            block.pop(key, None)
            if not block:
                daemon.pop("relay", None)
        else:
            block[key] = value

    update(_mutate)


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
