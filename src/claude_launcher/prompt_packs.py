"""Prompt packs: agent guidance claunch ships, installed per workspace.

A pack is one Markdown file under ``claude_launcher/prompt_packs/`` with a
small YAML header (``title``, ``description``). The body is guidance an agent
reads at the start of a session — how this machine's issue board is driven,
what an Unreal project hands to a person instead of doing itself. A new pack
is a new file in that directory; nothing else lists them.

Two steps, on purpose:

1. **Install into a workspace** (Settings ▸ Install, or ``claunch prompts
   install``). The pack is copied to ``<workspace>/.claunch/prompts/<id>.md``.
   A workspace opts in; a profile never does, because a pack describes a
   repository's working rules and a profile spans repositories.
2. **Choose it at session creation.** The create form offers every pack
   installed in the chosen workspace as a checkbox, checked when the pack
   says it starts ticked (:func:`default_packs`); a CLI create or a
   spawn names its packs itself (``--prompt-pack``, ``prompt_packs``). The
   ones chosen are put into the session's opening
   (:func:`opening_block`). The copy in the workspace is what is read, so a
   workspace that edited its copy gets its edit.

The two settings in the config override **item by item, never whole**: a
role row or a pack's tick that the config states wins for that role or that
pack, and everything it does not mention keeps the package's answer. So a
role or a pack added to the package reaches an installation whose config was
written before it existed (claunch-imo9c.3); a row left out asks for the
package's row, and ``{"worker": []}`` is how the config states "this role
requires none".

A copy is compared against the package and against what was last written
(:data:`SEED_RECORD`), which gives four answers (:func:`status`): not
installed, current, stale (the package moved on and the copy is untouched —
safe to refresh) and edited (someone changed the copy — refreshing needs
``force``). A Markdown file a workspace put in that directory itself is a
pack too (``local``): it is offered at creation like the shipped ones.

A mesh role may also *require* packs (:func:`role_packs`). The packaged
roles keep a one- or two-sentence stance and put the rest of their guidance
in a ``role-<name>`` pack, so a session created with a role gets that pack
in its opening whether or not it was ticked. No pack needs installing to be
given, ticked or required: the workspace's copy is read when it has one, and
the shipped copy otherwise (:func:`pack_copy`).

A pack whose header says ``delivery: on-demand`` (:data:`ON_DEMAND`) is the
other kind: text given to a session that is already running, at the moment
it is asked for -- the retrospective a run's last node or a person sends
(``claunch retro``, :func:`on_demand_block`). It is never part of an opening,
so it is excluded from everything that builds one, by the same rule in every
place: the create form does not offer it, it cannot be ticked, saved as a
default pack, or required by a role, and a saved setting that names one is
read without it (:func:`offerable`). It still installs into a workspace like
any pack, so a workspace can edit its text.

A workspace copy whose header says ``delivery: always`` (:data:`ALWAYS`) is
the workspace's own prompt: every session created in that workspace gets it,
on every creation path (the create form, ``claunch new-session``, a spawn),
whatever was ticked and whatever the role (:func:`always_packs`), unless
the request turns it off (``workspace_prompt: false``, :func:`always_wanted`;
the create form's box starts ticked). Only the
workspace's copy counts -- claunch's own copy of a pack never makes it
always, because "every session here" is a decision a workspace makes about
itself. An on-demand pack is never in an opening, so on-demand wins over
always for a shipped id.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from . import atomic, digests

#: The shipped packs.
PACKAGE_DIR = Path(__file__).resolve().parent / "prompt_packs"
#: Where a workspace keeps its installed copies, relative to its root.
INSTALL_DIR = Path(".claunch") / "prompts"
#: ``{pack id: sha256 of the bytes last written}`` beside the copies.
SEED_RECORD = ".seeded.json"

MISSING = "missing"
CURRENT = "current"
STALE = "stale"
EDITED = "edited"
LOCAL = "local"

#: Where the text a session was given came from (:attr:`Pack.source`): the
#: workspace's installed copy, the copy that ships with claunch (for a pack
#: the workspace has not installed), or the daemon instance's own prompt
#: (:mod:`claude_launcher.daemon.daemon_prompt`).
WORKSPACE = "workspace"
PACKAGE = "package"
DAEMON = "daemon"

#: ``delivery`` header values. :data:`OPENING` (the default) is a pack an
#: opening may carry; :data:`ON_DEMAND` is one only ever sent later;
#: :data:`ALWAYS` is a workspace copy every session created there gets.
OPENING = "opening"
ON_DEMAND = "on-demand"
ALWAYS = "always"
_DELIVERIES = (OPENING, ON_DEMAND, ALWAYS)
#: The create/spawn request key that leaves the workspace's own prompt out.
WORKSPACE_PROMPT_KEY = "workspace_prompt"

#: A pack id is its file name without ``.md``: lower-case words and dashes,
#: so it is safe in a path, a URL and a checkbox value alike.
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_FRONT = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?", re.S)


class PackError(Exception):
    """An unknown pack, a bad id, or a copy that may not be overwritten."""


@dataclass(frozen=True)
class Pack:
    id: str
    title: str
    description: str
    body: str
    raw: str
    #: :data:`WORKSPACE`, :data:`PACKAGE` (see :func:`pack_copy`) or
    #: :data:`DAEMON`.
    source: str = WORKSPACE
    #: :data:`OPENING`, :data:`ON_DEMAND` or :data:`ALWAYS`, from the header.
    delivery: str = OPENING
    #: Whether a create form ticks this pack on every opening it draws
    #: (:func:`default_packs`). ``None`` means the header says nothing, which
    #: is different from saying ``false``: :func:`default_packs` resolves the
    #: chain shipped header -> config -> workspace copy, and only a stated
    #: value takes part in it.
    check_default: Optional[bool] = None

    @property
    def on_demand(self) -> bool:
        return self.delivery == ON_DEMAND

    @property
    def always(self) -> bool:
        return self.delivery == ALWAYS

    def to_dict(self) -> dict:
        out = {
            "id": self.id, "title": self.title,
            "description": self.description,
        }
        # Only the exception is spelled out, so an opening pack's row reads
        # as it did before the header existed.
        if self.delivery != OPENING:
            out["delivery"] = self.delivery
        if self.check_default is not None:
            out["check_default"] = self.check_default
        return out


def _write_text(path: Path, text: str) -> None:
    """Replace ``path`` with ``text`` in one rename, LF line endings kept."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with atomic.scratch(path) as tmp:
        tmp.write_text(text, encoding="utf-8", newline="\n")
        atomic.replace(tmp, path)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse(pack_id: str, raw: str) -> Pack:
    """A pack from its file text: the YAML header, then the body."""
    meta: dict = {}
    body = raw
    m = _FRONT.match(raw)
    if m:
        try:
            loaded = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError:
            loaded = {}
        if isinstance(loaded, dict):
            meta = loaded
        body = raw[m.end():]
    delivery = str(meta.get("delivery") or "").strip().lower()
    # ``check_default`` is the header that decides whether a create form
    # ticks this pack: the pack says what it is for, and a workspace copy
    # that disagrees says what that workspace wants. Anything but a stated
    # boolean is "says nothing", so an old copy with an unrelated header
    # still parses and takes no part in the chain.
    return Pack(
        id=pack_id,
        title=str(meta.get("title") or pack_id).strip(),
        description=str(meta.get("description") or "").strip(),
        body=body.strip(),
        raw=raw,
        delivery=delivery if delivery in _DELIVERIES else OPENING,
        check_default=as_bool(meta.get("check_default")),
    )


