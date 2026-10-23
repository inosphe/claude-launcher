"""Provider presets: one YAML file per provider, shipped with the package.

A *preset* is a starting value for the dashboard's provider form
(``#/settings/providers``): the ``providers.<name>`` entry a person would
otherwise type into ``~/.claunch.yaml`` by hand.  Picking a preset fills the
form; nothing is installed until the form is saved, and every field stays
editable afterwards.

The files are ``src/claude_launcher/provider-presets/<name>.yaml`` -- in the
source tree, shipped inside the package, one provider per file::

    # src/claude_launcher/provider-presets/gateway.yaml
    label: Example gateway           # optional, shown in the picker
    note: serves both protocols      # optional, one line under it
    provider:                        # required: the entry itself
      endpoints:
        anthropic: https://gateway.example.com
        openai: https://gateway.example.com/v1
      models: {default: some-model}
      context_window: 200000

Why files and not a block in the config: the config file is one document that
every reader parses whole, so a malformed preset would take the launcher's
configuration down with it. Here a file that does not parse marks only itself
(:attr:`Preset.error`) and the other presets still stand -- the same isolation
goal as the ``workflows/`` per-file layer.

Why the package and not a layer under the launcher home: a preset is not an
install target and not machine state. It is a template for the install form --
picking one and saving writes ``~/.claunch.yaml``, which is the only place a
provider then lives, and where any per-machine adjustment belongs. Shipping
the files with the package means the set travels with the code that reads it
and nothing has to copy them; there is no second layer to drift from the
first, and no writer here that could rewrite the installed tree.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from . import provider_spec

#: Directory inside the package holding one file per preset.
DIRNAME = "provider-presets"

#: The shipped directory: a sibling of this module, so it travels in the wheel
#: (``packages = ["src/claude_launcher"]`` in ``pyproject.toml``).
PACKAGE_DIR = Path(__file__).resolve().parent / DIRNAME

#: A different directory for the same files. Not a user setting -- the shipped
#: presets are the package's own -- but the test suite must not write into the
#: source tree, and an experiment may want its own set.
ENV_DIR = "CLAUDE_LAUNCHER_PRESET_DIR"

#: A preset file name doubles as the suggested provider name, so it is held to
#: the same shape a ``providers:`` key should have.
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

#: Keys a preset file may carry besides the entry itself.
META_FIELDS = ("label", "note", "preset_name")


class PresetError(Exception):
    """A preset name or file that cannot be used."""


@dataclass(frozen=True)
class Preset:
    """One provider preset, as read from its own file."""

    name: str
    path: Path
    entry: Dict[str, object] = field(default_factory=dict)
    label: str = ""
    note: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "label": self.label or self.name,
            "note": self.note,
            "path": str(self.path),
            "entry": dict(self.entry),
            "error": self.error,
        }


def directory() -> Path:
    """The preset directory (it need not exist; a missing one means no presets).

    :data:`PACKAGE_DIR` unless :data:`ENV_DIR` names another one.
    """
    override = os.environ.get(ENV_DIR, "").strip()
    return Path(override) if override else PACKAGE_DIR


def path_for(name: str, *, base: Optional[Path] = None) -> Path:
    """The file backing preset ``name``; the name is validated, never joined raw."""
    text = str(name or "").strip()
    if not NAME_RE.fullmatch(text):
        raise PresetError(
            f"invalid preset name {name!r}: use letters, digits, dot, dash or "
            "underscore, starting with a letter or digit"
        )
    return (base or directory()) / f"{text}.yaml"


def _entry_of(document: dict) -> Dict[str, object]:
    """The provider entry inside a preset document.

    A file may wrap it under ``provider:`` (the shape this module documents) or
    be the entry itself -- a person who copies a block straight out of
    ``~/.claunch.yaml`` writes the second one, and refusing it would make the
    common case the broken one.
    """
    for key in ("provider", "providers"):
        inner = document.get(key)
        if isinstance(inner, dict):
            # `providers: {name: {...}}` is a whole config section pasted in;
            # take the single entry it holds.
            if key == "providers" and len(inner) == 1:
                only = next(iter(inner.values()))
                if isinstance(only, dict):
                    return dict(only)
            if key == "provider":
                return dict(inner)
    return dict(document)


def read(name: str, path: Path) -> Preset:
    """Read one preset file; a broken file becomes a :class:`Preset` with ``error``."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return Preset(name=name, path=path, error=f"cannot read {path}: {exc}")
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return Preset(name=name, path=path, error=f"{path.name}: {exc}")
    if document is None:
        return Preset(name=name, path=path, error=f"{path.name} is empty")
    if not isinstance(document, dict):
        return Preset(name=name, path=path, error=f"{path.name} must be a mapping")
    label = str(document.get("label") or "").strip()
    note = str(document.get("note") or "").strip()
    entry = _entry_of(document)
    for key in META_FIELDS:
        entry.pop(key, None)
    if not entry:
        return Preset(
            name=name, path=path, label=label, note=note,
            error=f"{path.name} holds no provider entry",
        )
    unknown = provider_spec.unknown_fields(entry)
    if unknown:
        return Preset(
            name=name, path=path, label=label, note=note, entry=entry,
            error=(
                f"{path.name}: unknown field(s) {', '.join(unknown)}"
            ),
        )
    try:
        provider_spec.from_entry(entry, f"preset {name}")
    except provider_spec.SpecError as exc:
        return Preset(
            name=name, path=path, label=label, note=note, entry=entry, error=str(exc)
        )
    return Preset(name=name, path=path, entry=entry, label=label, note=note)


def load_all(base: Optional[Path] = None) -> List[Preset]:
    """Every preset in the directory, sorted by name.

    One unreadable or malformed file never hides the others: each is read on
    its own and reports its own ``error``.
    """
    root = base or directory()
    if not root.is_dir():
        return []
    out: List[Preset] = []
    for path in sorted(root.glob("*.yaml")):
        name = path.stem
        if not NAME_RE.fullmatch(name):
            out.append(
                Preset(
                    name=name, path=path,
                    error=(
                        f"{path.name}: the file name is not a provider name "
                        "(letters, digits, dot, dash or underscore)"
                    ),
                )
            )
            continue
        out.append(read(name, path))
    return out


# A writer for preset files lived here and is gone with the directory move: it
# could only ever target the installed package, which is the wrong thing for a
# running launcher to rewrite. A preset is added by committing a file.
