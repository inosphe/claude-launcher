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
      max_children: 4          # direct children per parent -- a SOFT cap
      max_depth: 3             # root session = depth 0 -- hard
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

``max_children`` is a **soft** cap for exactly that reason: it exists to
interrupt a fan-out loop, not to forbid a fifth child anyone actually wanted.
So it does not refuse — it *warns*. A request that says nothing about the cap
crosses it and comes back carrying :func:`over_limit_warning`, and a parent
standing at 5/4 afterwards is fine. The strict reading is still available, but
it has to be asked for: ``over_limit: false`` (the CLI's ``--within-limit``,
the wizard's *Over limit* row answered *no*) is refused at the cap, which is
what a fleet that wants the fan-out loop stopped dead sets.

That default is the way round it is because of who pays for each mistake. A
cap that refuses costs a real turn every time it is wrong — the agent that
wanted a fifth child has to read the refusal, decide, and ask again — while a
cap that warns costs one extra session when it is wrong, which is cheap and
visible in ``children``. ``max_depth`` stays hard on the same reasoning read
the other way: runaway recursion is the mistake the limits are for, depth is
the axis it runs away on, and there the cheap-when-wrong direction is the
refusal.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from . import (
    harness_policy,
    harnesses,
    lineage,
    profile as profile_mod,
    providers,
    store,
    transcripts,
    workspaces,
    worktree as worktree_mod,
)

#: Policy defaults. Permissive enough that spawning works out of the box,
#: restrictive enough that a child is always recognisably a copy of its
#: parent: the fields that decide *what runs* stay inherited until the user
#: says otherwise.
DEFAULTS = {
    "enabled": True,
    "max_children": 4,
    "max_depth": 3,
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

#: What ``worktree: true`` becomes between :func:`check` and
#: :func:`make_worktree`. "A checkout of its own, you name it" is a question
#: neither of them can answer alone: the name is ``<parent>-<child>-<stamp>``
#: and the child has no name until the manager stages it, so the request
#: travels as this sentinel and is resolved where both halves are known. It
#: is not a legal worktree name (``validate_name`` refuses ``@``), which is
#: why it cannot collide with one a caller typed.
AUTO_WORKTREE = "@auto"

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


def over_limit_warning(max_children: int, children: int) -> str:
    """The one sentence a crossed child cap says, wherever it is reported.

    Written once because it travels: :func:`check` hands it to the daemon,
    which puts it in the spawn response, which the CLI prints and an agent's
    ``spawn`` tool reads back. A warning phrased three different ways would
    read as three different conditions.
    """
    return (
        f"child limit crossed: {children} direct child(ren) were already "
        f"RUNNING and the cap is {max_children} (spawn.max_children) — the "
        "child was created anyway, because the cap is soft. Ended children "
        "are not in that count and do not hold a slot, so a roster listing "
        "exited ones will show more than this number. End one you no longer "
        "need with 'kill' to free its slot; send 'over_limit: false' "
        "('--within-limit') to be refused at the cap instead"
    )


def over_limit_notice(max_children: int, children: int) -> str:
    """The same fact stated *before* the spawn, for a form that is offering it.

    :func:`over_limit_warning` is past tense — it reports a crossing that has
    happened. A picker showing the cap has not crossed anything yet, so it
    gets its own wording rather than a warning about a child that does not
    exist.
    """
    return (
        f"child limit reached ({children} running/{max_children}) — spawning "
        "anyway is allowed and the daemon counts it against you"
    )


@dataclass(frozen=True)
class SpawnPolicy:
    enabled: bool = True
    max_children: int = 4
    max_depth: int = 3
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
        return cls(
            enabled=bool(block["enabled"]),
            max_children=max(0, _int(block["max_children"], DEFAULTS["max_children"])),
            max_depth=max(0, _int(block["max_depth"], DEFAULTS["max_depth"])),
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
    warnings: Optional[List[str]] = None,
) -> dict:
    """Validate a spawn request and return the child's inherited overrides.

    ``parent`` is the parent session's definition (``SessionDef.to_dict()``),
    ``depth`` its own depth in the session tree and ``children`` how many
    direct children it has **running** — the cap is on agents alive at once,
    so an ended one does not hold its slot. The return value is the subset of a
    :class:`SessionDef` the child should be built from — inherited values,
    with any *permitted* override applied.

    That running count leaves here inside a warning, where it is read next to
    rosters that DO list exited sessions — and a reader who folds the two
    together concludes the cap is counting the dead and goes hunting a bug in
    a filter that is correct. Which is why :func:`over_limit_warning` says
    "running" out loud rather than printing a bare ``N/M``.

    ``warnings`` is a list this appends to: a request that is *allowed* but
    worth saying something about leaves its sentence there. An out-parameter
    rather than a second return value because the only such case today is the
    soft child cap, and a caller that does not care about it — every test that
    only asks "was this permitted" — should not have to unpack a tuple to find
    out. Passing nothing discards the warnings; it never changes the verdict.

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
        # A SOFT cap: it interrupts a fan-out loop, it does not forbid a child
        # somebody wanted on purpose. So the default is to cross it and SAY
        # so; only a request that asked for the strict reading outright is
        # refused.
        #
        # Three-valued on purpose, and a plain truthiness test would collapse
        # it: "did not say" and "said no" are the same falsy value and mean
        # opposite things here. A JSON ``null`` counts as not saying — it is
        # what a client sends for a field nobody filled in, and reading it as
        # a no would refuse a spawn on the strength of an empty form.
        answer = request.get("over_limit")
        if answer is None or answer:
            if warnings is not None:
                warnings.append(
                    over_limit_warning(policy.max_children, children)
                )
        else:
            raise SpawnDenied(
                f"this session already has {children} direct child(ren) "
                f"running, the limit is {policy.max_children} "
                "(spawn.max_children) and this request asked to be held to it "
                "('over_limit: false') — reuse one of them, or end one first "
                "('kill'), which frees its slot; drop that field and the cap "
                "warns instead of refusing"
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

    if request.get("harness"):
        raise SpawnDenied(
            "a child's harness is read-only and comes from its profile; "
            "choose an allowed profile instead of sending 'harness'"
        )

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
        if value in (None, "", [], {}) or value is False:
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
            #
            # `true` is "one of its own, you name it" -- the language the
            # `quick_job` YAML default and the dashboard's quick-job form
            # already speak, and the only answer available to a caller that
            # cannot name the checkout because it does not yet know which
            # session will get it. Read as a name it used to cut a worktree
            # called `True`, on a branch called `True`.
            child["worktree"] = (
                AUTO_WORKTREE
                if value is True
                else worktree_mod.validate_name(str(value))
            )
        else:
            child[key] = str(value)

    # A profile override can legitimately change the harness; that is now the
    # only route. Re-resolve it before fork validation and discard inherited
    # command/auth fields that belonged to the parent's different program.
    if child.get("profile"):
        selected = lineage.effective_harness(
            profile_mod.require_selector(str(child["profile"]))
        )
        if selected != child.get("harness"):
            child["harness"] = selected
            if not request.get("args"):
                child["args"] = []
            if not request.get("borrow"):
                child["borrow"] = None
            if not request.get("null_token"):
                child["null_token"] = False

    if request.get("fork"):
        _fork_parents_conversation(child, parent, request)

    return child


#: The request keys that send a child somewhere other than its parent's
#: directory. They are what makes a fork impossible, so they are listed once,
#: here, rather than spelled again in the refusal.
_MOVES_THE_CHILD = ("cwd", "workspace", "worktree")


def can_fork(parent: Optional[dict]) -> bool:
    """Whether ``parent`` has a conversation a child could be handed a copy of.

    The same facts :func:`_fork_parents_conversation` refuses on, asked ahead
    of time — it is what the capability report and the spawn wizard's Fork row
    both need, and asking it in one place is what keeps the offer and the
    refusal from disagreeing.

    A pinned id is not yet a conversation: claude writes the jsonl on the
    session's first turn, and a parent spawned seconds ago has an id and no
    transcript (measured window 7-28 s — see
    :func:`claude_launcher.daemon.harness.restores_blank`). ``--resume`` of a
    file claude never wrote is fatal on startup, so offering the fork there
    hands the operator a child that exits instead of one that inherits.
    :func:`_on_disk` is what asks.
    """
    if not parent:
        return False
    return (
        (parent.get("harness") or "") == "claude"
        and bool(parent.get("conversation_id"))
        and _on_disk(parent)
    )


def _on_disk(parent: dict) -> bool:
    """Whether the parent's pinned conversation has a transcript behind it.

    Generous in exactly one direction, and deliberately. When the profile
    cannot be resolved (an unknown selector, an unreadable config) this
    answers *yes* rather than no: a wrong yes costs one child that fails
    loudly at startup with claude's own "No conversation found with session
    ID", while a wrong no takes the fork off the form with a reason that is
    not true and no way for the operator to tell. The unprovable case is the
    one where the loud failure is the better of the two.
    """
    conversation = str(parent.get("conversation_id") or "")
    if not conversation:
        return False
    selector = str(parent.get("profile") or "")
    if not selector:
        return True
    try:
        prof = profile_mod.require_selector(selector)
    except Exception:  # noqa: BLE001 — ProfileError and anything under it
        return True
    return transcripts.exists(
        prof.config_dir, conversation, str(parent.get("cwd") or "")
    )


def _fork_parents_conversation(child: dict, parent: dict, request: dict) -> None:
    """Point the child at a COPY of the parent's own conversation.

    ``fork`` is the spawn-side spelling of claude's ``--resume <id>
    --fork-session``: the child opens the parent's conversation, copied, so it
    starts knowing everything the parent knew and diverges from its first
    word. The parent's own conversation is untouched.

    **Ungated on purpose**, and for the same reason as ``null_token``: it
    grants the child nothing its parent did not already hold. The thing being
    copied *is* the parent's context, the child is the parent's own creation,
    and no directory, profile or token becomes reachable that was not
    reachable before — so there is no unlock to write in ``~/.claunch.yaml``,
    only two things to be true.

    The first is the harness: a forked conversation is a claude transcript,
    and there is nothing to hand another program. The second is the
    directory, and it is the one that surprises people. Claude Code keeps
    transcripts **per working directory** (see
    :func:`claude_launcher.worktree.resolve`), so the parent's conversation
    only resolves where the parent held it. A child sent to a workspace or
    cut a worktree of its own would look for that conversation in a directory
    it was never written to and wake up with nothing — a fork in name, empty
    in fact. That is refused here rather than launched: an empty child that
    was asked to inherit everything is the failure nobody would think to look
    for.
    """
    if (child.get("harness") or "") != "claude":
        raise SpawnDenied(
            "'fork' copies the parent's claude conversation, which is a "
            f"claude transcript — it has no meaning for the "
            f"{child.get('harness')!r} harness; drop 'fork', or drop the "
            "harness swap"
        )
    moved = [k for k in _MOVES_THE_CHILD if request.get(k)]
    if moved:
        raise SpawnDenied(
            f"'fork' cannot be combined with {moved[0]!r}: claude keeps "
            "transcripts per working directory, so the parent's conversation "
            "only opens where the parent held it — a child started elsewhere "
            "would find nothing and boot empty. Fork the conversation and "
            "stay put, or move the child and let it start fresh"
        )
    conversation = str(parent.get("conversation_id") or "")
    if not conversation:
        raise SpawnDenied(
            "the parent has no conversation to fork: it steers its own with "
            "harness args, or it opened claude's picker and nothing is "
            "pinned yet — spawn without 'fork'"
        )
    if not _on_disk(parent):
        raise SpawnDenied(
            f"the parent's conversation {conversation} has no transcript on "
            "disk yet — claude writes it on the session's first turn, and "
            "'--resume' of a file it never wrote kills the child on startup. "
            "Let the parent take a turn, then fork"
        )
    # Spelled as the two fields the harness already knows how to launch:
    # `--resume <id> --fork-session`. normalize() then pins the child a fresh
    # conversation id of its own, so the copy is restorable like any other.
    child["resume"] = conversation
    child["fork_session"] = True



def make_worktree(
    child: dict, request: dict, *, parent: str = "", name: str = ""
) -> dict:
    """Cut the checkout ``check`` recorded, and point the child at it.

    Split from :func:`check` because they answer different questions and fail
    at different costs. ``check`` decides what a child *may* be, and a
    decision leaves nothing on disk; this makes a directory, so it runs once
    the request has already passed everything that could refuse it -- a
    worktree left behind by a spawn that was denied afterwards would be
    litter nobody asked for and nobody would find.

    It is cut from ``child["cwd"]``, which is the parent's directory or the
    workspace that replaced it, so "a worktree of the workspace I sent it to"
    means what it says. ``rebase_onto`` puts the checkout on that branch: a
    *reused* one is rebased onto it first (see
    :func:`claude_launcher.worktree.rebase`), a fresh one is cut from it
    instead of from the trunk -- which is how a nested worker's branch is
    made to begin on its parent's branch (a stacked pull request).

    **This is the one place a child gets a directory that is nobody's
    workspace**, and it is allowed for the same reason ``allow_workspace`` is:
    the checkout is derived from the repository the parent is already in, not
    a path an agent named. What it buys is the thing a fleet needs and a
    shared checkout cannot give -- two children of one parent editing the
    same repository without editing each other's files.

    ``parent`` and ``name`` are the two session names, and they are what
    :data:`AUTO_WORKTREE` is resolved with -- which is why the manager settles
    the child's name *before* calling this rather than letting
    :meth:`~claude_launcher.daemon.manager.Manager.stage` invent one after.
    """
    wanted = child.pop("worktree", "")
    if not wanted:
        return child
    if wanted == AUTO_WORKTREE:
        wanted = worktree_mod.child_name(parent, name)
    tree = worktree_mod.resolve(
        child.get("cwd") or "",
        wanted,
        rebase_onto=str(request.get("rebase_onto") or ""),
    )
    if tree is not None:
        child["cwd"] = str(tree.path)
    return child

def capabilities(
    policy: SpawnPolicy,
    *,
    depth: int,
    children: int,
    parent: Optional[dict] = None,
) -> dict:
    """What this session may spawn right now — the report the MCP tool shows.

    Answering "can I, and with what" in one place means an agent does not have
    to provoke a :class:`SpawnDenied` to find out.

    ``parent`` is the parent's own definition, and it is optional because most
    of this report is the policy's alone. Only ``fork`` needs it: whether
    there is a conversation to copy is a fact about that one session, not
    about what the user unlocked.
    """
    remaining = max(0, policy.max_children - children)
    blocked = []
    if not policy.enabled:
        blocked.append("spawning is disabled (spawn.enabled)")
    if depth >= policy.max_depth:
        blocked.append(f"depth limit reached ({depth}/{policy.max_depth})")
    # Soft, and reported apart: the child cap does not refuse, it warns, so a
    # client that reads only ``blocked_by`` must not find it there and put up
    # a dead end over a spawn the daemon would have allowed. It stays in
    # ``soft_blocked_by`` because a form with a person in front of it should
    # still SAY the cap is reached -- offering the crossing, pre-answered
    # yes, rather than hiding that anything is unusual.
    soft = []
    if not remaining:
        soft.append(over_limit_notice(policy.max_children, children))
    report = {
        "can_spawn": not blocked,
        "blocked_by": blocked,
        "soft_blocked_by": soft,
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
            # Always choosable: it removes a credential rather than granting
            # one, so no unlock stands in front of it.
            + ["null_token"]
            # Ungated too, but conditional on the parent rather than on the
            # policy: forking copies a conversation, and a parent that holds
            # none has nothing to offer. Reported only when it would work, so
            # an agent reading this list does not have to try it to find out.
            + (["fork"] if can_fork(parent) else [])
        ),
        # Compatibility field for older clients. Harness selection itself is
        # gone; an allowed profile may still resolve to a different harness.
        "spawnable_harnesses": [],
    }
    if policy.allow_profile:
        # The values, not just the field names, same reason as workspaces
        # below: profile names live in a registry the agent cannot see, and
        # unlocking profile/borrow without naming the options would leave it
        # guessing.
        profiles = profile_mod.list_all()
        report["profiles"] = [p.name for p in profiles]
        selectors = []
        options = []
        errors = {}
        for p in profiles:
            try:
                allowed = harness_policy.allowed_names(p)
            except (
                harness_policy.HarnessPolicyError,
                lineage.LineageError,
                providers.ProviderError,
            ) as exc:
                errors[p.name] = str(exc)
                continue
            default_name = None
            try:
                default_name = lineage.effective_harness(p)
                default_selector = f"{p.name}:{default_name}"
                selectors.append(default_selector)
                options.append(
                    {
                        "value": default_selector,
                        "label": f"{p.name}/{default_name}",
                        "profile": p.name,
                        "harness": default_name,
                        "default": True,
                    }
                )
            except (
                harness_policy.HarnessPolicyError,
                lineage.LineageError,
                providers.ProviderError,
            ) as exc:
                # The actual creation path still fails closed. The capability
                # report names malformed profile policy instead of taking the
                # whole child form down or offering unrestricted selectors.
                errors[p.name] = str(exc)
            for name in allowed:
                if name == default_name:
                    continue
                selector = f"{p.name}:{name}"
                selectors.append(selector)
                options.append(
                    {
                        "value": selector,
                        "label": f"{p.name}/{name}",
                        "profile": p.name,
                        "harness": name,
                        "default": False,
                    }
                )
        report["profile_selectors"] = selectors
        report["profile_options"] = options
        if errors:
            report["profile_errors"] = errors
    if policy.allow_workspace:
        # The values, not just the field name. Everything else in
        # ``may_choose`` is something the agent already knows how to spell;
        # a workspace name only exists in a registry it cannot see, so
        # naming the field without listing the options would leave it
        # guessing — the one thing the registry exists to prevent.
        report["workspaces"] = [w.to_dict() for w in workspaces.list_all()]
    return report