def as_bool(value) -> Optional[bool]:
    """A header's boolean, or ``None`` when it says nothing usable."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "yes", "on", "1"):
            return True
        if text in ("false", "no", "off", "0"):
            return False
    return None


def check_id(pack_id: str) -> str:
    pack_id = str(pack_id or "").strip()
    if not _ID.match(pack_id):
        raise PackError(
            f"bad prompt pack id {pack_id!r}: lower-case letters, digits and "
            "dashes only"
        )
    return pack_id


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def catalog() -> List[Pack]:
    """Every shipped pack, by id."""
    if not PACKAGE_DIR.is_dir():
        return []
    out = []
    for path in sorted(PACKAGE_DIR.glob("*.md")):
        if _ID.match(path.stem):
            out.append(parse(path.stem, _read(path)))
    return out


def shipped(pack_id: str) -> Pack:
    pack_id = check_id(pack_id)
    path = PACKAGE_DIR / f"{pack_id}.md"
    if not path.is_file():
        raise PackError(f"no prompt pack named {pack_id!r} ships with claunch")
    return parse(pack_id, _read(path))


def on_demand_ids() -> List[str]:
    """The shipped packs that are only ever sent later, never in an opening."""
    return [p.id for p in catalog() if p.on_demand]


def is_on_demand(pack_id: str, workspace: Optional[Path] = None) -> bool:
    """Whether ``pack_id`` is an on-demand pack (:data:`ON_DEMAND`).

    claunch's own header decides for a pack it ships, whatever a workspace
    copy says: which kind a pack is decides which settings may name it, and
    those settings are not per workspace. A pack only a workspace has is
    decided by that copy's header.
    """
    try:
        return shipped(pack_id).on_demand
    except PackError:
        pass
    if workspace is not None:
        for pack in installed(workspace):
            if pack.id == pack_id:
                return pack.on_demand
    return False


def offerable(ids, workspace: Optional[Path] = None) -> List[str]:
    """``ids`` without the on-demand packs: what an opening may carry."""
    return [i for i in ids if not is_on_demand(i, workspace)]


def check_offerable(ids, what: str) -> List[str]:
    """Refuse an on-demand pack where a setting or a tick names one."""
    bad = [i for i in ids if is_on_demand(i)]
    if bad:
        raise PackError(_on_demand_refusal(bad, what))
    return list(ids)


def _on_demand_refusal(ids, what: str) -> str:
    return (
        f"{', '.join(ids)}: delivered on demand to a running session "
        f"(claunch retro), never in an opening; it cannot be {what}"
    )


def install_dir(workspace: Path) -> Path:
    return Path(workspace) / INSTALL_DIR


def _record(workspace: Path) -> Dict[str, str]:
    path = install_dir(workspace) / SEED_RECORD
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def _write_record(workspace: Path, record: Dict[str, str]) -> None:
    path = install_dir(workspace) / SEED_RECORD
    if not record:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    _write_text(path, json.dumps(record, indent=2, sort_keys=True) + "\n")


def status(workspace: Path, pack: Pack) -> str:
    """Where ``workspace``'s copy of a shipped ``pack`` stands."""
    path = install_dir(workspace) / f"{pack.id}.md"
    if not path.is_file():
        return MISSING
    have = _sha(_read(path))
    if have == _sha(pack.raw):
        return CURRENT
    seeded = _record(workspace).get(pack.id)
    if seeded and seeded == have:
        return STALE
    return EDITED


