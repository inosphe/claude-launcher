"""The harnesses a session can run, declared rather than hard-coded.

A *harness* is the CLI agent program a session runs. The set used to be
"``claude``, plus whatever the user happened to write under ``harnesses:``",
which meant a fresh install knew exactly one — and the web UI had to take the
name as free text, with a typo indistinguishable from an unconfigured harness.

The set is now a **document**. The packaged default declares the harnesses
claunch knows about (:data:`DEFAULT_YAML`), and ``~/.claunch.yaml`` may
override or extend it::

    harnesses:
      codex:
        command: codex          # string or argv list
        args: ["--yolo"]        # optional, before the session's own args
        env: {KEY: VALUE}       # optional overrides
        description: "..."      # optional, shown in pickers
      pi: null                  # a tombstone: drop a packaged harness

Overriding is **per harness, not per field** (as with :mod:`mesh_roles`): a
name in the config replaces that harness's whole definition, so a half-merged
declaration — new command, inherited flags — can never happen.

Being *declared* is not the same as being *installed*: ``pi`` ships in the
default set whether or not the machine has it. :meth:`Harness.available`
answers that separately, which is what lets the web UI list a harness it
cannot run yet as a disabled option instead of hiding it (a hidden option
reads as "claunch does not support pi", which is the wrong thing to learn).
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import yaml

from . import config, store

#: The harness spawned through the profile machinery rather than a plain
#: command — its executable comes from ``config.claude_bin()``, not from this
#: document (see :func:`registry`).
CLAUDE_HARNESS = "claude"


class HarnessConfigError(Exception):
    """Raised for an unreadable harness declaration."""


# --------------------------------------------------------------------------- #
# the packaged default
#
# YAML rather than a dict literal so it reads exactly like the block a user
# writes in ~/.claunch.yaml, and so the default is proven by the same parser
# every user declaration goes through.
# --------------------------------------------------------------------------- #
DEFAULT_YAML = """\
version: 1

harnesses:

  claude:
    builtin: true
    description: >-
      Claude Code, run under a claunch profile (isolated config dir, provider
      and token). Needs --profile; the executable is CLAUDE_LAUNCHER_BIN.

  codex:
    command: codex
    description: OpenAI's Codex CLI.

  pi:
    command: pi
    description: The pi CLI agent.
"""


@dataclass(frozen=True)
class Harness:
    """One declared harness: what it runs, and what it is called."""

    name: str
    #: argv prefix. Empty for the builtin claude harness, whose command is
    #: assembled by :mod:`claude_launcher.daemon.harness` from the profile.
    command: List[str] = field(default_factory=list)
    #: Flags inserted before the session's own args.
    args: List[str] = field(default_factory=list)
    #: Environment overrides layered under the session's own ``--env``.
    env: Dict[str, str] = field(default_factory=dict)
    description: str = ""
    builtin: bool = False

    def program(self) -> str:
        """The executable whose presence decides :meth:`available`."""
        if self.builtin:
            return config.claude_bin()
        return self.command[0] if self.command else self.name

    def available(self) -> bool:
        """Whether this machine can actually run it right now.

        ``shutil.which`` resolves against the *daemon's* PATH — the same
        environment a session's child is spawned with — so a false answer here
        is a spawn that would have failed.
        """
        return shutil.which(self.program()) is not None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "command": list(self.command),
            "args": list(self.args),
            "description": self.description,
            "builtin": self.builtin,
            # Resolved per call, never stored: installing pi should not need a
            # config edit, and a PATH change is exactly what this reports.
            "available": self.available(),
            "program": self.program(),
        }


def _as_list(value, what: str) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    raise HarnessConfigError(f"{what} must be a string or a list, got {value!r}")


def _parse_entry(name: str, body) -> Harness:
    if not isinstance(body, dict):
        raise HarnessConfigError(
            f"harness {name!r} must be a mapping, got {body!r}"
        )
    builtin = bool(body.get("builtin")) or name == CLAUDE_HARNESS
    command = _as_list(body.get("command"), f"harness {name!r} command")
    if builtin:
        # claude's executable is CLAUDE_LAUNCHER_BIN, and its argv is built
        # from the profile. Accepting a 'command:' here would be a setting
        # that silently does nothing.
        command = []
    elif not command:
        command = [name]
    env = body.get("env")
    return Harness(
        name=name,
        command=command,
        args=_as_list(body.get("args"), f"harness {name!r} args"),
        env=(
            {str(k): str(v) for k, v in env.items()} if isinstance(env, dict) else {}
        ),
        description=str(body.get("description") or "").strip(),
        builtin=builtin,
    )


def parse(document) -> Dict[str, Optional[dict]]:
    """Validate a harness document (YAML text or a parsed mapping).

    Returns ``name -> body`` with ``None`` kept as the tombstone that deletes
    a packaged harness.
    """
    if isinstance(document, str):
        try:
            doc = yaml.safe_load(document)
        except yaml.YAMLError as exc:
            raise HarnessConfigError(f"not valid YAML: {exc}") from None
    else:
        doc = document
    if doc is None:
        return {}
    if not isinstance(doc, dict):
        raise HarnessConfigError(
            f"harnesses must be a mapping, got {type(doc).__name__}"
        )
    section = doc.get("harnesses", doc)
    if not isinstance(section, dict):
        raise HarnessConfigError("'harnesses' must be a mapping of name -> entry")
    out: Dict[str, Optional[dict]] = {}
    for raw_name, body in section.items():
        name = str(raw_name).strip()
        if not name:
            continue
        if body is None:
            out[name] = None
            continue
        _parse_entry(name, body)  # prove it before it reaches the registry
        out[name] = body
    return out


def registry(doc: Optional[dict] = None) -> Dict[str, Harness]:
    """The harnesses in force: the packaged set with the config's on top.

    Lenient where the packaged document is strict: a hand-edited
    ``harnesses:`` block that no longer parses must not stop every session
    command from running, so a bad entry is skipped and the rest stand.
    """
    merged: Dict[str, Harness] = {}
    for name, body in parse(DEFAULT_YAML).items():
        if body is not None:
            merged[name] = _parse_entry(name, body)
    doc = store.load() if doc is None else doc
    section = doc.get("harnesses")
    if isinstance(section, dict):
        for raw_name, body in section.items():
            name = str(raw_name).strip()
            if not name:
                continue
            if body is None:
                merged.pop(name, None)  # tombstone
                continue
            try:
                merged[name] = _parse_entry(name, body)
            except HarnessConfigError:
                continue
    return merged


def get(name: str, doc: Optional[dict] = None) -> Optional[Harness]:
    return registry(doc).get(str(name or "").strip())


def names(doc: Optional[dict] = None) -> List[str]:
    """Declared harness names, claude first and the rest alphabetical.

    claude leads because it is the default and the only one with the profile
    machinery behind it; the pickers show them in this order.
    """
    reg = registry(doc)
    rest = sorted(n for n in reg if n != CLAUDE_HARNESS)
    return ([CLAUDE_HARNESS] if CLAUDE_HARNESS in reg else []) + rest
