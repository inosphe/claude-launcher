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
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
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
    #: The header's ``default: true``: ticked on the create form unless
    #: Settings saved its own list (:func:`default_packs`).
    default: bool = False

    def to_dict(self) -> dict:
        return {
            "id": self.id, "title": self.title,
            "description": self.description, "default": self.default,
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
        default=meta.get("default") is True,
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


def resolve(workspace: Optional[Path], ids: List[str]) -> List[Pack]:
    """The workspace's copies of ``ids``, in the order asked.

    Raises for an id that is not installed there: a checkbox the form offered
    names a file that exists, so a miss is a stale form or a typo, and a
    session started without guidance it was promised is worse than a 400.
    """
    ids = [check_id(i) for i in dict.fromkeys(ids or [])]
    if not ids:
        return []
    if workspace is None:
        raise PackError(
            "prompt packs are installed per workspace, and this directory is "
            "in no registered workspace"
        )
    have = {p.id: p for p in installed(workspace)}
    missing = [i for i in ids if i not in have]
    if missing:
        raise PackError(
            f"not installed in {workspace}: {', '.join(missing)} — install "
            "it in Settings ▸ Install first"
        )
    return [have[i] for i in ids]


def text_id(body: str) -> str:
    """The content id a session is handed beside a pack's text."""
    return digests.text_digest((body or "").strip())


def opening_block(pack_id: str, body: str) -> str:
    """One pack as it goes into an opening, with its id beside it.

    The id is what a session reminder and ``rebrief`` name later
    (:mod:`claude_launcher.daemon.rebrief`): an agent that finds it here,
    next to the text, still has the pack after a compaction.
    """
    return (
        "---\n"
        f"# claunch prompt pack: {pack_id} -- installed in this workspace, "
        "chosen at session creation\n"
        f"text id: {text_id(body)}\n"
        f"file: {(INSTALL_DIR / (pack_id + '.md')).as_posix()}\n"
        "---\n"
        f"{body.strip()}"
    )


def records(packs: List[Pack]) -> List[Dict[str, str]]:
    """What a session definition keeps of the packs it was given."""
    return [{"id": p.id, "text": p.body} for p in packs]


def opening(recorded) -> str:
    """The opening's pack section, from a definition's recorded packs."""
    return "\n\n".join(
        opening_block(r["id"], r["text"]) for r in recorded or ()
    )


# --------------------------------------------------------------------------- #
# role → pack: packs a mesh role makes mandatory
# --------------------------------------------------------------------------- #
#: Where the mapping lives in the central config (``~/.claunch.yaml``).
CONFIG_KEY = "prompt_packs"
#: What an untouched config means. A role here is matched against a session's
#: primary role and its subroles alike, so ``unreal`` held beside ``worker``
#: brings its pack with it.
DEFAULT_ROLE_PACKS: Dict[str, List[str]] = {"unreal": ["unreal-operator"]}

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
    """The pack ids the create form ticks before anyone touches it.

    Saved in Settings as ``prompt_packs.default_packs``; until then, every
    shipped pack whose header says ``default: true`` (``common``). A pack on
    this list that a workspace has not installed is simply not offered there.
    """
    from . import store

    doc = store.load() if doc is None else doc
    block = doc.get(CONFIG_KEY)
    if isinstance(block, dict) and "default_packs" in block:
        raw = block.get("default_packs") or []
        if isinstance(raw, list):
            return [i for i in dict.fromkeys(str(x) for x in raw) if _ID.match(i)]
        return []
    return [p.id for p in catalog() if p.default]


def set_default_packs(ids) -> List[str]:
    """Validate and save the default list; ``None`` restores the headers'."""
    from . import store

    if ids is None:
        clean = None
    else:
        if isinstance(ids, str):
            ids = [part for part in ids.split(",") if part.strip()]
        if not isinstance(ids, list):
            raise PackError("default packs must be a list of pack ids")
        clean = [check_id(i) for i in dict.fromkeys(str(i).strip() for i in ids)]

    def _mutate(doc: dict) -> None:
        block = doc.get(CONFIG_KEY)
        if not isinstance(block, dict):
            block = {}
        if clean is None:
            block.pop("default_packs", None)
        else:
            block["default_packs"] = clean
        if block:
            doc[CONFIG_KEY] = block
        else:
            doc.pop(CONFIG_KEY, None)

    store.update(_mutate)
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
    :func:`required_for`'s answer. A required pack that is not installed in
    the workspace is refused like a ticked one: the role promises the
    session that guidance, and the create form shows it as locked on.
    """
    required = required_for(roles, mapping)
    ids = list(dict.fromkeys([*(chosen or []), *required]))
    try:
        packs = resolve(workspace, ids)
    except PackError as exc:
        if required:
            why = "; ".join(
                f"{p} (required by role {', '.join(r)})"
                for p, r in required.items()
            )
            raise PackError(f"{exc} -- {why}") from None
        raise
    return records(packs), required
