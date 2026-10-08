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
   installed in the chosen workspace as a checkbox, checked by default; the
   ones left checked are put into the session's opening
   (:func:`opening_block`). The copy in the workspace is what is read, so a
   workspace that edited its copy gets its edit.

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

    def to_dict(self) -> dict:
        return {
            "id": self.id, "title": self.title,
            "description": self.description,
        }


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
    return Pack(
        id=pack_id,
        title=str(meta.get("title") or pack_id).strip(),
        description=str(meta.get("description") or "").strip(),
        body=body.strip(),
        raw=raw,
    )


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


def workspace_rows(workspace: Path) -> List[dict]:
    """One row per pack a workspace could hold: shipped ones, then local."""
    rows = []
    shipped_ids = set()
    for pack in catalog():
        shipped_ids.add(pack.id)
        rows.append({**pack.to_dict(), "status": status(workspace, pack)})
    for pack in installed(workspace):
        if pack.id not in shipped_ids:
            rows.append({**pack.to_dict(), "status": LOCAL})
    return rows


def text_id(body: str) -> str:
    """The content id a session is handed beside a pack's text."""
    return digests.text_digest((body or "").strip())


def opening_block(
    pack_id: str, body: str, source: str = WORKSPACE, *,
    required_by=None, ticked: bool = False,
) -> str:
    """One pack as it goes into an opening, with its id beside it.

    The id is what a session reminder and ``rebrief`` name later
    (:mod:`claude_launcher.daemon.rebrief`): an agent that finds it here,
    next to the text, still has the pack after a compaction. The header says
    where the text came from, because a package copy has no file in the
    workspace to point at -- and, for that copy, why it is there: the roles
    that require it (``required_by``), else that it was ticked at creation.
    A record from before the reason was kept says neither.
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
    (:func:`opening_block`).
    """
    required = required or {}
    asked = set(asked or ())
    out = []
    for p in packs:
        rec: Dict[str, object] = {"id": p.id, "text": p.body}
        if p.source in (PACKAGE, DAEMON):
            rec["source"] = p.source
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
        )
        for r in recorded or ()
    )


# --------------------------------------------------------------------------- #
# role → pack: packs a mesh role makes mandatory
# --------------------------------------------------------------------------- #
#: Where the mapping lives in the central config (``~/.claunch.yaml``).
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
    "pm": ["role-pm"],
    "reviewer": ["role-reviewer"],
    "specialist": ["role-specialist"],
    "unreal": ["unreal-operator"],
    "worker": ["role-worker"],
}

#: The default packs of an untouched config (:func:`default_packs`). A
#: setting's value like :data:`DEFAULT_ROLE_PACKS`, kept here rather than in
#: the packs' headers: which packs a form ticks is not a property of a pack.
DEFAULT_PACKS: List[str] = ["asking-the-user", "common"]

_ROLE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def clean_role_packs(raw) -> Dict[str, List[str]]:
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
        if clean:
            out[role] = clean
    return dict(sorted(out.items()))


def role_packs(doc: Optional[dict] = None) -> Dict[str, List[str]]:
    """The role → required packs mapping, or :data:`DEFAULT_ROLE_PACKS`.

    A saved empty mapping is kept as empty: clearing every row in Settings is
    a decision, and reading it back as the default would undo it.
    """
    from . import store

    doc = store.load() if doc is None else doc
    block = doc.get(CONFIG_KEY)
    if not isinstance(block, dict) or "role_packs" not in block:
        return {k: list(v) for k, v in DEFAULT_ROLE_PACKS.items()}
    try:
        return clean_role_packs(block.get("role_packs") or {})
    except PackError:
        return {}


def set_role_packs(mapping) -> Dict[str, List[str]]:
    """Validate and save the mapping; ``None`` restores the default."""
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


def default_packs(doc: Optional[dict] = None) -> List[str]:
    """The pack ids New session and Spawn child tick on every opening.

    Saved in Settings as ``prompt_packs.default_packs``; until then
    :data:`DEFAULT_PACKS`. Installed or not makes no difference: a session
    gets the workspace's copy when it has one and claunch's otherwise
    (:func:`pack_copy`), and the form says which. An untick holds for the
    one opening it was made in.
    """
    from . import store

    doc = store.load() if doc is None else doc
    block = doc.get(CONFIG_KEY)
    if isinstance(block, dict) and "default_packs" in block:
        raw = block.get("default_packs") or []
        if isinstance(raw, list):
            return [i for i in dict.fromkeys(str(x) for x in raw) if _ID.match(i)]
        return []
    return list(DEFAULT_PACKS)


def _clean_ids(ids, what: str) -> List[str]:
    if isinstance(ids, str):
        ids = [part for part in ids.split(",") if part.strip()]
    if not isinstance(ids, list):
        raise PackError(f"{what} must be a list of pack ids")
    return [check_id(i) for i in dict.fromkeys(str(i).strip() for i in ids)]


def _save_list(key: str, clean: Optional[List[str]]) -> None:
    """Write one list of the settings block; ``None`` drops the key."""
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


def set_default_packs(ids) -> List[str]:
    """Validate and save the default list; ``None`` restores :data:`DEFAULT_PACKS`."""
    _save_list("default_packs",
               None if ids is None else _clean_ids(ids, "default packs"))
    return default_packs()


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
    mapping: Optional[Dict[str, List[str]]] = None,
) -> tuple:
    """The packs a session gets: what was ticked, plus what its roles require.

    Returns ``(records, required)``, where ``required`` is
    :func:`required_for`'s answer. Every pack is read through
    :func:`pack_copy`, so a ticked one needs no install either: the form
    offers claunch's own packs beside the workspace's, picked freely whatever
    the role. The order is the ticked ones first, then the required ones,
    each once.
    """
    required = required_for(roles, mapping)
    asked = [check_id(i) for i in dict.fromkeys(chosen or [])]
    packs = []
    for pack_id in dict.fromkeys([*asked, *required]):
        try:
            packs.append(pack_copy(workspace, pack_id))
        except PackError as exc:
            if pack_id not in required:
                raise
            raise PackError(
                f"{exc} (required by role {', '.join(required[pack_id])})"
            ) from None
    return records(packs, required=required, asked=asked), required


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
