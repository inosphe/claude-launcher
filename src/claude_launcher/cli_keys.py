"""``claunch keys ...`` -- several tokens per profile, and how one is picked.

``set-token`` still sets the profile's effective token; this family manages
the pool beside it (:mod:`claude_launcher.profile_keys`). Every launch of a
pooled profile writes the selected key into ``.launcher-token``, which is why
nothing else in the launcher had to learn about pools.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import credentials, profile_keys, stdio
from . import profile as profile_mod


def _cmd_list(args: argparse.Namespace) -> int:
    p = profile_mod.require(args.name)
    try:
        data = profile_keys.summary(p)
    except profile_keys.KeysError as exc:
        raise SystemExit(f"claunch: {exc}")
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0
    if not data["pooled"]:
        token = credentials.stored_token(p)
        if token:
            print(
                f"profile {p.name!r} holds one token ({profile_keys.mask(token)}); "
                f"add more with 'claunch keys add {p.name}'"
            )
        else:
            print(f"profile {p.name!r} holds no token")
        return 0
    nxt = profile_keys.peek(p)
    head = f"profile {p.name!r}: {len(data['keys'])} keys, strategy {data['strategy']}"
    if nxt is not None:
        head += f", next {nxt.key.id}" + (
            f" (fell back from {nxt.fallback})" if nxt.fallback else ""
        )
    print(head)
    for key in data["keys"]:
        mark = "*" if key["current"] else " "
        used = (
            f"{key['use_count']} uses, last {key['last_used_at']}"
            if key["use_count"]
            else "never used"
        )
        label = f" {key['label']}" if key["label"] else ""
        print(f" {mark} {key['id']:<10}{label:<12}{key['masked']:<16} {used}")
    return 0


def _cmd_add(args: argparse.Namespace) -> int:
    p = profile_mod.require(args.name)
    token = args.token or stdio.read_stdin_line()
    try:
        key = profile_keys.add(p, token, label=args.label or "")
    except profile_keys.KeysError as exc:
        raise SystemExit(f"claunch: {exc}")
    print(f"added key {key.id} to profile {p.name!r} ({key.masked()}); it is now the effective token")
    return 0


def _cmd_remove(args: argparse.Namespace) -> int:
    p = profile_mod.require(args.name)
    try:
        key = profile_keys.remove(p, args.ident)
        remaining = profile_keys.load(p)
    except profile_keys.KeysError as exc:
        raise SystemExit(f"claunch: {exc}")
    print(f"removed key {key.id} ({key.masked()}) from profile {p.name!r}")
    if remaining is None:
        print(
            "that was the last key; the profile now inherits a parent token "
            "or has none"
        )
    return 0


def _cmd_strategy(args: argparse.Namespace) -> int:
    p = profile_mod.require(args.name)
    if not args.strategy:
        data = profile_keys.summary(p)
        if not data["pooled"]:
            print(f"profile {p.name!r} has no key pool; strategy applies once it holds keys")
        else:
            print(data["strategy"])
        return 0
    try:
        pool = profile_keys.set_strategy(p, args.strategy)
    except profile_keys.KeysError as exc:
        raise SystemExit(f"claunch: {exc}")
    print(f"profile {p.name!r}: strategy {pool.strategy} ({len(pool.keys)} keys)")
    return 0


def _cmd_select(args: argparse.Namespace) -> int:
    p = profile_mod.require(args.name)
    if args.apply:
        selection = profile_keys.select_for_launch(p)
    else:
        selection = profile_keys.peek(p)
    if selection is None:
        print(f"profile {p.name!r} has no key pool; its single token is used as-is")
        return 0
    source = (
        f"profile {selection.owner.name!r}"
        if selection.owner.name != p.name
        else "this profile"
    )
    print(
        f"{selection.key.id} ({selection.key.masked()}) by {selection.strategy} "
        f"from {source}"
    )
    if selection.fallback:
        print(f"note: fell back to rotation — {selection.fallback}", file=sys.stderr)
    if args.apply:
        print("applied: .launcher-token now holds this key")
    return 0


def register(sub) -> None:
    p = sub.add_parser(
        "keys",
        help="register several tokens on a profile and choose one per launch",
        description=(
            "A profile may hold more than one launcher token. The chosen key "
            "is written to .launcher-token before each launch, so every "
            "reader (get-token, inheritance, borrow checks) keeps seeing one "
            "effective token. Strategies: rotation (round-robin in file "
            "order), usage-min / usage-max (compare the keys' own usage where "
            "the token is the account credential), random."
        ),
    )
    keys_sub = p.add_subparsers(dest="keys_command", metavar="COMMAND")

    p_list = keys_sub.add_parser("list", help="show the profile's keys and strategy")
    p_list.add_argument("name")
    p_list.add_argument("--json", action="store_true", help="machine-readable output")
    p_list.set_defaults(func=_cmd_list)

    p_add = keys_sub.add_parser("add", help="append a key and make it effective")
    p_add.add_argument("name")
    p_add.add_argument("token", nargs="?", help="token value; read from stdin if omitted")
    p_add.add_argument("--label", default="", help="a name for this key (e.g. 'home')")
    p_add.set_defaults(func=_cmd_add)

    p_rm = keys_sub.add_parser("remove", help="remove a key by id or 1-based index")
    p_rm.add_argument("name")
    p_rm.add_argument("ident")
    p_rm.set_defaults(func=_cmd_remove)

    p_strategy = keys_sub.add_parser(
        "strategy", help="print or set the selection strategy"
    )
    p_strategy.add_argument("name")
    p_strategy.add_argument(
        "strategy", nargs="?", choices=list(profile_keys.STRATEGIES)
    )
    p_strategy.set_defaults(func=_cmd_strategy)

    p_select = keys_sub.add_parser(
        "select", help="show (or with --apply, consume) the key a launch would use"
    )
    p_select.add_argument("name")
    p_select.add_argument(
        "--apply",
        action="store_true",
        help="advance the pool as a launch would (default: only report)",
    )
    p_select.set_defaults(func=_cmd_select)

    p.set_defaults(func=lambda args: (p.print_help(), 0)[1])
