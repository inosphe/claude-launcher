"""The harnesses a session can run, declared rather than hard-coded.

A *harness* is the CLI agent program a session runs. The set used to be
"``claude``, plus whatever the user happened to write under ``harnesses:``",
which meant a fresh install knew exactly one. Profiles now name a declared
harness once; session creation only displays that resolved value read-only.

The set is now a **document**. The packaged default declares the harnesses
claunch knows about (:data:`DEFAULT_YAML`), and ``~/.claunch.yaml`` may
override or extend it::

    harnesses:
      codex:
        command: codex          # string or argv list
        args: ["--yolo"]        # optional, before the session's own args
        restore_args: [resume, --last]  # optional, only on relaunch
        env: {KEY: VALUE}       # optional overrides
        models: [small, large]  # optional session-start model choices
        home_env: CODEX_HOME     # optional isolated per-profile home
        auth: oauth              # claude, oauth, api-key, or none
        clear_env: [OPENAI_API_KEY]  # forbidden ambient credentials
        login_args: [login]      # optional interactive login argv
        description: "..."      # optional, shown in status surfaces
      pi:
        command: pi
        auth: api-key
        token_env: ANTHROPIC_API_KEY
        provider_adapter: pi     # claunch provider -> Pi model projection
      agent: null                # a tombstone: drop a packaged harness

Overriding is **per harness, not per field** (as with :mod:`mesh_roles`): a
name in the config replaces that harness's whole definition, so a half-merged
declaration — new command, inherited flags — can never happen.

Being *declared* is not the same as being *installed*: ``pi`` ships in the
default set whether or not the machine has it. :meth:`Harness.available`
answers that separately, so a profile selecting a missing executable can be
shown as unavailable without reopening harness selection at session creation.
"""

from __future__ import annotations

import re
import shutil
import sys
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from . import config, store

#: The harness spawned through the profile machinery rather than a plain
#: command — its executable comes from ``config.claude_bin()``, not from this
#: document (see :func:`registry`).
CLAUDE_HARNESS = "claude"


class HarnessConfigError(Exception):
    """Raised for an unreadable harness declaration."""


@dataclass(frozen=True)
class BtwCapability:
    """A harness-native ephemeral side-conversation command.

    claunch passes this command through its terminal transport. The harness
    owns the side conversation and its presentation; this declaration gives
    launch surfaces the supported command and its observable constraints.
    """

    command: str
    aliases: List[str] = field(default_factory=list)
    minimum_version: str = ""
    requires_started_conversation: bool = False
    available_while_busy: bool = False
    context: str = ""
    history: str = ""
    tool_access: str = ""
    response_mode: str = ""

    def to_dict(self) -> dict:
        return {
            "command": self.command,
            "aliases": list(self.aliases),
            "minimum_version": self.minimum_version,
            "requires_started_conversation": self.requires_started_conversation,
            "available_while_busy": self.available_while_busy,
            "context": self.context,
            "history": self.history,
            "tool_access": self.tool_access,
            "response_mode": self.response_mode,
        }


# The source of the packaged harness/auth contract is a real package resource,
# so changing or adding a harness does not require editing runner logic.
DEFAULT_RESOURCE = "harnesses.yaml"
DEFAULT_YAML = (
    resources.files(__package__)
    .joinpath(DEFAULT_RESOURCE)
    .read_text(encoding="utf-8")
)


