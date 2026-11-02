"""VM-only mode (vm-setup-8s6, s2-vmprompt): a per-session switch that makes the host
refuse builds, package installs and program runs, with a message that says to use
``vm_exec``. It is a nudge, not a security boundary (see :data:`LIMITS`).

The module is harness-neutral: the rules, the decision, the switch (a file in the
session's directory) and the history of blocks. A harness adapter is thin:

* Claude Code: ``harness.REBRIEF_HOOK_SETTINGS`` carries a ``PreToolUse`` hook for
  ``Bash`` that runs ``claunch vm guard`` only when the switch file exists. The
  command reads the tool call from stdin, calls :func:`decide` and prints a
  ``permissionDecision: deny`` with the reason. Any failure allows the call (fail-open).
* The switch is a file so that it takes effect on the next tool call of a running
  session and works with the daemon down. The daemon writes it (user-only routes);
  the hook only reads it and appends to the history.

A session can be switched only if it was started with the hook: the daemon records
that at spawn (:func:`mark_hooked`). A session without it (started before this
version, a harness with no adapter, or caller-supplied ``--settings``) is refused
with the reason, which the page shows.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import paths

FLAG = "vm-only.json"
MARK = "vm-guard-hook"
HISTORY = "vm-only-blocks.jsonl"
HISTORY_KEEP = 20
HISTORY_TRIM_BYTES = 64 * 1024

LIMITS = ("접두 규칙이므로 `bash -c`·스크립트 파일·변수 치환 등으로 우회할 수 있다. "
          "세션이 VM에서 실행하도록 유도하는 장치이며 보안 경계가 아니다.")

#: Token prefixes that are refused (the approved list). A rule matches a command
#: segment whose leading tokens equal it; ``("make",)`` matches ``make -j8 all``.
BUILD: Tuple[Tuple[str, ...], ...] = (
    ("make",), ("cmake",), ("ninja",), ("xcodebuild",), ("swift", "build"), ("swift", "test"), ("swiftc",),
    ("cargo", "build"), ("cargo", "test"), ("cargo", "run"), ("go", "build"), ("go", "test"), ("go", "run"),
    ("gradle",), ("./gradlew",), ("mvn",), ("npm", "run"), ("npm", "test"), ("pnpm", "build"), ("pnpm", "test"),
    ("yarn", "build"), ("yarn", "test"), ("pytest",), ("python", "-m", "pytest"), ("python3", "-m", "pytest"),
    ("uv", "run", "pytest"), ("tsc",),
)
INSTALL: Tuple[Tuple[str, ...], ...] = (
    ("npm", "install"), ("npm", "i"), ("npm", "ci"), ("pnpm", "install"), ("yarn", "install"), ("yarn", "add"),
    ("pip", "install"), ("pip3", "install"), ("uv", "pip", "install"), ("uv", "sync"), ("uv", "add"),
    ("brew", "install"), ("brew", "upgrade"), ("gem", "install"), ("bundle", "install"), ("pod", "install"),
    ("cargo", "install"), ("go", "install"), ("go", "get"),
)
RUN: Tuple[Tuple[str, ...], ...] = (
    ("open",), ("osascript",), ("node",), ("python",), ("python3",), ("uv", "run"), ("swift", "run"),
    ("xcrun", "simctl"),
)
KINDS = (("build", BUILD), ("install", INSTALL), ("run", RUN))

#: Words that put another command after them.
WRAPPERS = {"sudo", "env", "command", "time", "nohup", "exec", "nice", "caffeinate"}
SHELLS = {"bash", "sh", "zsh", "dash"}
_SPLIT = re.compile(r"\|\||&&|;|\||&|\n")
_SUBST = re.compile(r"\$\(([^()]*)\)|`([^`]*)`")
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_PYTHON = re.compile(r"^python3(\.\d+)?$")
DEPTH = 3


def _tokens(segment: str) -> List[str]:
    try:
        toks = shlex.split(segment, posix=True)
    except ValueError:
        toks = segment.split()
    return toks


def _unwrap(toks: List[str]) -> List[str]:
    """Drop leading assignments and wrapper words (``FOO=1 sudo -E make``)."""
    while toks:
        head = toks[0]
        if _ASSIGN.match(head):
            toks = toks[1:]
        elif head in WRAPPERS:
            toks = toks[1:]
            while toks and (toks[0].startswith("-") or _ASSIGN.match(toks[0])):
                toks = toks[1:]
        else:
            break
    return toks


def _match(toks: List[str]) -> Optional[Tuple[str, str]]:
    if not toks:
        return None
    first = "python3" if _PYTHON.match(toks[0]) else toks[0]
    toks = [first] + toks[1:]
    if toks[0].startswith("./") and toks[0] != "./gradlew":
        return "run", "./*"
    for kind, rules in KINDS:
        for rule in rules:
            if tuple(toks[:len(rule)]) == rule:
                return kind, " ".join(rule)
    return None


def check(command: str, _depth: int = 0) -> Optional[Tuple[str, str, str]]:
    """``(kind, rule, head)`` for the first refused part of ``command``, or None.
    ``head`` is the first words of that part (no arguments beyond three words, so
    nothing that looks like a secret is kept)."""
    if not isinstance(command, str) or not command.strip() or _depth > DEPTH:
        return None
    inner = [a or b for a, b in _SUBST.findall(command)]
    for sub in inner:
        hit = check(sub, _depth + 1)
        if hit:
            return hit
    whole = _tokens(command)                      # `bash -c '...'` anywhere, quotes kept together
    for i, tok in enumerate(whole[:-2]):
        if os.path.basename(tok) in SHELLS and whole[i + 1] == "-c":
            hit = check(whole[i + 2], _depth + 1)
            if hit:
                return hit
    for segment in _SPLIT.split(command):
        toks = _unwrap(_tokens(segment))
        if not toks:
            continue
        found = _match(toks)
        if found:
            return found[0], found[1], " ".join(toks[:3])
    return None


def message(kind: str, rule: str) -> str:
    return (f"VM 전용 모드: 호스트에서 {kind} 명령({rule})을 실행하지 않습니다. 같은 작업을 vm_exec로 VM에서 실행하십시오 "
            "(vm_list로 VM과 공유 폴더를 확인하고, 공유 폴더는 게스트의 로컬 사본에서 빌드합니다. 프롬프트 팩 mcp-vm 참조). "
            "git, claunch, 읽기 명령은 호스트에서 그대로 쓸 수 있습니다.")


# ---- the switch, per session ------------------------------------------------------------- #
def _dir(name: str) -> Path:
    return paths.session_dir(name)


def flag_path(name: str) -> Path:
    return _dir(name) / FLAG


def is_on(name: str) -> bool:
    return flag_path(name).exists()


def hooked(name: str) -> bool:
    """The session's process was started with the guard hook."""
    return (_dir(name) / MARK).exists()