def install(workspace: Path, pack_id: str, *, force: bool = False) -> str:
    """Copy a shipped pack into ``workspace``; returns the status it had.

    Refuses to overwrite an edited copy unless ``force``: the edit is the
    workspace's own decision, and the package has no way to tell a fix from
    a regression in it.
    """
    pack = shipped(pack_id)
    before = status(workspace, pack)
    if before == EDITED and not force:
        raise PackError(
            f"{pack.id}: the copy in this workspace was edited; replacing it "
            "needs force"
        )
    if before == CURRENT:
        return before
    target = install_dir(workspace) / f"{pack.id}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    _write_text(target, pack.raw)
    record = _record(workspace)
    record[pack.id] = _sha(pack.raw)
    _write_record(workspace, record)
    return before


def install_all(workspace: Path) -> List[dict]:
    """Install missing/stale shipped packs, preserving edits and local files.

    Each result records a failure independently so callers can show partial
    success and retry without replacing a workspace's custom text.
    """
    results = []
    for pack in catalog():
        try:
            before = status(workspace, pack)
            if before in (MISSING, STALE):
                install(workspace, pack.id)
            results.append({"id": pack.id, "was": before,
                            "changed": before in (MISSING, STALE)})
        except (PackError, OSError) as exc:
            results.append({"id": pack.id, "error": str(exc), "changed": False})
    return results


def remove(workspace: Path, pack_id: str, *, force: bool = False) -> str:
    """Delete ``workspace``'s copy of a pack; returns the status it had.

    An edited copy of a shipped pack, and a local pack (which exists nowhere
    else), are only removed with ``force``.
    """
    pack_id = check_id(pack_id)
    path = install_dir(workspace) / f"{pack_id}.md"
    try:
        pack = shipped(pack_id)
        before = status(workspace, pack)
    except PackError:
        pack = None
        before = LOCAL if path.is_file() else MISSING
    if before == MISSING:
        return before
    if before in (EDITED, LOCAL) and not force:
        raise PackError(
            f"{pack_id}: this copy exists only in the workspace "
            f"({'edited' if before == EDITED else 'written there'}); removing "
            "it needs force"
        )
    path.unlink()
    record = _record(workspace)
    if record.pop(pack_id, None) is not None:
        _write_record(workspace, record)
    return before


def installed(workspace: Path) -> List[Pack]:
    """Every pack present in ``workspace``, as the workspace's copy reads."""
    root = install_dir(workspace)
    if not root.is_dir():
        return []
    out = []
    for path in sorted(root.glob("*.md")):
        if _ID.match(path.stem):
            try:
                out.append(parse(path.stem, _read(path)))
            except OSError:
                continue
    return out


def always_packs(workspace: Optional[Path]) -> List[Pack]:
    """The workspace's own prompt: its copies headed ``delivery: always``.

    Read from the workspace's directory only (:data:`ALWAYS`); an id claunch
    ships as on-demand stays out, as it does from every opening.
    """
    if workspace is None:
        return []
    return [p for p in installed(workspace)
            if p.always and not is_on_demand(p.id, workspace)]


def request_switch(body: dict, key: str) -> bool:
    """Whether a create/spawn request leaves ``key`` on: yes unless it says no."""
    raw = body.get(key, True)
    if isinstance(raw, str):
        return raw.strip().lower() not in ("0", "false", "no", "off", "")
    return raw is not False and raw is not None and raw != 0


def always_wanted(body: dict) -> bool:
    """Whether a create/spawn request takes the workspace's own prompt."""
    return request_switch(body, WORKSPACE_PROMPT_KEY)