@dataclass(frozen=True)
class Harness:
    """One declared harness: what it runs, and what it is called."""

    name: str
    #: argv prefix. Empty for the builtin claude harness, whose command is
    #: assembled by :mod:`claude_launcher.daemon.harness` from the profile.
    command: List[str] = field(default_factory=list)
    #: Flags inserted before the session's own args.
    args: List[str] = field(default_factory=list)
    #: Closed model choices offered by session-start forms.  The selected
    #: value is handed to the harness as ``--model=<value>``; profile/provider
    #: env remains responsible for resolving aliases to backend model ids.
    models: List[str] = field(default_factory=list)
    #: Closed reasoning-effort choices exposed by session creation forms.
    efforts: List[str] = field(default_factory=list)
    #: Builtin tools claunch itself adds to this harness (today: Pi's
    #: ``full_read``). Session forms offer them as switches; the default per
    #: profile is ``harness_options.<harness>.tools``.
    tools: List[str] = field(default_factory=list)
    #: Optional templates for native model/effort selection. ``{model}`` and
    #: ``{effort}`` are replaced at launch, keeping harness syntax declarative.
    model_args: List[str] = field(default_factory=list)
    effort_args: List[str] = field(default_factory=list)
    #: Harness-native ``/btw`` capability and its interaction limits. None
    #: means claunch has no declaration for a side-conversation command.
    btw: Optional[BtwCapability] = None
    #: Arguments appended only when claunch restores an existing session.
    restore_args: List[str] = field(default_factory=list)
    #: Environment overrides layered under the session's own ``--env``.
    env: Dict[str, str] = field(default_factory=dict)
    description: str = ""
    builtin: bool = False
    #: Environment variable that points this harness at a profile-specific
    #: home/config directory. Exactly which files follow it is a contract of
    #: the external harness. The builtin Claude harness is kept at the profile
    #: root for backwards compatibility.
    home_env: str = ""
    #: ``claude`` uses the launcher's provider/token machinery, ``oauth``
    #: keeps credentials in the harness-owned home, and ``api-key`` receives
    #: the profile's one launcher-managed token through this declaration.
    auth: str = "none"
    #: Destination for the profile's single ``claunch set-token`` value. This
    #: belongs to the harness declaration, not to the profile/YAML env.
    token_env: str = ""
    #: Optional adapter that projects a selected claunch API provider into the
    #: harness's native provider/model mechanism. ``pi`` loads the packaged Pi
    #: extension and supplies its model selection on every launch.
    provider_adapter: str = ""
    #: Variables always removed before launching this harness (principally
    #: ambient API keys that would bypass an OAuth login).
    clear_env: List[str] = field(default_factory=list)
    #: Variables forced to the empty string on every launch. Claude uses this
    #: to suppress its competing X-Api-Key route even when profile/YAML or the
    #: parent shell happened to define it.
    empty_env: List[str] = field(default_factory=list)
    login_args: List[str] = field(default_factory=list)
    #: Non-interactive argv placed before the health-check prompt. Empty
    #: means this custom harness cannot be checked safely by ``validate``.
    heartbeat_args: List[str] = field(default_factory=list)
    #: Harness-native argv for the two runtime choices exposed by the
    #: launchers.  Keeping these on the declaration prevents UI/manager code
    #: from growing another harness-name switch every time one is added.
    skip_permissions_args: List[str] = field(default_factory=list)
    full_access_args: List[str] = field(default_factory=list)
    #: The explicit opposite of ``full_access_args``.  Forms emit one group
    #: or the other so an unticked choice is not lost as "no argv".
    full_access_off_args: List[str] = field(default_factory=list)
    #: Packaged defaults superseded whenever either managed permission or
    #: sandbox group is present in the session's own args.
    mode_conflict_args: List[str] = field(default_factory=list)
    usage: str = ""
    #: One-time opening message: positional prompt (``argv``) or terminal
    #: delivery after launch (``pty``).
    opening_transport: str = "pty"
    #: Readiness evidence required before terminal delivery.
    input_readiness: str = "immediate"
    #: How Enter is paced after a bracketed paste.
    submit_strategy: str = "fixed"
    paste_enter_delay: Optional[float] = None

    @property
    def borrow_mode(self) -> str:
        """How this harness can consume another base profile's credential."""
        if self.builtin and self.auth == "claude":
            return "provider-token"
        if self.auth == "api-key" and self.token_env:
            return "token"
        return "none"

    @property
    def borrowable(self) -> bool:
        return self.borrow_mode != "none"

    def program(self) -> str:
        """The executable whose presence decides :meth:`available`."""
        if self.builtin:
            return config.claude_bin()
        return self.command[0] if self.command else self.name

    def launch_command(self) -> List[str]:
        """Runnable argv prefix, including Windows ``.CMD`` resolution.

        npm-installed agents commonly expose ``codex.cmd``/``kimi.cmd`` on
        Windows. ``shutil.which`` understands PATHEXT while CreateProcess does
        not resolve a bare extensionless argv element reliably. The Codex npm
        shim needs one additional step: ``cmd.exe`` truncates a quoted argument
        at its first newline, so invoke the shim's Node entry point directly.
        This preserves a multi-line opening briefing as one argv element.
        """
        if self.builtin:
            return []
        first = shutil.which(self.program()) or self.program()
        prefix = self._windows_codex_npm_command(first) or [first]
        return [*prefix, *self.command[1:]]

    def _windows_codex_npm_command(self, first: str) -> Optional[List[str]]:
        """Bypass Codex's npm ``.CMD`` shim when its package is available.

        A Windows batch file receives the opening through ``%*``. Embedded
        CR/LF characters end that command even while the argument is quoted;
        the Node and native Codex children consequently receive only the
        delivery-stamp line. npm's generated shim delegates to the package's
        ``bin/codex.js`` file, so calling that same entry point through Node
        keeps the complete argument and retains the package's normal launcher.

        Custom Codex commands and non-npm installations keep their declared
        executable. The candidate paths below are the two branches in npm's
        generated Windows shim: a colocated ``node.exe`` or Node from PATH.
        """
        if (
            sys.platform != "win32"
            or self.name != "codex"
            or Path(first).suffix.lower() != ".cmd"
        ):
            return None
        shim = Path(first)
        entry = (
            shim.parent
            / "node_modules"
            / "@openai"
            / "codex"
            / "bin"
            / "codex.js"
        )
        if not entry.is_file():
            return None
        local_node = shim.with_name("node.exe")
        node = str(local_node) if local_node.is_file() else shutil.which("node")
        if not node:
            return None
        return [node, str(entry)]

    def available(self) -> bool:
        """Whether this machine can actually run it right now.

        ``shutil.which`` resolves against the *daemon's* PATH — the same
        environment a session's child is spawned with — so a false answer here
        is a spawn that would have failed.
        """
        return shutil.which(self.program()) is not None

    def profile_home(self, config_dir: Path) -> Path:
        """Directory this harness owns inside a claunch profile.

        Claude profiles predate the harness model and already store their
        settings directly in ``config_dir``. Moving them would log every
        existing profile out, so only non-builtin harnesses get a child.
        """
        return config_dir if self.builtin else config_dir / self.name

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "command": list(self.command),
            "args": list(self.args),
            "models": list(self.models),
            "efforts": list(self.efforts),
            "tools": list(self.tools),
            "model_args": list(self.model_args),
            "effort_args": list(self.effort_args),
            "btw": self.btw.to_dict() if self.btw else None,
            "restore_args": list(self.restore_args),
            "description": self.description,
            "builtin": self.builtin,
            "home_env": self.home_env,
            "auth": self.auth,
            "token_env": self.token_env,
            "provider_adapter": self.provider_adapter,
            "clear_env": list(self.clear_env),
            "empty_env": list(self.empty_env),
            "login_args": list(self.login_args),
            "heartbeat_args": list(self.heartbeat_args),
            "skip_permissions_args": list(self.skip_permissions_args),
            "full_access_args": list(self.full_access_args),
            "full_access_off_args": list(self.full_access_off_args),
            "mode_conflict_args": list(self.mode_conflict_args),
            "usage": self.usage,
            "opening_transport": self.opening_transport,
            "input_readiness": self.input_readiness,
            "submit_strategy": self.submit_strategy,
            "paste_enter_delay": self.paste_enter_delay,
            "borrowable": self.borrowable,
            "borrow_mode": self.borrow_mode,
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


