"""The profile sync server: one shared ``~/.claunch.yaml`` document per namespace.

Small on purpose. It stores documents, not profiles: it never interprets what a
profile *means*, so a launcher upgrade that adds config keys needs no server
change. See :mod:`claude_launcher.sync` for the client half.
"""

from .docs import DocStore, RevisionMismatch, SyncServerError
from .users import User, UserStore

__all__ = [
    "DocStore",
    "RevisionMismatch",
    "SyncServerError",
    "User",
    "UserStore",
]