def workspace_rows(workspace: Path) -> List[dict]:
    """One row per pack a workspace could hold: shipped ones, then local.

    A shipped row describes the package's pack, so its ``check_default`` is
    the package's answer and says nothing about this workspace. The copy's
    own statement, when it makes one, is reported beside it as
    ``copy_check_default`` -- that is the value :func:`default_packs` applies
    last for this workspace, and a card that showed only the package's answer
    would draw a switch that does nothing here.
    """
    rows = []
    shipped_ids = set()
    here = {p.id: p for p in installed(workspace)}
    for pack in catalog():
        shipped_ids.add(pack.id)
        copy = here.get(pack.id)
        rows.append({
            **pack.to_dict(),
            "status": status(workspace, pack),
            "copy_check_default": copy.check_default if copy is not None else None,
        })
    for pack in installed(workspace):
        if pack.id not in shipped_ids:
            rows.append({**pack.to_dict(), "status": LOCAL,
                         "copy_check_default": pack.check_default})
    return rows


def text_id(body: str) -> str:
    """The content id a session is handed beside a pack's text."""
    return digests.text_digest((body or "").strip())


def opening_block(
    pack_id: str, body: str, source: str = WORKSPACE, *,
    required_by=None, ticked: bool = False, always: bool = False,
) -> str:
    """One pack as it goes into an opening, with its id beside it.

    The id is what a session reminder and ``rebrief`` name later
    (:mod:`claude_launcher.daemon.rebrief`): an agent that finds it here,
    next to the text, still has the pack after a compaction. The header says
    where the text came from, because a package copy has no file in the
    workspace to point at -- and, for that copy, why it is there: the roles
    that require it (``required_by``), else that it was ticked at creation.
    A record from before the reason was kept says neither. A workspace copy
    that is the workspace's own prompt (``always``) says so.
    """
    if source == PACKAGE:
        if required_by:
            n = len(required_by)
            why = f"required by role{'s' if n > 1 else ''} {', '.join(required_by)}, "
        elif ticked:
            why = "ticked at session creation, "
        else:
            why = ""
        origin = f"shipped with claunch, {why}not installed in this workspace"
        where = f"file: (not installed in this workspace; claunch's {pack_id}.md)"
    elif source == DAEMON:
        origin = "this daemon's own prompt, given to every session it starts"
        where = "file: the daemon prompt (MCP tool daemon_prompt shows and edits it)"
    elif source == "prompter":
        origin = "from the configured prompter server, selected at session creation"
        where = "file: (server prompt; snapshot saved with this session)"
    elif always:
        origin = "this workspace's own prompt, given to every session created in it"
        where = f"file: {(INSTALL_DIR / (pack_id + '.md')).as_posix()}"
    else:
        origin = "installed in this workspace, chosen at session creation"
        where = f"file: {(INSTALL_DIR / (pack_id + '.md')).as_posix()}"
    return (
        "---\n"
        f"# claunch prompt pack: {pack_id} -- {origin}\n"
        f"text id: {text_id(body)}\n"
        f"{where}\n"
        "---\n"
        f"{body.strip()}"
    )


#: The on-demand pack ``claunch retro`` sends (:func:`on_demand_block`).
RETROSPECTIVE = "retrospective"


def on_demand_block(workspace: Optional[Path], pack_id: str = RETROSPECTIVE) -> str:
    """An on-demand pack as it is sent to a running session.

    The workspace's copy when installed, else claunch's (:func:`pack_copy`),
    under a header that says it came after the opening and from where. Not
    recorded on the session definition: a reminder restating it every turn
    would re-ask for work that was asked for once. Raises :class:`PackError`
    for a pack that is not on-demand -- an opening pack sent this way would
    be guidance the session's definition does not know it was given.
    """
    pack = pack_copy(workspace, pack_id)
    if not is_on_demand(pack.id, workspace):
        raise PackError(
            f"{pack.id} is an opening pack; only an on-demand pack is sent to "
            "a running session"
        )
    if pack.source == PACKAGE:
        where = f"file: (not installed in this workspace; claunch's {pack.id}.md)"
    else:
        where = f"file: {(INSTALL_DIR / (pack.id + '.md')).as_posix()}"
    return (
        "---\n"
        f"# claunch prompt pack: {pack.id} -- sent on demand to this running "
        "session, not part of its opening\n"
        f"text id: {text_id(pack.body)}\n"
        f"{where}\n"
        "---\n"
        f"{pack.body.strip()}"
    )


#: The id prefix of the packs that carry a claunch tool family's guidance
#: (``mcp-threads``, ``mcp-qa``, ``mcp-sim``). The MCP server instructions
#: only point at them, so a session may read one when it first uses that
#: family instead of carrying it from creation (:func:`loadable_copy`).
MCP_PREFIX = "mcp-"


def is_mcp(pack_id: str) -> bool:
    """Whether ``pack_id`` names a tool family's guidance pack."""
    return str(pack_id or "").startswith(MCP_PREFIX)


def mcp_ids(workspace: Optional[Path] = None) -> List[str]:
    """The mcp- packs a session may load: claunch's, plus a workspace's own."""
    ids = {p.id for p in catalog() if is_mcp(p.id) and not p.on_demand}
    if workspace is not None:
        ids.update(p.id for p in installed(workspace) if is_mcp(p.id) and not p.on_demand)
    return sorted(ids)


