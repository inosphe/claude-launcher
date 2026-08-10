"""Sync server entrypoint: ``python -m claude_launcher.syncserver``.

Equivalent to ``claunch sync-server serve``; kept as a module entrypoint so the
server can run without the rest of the CLI (in a container, under systemd, or
behind a TLS-terminating reverse proxy).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from .. import config
from .api import serve
from .docs import DocStore
from .users import UserStore


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="claunch-sync-server",
        description="Serve shared claunch profile documents over HTTP.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8378, help="bind port (default: 8378)")
    parser.add_argument(
        "--data-dir",
        metavar="DIR",
        help="documents + user list (default: <launcher home>/sync-server, "
        "or CLAUNCH_SYNC_SERVER_DIR)",
    )
    args = parser.parse_args(argv)
    root = Path(args.data_dir).expanduser() if args.data_dir else config.sync_server_dir()
    try:
        return asyncio.run(serve(DocStore(root), UserStore(root), args.host, args.port))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
