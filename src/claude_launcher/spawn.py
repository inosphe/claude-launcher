"""Agent-initiated session spawning: what a session may create, and how much.

An agent running inside a managed session can ask the daemon for a **child
session** — another harness, in its own PTY, that the parent then briefs and
talks to over a mesh. That is a genuinely new kind of caller: every other way a
session comes into being has a human behind it, typing ``claunch new-session``
or clicking the dashboard.

So the request is deliberately *not* the full :class:`SessionDef`. A child
**inherits** its parent's harness, profile, cwd, args and env, and the agent
supplies only the things that make the child a different worker — a name, a
mesh handle and role, an opening task. Everything else has to be unlocked, per
field, in ``~/.claunch.yaml``::

    spawn:
      enabled: true
      max_children: 4          # direct children per parent
      max_depth: 3             # root session = depth 0
      allow_harness: [codex]   # [] = the parent's harness only
      allow_profile: false
      allow_cwd: false
      allow_workspace: true    # ...the one field that starts open
      allow_args: false
      allow_env: false

``cwd`` and ``workspace`` both set the child's working directory, and they are
unlocked separately because they are not the same risk. ``cwd`` is a free-text
path, which is the easiest thing in a spawn request to get wrong — a typo, a
stale path, the wrong drive — and it fails late, as a PTY that could not start.
``workspace`` names a directory the user registered once with ``claunch
workspace add``, so it is a pick from a list rather than a spelling, and an
unknown name is refused *with the known ones* instead of spawning something
nobody vouched for.

That difference is why ``allow_workspace`` is the **one unlock that defaults
to true**: every other field lets an agent invent a value, while this one only
lets it choose from a list the user already vouched for, and an empty registry
means it can choose nothing at all. It is the web UI's directory picker handed
to an agent, which has neither a filesystem in front of it nor a shell that
completes paths. What it does widen is reach — a child can be sent into
another registered repository and will edit the files there — so a fleet that
registered its workspaces for the *browser* and does not want agents moving
between them turns this one off.

**This is a surface, not a sandbox.** An agent holds the daemon's API token
(it reads the same token file the CLI does), so nothing here stops a
determined agent from calling ``POST /api/sessions`` directly and building
whatever it likes. What the policy buys is the same thing cflow's missing
``approve`` tool buys: the *offered* action is the safe one, so an agent
following its tools cannot wander into spawning a session under another
profile, in another directory, with flags nobody chose. Treat the numbers as
blast-radius limits on honest mistakes — runaway recursion, a fan-out loop —
not as a security boundary against a hostile session.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import store, workspaces

#: Policy defaults. Permissive enough that spawning works out of the box,
#: restrictive enough that a child is always recognisably a copy of its
#: parent: the fields that decide *what runs* stay inherited until the user
#: says otherwise.
DEFAULTS = {
    "enabled": True,
    "max_children": 4,
    "max_depth": 3,
    "allow_harness": [],
    "allow_profile": False,
    "allow_cwd": False,
    # The exception to "inherited until the user says otherwise": a workspace
    # is a pick from a list the user vouched for, not a value an agent
    # invents, so an unregistered directory stays unreachable either way.
    "allow_workspace": True,
    "allow_args": False,
    "allow_env": False,
}

#: The per-field unlocks, mapped to the request key they govern. Kept as data
#: so the error message, the capability report and the check itself cannot
#: drift apart.
_GATED_FIELDS = (
    ("profile", "allow_profile"),
    ("cwd", "allow_cwd"),
    ("workspace", "allow_workspace"),
    ("args", "allow_args"),
    ("env", "allow_env"),
)

#: Refusals for keys that do not name a field of the child's definition.
#: ``workspace`` resolves to ``cwd``, so the generic "inherits its parent's
#: workspace" would name something the child does not have.
_DENIALS = {
    "workspace": (
        "a spawned session inherits its parent's working directory — set "
        "'spawn.allow_workspace: true' in ~/.claunch.yaml to let an agent "
        "move a child to a directory you registered with 'claunch workspace add'"
    ),
}


class SpawnDenied(Exception):
    """Raised when a spawn request exceeds what the policy allows.

    Distinct from :class:`~claude_launcher.daemon.harness.HarnessError`: that
    one means the definition is unbuildable, this one means it was buildable
    and refused. The API maps it to 403, so an agent can tell "I asked for
    something I am not allowed" from "I asked for something impossible".
    """


@dataclass(frozen=True)
class SpawnPolicy:
    enabled: bool = True
    max_children: int = 4
    max_depth: int = 3
    allow_harness: Tuple[str, ...] = ()
    allow_profile: bool = False
    allow_cwd: bool = False
    allow_workspace: bool = True
    allow_args: bool = False
    allow_env: bool = False

    @classmethod
    def load(cls, doc: Optional[dict] = None) -> "SpawnPolicy":
        """Read the ``spawn`` block, falling back to :data:`DEFAULTS`.

        A malformed block is read as the defaults rather than raising: the
        policy is consulted on a path an agent triggers, and a YAML typo that
        made every spawn fail with a parse error would be diagnosed as a
        broken feature, not a broken config.
        """
        raw = (doc if doc is not None else store.load()).get("spawn")
        block = dict(DEFAULTS)
        if isinstance(raw, dict):
            block.update({k: v for k, v in raw.items() if k in DEFAULTS})
        harness = block.get("allow_harness")
        if isinstance(harness, str):
            harness = [harness]
        if not isinstance(harness, (list, tuple)):
            harness = []
        return cls(
            enabled=bool(block["enabled"]),
            max_children=max(0, _int(block["max_children"], DEFAULTS["max_children"])),
            max_depth=max(0, _int(block["max_depth"], DEFAULTS["max_depth"])),
            allow_harness=tuple(str(h).strip() for h in harness if str(h).strip()),
            allow_profile=bool(block["allow_profile"]),
            allow_cwd=bool(block["allow_cwd"]),
            allow_workspace=bool(block["allow_workspace"]),
            allow_args=bool(block["allow_args"]),
            allow_env=bool(block["allow_env"]),
        )

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "max_children": self.max_children,
            "max_depth": self.max_depth,
            "allow_harness": list(self.allow_harness),
            "allow_profile": self.allow_profile,
            "allow_cwd": self.allow_cwd,
            "allow_workspace": self.allow_workspace,
            "allow_args": self.allow_args,
            "allow_env": self.allow_env,
        }


def _int(value, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def resolve_workspace(token: str) -> str:
    """The directory a workspace name (or path) stands for.

    Resolving *here* rather than letting the path travel to the PTY is the
    whole point of the registry: an unknown name is answered with the known
    ones, in a refusal the agent can act on, instead of surfacing three layers
    down as a harness that could not start in a directory nobody vouched for.
    """
    found = workspaces.find(token)
    if found is None:
        known = ", ".join(w.name for w in workspaces.list_all())
        raise SpawnDenied(
            f"no workspace named {token!r} — registered: "
            f"{known or '(none)'}. Registering one is the user's call: "
            "'claunch workspace add DIR'"
        )
    if not found.exists():
        # A workspace on a removable drive is legitimately absent half the
        # time, so this is a state, not a broken registry — say which it is.
        raise SpawnDenied(
            f"workspace {found.name!r} points at {found.path}, which is not "
            "there right now — a child cannot start in it"
        )
    return found.path


def check(
    policy: SpawnPolicy,
    request: dict,
    *,
    parent: dict,
    depth: int,
    children: int,
) -> dict:
    """Validate a spawn request and return the child's inherited overrides.

    ``parent`` is the parent session's definition (``SessionDef.to_dict()``),
    ``depth`` its own depth in the session tree and ``children`` how many
    direct children it already has. The return value is the subset of a
    :class:`SessionDef` the child should be built from — inherited values,
    with any *permitted* override applied.

    Raises :class:`SpawnDenied` with a message written for the agent that will
    read it: what was refused, and which config key would allow it.
    """
    if not policy.enabled:
        raise SpawnDenied(
            "spawning is switched off on this daemon "
            "(set 'spawn.enabled: true' in ~/.claunch.yaml)"
        )
    if depth >= policy.max_depth:
        raise SpawnDenied(
            f"this session is already {depth} level(s) deep and the limit is "
            f"{policy.max_depth} (spawn.max_depth) — give the work to an "
            "existing session instead of nesting further"
        )
    if children >= policy.max_children:
        raise SpawnDenied(
            f"this session already has {children} direct child(ren), the "
            f"limit is {policy.max_children} (spawn.max_children) — reuse one "
            "of them, or kill one first"
        )

    child = {
        "harness": parent.get("harness") or "",
        "profile": parent.get("profile") or None,
        "cwd": parent.get("cwd") or "",
        "args": list(parent.get("args") or ()),
        "env": dict(parent.get("env") or {}),
    }

    harness = str(request.get("harness") or "").strip()
    if harness and harness != child["harness"]:
        if harness not in policy.allow_harness:
            allowed = ", ".join(policy.allow_harness) or "(none)"
            raise SpawnDenied(
                f"harness {harness!r} is not spawnable from a session — the "
                f"child inherits {child['harness']!r}; harnesses unlocked by "
                f"spawn.allow_harness: {allowed}"
            )
        child["harness"] = harness
        # A harness swap invalidates the inherited command line: those args
        # were written for a different program, and passing them on is a
        # spawn failure at best and a misread flag at worst.
        child["args"] = []

    if request.get("workspace") and request.get("cwd"):
        raise SpawnDenied(
            "give 'workspace' or 'cwd', not both — they set the same thing, "
            "and which one won would be a coin toss"
        )

    for key, gate in _GATED_FIELDS:
        value = request.get(key)
        if value in (None, "", [], {}):
            continue
        if not getattr(policy, gate):
            raise SpawnDenied(
                _DENIALS.get(key)
                or (
                    f"a spawned session inherits its parent's {key} — set "
                    f"'spawn.{gate}: true' in ~/.claunch.yaml to let an agent "
                    f"choose it"
                )
            )
        if key == "args":
            child["args"] = [str(a) for a in value]
        elif key == "env":
            child["env"] = {**child["env"], **{str(k): str(v) for k, v in value.items()}}
        elif key == "workspace":
            child["cwd"] = resolve_workspace(str(value))
        else:
            child[key] = str(value)

    return child


def capabilities(policy: SpawnPolicy, *, depth: int, children: int) -> dict:
    """What this session may spawn right now — the report the MCP tool shows.

    Answering "can I, and with what" in one place means an agent does not have
    to provoke a :class:`SpawnDenied` to find out.
    """
    remaining = max(0, policy.max_children - children)
    blocked = []
    if not policy.enabled:
        blocked.append("spawning is disabled (spawn.enabled)")
    if depth >= policy.max_depth:
        blocked.append(f"depth limit reached ({depth}/{policy.max_depth})")
    if not remaining:
        blocked.append(f"child limit reached ({children}/{policy.max_children})")
    report = {
        "can_spawn": not blocked,
        "blocked_by": blocked,
        "depth": depth,
        "max_depth": policy.max_depth,
        # ``children_used``, not ``children``: this report is merged into a
        # payload that also carries the actual child list, and a count under
        # that name would quietly replace it.
        "children_used": children,
        "children_remaining": remaining,
        "may_choose": sorted(
            [key for key, gate in _GATED_FIELDS if getattr(policy, gate)]
            + (["harness"] if policy.allow_harness else [])
        ),
        "spawnable_harnesses": list(policy.allow_harness),
    }
    if policy.allow_workspace:
        # The values, not just the field name. Everything else in
        # ``may_choose`` is something the agent already knows how to spell;
        # a workspace name only exists in a registry it cannot see, so
        # naming the field without listing the options would leave it
        # guessing — the one thing the registry exists to prevent.
        report["workspaces"] = [w.to_dict() for w in workspaces.list_all()]
    return report