def loadable_copy(workspace: Optional[Path], pack_id: str) -> Pack:
    """The copy of an mcp- pack a running session loads through ``rebrief``.

    The same copy an opening would carry (:func:`pack_copy`), so a pack
    loaded later and one ticked at creation are the same text under the
    same id. Only the mcp- packs: they read like a SKILL document, needed
    when a tool family is used and not before. Any other pack is either
    part of an opening, where the agent already has it, or an on-demand
    pack a person sends (:func:`on_demand_block`). Raises :class:`PackError`.
    """
    pack_id = check_id(pack_id)
    if not is_mcp(pack_id):
        raise PackError(
            f"{pack_id} is not an MCP tool family pack; only {MCP_PREFIX}* "
            "packs load through rebrief (the prompt_pack tool reads any pack)"
        )
    pack = pack_copy(workspace, pack_id)
    if pack.on_demand:
        raise PackError(f"{pack_id} is delivered on demand by a person, not loaded")
    return pack


def loaded_block(pack: Pack) -> str:
    """An mcp- pack as ``rebrief`` hands it to a running session."""
    if pack.source == PACKAGE:
        where = f"file: (not installed in this workspace; claunch's {pack.id}.md)"
    else:
        where = f"file: {(INSTALL_DIR / (pack.id + '.md')).as_posix()}"
    return (
        "---\n"
        f"# claunch prompt pack: {pack.id} -- loaded on demand through "
        "rebrief, not part of the opening\n"
        f"text id: {text_id(pack.body)}\n"
        f"{where}\n"
        "---\n"
        f"{pack.body.strip()}"
    )


def sent_block(pack: Pack) -> str:
    """Any other pack as a person sends it to a running session.

    The packs panel of the web UI (``daemon.session_packs``) sends a pack the
    session was not given at creation and that is neither an mcp- pack nor an
    on-demand one. Like :func:`on_demand_block` it is not recorded on the
    definition, and the header says so. A delivered send is held instead
    (``session_packs.record_sent``): the session reminder names its text id
    and ``rebrief`` serves it back after a compaction, until a person drops it.
    """
    if pack.source == PACKAGE:
        where = f"file: (not installed in this workspace; claunch's {pack.id}.md)"
    else:
        where = f"file: {(INSTALL_DIR / (pack.id + '.md')).as_posix()}"
    return (
        "---\n"
        f"# claunch prompt pack: {pack.id} -- sent by a person to this running "
        "session, not part of its opening\n"
        f"text id: {text_id(pack.body)}\n"
        f"{where}\n"
        "---\n"
        f"{pack.body.strip()}"
    )


def records(
    packs: List[Pack], *,
    required: Optional[Dict[str, List[str]]] = None, asked=(),
) -> List[Dict[str, object]]:
    """What a session definition keeps of the packs it was given.

    ``source`` is written only for a package copy or the daemon's prompt, so
    the record of a workspace copy reads exactly as it did before the key
    existed. A package copy also keeps why it went in -- ``required_by`` (the
    roles, from :func:`required_for`) and ``ticked`` -- because it has no
    workspace file to explain its presence and its opening header says which
    (:func:`opening_block`). A workspace copy headed ``delivery: always``
    keeps ``always`` -- the one key such a record adds -- so its header still
    says why it is there after a reminder or a rebrief rewrites it.
    """
    required = required or {}
    asked = set(asked or ())
    out = []
    for p in packs:
        rec: Dict[str, object] = {"id": p.id, "text": p.body}
        if p.source in (PACKAGE, DAEMON):
            rec["source"] = p.source
        elif p.always:
            rec["always"] = True
        if p.source == PACKAGE:
            if required.get(p.id):
                rec["required_by"] = list(required[p.id])
            if p.id in asked:
                rec["ticked"] = True
        out.append(rec)
    return out


def opening(recorded) -> str:
    """The opening's pack section, from a definition's recorded packs."""
    return "\n\n".join(
        opening_block(
            r["id"], r["text"], r.get("source") or WORKSPACE,
            required_by=r.get("required_by"), ticked=bool(r.get("ticked")),
            always=bool(r.get("always")),
        )
        for r in recorded or ()
    )


# --------------------------------------------------------------------------- #
# role → pack: packs a mesh role makes mandatory
# --------------------------------------------------------------------------- #
#: Where the mapping lives in the central config (``~/.claude-launcher/.claunch.yaml``).
CONFIG_KEY = "prompt_packs"
#: What an untouched config means. A role here is matched against a session's
#: primary role and its subroles alike, so ``unreal`` held beside ``worker``
#: brings its pack with it. Every role of the packaged vocabulary
#: (``daemon.mesh_roles.DEFAULT_YAML``) keeps only a one- or two-sentence
#: stance; how the role works is its ``role-<name>`` pack, listed here.
DEFAULT_ROLE_PACKS: Dict[str, List[str]] = {
    "analyst": ["role-analyst"],
    "free-role": ["role-free"],
    "leader": ["role-leader"],
    "merger": ["role-merger"],
    "operator": ["role-operator"],
    "oneshot": ["role-oneshot"],
    "pm": ["role-pm"],
    "reviewer": ["role-reviewer"],
    "specialist": ["role-specialist"],
    "unreal": ["unreal-operator"],
    "worker": ["role-worker"],
    "sync-integrator": ["role-sync-integrator"],
    "local-sync-manager": ["role-local-sync-manager"],
}

