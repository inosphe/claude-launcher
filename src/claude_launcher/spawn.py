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

from . import profile as profile_mod, store, workspaces, worktree as worktree_mod

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
    # Same exception, same reason: a worktree is not a directory an agent
    # invented, it is a second checkout of the repository the parent is
    # already standing in -- derived from what the child would have inherited
    # anyway, and the one way a child gets a checkout it cannot collide in.
    "allow_worktree": True,
    "allow_args": False,
    "allow_env": False,
}

#: The per-field unlocks, mapped to the request key they govern. Kept as data
#: so the error message, the capability report and the check itself cannot
#: drift apart.
_GATED_FIELDS = (
    ("profile", "allow_profile"),
    # The same gate as profile because it is the same question — whose login
    # does the child hold. A borrow keeps the inherited config dir and swaps
    # only the auth, which is not a smaller grant than swapping the profile.
    ("borrow", "allow_profile"),
    ("cwd", "allow_cwd"),
    ("workspace", "allow_workspace"),
    ("args", "allow_args"),
    ("env", "allow_env"),
    # Last on purpose: it is cut from whatever directory the fields above
    # settled on, so a worktree of a workspace is a worktree of that
    # workspace and not of the parent's own checkout.
    ("worktree", "allow_worktree"),
)

#: Refusals for keys that do not name a field of the child's definition.
#: ``workspace`` resolves to ``cwd``, so the generic "inherits its parent's
#: workspace" would name something the child does not have.
_DENIALS = {
    "borrow": (
        "a spawned session authenticates the way its parent does — set "
        "'spawn.allow_profile: true' in ~/.claunch.yaml to let an agent "
        "choose whose login a child runs under (profile and borrow alike)"
    ),
    "workspace": (
        "a spawned session inherits its parent's working directory — set "
        "'spawn.allow_workspace: true' in ~/.claunch.yaml to let an agent "
        "move a child to a directory you registered with 'claunch workspace add'"
    ),
    "worktree": (
        "a spawned session inherits its parent's working directory, and may "
        "not cut a checkout of its own — set 'spawn.allow_worktree: true' in "
        "~/.claunch.yaml to let a child have a git worktree of the repository "
        "its parent is in"
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
    allow_worktree: bool = True
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
            allow_worktree=bool(block["allow_worktree"]),
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
            "allow_worktree": self.allow_worktree,
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
    direct children it has **running** — the cap is on agents alive at once,
    so an ended one does not hold its slot. The return value is the subset of a
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
            f"this session already has {children} direct child(ren) running, "
            f"the limit is {policy.max_children} (spawn.max_children) — reuse "
            "one of them, or end one first ('kill'), which frees its slot"
        )

    child = {
        "harness": parent.get("harness") or "",
        "profile": parent.get("profile") or None,
        "cwd": parent.get("cwd") or "",
        "args": list(parent.get("args") or ()),
        "env": dict(parent.get("env") or {}),
        # Auth travels with the profile: a child of a session that borrows
        # (or runs tokenless) authenticates the way its parent does, or it
        # would not be recognisably a copy of it.
        "borrow": parent.get("borrow") or None,
        "null_token": bool(parent.get("null_token")),
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
        # ...and the inherited auth with it: borrow/null are claude-only
        # machinery, and dragging them onto another harness would build a
        # definition normalize() refuses — a spawn that fails over a field
        # nobody in this request ever named.
        child["borrow"] = None
        child["null_token"] = False

    if request.get("workspace") and request.get("cwd"):
        raise SpawnDenied(
            "give 'workspace' or 'cwd', not both — they set the same thing, "
            "and which one won would be a coin toss"
        )

    if request.get("null_token"):
        # Ungated on purpose: --null takes a credential away rather than
        # granting one — the child boots logged out. It replaces the auth the
        # child would have inherited, a parent's borrow included; asked for
        # *alongside* a borrow of its own, the definition is refused
        # downstream, exactly as `run` refuses the pair.
        child["null_token"] = True
        child["borrow"] = None

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
        elif key == "borrow":
            child["borrow"] = str(value)
            # An explicit borrow replaces inherited tokenlessness — unless
            # this same request also said null, which is refused downstream
            # rather than resolved by whichever key happened to win here.
            if not request.get("null_token"):
                child["null_token"] = False
        elif key == "env":
            child["env"] = {**child["env"], **{str(k): str(v) for k, v in value.items()}}
        elif key == "workspace":
            child["cwd"] = resolve_workspace(str(value))
        elif key == "worktree":
            # Recorded, not cut. `check` decides what a child may be; making
            # a directory is not deciding, and a request that is refused
            # further down must not leave a checkout on disk behind it. See
            # `make_worktree`, which the manager calls once staging is sure.
            child["worktree"] = worktree_mod.validate_name(str(value))
        else:
            child[key] = str(value)

    return child



def make_worktree(child: dict, request: dict) -> dict:
    """Cut the checkout ``check`` recorded, and point the child at it.

    Split from :func:`check` because they answer different questions and fail
    at different costs. ``check`` decides what a child *may* be, and a
    decision leaves nothing on disk; this makes a directory, so it runs once
    the request has already passed everything that could refuse it -- a
    worktree left behind by a spawn that was denied afterwards would be
    litter nobody asked for and nobody would find.

    It is cut from ``child["cwd"]``, which is the parent's directory or the
    workspace that replaced it, so "a worktree of the workspace I sent it to"
    means what it says. ``rebase_onto`` brings a *reused* checkout up to date
    first (see :func:`claude_launcher.worktree.rebase`) -- a fresh one is cut
    from the repository as it stands and has nothing to catch up on.

    **This is the one place a child gets a directory that is nobody's
    workspace**, and it is allowed for the same reason ``allow_workspace`` is:
    the checkout is derived from the repository the parent is already in, not
    a path an agent named. What it buys is the thing a fleet needs and a
    shared checkout cannot give -- two children of one parent editing the
    same repository without editing each other's files.
    """
    name = child.pop("worktree", "")
    if not name:
        return child
    tree = worktree_mod.resolve(
        child.get("cwd") or "",
        name,
        rebase_onto=str(request.get("rebase_onto") or ""),
    )
    if tree is not None:
        child["cwd"] = str(tree.path)
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
        # that name would quietly replace it. It counts the RUNNING ones, so
        # it can read lower than that list — which also carries the exited.
        "children_used": children,
        "children_remaining": remaining,
        "may_choose": sorted(
            [key for key, gate in _GATED_FIELDS if getattr(policy, gate)]
            + (["harness"] if policy.allow_harness else [])
            # Always choosable: it removes a credential rather than granting
            # one, so no unlock stands in front of it.
            + ["null_token"]
        ),
        "spawnable_harnesses": list(policy.allow_harness),
    }
    if policy.allow_profile:
        # The values, not just the field names, same reason as workspaces
        # below: profile names live in a registry the agent cannot see, and
        # unlocking profile/borrow without naming the options would leave it
        # guessing.
        report["profiles"] = [p.name for p in profile_mod.list_all()]
    if policy.allow_workspace:
        # The values, not just the field name. Everything else in
        # ``may_choose`` is something the agent already knows how to spell;
        # a workspace name only exists in a registry it cannot see, so
        # naming the field without listing the options would leave it
        # guessing — the one thing the registry exists to prevent.
        report["workspaces"] = [w.to_dict() for w in workspaces.list_all()]
    return report