_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_HARNESS_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_SLASH_COMMAND_RE = re.compile(r"^/[a-z][a-z0-9-]*$")
_VERSION_RE = re.compile(r"^\d+(?:\.\d+){1,2}(?:[-+][A-Za-z0-9.-]+)?$")


def _env_names(value, what: str) -> List[str]:
    names = _as_list(value, what)
    for name in names:
        if not _ENV_NAME_RE.fullmatch(name):
            raise HarnessConfigError(f"{what} contains invalid env name {name!r}")
    return names


def _bool(value, what: str) -> bool:
    if isinstance(value, bool):
        return value
    raise HarnessConfigError(f"{what} must be true or false, got {value!r}")


def _btw_capability(name: str, value) -> Optional[BtwCapability]:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise HarnessConfigError(
            f"harness {name!r} btw must be a mapping, got {value!r}"
        )
    command = value.get("command")
    if not isinstance(command, str) or not _SLASH_COMMAND_RE.fullmatch(command):
        raise HarnessConfigError(
            f"harness {name!r} btw command must be a slash command, got "
            f"{command!r}"
        )
    raw_aliases = value.get("aliases", [])
    if not isinstance(raw_aliases, list) or not all(
        isinstance(alias, str) and _SLASH_COMMAND_RE.fullmatch(alias)
        for alias in raw_aliases
    ):
        raise HarnessConfigError(
            f"harness {name!r} btw aliases must be a list of slash commands"
        )
    aliases = list(raw_aliases)
    if command in aliases or len(set(aliases)) != len(aliases):
        raise HarnessConfigError(
            f"harness {name!r} btw aliases must be unique and exclude "
            "the primary command"
        )
    minimum_version = value.get("minimum_version")
    if (
        not isinstance(minimum_version, str)
        or not _VERSION_RE.fullmatch(minimum_version)
    ):
        raise HarnessConfigError(
            f"harness {name!r} btw minimum_version must be a version string, "
            f"got {minimum_version!r}"
        )
    enums = {
        "context": {"current-conversation", "reference-parent"},
        "history": {"ephemeral"},
        "tool_access": {"none", "restricted"},
        "response_mode": {"single-response", "conversation"},
    }
    parsed = {}
    for field_name, allowed in enums.items():
        field_value = value.get(field_name)
        if field_value not in allowed:
            expected = ", ".join(sorted(allowed))
            raise HarnessConfigError(
                f"harness {name!r} btw {field_name} must be one of "
                f"{expected}, got {field_value!r}"
            )
        parsed[field_name] = field_value
    return BtwCapability(
        command=command,
        aliases=aliases,
        minimum_version=minimum_version,
        requires_started_conversation=_bool(
            value.get("requires_started_conversation"),
            f"harness {name!r} btw requires_started_conversation",
        ),
        available_while_busy=_bool(
            value.get("available_while_busy"),
            f"harness {name!r} btw available_while_busy",
        ),
        **parsed,
    )


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
    auth = str(body.get("auth") or ("claude" if builtin else "none")).strip()
    if auth not in {"claude", "oauth", "api-key", "none"}:
        raise HarnessConfigError(
            f"harness {name!r} auth must be claude, oauth, api-key or none"
        )
    # ``api_key_*`` was briefly exposed before the credential model was
    # collapsed back to one profile token. Accept it as an input-only alias so
    # an existing local harness declaration keeps launching after upgrade.
    token_env = str(body.get("token_env") or body.get("api_key_env") or "").strip()
    if token_env and not _ENV_NAME_RE.fullmatch(token_env):
        raise HarnessConfigError(
            f"harness {name!r} token_env is not a valid env name: "
            f"{token_env!r}"
        )
    if auth == "api-key" and not token_env:
        raise HarnessConfigError(
            f"harness {name!r} uses api-key auth but has no token_env"
        )
    if auth == "oauth" and token_env:
        raise HarnessConfigError(
            f"harness {name!r} uses oauth auth and cannot declare token_env"
        )
    provider_adapter = str(body.get("provider_adapter") or "").strip()
    if provider_adapter not in {"", "pi"}:
        raise HarnessConfigError(
            f"harness {name!r} provider_adapter must be pi, got "
            f"{provider_adapter!r}"
        )
    if provider_adapter == "pi" and auth != "api-key":
        raise HarnessConfigError(
            f"harness {name!r} provider_adapter pi requires api-key auth"
        )
    strategies = {
        "opening_transport": (
            str(body.get("opening_transport") or "pty").strip(),
            {"argv", "pty"},
        ),
        "input_readiness": (
            str(body.get("input_readiness") or "immediate").strip(),
            {"immediate", "bracketed-paste"},
        ),
        "submit_strategy": (
            str(body.get("submit_strategy") or "fixed").strip(),
            {"fixed", "screen"},
        ),
    }
    for field_name, (value, allowed) in strategies.items():
        if value not in allowed:
            expected = ", ".join(sorted(allowed))
            raise HarnessConfigError(
                f"harness {name!r} {field_name} must be one of {expected}, "
                f"got {value!r}"
            )
    raw_delay = body.get("paste_enter_delay")
    paste_enter_delay = None
    if raw_delay is not None:
        try:
            paste_enter_delay = float(raw_delay)
        except (TypeError, ValueError):
            raise HarnessConfigError(
                f"harness {name!r} paste_enter_delay must be a number"
            ) from None
        if paste_enter_delay < 0:
            raise HarnessConfigError(
                f"harness {name!r} paste_enter_delay must be non-negative"
            )
    return Harness(
        name=name,
        command=command,
        args=_as_list(body.get("args"), f"harness {name!r} args"),
        models=_as_list(body.get("models"), f"harness {name!r} models"),
        efforts=_as_list(body.get("efforts"), f"harness {name!r} efforts"),
        tools=_as_list(body.get("tools"), f"harness {name!r} tools"),
        model_args=_as_list(body.get("model_args"), f"harness {name!r} model_args"),
        effort_args=_as_list(body.get("effort_args"), f"harness {name!r} effort_args"),
        btw=_btw_capability(name, body.get("btw")),
        restore_args=_as_list(
            body.get("restore_args"), f"harness {name!r} restore_args"
        ),
        env=(
            {str(k): str(v) for k, v in env.items()} if isinstance(env, dict) else {}
        ),
        description=str(body.get("description") or "").strip(),
        builtin=builtin,
        home_env=str(body.get("home_env") or "").strip(),
        auth=auth,
        token_env=token_env,
        provider_adapter=provider_adapter,
        clear_env=_env_names(body.get("clear_env"), f"harness {name!r} clear_env"),
        empty_env=_env_names(
            body.get(
                "empty_env",
                body.get("token_clear_env", body.get("api_key_clear_env")),
            ),
            f"harness {name!r} empty_env",
        ),
        login_args=_as_list(body.get("login_args"), f"harness {name!r} login_args"),
        heartbeat_args=_as_list(
            body.get("heartbeat_args"), f"harness {name!r} heartbeat_args"
        ),
        skip_permissions_args=_as_list(
            body.get("skip_permissions_args"),
            f"harness {name!r} skip_permissions_args",
        ),
        full_access_args=_as_list(
            body.get("full_access_args"), f"harness {name!r} full_access_args",
        ),
        full_access_off_args=_as_list(
            body.get("full_access_off_args"),
            f"harness {name!r} full_access_off_args",
        ),
        mode_conflict_args=_as_list(
            body.get("mode_conflict_args"),
            f"harness {name!r} mode_conflict_args",
        ),
        usage=str(body.get("usage") or "").strip(),
        opening_transport=strategies["opening_transport"][0],
        input_readiness=strategies["input_readiness"][0],
        submit_strategy=strategies["submit_strategy"][0],
        paste_enter_delay=paste_enter_delay,
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
        if not _HARNESS_NAME_RE.fullmatch(name):
            raise HarnessConfigError(
                f"invalid harness name {name!r}: use letters, digits, '.', '_' or '-'"
            )
        if body is None:
            out[name] = None
            continue
        _parse_entry(name, body)  # prove it before it reaches the registry
        out[name] = body
    return out


#: The packaged half of :func:`registry`, parsed once. Its input is a string
#: constant baked into the package, so there is nothing to invalidate -- and
#: it was being re-parsed on every ``registry()`` call, which is every
#: ``get()``, which the profile listing does once per profile in its loop.
#: :class:`Harness` is frozen, so the entries are shareable; the caller gets a
#: fresh dict to lay the config's own harnesses over.
_BUILTIN: Optional[Dict[str, Harness]] = None


def _builtin() -> Dict[str, Harness]:
    global _BUILTIN
    if _BUILTIN is None:
        _BUILTIN = {
            name: _parse_entry(name, body)
            for name, body in parse(DEFAULT_YAML).items()
            if body is not None
        }
    return _BUILTIN


def registry(doc: Optional[dict] = None) -> Dict[str, Harness]:
    """The harnesses in force: the packaged set with the config's on top.

    Lenient where the packaged document is strict: a hand-edited
    ``harnesses:`` block that no longer parses must not stop every session
    command from running, so a bad entry is skipped and the rest stand.
    """
    merged: Dict[str, Harness] = dict(_builtin())
    doc = store.load() if doc is None else doc
    section = doc.get("harnesses")
    if isinstance(section, dict):
        for raw_name, body in section.items():
            name = str(raw_name).strip()
            if not name:
                continue
            if not _HARNESS_NAME_RE.fullmatch(name):
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