_ROLE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def shipped_default_packs() -> List[str]:
    """The shipped packs whose own header says they start ticked.

    Which packs a form ticks is read from the packs (``check_default: true``)
    rather than from a constant here, so a workspace copy that disagrees wins
    like any other edit there, and a pack added to the package needs no
    second edit in this file. An on-demand pack is never part of an opening,
    so it never starts there.
    """
    return [p.id for p in catalog() if p.check_default and not p.on_demand]


def clean_role_packs(raw, *, strict: bool = True) -> Dict[str, List[str]]:
    """Validate a role → packs mapping.

    An empty list is kept: ``{"worker": []}`` says that role requires no
    pack, which is a different statement from leaving the row out (that one
    asks for :data:`DEFAULT_ROLE_PACKS`' answer). An on-demand pack is
    refused (``strict``), or dropped when reading back a mapping saved before
    the pack was one -- one stale entry must not cost every role its packs.
    """
    if not isinstance(raw, dict):
        raise PackError("role packs must be a mapping of role -> [pack ids]")
    out: Dict[str, List[str]] = {}
    for role, ids in raw.items():
        role = str(role or "").strip().lower()
        if not _ROLE.match(role):
            raise PackError(f"bad role name {role!r}")
        if isinstance(ids, str):
            ids = [part for part in ids.split(",") if part.strip()]
        if not isinstance(ids, list):
            raise PackError(f"{role}: packs must be a list of pack ids")
        clean = [check_id(i) for i in dict.fromkeys(str(i).strip() for i in ids)]
        clean = (check_offerable(clean, f"required by role {role}")
                 if strict else offerable(clean))
        out[role] = clean
    return dict(sorted(out.items()))


def role_packs(doc: Optional[dict] = None) -> Dict[str, List[str]]:
    """The role → required packs mapping: :data:`DEFAULT_ROLE_PACKS`, with the
    rows the config names replaced one by one.

    The config overrides rows, never the mapping: a role the package added
    after the config was written keeps the package's packs, and a role the
    config names takes its own. ``{"worker": []}`` is how a config says a
    role requires none -- the row wins as empty, which is why
    :func:`clean_role_packs` keeps empty lists.
    """
    default = {k: list(v) for k, v in DEFAULT_ROLE_PACKS.items()}
    return {**default, **saved_role_packs(doc)}


def set_role_packs(mapping) -> Dict[str, List[str]]:
    """Validate and save the rows the config states; ``None`` clears them.

    A row saved here overrides that role only. ``{"worker": []}`` is kept as
    an explicit "this role requires none", which is what the Settings card's
    Remove writes.
    """
    from . import store

    clean = None if mapping is None else clean_role_packs(mapping)

    def _mutate(doc: dict) -> None:
        block = doc.get(CONFIG_KEY)
        if not isinstance(block, dict):
            block = {}
        if clean is None:
            block.pop("role_packs", None)
        else:
            block["role_packs"] = clean
        if block:
            doc[CONFIG_KEY] = block
        else:
            doc.pop(CONFIG_KEY, None)

    store.update(_mutate)
    return role_packs()


def _stated_from_list(ids) -> Dict[str, bool]:
    """A whole-set statement turned into one per pack.

    A config written before this setting took a map holds a list: the packs
    to tick. It says nothing about the packs it leaves out of the shipped
    set, so the only faithful reading is that those do not start ticked --
    which for the old list of defaults is exactly the old behaviour. A pack
    outside the shipped set that the list names is stated true.
    """
    named = {i for i in dict.fromkeys(str(x).strip() for x in ids) if _ID.match(i)}
    out = {i: False for i in shipped_default_packs()}
    for pack_id in offerable(sorted(named)):
        out[pack_id] = True
    return out


def saved_default_packs(doc: Optional[dict] = None) -> Dict[str, bool]:
    """The ``check_default`` values the config states, by pack id: ``{}``
    when it states none.

    One statement per pack, and only for the packs it states -- a pack the
    config does not mention keeps the answer of its own header
    (:func:`default_packs`), so a pack added to the package later starts
    ticked without the operator editing anything. The older list form is a
    statement about every shipped default, so it is read as one
    (:func:`_stated_from_list`).
    """
    from . import store

    doc = store.load() if doc is None else doc
    block = doc.get(CONFIG_KEY)
    if not isinstance(block, dict):
        return {}
    raw = block.get("default_packs")
    if isinstance(raw, list):
        return _stated_from_list(raw)
    out: Dict[str, bool] = {}
    if isinstance(raw, dict):
        for pack_id, value in raw.items():
            stated = as_bool(value)
            if stated is not None and _ID.match(str(pack_id)):
                out[str(pack_id)] = stated
    return out