def mark_hooked(name: str, yes: bool) -> None:
    path = _dir(name) / MARK
    if yes:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(int(time.time())), encoding="utf-8")
    else:
        path.unlink(missing_ok=True)


def can_enable(name: str, harness: str) -> Tuple[bool, str]:
    """``(ok, why)``: why the switch cannot be turned on, or ''."""
    if harness != "claude":
        return False, f"{harness or '이'} harness에는 VM 전용 모드 어댑터가 없습니다(현재 Claude Code만 지원)"
    if not hooked(name):
        return False, ("이 세션은 VM 전용 hook 없이 시작되었습니다(이 기능 이전에 시작했거나 --settings를 직접 지정함). "
                       "세션을 다시 시작하면 켤 수 있습니다")
    return True, ""


def set_on(name: str, on: bool) -> None:
    path = flag_path(name)
    if on:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"on": True, "since": time.time()}), encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    else:
        path.unlink(missing_ok=True)


def since(name: str) -> float:
    try:
        return float(json.loads(flag_path(name).read_text(encoding="utf-8")).get("since") or 0)
    except (OSError, ValueError, AttributeError, TypeError):
        return 0.0


# ---- the history of blocks ------------------------------------------------------------------- #
def record_block(name: str, kind: str, rule: str, head: str) -> None:
    path = _dir(name) / HISTORY
    try:
        if path.exists() and path.stat().st_size > HISTORY_TRIM_BYTES:
            lines = path.read_text(encoding="utf-8").splitlines()[-HISTORY_KEEP:]
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": time.time(), "kind": kind, "rule": rule, "head": head[:80]},
                                ensure_ascii=False) + "\n")
    except OSError:
        pass                                       # a missing record never changes the answer


def blocks(name: str) -> List[Dict[str, object]]:
    """The last blocks, newest first."""
    try:
        lines = (_dir(name) / HISTORY).read_text(encoding="utf-8").splitlines()[-HISTORY_KEEP:]
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return list(reversed(out))


def status(name: str, harness: str) -> Dict[str, object]:
    ok, why = can_enable(name, harness)
    on = is_on(name)
    return {"session": name, "on": on, "can_enable": ok or on, "why": "" if (ok or on) else why,
            "since": since(name) if on else 0.0, "blocks": blocks(name)}


# ---- the Claude Code adapter ---------------------------------------------------------------- #
def hook_response(payload: dict, session: str) -> Optional[dict]:
    """What the PreToolUse hook prints for ``payload``, or None to allow. Never raises."""
    try:
        if payload.get("tool_name") != "Bash" or not is_on(session):
            return None
        command = (payload.get("tool_input") or {}).get("command")
        hit = check(command)
        if hit is None:
            return None
        kind, rule, head = hit
        record_block(session, kind, rule, head)
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": message(kind, rule)}}
    except Exception:  # noqa: BLE001 - fail-open
        return None


#: ``command`` of the hook: the Python CLI starts only when the switch file exists.
HOOK_COMMAND = 'if [ -e "$CLAUNCH_SCRATCH/../' + FLAG + '" ]; then exec claunch vm guard; fi'
