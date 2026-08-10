"""Sync server accounts: who may read and write which namespaces.

Tokens are stored as SHA-256 hashes, never in the clear — the plaintext is
returned once, when the account is created or its token rotated, and the server
itself cannot recover it afterwards. A stolen ``users.yaml`` therefore does not
hand over anyone's profile document.

An account owns a list of namespaces; the single entry ``"*"`` grants access to
all of them (useful for an admin or a personal one-account server).
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from .docs import SyncServerError, validate_namespace

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

#: Grants every namespace.
WILDCARD = "*"

VERSION = 1


@dataclass(frozen=True)
class User:
    """One account: a name, a hashed token and the namespaces it may touch."""

    name: str
    token_sha256: str
    namespaces: List[str] = field(default_factory=list)

    def may_access(self, namespace: str) -> bool:
        return WILDCARD in self.namespaces or namespace in self.namespaces


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _validate_name(name: str) -> str:
    name = (name or "").strip()
    if not _NAME_RE.match(name):
        raise SyncServerError(
            f"invalid user name {name!r}: use letters, digits, '.', '_' or '-'"
        )
    return name


class UserStore:
    """The account list at ``<root>/users.yaml``."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.path = self.root / "users.yaml"

    # ------------------------------------------------------------------ #
    # persistence
    # ------------------------------------------------------------------ #
    def _load(self) -> Dict[str, User]:
        if not self.path.is_file():
            return {}
        try:
            data = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise SyncServerError(f"cannot read {self.path}: {exc}") from None
        if not isinstance(data, dict):
            raise SyncServerError(f"{self.path} must be a mapping at the top level")
        raw = data.get("users")
        if not isinstance(raw, dict):
            return {}
        out: Dict[str, User] = {}
        for name, spec in raw.items():
            if not isinstance(spec, dict):
                continue
            namespaces = spec.get("namespaces")
            out[str(name)] = User(
                name=str(name),
                token_sha256=str(spec.get("token_sha256") or ""),
                namespaces=[str(n) for n in namespaces]
                if isinstance(namespaces, list)
                else [str(name)],
            )
        return out

    def _save(self, users: Dict[str, User]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": VERSION,
            "users": {
                u.name: {"token_sha256": u.token_sha256, "namespaces": list(u.namespaces)}
                for u in users.values()
            },
        }
        text = yaml.safe_dump(
            payload, sort_keys=True, allow_unicode=True, default_flow_style=False
        )
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        _restrict(tmp)
        os.replace(tmp, self.path)

    # ------------------------------------------------------------------ #
    # queries
    # ------------------------------------------------------------------ #
    def list(self) -> List[User]:
        return sorted(self._load().values(), key=lambda u: u.name)

    def get(self, name: str) -> Optional[User]:
        return self._load().get(name)

    def authenticate(self, token: str) -> Optional[User]:
        """The account a bearer token belongs to, or ``None``.

        Every account is checked with a constant-time comparison and the loop is
        not short-circuited, so a wrong token takes the same work regardless of
        how close it was.
        """
        if not token:
            return None
        digest = hash_token(token)
        found: Optional[User] = None
        for user in self._load().values():
            if secrets.compare_digest(user.token_sha256, digest):
                found = user
        return found

    # ------------------------------------------------------------------ #
    # mutation
    # ------------------------------------------------------------------ #
    def add(self, name: str, namespaces: Optional[List[str]] = None) -> str:
        """Create an account and return its token — the only time it is visible."""
        name = _validate_name(name)
        users = self._load()
        if name in users:
            raise SyncServerError(f"user {name!r} already exists")
        token = secrets.token_urlsafe(32)
        users[name] = User(
            name=name,
            token_sha256=hash_token(token),
            namespaces=_clean_namespaces(namespaces, default=name),
        )
        self._save(users)
        return token

    def rotate(self, name: str) -> str:
        """Issue a new token for an existing account, invalidating the old one."""
        users = self._load()
        user = users.get(name)
        if user is None:
            raise SyncServerError(f"no such user {name!r}")
        token = secrets.token_urlsafe(32)
        users[name] = User(
            name=user.name, token_sha256=hash_token(token), namespaces=user.namespaces
        )
        self._save(users)
        return token

    def set_namespaces(self, name: str, namespaces: List[str]) -> User:
        users = self._load()
        user = users.get(name)
        if user is None:
            raise SyncServerError(f"no such user {name!r}")
        updated = User(
            name=user.name,
            token_sha256=user.token_sha256,
            namespaces=_clean_namespaces(namespaces, default=name),
        )
        users[name] = updated
        self._save(users)
        return updated

    def remove(self, name: str) -> None:
        users = self._load()
        if name not in users:
            raise SyncServerError(f"no such user {name!r}")
        del users[name]
        self._save(users)


def _clean_namespaces(namespaces: Optional[List[str]], *, default: str) -> List[str]:
    if not namespaces:
        return [validate_namespace(default)]
    out: List[str] = []
    for raw in namespaces:
        name = str(raw).strip()
        if name == WILDCARD:
            return [WILDCARD]
        name = validate_namespace(name)
        if name not in out:
            out.append(name)
    return out


def _restrict(path: Path) -> None:
    """Best-effort 0600 on the token file (a no-op where chmod is meaningless)."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