def saved_role_packs(doc: Optional[dict] = None) -> Dict[str, List[str]]:
    """The rows the config states, by role: ``{}`` when it states none.

    :func:`role_packs` merges this over :data:`DEFAULT_ROLE_PACKS`; this is
    the config's half alone, which is what a form needs to tell an operator's
    row from the package's.
    """
    from . import store

    doc = store.load() if doc is None else doc
    block = doc.get(CONFIG_KEY)
    if not isinstance(block, dict) or "role_packs" not in block:
        return {}
    try:
        return clean_role_packs(block.get("role_packs") or {}, strict=False)
    except PackError:
        return {}


def default_packs(doc: Optional[dict] = None, workspace: Optional[Path] = None) -> List[str]:
    """The pack ids New session and Spawn child tick on every opening.

    Three layers, each overriding the one before it for the packs it names:
    a shipped pack's own ``check_default`` header
    (:func:`shipped_default_packs`), then what the config states in
    ``prompt_packs.default_packs`` (:func:`saved_default_packs`), then the
    workspace's installed copy when it has one and its header states
    something. A pack named by none of the three does not start ticked.

    The workspace's copy is last on purpose: "every session created here"
    is a decision that workspace makes about itself, so it wins over a
    setting written for the whole machine -- the same precedence
    :func:`pack_copy` gives the copy's text.

    Installed or not makes no difference to what a session gets: the
    workspace's copy is read when it has one and claunch's otherwise
    (:func:`pack_copy`), and the form says which. An untick holds for the
    one opening it was made in.
    """
    stated = {p.id: p.check_default for p in catalog()
              if p.check_default is not None}
    stated.update(saved_default_packs(doc))
    if workspace is not None:
        for pack in installed(workspace):
            if pack.check_default is not None:
                stated[pack.id] = pack.check_default
    # Order: the shipped catalog's own order first, then any other pack a
    # layer names, so the form's rows read the same way every time.
    order = [p.id for p in catalog()]
    order += sorted(set(stated) - set(order))
    return offerable([i for i in order if stated.get(i)])


def set_default_packs(values) -> List[str]:
    """Validate and save the ``check_default`` overrides; ``None`` clears them.

    ``values`` is ``{pack id: bool}`` -- the rows the operator changed, which
    is what the Settings form sends. A list of ids is still read as the whole
    answer ("these are ticked and the other shipped defaults are not"), which
    is the form older builds sent. ``None`` drops the setting, so every pack
    answers from its own header again.
    """
    if values is None:
        _save_list("default_packs", None)
        return default_packs()
    if isinstance(values, dict):
        clean: Dict[str, bool] = {}
        for pack_id, value in values.items():
            stated = as_bool(value)
            if stated is None:
                raise PackError(f"{pack_id}: check_default must be true or false")
            clean[check_offerable([check_id(pack_id)], "a default pack")[0]] = stated
    else:
        ids = _clean_ids(values, "default packs")
        clean = _stated_from_list(check_offerable(ids, "a default pack"))
    _save_list("default_packs", dict(sorted(clean.items())))
    return default_packs()


def _clean_ids(ids, what: str) -> List[str]:
    if isinstance(ids, str):
        ids = [part for part in ids.split(",") if part.strip()]
    if not isinstance(ids, list):
        raise PackError(f"{what} must be a list of pack ids")
    return [check_id(i) for i in dict.fromkeys(str(i).strip() for i in ids)]


def _save_list(key: str, clean) -> None:
    """Write one value of the settings block; ``None`` drops the key."""
    from . import store

    def _mutate(doc: dict) -> None:
        block = doc.get(CONFIG_KEY)
        if not isinstance(block, dict):
            block = {}
        if clean is None:
            block.pop(key, None)
        else:
            block[key] = clean
        if block:
            doc[CONFIG_KEY] = block
        else:
            doc.pop(CONFIG_KEY, None)

    store.update(_mutate)


def required_for(
    roles, mapping: Optional[Dict[str, List[str]]] = None
) -> Dict[str, List[str]]:
    """``{pack id: [roles that require it]}`` for a session holding ``roles``.

    ``roles`` is the primary role and the subroles together, already in their
    canonical names; the order of the result follows the mapping's.
    """
    mapping = role_packs() if mapping is None else mapping
    out: Dict[str, List[str]] = {}
    for role in dict.fromkeys(str(r).strip().lower() for r in roles or ()):
        for pack_id in mapping.get(role, ()):
            out.setdefault(pack_id, []).append(role)
    return out


