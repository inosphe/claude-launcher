"""The shared harness layer: declared once in the store, converged onto every profile.

A profile *is* its own ``CLAUDE_CONFIG_DIR``, so everything Claude Code keeps
there exists once **per profile**: the installed plugins under ``plugins/``, the
marketplaces they came from, and the global ``settings.json`` keys. Nothing
carries a decision made in one profile across to the others. :mod:`seed` copies
the global config at *creation* time only, so a plugin installed afterwards
reaches the one profile it was installed in and no other, and the set drifts
apart without anything reporting it.

This module is the missing path, and it is the same shape the launcher already
uses for env: the config file holds the declaration (``shared`` -- see
:mod:`store`), and applying it writes the profile. :func:`plan` says what a
profile is missing; :func:`apply_to` closes the gap.

Plugins are installed by **running Claude Code's own ``claude plugin`` CLI**
with ``CLAUDE_CONFIG_DIR`` pointed at the profile, never by copying files from
another profile. One install writes three places that have to agree -- the
plugin's files under ``plugins/``, the two JSON indexes beside them, and
``enabledPlugins``/``extraKnownMarketplaces`` in ``settings.json`` -- and the
indexes record *absolute* install paths naming the profile they were written
for. A copied index therefore points every other profile back at the profile it
came from, which works until that one is removed. Letting the CLI do its own
install keeps all three consistent and machine-correct.

Convergence is additive on purpose. ``apply`` installs what is declared and
missing; it never removes a plugin a profile has but the declaration does not,
because a profile is also allowed to have plugins of its own. Removal is an
explicit act (``claunch plugin uninstall``), which drops the declaration and
runs the uninstall across the profiles in the same call.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import config, settings, store
from .profile import Profile

#: Where Claude Code records what a profile knows, under its config dir.
KNOWN_MARKETPLACES = ("plugins", "known_marketplaces.json")
INSTALLED_PLUGINS = ("plugins", "installed_plugins.json")

#: Scope every install uses: the profile's config dir is the "user" scope, which
#: is exactly the per-profile state this layer is about. A project-scope install
#: would land in whatever directory the command happened to run in.
SCOPE = "user"

#: What a pending action is about.
MARKETPLACE = "marketplace"
PLUGIN = "plugin"
SETTING = "setting"


class PluginError(Exception):
    """Raised for a declaration that cannot be applied."""


@dataclass(frozen=True)
class Action:
    """One thing a profile is missing, and what applying it would do."""

    kind: str
    target: str
    detail: str = ""

    def describe(self) -> str:
        if self.kind == SETTING:
            return f"setting {self.target}={self.detail}"
        return f"{self.kind} {self.target}"


@dataclass
class Result:
    """What :func:`apply_to` did to one profile."""

    profile: str
    done: List[Action] = field(default_factory=list)
    failed: List[Tuple[Action, str]] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.done)

    @property
    def ok(self) -> bool:
        return not self.failed


# --------------------------------------------------------------------------- #
# reading a profile's current state
# --------------------------------------------------------------------------- #
def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _normalise(source: str) -> str:
    """Compare marketplace sources without tripping over Windows path spelling."""
    return str(source).strip().replace("\\", "/").rstrip("/").casefold()


def source_of(entry: dict) -> Optional[str]:
    """The ``claude plugin marketplace add`` argument that produced ``entry``."""
    block = entry.get("source") if isinstance(entry, dict) else None
    if not isinstance(block, dict):
        return None
    for key in ("repo", "path", "url"):
        value = block.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def known_marketplaces(profile: Profile) -> Dict[str, str]:
    """Marketplace name to source string, as this profile has them registered."""
    data = _read_json(profile.config_dir.joinpath(*KNOWN_MARKETPLACES))
    found: Dict[str, str] = {}
    for name, entry in data.items():
        source = source_of(entry) if isinstance(entry, dict) else None
        if source:
            found[str(name)] = source
    return found


def installed_plugins(profile: Profile) -> List[str]:
    """Plugin ids this profile has installed *and* enabled.

    Both halves are required. The files can be present while ``enabledPlugins``
    says false (a disabled plugin), and the setting can name an id whose files
    were never fetched -- neither is "installed" for this layer's purpose, and
    re-running the install is the fix for both.
    """
    data = _read_json(profile.config_dir.joinpath(*INSTALLED_PLUGINS))
    block = data.get("plugins")
    present = set(block) if isinstance(block, dict) else set()
    enabled = settings.load(profile).get("enabledPlugins")
    enabled_ids = (
        {k for k, v in enabled.items() if v is True}
        if isinstance(enabled, dict)
        else set()
    )
    return sorted(present & enabled_ids)


def discover_marketplace_source(
    name: str, profiles: Sequence[Profile]
) -> Optional[str]:
    """Find where a marketplace named ``name`` was added from, in any profile.

    Installing ``plugin@marketplace`` needs that marketplace declared, and the
    usual order of events is that the user tried it in one profile first. Rather
    than making them re-type the source, read it back off whichever profile
    already knows it.
    """
    for p in profiles:
        source = known_marketplaces(p).get(name)
        if source:
            return source
    return None


# --------------------------------------------------------------------------- #
# planning
# --------------------------------------------------------------------------- #
def plan(profile: Profile, doc: Optional[dict] = None) -> List[Action]:
    """Every declared thing this profile is missing, in the order to apply it."""
    doc = store.load() if doc is None else doc
    actions: List[Action] = []

    have_sources = {_normalise(s) for s in known_marketplaces(profile).values()}
    for source in store.shared_marketplaces(doc):
        if _normalise(source) not in have_sources:
            actions.append(Action(MARKETPLACE, source))

    have_plugins = set(installed_plugins(profile))
    for plugin_id in store.shared_plugins(doc):
        if plugin_id not in have_plugins:
            actions.append(Action(PLUGIN, plugin_id))

    current = settings.load(profile)
    for key, value in store.shared_settings(doc).items():
        if current.get(key) != value:
            actions.append(Action(SETTING, key, json.dumps(value, ensure_ascii=False)))
    return actions


# --------------------------------------------------------------------------- #
# applying
# --------------------------------------------------------------------------- #
#: Signature of the process runner, so tests can drive this without a real
#: ``claude`` on PATH: ``(argv, env) -> (returncode, output)``.
Runner = Callable[[List[str], Dict[str, str]], Tuple[int, str]]


def _run(argv: List[str], env: Dict[str, str]) -> Tuple[int, str]:
    try:
        proc = subprocess.run(
            argv,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        return 1, str(exc)
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()


def error_line(output: str) -> str:
    """The one line of a failed ``claude plugin`` run worth putting in a report.

    The *last* non-empty line, not the first: the CLI prints progress ("Installing
    plugin ...") before it prints why it stopped, so quoting the first line
    reports that the command started and says nothing about the failure.
    """
    lines = [line.strip() for line in str(output or "").splitlines() if line.strip()]
    return lines[-1] if lines else "failed"


def _env_for(profile: Profile) -> Dict[str, str]:
    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = str(profile.config_dir)
    return env


def command_for(action: Action) -> Optional[List[str]]:
    """The ``claude`` argv that performs ``action`` (``None`` for a settings key)."""
    claude = config.claude_bin()
    if action.kind == MARKETPLACE:
        return [
            claude, "plugin", "marketplace", "add", action.target, "--scope", SCOPE
        ]
    if action.kind == PLUGIN:
        # ``-y`` because this never runs on a TTY the user is watching, and the
        # CLI refuses a marketplace-declared command without it.
        return [claude, "plugin", "install", action.target, "--scope", SCOPE, "-y"]
    return None


def apply_to(
    profile: Profile,
    *,
    doc: Optional[dict] = None,
    dry_run: bool = False,
    runner: Optional[Runner] = None,
) -> Result:
    """Converge one profile onto the declaration; report what changed and what failed.

    A failure does not stop the rest: a marketplace that cannot be reached must
    not keep a settings key from being written, and the caller needs the whole
    picture in one pass rather than one problem per run.
    """
    run = runner or _run
    result = Result(profile=profile.name)
    actions = plan(profile, doc)
    if not profile.exists():
        for action in actions:
            result.failed.append(
                (action, f"profile directory missing: {profile.config_dir}")
            )
        return result
    env = _env_for(profile)
    for action in actions:
        if dry_run:
            result.done.append(action)
            continue
        if action.kind == SETTING:
            data = settings.load(profile)
            data[action.target] = json.loads(action.detail)
            settings.save(profile, data)
            result.done.append(action)
            continue
        argv = command_for(action)
        code, output = run(list(argv or []), env)
        if code == 0:
            result.done.append(action)
        else:
            result.failed.append((action, output or f"exit {code}"))
    return result


def apply_all(
    profiles: Sequence[Profile],
    *,
    dry_run: bool = False,
    runner: Optional[Runner] = None,
) -> List[Result]:
    """Apply the declaration to each profile, reading the store once."""
    doc = store.load()
    return [apply_to(p, doc=doc, dry_run=dry_run, runner=runner) for p in profiles]


# --------------------------------------------------------------------------- #
# editing the declaration
# --------------------------------------------------------------------------- #
def declare_marketplace(source: str) -> bool:
    """Add ``source`` to the declaration; ``False`` if it was already there."""
    source = str(source).strip()
    if not source:
        raise PluginError("a marketplace source cannot be empty")
    current = store.shared_marketplaces()
    if any(_normalise(s) == _normalise(source) for s in current):
        return False
    store.set_shared_field("marketplaces", [*current, source])
    return True


def undeclare_marketplace(source: str) -> bool:
    """Drop ``source`` from the declaration; ``False`` if it was not declared."""
    current = store.shared_marketplaces()
    kept = [s for s in current if _normalise(s) != _normalise(str(source))]
    if len(kept) == len(current):
        return False
    store.set_shared_field("marketplaces", kept)
    return True


def declare_plugin(plugin_id: str) -> bool:
    """Add ``plugin_id`` to the declaration; ``False`` if it was already there."""
    plugin_id = str(plugin_id).strip()
    if not plugin_id:
        raise PluginError("a plugin id cannot be empty")
    current = store.shared_plugins()
    if plugin_id in current:
        return False
    store.set_shared_field("plugins", [*current, plugin_id])
    return True


def undeclare_plugin(plugin_id: str) -> bool:
    """Drop ``plugin_id`` from the declaration; ``False`` if it was not declared."""
    current = store.shared_plugins()
    kept = [p for p in current if p != str(plugin_id).strip()]
    if len(kept) == len(current):
        return False
    store.set_shared_field("plugins", kept)
    return True


def marketplace_of(plugin_id: str) -> Optional[str]:
    """The ``@marketplace`` half of a plugin id, if it carries one."""
    parts = str(plugin_id).rsplit("@", 1)
    return parts[1].strip() if len(parts) == 2 and parts[1].strip() else None


def set_shared_setting(key: str, value) -> bool:
    """Declare one ``settings.json`` key; ``False`` if it already said this.

    Re-declaring the same value writes nothing, the way re-declaring a plugin
    or a marketplace does. A no-op that still rewrites the config file is not
    free: the rewrite goes through :func:`atomic.replace`, which is refused
    outright while another process holds the destination open -- an editor
    with the file loaded is enough on Windows -- so a command with nothing to
    do would still fail.
    """
    current = store.shared_settings()
    if str(key) in current and current[str(key)] == value:
        return False
    current[str(key)] = value
    store.set_shared_field("settings", current)
    return True


def unset_shared_setting(key: str) -> bool:
    """Stop managing one settings key; ``False`` if it was not declared.

    The key is left in the profiles that already have it. Undeclaring says "the
    launcher no longer decides this", which is a different act from setting it
    back to whatever each profile had before -- that value is not recorded
    anywhere, so it could not be restored even if this tried.
    """
    current = store.shared_settings()
    if str(key) not in current:
        return False
    current.pop(str(key))
    store.set_shared_field("settings", current)
    return True


def uninstall_from(
    profile: Profile, plugin_id: str, *, runner: Optional[Runner] = None
) -> Tuple[bool, str]:
    """Run ``claude plugin uninstall`` in one profile; returns ``(ok, output)``."""
    run = runner or _run
    if plugin_id not in installed_plugins(profile):
        return True, "not installed"
    argv = [config.claude_bin(), "plugin", "uninstall", plugin_id, "--scope", SCOPE]
    code, output = run(argv, _env_for(profile))
    return code == 0, output or (f"exit {code}" if code else "")