def choose(
    workspace: Optional[Path], chosen, roles, *,
    mapping: Optional[Dict[str, List[str]]] = None, always: bool = True,
) -> tuple:
    """The packs a session gets: the workspace's own prompt, what was ticked,
    and what its roles require.

    Returns ``(records, required)``, where ``required`` is
    :func:`required_for`'s answer. Every pack is read through
    :func:`pack_copy`, so a ticked one needs no install either: the form
    offers claunch's own packs beside the workspace's, picked freely whatever
    the role. The order is the workspace's ``delivery: always`` copies
    (:func:`always_packs`) first, then the ticked ones, then the required
    ones, each once. An on-demand pack is refused when ticked and skipped
    when a role names it (:func:`offerable`): it is never part of an opening.
    With ``always`` false (the request turned the workspace's prompt off) an
    always copy goes in only when ticked or required, as an ordinary pack.
    """
    required = required_for(roles, mapping)
    required = {k: v for k, v in required.items()
                if not is_on_demand(k, workspace)}
    asked = [check_id(i) for i in dict.fromkeys(chosen or [])]
    bad = [i for i in asked if is_on_demand(i, workspace)]
    if bad:
        raise PackError(_on_demand_refusal(bad, "ticked at session creation"))
    packs = always_packs(workspace) if always else []
    given = {p.id for p in packs}
    for pack_id in dict.fromkeys([*asked, *required]):
        if pack_id in given:
            continue
        try:
            packs.append(pack_copy(workspace, pack_id))
        except PackError as exc:
            if pack_id not in required:
                raise
            raise PackError(
                f"{exc} (required by role {', '.join(required[pack_id])})"
            ) from None
    recs = records(packs, required=required, asked=asked)
    if not always:
        for rec in recs:
            rec.pop("always", None)
    return recs, required


def preview(workspace: Optional[Path], pack_id: str) -> dict:
    """What the create form shows when a pack's text is asked for.

    The same copy :func:`pack_copy` would put into the opening, plus where it
    came from: ``source`` (:data:`WORKSPACE` or :data:`PACKAGE`) and, for a
    workspace copy, ``status`` -- :data:`CURRENT`, :data:`STALE` or
    :data:`EDITED` against the shipped text, :data:`LOCAL` when claunch ships
    no pack of that id. Raises :class:`PackError` as :func:`pack_copy` does.
    """
    pack = pack_copy(workspace, pack_id)
    out = {
        **pack.to_dict(),
        "body": pack.body,
        "source": pack.source,
        "lines": len(pack.body.splitlines()),
        "text_id": text_id(pack.body),
    }
    if pack.source == WORKSPACE and workspace is not None:
        try:
            out["status"] = status(workspace, shipped(pack.id))
        except PackError:
            out["status"] = LOCAL
        out["path"] = str(install_dir(workspace) / f"{pack.id}.md")
    return out


def pack_copy(workspace: Optional[Path], pack_id: str) -> Pack:
    """The text a session is given for ``pack_id``, ticked or role-required.

    The workspace's copy when it installed one (an edit there wins), else the
    copy claunch ships, marked :data:`PACKAGE`. So a workspace that never
    installed a pack still gets it instead of a refused create. Raises only
    for an id that is neither installed nor shipped -- a stale form, a typo,
    or a role mapping naming a pack that exists nowhere.
    """
    pack_id = check_id(pack_id)
    if workspace is not None:
        for pack in installed(workspace):
            if pack.id == pack_id:
                return pack
    try:
        return replace(shipped(pack_id), source=PACKAGE)
    except PackError:
        where = f"installed in {workspace}" if workspace else "in a workspace"
        raise PackError(
            f"{pack_id} is neither {where} nor shipped with claunch"
        ) from None


def current_copy(
    workspace: Optional[Path], record: dict, *, memo: Optional[dict] = None,
) -> Optional[Pack]:
    """The copy a session's recorded pack follows now, or None when gone.

    Which files are watched depends on where the recorded text came from.
    A workspace copy follows the workspace's file only: it never falls back
    to claunch's copy, because the workspace chose to replace that text. A
    claunch copy follows both, and a workspace copy that has appeared since
    wins -- from then on the record is a workspace copy and follows only
    that file. The daemon's own prompt is not a file here and is resolved
    by the daemon (:mod:`claude_launcher.daemon.daemon_prompt`).

    ``memo`` is a dict the caller keeps for one pass over many records, so
    each workspace directory and each shipped pack is read once per pass.
    """
    memo = {} if memo is None else memo
    pack_id = str(record.get("id") or "")
    source = record.get("source") or WORKSPACE
    if source == DAEMON or not _ID.match(pack_id):
        return None
    if workspace is not None:
        key = ("workspace", str(workspace))
        if key not in memo:
            memo[key] = {p.id: p for p in installed(workspace)}
        if pack_id in memo[key]:
            return memo[key][pack_id]
    if source == WORKSPACE:
        return None
    key = ("package", pack_id)
    if key not in memo:
        try:
            memo[key] = replace(shipped(pack_id), source=PACKAGE)
        except PackError:
            memo[key] = None
    return memo[key]
